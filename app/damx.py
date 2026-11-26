#!/usr/bin/python3
"""
DAMX - settings app for the Acer Nitro ANV15-41 (GTK4 + libadwaita).

Where each setting goes:
  - Power mode right now -> GNOME's power-profiles-daemon over D-Bus. This is
                            the same switch as the power menu in the top bar,
                            so the two can never disagree.
  - Remembered modes (plugged in / on battery), battery low level, and the
    Acer hardware settings -> the damxd service through /run/damx.sock.
  - Automatic Power Saver   -> GNOME's own setting (gsettings), per user.

Read-only info (battery, CPU temperature, fan RPM) comes straight from /sys,
which any user may read. The NVIDIA GPU's temperature comes from nvidia-smi,
but only while the GPU is already awake - asking a sleeping GPU would wake it
up and drain the battery.
"""

import collections
import glob
import time
import json
import os
import socket
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

APP_ID = "io.github.anbu05.DAMX"
# DAMX_SOCKET lets the test harness point the app at a fake service.
SOCKET_PATH = os.environ.get("DAMX_SOCKET", "/run/damx.sock")

PPD_NAME = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"
PPD_IFACE = "org.freedesktop.UPower.PowerProfiles"

# GNOME's "switch to Power Saver when the battery is low" setting.
GNOME_POWER_SCHEMA = "org.gnome.settings-daemon.plugins.power"
GNOME_AUTO_SAVER_KEY = "power-saver-profile-on-low-battery"

# (power-profiles-daemon name, label, icon)
POWER_MODES = [
    ("power-saver", "Power Saver", "power-profile-power-saver-symbolic"),
    ("balanced", "Balanced", "power-profile-balanced-symbolic"),
    ("performance", "Performance", "power-profile-performance-symbolic"),
]
MODE_NAMES = [name for name, _, _ in POWER_MODES]

# usb_charging values: keep USB ports powered while the laptop is off,
# until the battery drops to this level. 0 = off.
USB_CHOICES = [("0", "Off"), ("10", "10%"), ("20", "20%"), ("30", "30%")]

LOW_BATTERY_MIN, LOW_BATTERY_MAX = 10, 50   # same limits as damxd

REFRESH_SECONDS = 2
HISTORY_POINTS = 30          # 30 x 2 s = the last minute in each mini-graph

# Asking nvidia-smi for the temperature counts as GPU activity, and the GPU only
# goes to sleep after ~10 s without any. So after NVIDIA_IDLE_READS readings
# in a row at 0% load, we stop asking for NVIDIA_PAUSE_SECONDS to let it sleep.
NVIDIA_IDLE_READS = 3
NVIDIA_PAUSE_SECONDS = 20

CSS = """
.stat-card { padding: 10px 12px 8px 12px; }
.stat-value { font-size: 1.5em; font-weight: 800; font-feature-settings: "tnum"; }
.stat-unit { font-size: 0.85em; font-weight: 600; }
.mode-note { margin-top: 6px; }
.mode-memory { margin-top: 12px; }
"""


# --- Talking to damxd -------------------------------------------------------

def damxd_request(request):
    """Send one request to damxd and return its reply (or raise OSError)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


# --- Reading sensors straight from /sys ----------------------------------

def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_int(path, divide=1):
    value = read_file(path) if path else None
    try:
        return int(value) // divide
    except (TypeError, ValueError):
        return None


def hwmon_dir(name):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_file(os.path.join(d, "name")) == name:
            return d
    return None


# PCI address of the NVIDIA GPU (lspci: 01:00.0). Its runtime_status is
# "suspended" while the GPU sleeps.
NVIDIA_PCI = "/sys/bus/pci/devices/0000:01:00.0"


def nvidia_awake():
    return read_file(os.path.join(NVIDIA_PCI, "power", "runtime_status")) == "active"


def hwmon_file(name, file):
    d = hwmon_dir(name)
    return os.path.join(d, file) if d else None


# --- Stat card with a live mini-graph ------------------------------------

class StatCard(Gtk.Button):
    """A clickable card: caption, big number, and a graph of the last minute.

    span_min: the graph's vertical range never gets smaller than this, so
    a fan wobbling by 20 RPM doesn't look like a huge swing.
    """

    def __init__(self, caption, unit, span_min, on_click):
        super().__init__(hexpand=True)
        self.add_css_class("card")
        self.add_css_class("stat-card")
        self.unit = unit
        self.span_min = span_min
        self.history = collections.deque(maxlen=HISTORY_POINTS)
        self.connect("clicked", lambda _b: on_click())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.caption = Gtk.Label(label=caption, xalign=0)
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")

        value_box = Gtk.Box(spacing=4)
        self.value = Gtk.Label(label="–", xalign=0)
        self.value.add_css_class("stat-value")
        self.unit_label = Gtk.Label(label=unit, valign=Gtk.Align.BASELINE_FILL, yalign=0.75)
        self.unit_label.add_css_class("stat-unit")
        self.unit_label.add_css_class("dim-label")
        value_box.append(self.value)
        value_box.append(self.unit_label)

        self.graph = Gtk.DrawingArea(content_height=30, margin_top=4)
        self.graph.set_draw_func(self.draw_graph)

        box.append(self.caption)
        box.append(value_box)
        box.append(self.graph)
        self.set_child(box)

    def push(self, value, text=None):
        """Add the newest reading (or None if unavailable).
        text replaces the number (and hides the unit), e.g. "Asleep"."""
        self.value.set_label(text or (f"{value}" if value is not None else "–"))
        self.unit_label.set_visible(text is None)
        if value is not None:
            self.history.append(value)
        self.graph.queue_draw()

    def draw_graph(self, _area, cr, width, height):
        points = list(self.history)
        if len(points) < 2:
            return
        lo, hi = min(points), max(points)
        if hi - lo < self.span_min:      # keep a minimum range, centred
            mid = (hi + lo) / 2
            lo, hi = mid - self.span_min / 2, mid + self.span_min / 2
        step = width / (HISTORY_POINTS - 1)
        # Newest point sits at the right edge.
        x0 = width - step * (len(points) - 1)

        def xy(i, v):
            return x0 + i * step, height - 2 - (v - lo) / (hi - lo) * (height - 4)

        color = Adw.StyleManager.get_default().get_accent_color_rgba()
        # Soft fill under the line...
        cr.move_to(x0, height)
        for i, v in enumerate(points):
            cr.line_to(*xy(i, v))
        cr.line_to(width, height)
        cr.close_path()
        cr.set_source_rgba(color.red, color.green, color.blue, 0.15)
        cr.fill()
        # ...and the line itself.
        for i, v in enumerate(points):
            cr.line_to(*xy(i, v)) if i else cr.move_to(*xy(i, v))
        cr.set_source_rgba(color.red, color.green, color.blue, 1)
        cr.set_line_width(2)
        cr.stroke()


# --- Main window -------------------------------------------------------------

class DamxWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DAMX")
        self.set_default_size(520, 820)
        self.set_size_request(360, 400)

        # True while we copy hardware state into the widgets, so the widgets'
        # "changed" signals don't send the same value straight back.
        self.updating = False
        self.fan_timer = None
        self.low_battery_timer = None
        self.last_status = None
        self.scroll_animation = None
        self.nvidia_idle_reads = 0
        self.nvidia_pause_until = 0.0

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="DAMX", subtitle="Acer Nitro ANV15-41"))
        toolbar.add_top_bar(header)

        # Shown when the background service can't be reached.
        self.banner = Adw.Banner(title="The DAMX service isn't running — hardware settings are unavailable")
        toolbar.add_top_bar(self.banner)

        # Our own scrolled page (instead of Adw.PreferencesPage) so the stat
        # cards can scroll to a section when clicked.
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                               margin_top=18, margin_bottom=24, margin_start=12, margin_end=12)
        self.content.append(self.build_stats())
        self.power_group = self.build_power()
        self.content.append(self.power_group)
        self.fans_group = self.build_fans()
        self.content.append(self.fans_group)
        self.battery_group = self.build_battery()
        self.content.append(self.battery_group)
        self.content.append(self.build_usb())
        self.content.append(self.build_keyboard())
        self.content.append(self.build_footer())

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroller.set_child(Adw.Clamp(maximum_size=640, child=self.content))

        toolbar.set_content(self.scroller)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.connect_power_profiles()
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    # ----- building the sections -----

    def build_stats(self):
        # 6-column grid: top row 3 cards (2 columns each), bottom row the
        # two fans (3 columns each), so there's never a gap.
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=10, row_spacing=10)
        to_power = lambda: self.scroll_to(self.power_group)      # noqa: E731
        self.battery_card = StatCard("Battery", "%", 5, lambda: self.scroll_to(self.battery_group))
        self.cpu_temp_card = StatCard("CPU", "°C", 10, to_power)
        self.gpu_temp_card = StatCard("NVIDIA GPU", "°C", 10, lambda: self.scroll_to(self.fans_group))
        self.cpu_fan_card = StatCard("CPU Fan", "RPM", 600, lambda: self.scroll_to(self.fans_group))
        self.gpu_fan_card = StatCard("GPU Fan", "RPM", 600, lambda: self.scroll_to(self.fans_group))
        grid.attach(self.battery_card, 0, 0, 2, 1)
        grid.attach(self.cpu_temp_card, 2, 0, 2, 1)
        grid.attach(self.gpu_temp_card, 4, 0, 2, 1)
        grid.attach(self.cpu_fan_card, 0, 1, 3, 1)
        grid.attach(self.gpu_fan_card, 3, 1, 3, 1)
        return grid

    def build_power(self):
        group = Adw.PreferencesGroup(
            title="Power Mode",
            description="The same switch as the power menu in the top bar.")
        self.mode_toggles = Adw.ToggleGroup(homogeneous=True)
        self.mode_toggles.add_css_class("round")
        for name, label, icon in POWER_MODES:
            self.mode_toggles.add(Adw.Toggle(name=name, label=label, icon_name=icon))
        self.mode_toggles.connect("notify::active-name", self.on_mode_changed)
        group.add(self.mode_toggles)

        self.mode_note = Gtk.Label(
            label="On battery the laptop runs Balanced. Performance starts when you plug in.",
            wrap=True, xalign=0, visible=False)
        self.mode_note.add_css_class("caption")
        self.mode_note.add_css_class("dim-label")
        self.mode_note.add_css_class("mode-note")
        group.add(self.mode_note)

        # The two remembered modes. damxd switches between them when the
        # charger is plugged in / pulled out.
        memory = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        memory.add_css_class("boxed-list")
        memory.add_css_class("mode-memory")
        labels = Gtk.StringList.new([label for _, label, _ in POWER_MODES])
        self.ac_mode_row = Adw.ComboRow(title="When Plugged In", model=labels)
        self.ac_mode_row.add_prefix(Gtk.Image(icon_name="ac-adapter-symbolic"))
        self.battery_mode_row = Adw.ComboRow(title="On Battery", model=labels)
        self.battery_mode_row.add_prefix(Gtk.Image(icon_name="battery-symbolic"))
        self.ac_mode_row.connect("notify::selected", self.on_remembered_mode_changed, "ac")
        self.battery_mode_row.connect("notify::selected", self.on_remembered_mode_changed, "battery")
        memory.append(self.ac_mode_row)
        memory.append(self.battery_mode_row)
        group.add(memory)
        return group

    def build_battery(self):
        group = Adw.PreferencesGroup(title="Battery")
        self.limit_row = Adw.SwitchRow(
            title="Stop Charging at 80%",
            subtitle="Helps the battery last longer if the laptop is mostly plugged in")
        self.limit_row.connect("notify::active", self.on_limit_changed)
        group.add(self.limit_row)

        # GNOME's own setting; bound directly, so it always matches Settings → Power.
        self.auto_saver_row = Adw.SwitchRow(
            title="Automatic Power Saver",
            subtitle="Switch to Power Saver when the battery is low")
        schema = Gio.SettingsSchemaSource.get_default().lookup(GNOME_POWER_SCHEMA, True)
        if schema and schema.has_key(GNOME_AUTO_SAVER_KEY):
            self.gnome_power = Gio.Settings.new(GNOME_POWER_SCHEMA)
            self.gnome_power.bind(GNOME_AUTO_SAVER_KEY, self.auto_saver_row, "active",
                                  Gio.SettingsBindFlags.DEFAULT)
        else:
            self.auto_saver_row.set_sensitive(False)
        group.add(self.auto_saver_row)

        self.low_battery_row = Adw.SpinRow.new_with_range(LOW_BATTERY_MIN, LOW_BATTERY_MAX, 1)
        self.low_battery_row.set_title("Battery Low At")
        self.low_battery_row.set_subtitle("Power Saver and GNOME's low-battery warning start at this level (%)")
        self.low_battery_row.connect("notify::value", self.on_low_battery_changed)
        group.add(self.low_battery_row)

        self.calib_row = Adw.ActionRow(
            title="Calibrate Battery",
            subtitle="Charges to 100%, drains, then recharges so the % reading is accurate. Keep the charger plugged in.")
        self.calib_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.calib_button.connect("clicked", self.on_calibrate_clicked)
        self.calib_row.add_suffix(self.calib_button)
        group.add(self.calib_row)
        return group

    def build_fans(self):
        group = Adw.PreferencesGroup(title="Fans")
        self.fan_toggles = Adw.ToggleGroup(homogeneous=True)
        self.fan_toggles.add_css_class("round")
        for name, label in (("auto", "Auto"), ("max", "Max"), ("custom", "Custom")):
            self.fan_toggles.add(Adw.Toggle(name=name, label=label))
        self.fan_toggles.connect("notify::active-name", self.on_fan_mode_changed)
        group.add(self.fan_toggles)

        # Two sliders, only visible in Custom mode.
        self.custom_box = Adw.PreferencesGroup(margin_top=12, visible=False)
        self.cpu_scale = self.fan_slider_row("CPU Fan")
        self.gpu_scale = self.fan_slider_row("GPU Fan")
        group.add(self.custom_box)
        return group

    def fan_slider_row(self, title):
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 100, 5)
        scale.set_hexpand(True)
        scale.set_size_request(180, -1)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_format_value_func(lambda _s, v: f"{int(v)}%")
        scale.set_value(50)
        scale.connect("value-changed", self.on_fan_slider_changed)
        row.add_suffix(scale)
        self.custom_box.add(row)
        return scale

    def build_usb(self):
        group = Adw.PreferencesGroup(title="USB Charging")
        self.usb_row = Adw.ComboRow(
            title="Power USB Ports When Off",
            subtitle="Keeps charging devices after shutdown until the battery drops to this level",
            model=Gtk.StringList.new([label for _, label in USB_CHOICES]))
        self.usb_row.connect("notify::selected", self.on_usb_changed)
        group.add(self.usb_row)
        return group

    def build_keyboard(self):
        group = Adw.PreferencesGroup(title="Keyboard")
        self.backlight_row = Adw.SwitchRow(
            title="Turn Off Light When Idle",
            subtitle="Keyboard light goes off after 30 seconds without typing")
        self.backlight_row.connect("notify::active", self.on_backlight_changed)
        group.add(self.backlight_row)
        return group

    def build_footer(self):
        self.footer = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.footer.add_css_class("caption")
        self.footer.add_css_class("dim-label")
        return self.footer

    # ----- clicking a card scrolls to its section -----

    def scroll_to(self, widget):
        ok, point = widget.compute_point(self.content, Graphene.Point())
        if not ok:
            return
        adj = self.scroller.get_vadjustment()
        target = min(point.y - 12, adj.get_upper() - adj.get_page_size())
        if self.scroll_animation:
            self.scroll_animation.skip()
        self.scroll_animation = Adw.TimedAnimation.new(
            self.scroller, adj.get_value(), max(0, target), 350,
            Adw.PropertyAnimationTarget.new(adj, "value"))
        self.scroll_animation.play()

    # ----- power-profiles-daemon (power mode right now) -----

    def connect_power_profiles(self):
        try:
            self.ppd = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                PPD_NAME, PPD_PATH, PPD_IFACE, None)
            # Follow changes made from the top-bar menu live.
            self.ppd.connect("g-properties-changed", lambda *_: self.show_power_mode())
        except GLib.Error as e:
            self.ppd = None
            self.toast(f"Can't reach power-profiles-daemon: {e.message}")
        self.show_power_mode()

    def show_power_mode(self):
        if not self.ppd:
            return
        active = self.ppd.get_cached_property("ActiveProfile")
        if active:
            self.updating = True
            self.mode_toggles.set_active_name(active.unpack())
            self.updating = False
        self.update_mode_note()

    def on_mode_changed(self, toggles, _pspec):
        if self.updating or not self.ppd:
            return
        # damxd notices the change and remembers it for plugged in / battery.
        name = toggles.get_active_name()
        self.ppd.call(
            "org.freedesktop.DBus.Properties.Set",
            GLib.Variant("(ssv)", (PPD_IFACE, "ActiveProfile", GLib.Variant("s", name))),
            Gio.DBusCallFlags.NONE, -1, None, self.on_mode_set_done)

    def on_mode_set_done(self, proxy, result):
        try:
            proxy.call_finish(result)
        except GLib.Error as e:
            self.toast(f"Couldn't change power mode: {e.message}")
            self.show_power_mode()
        self.update_mode_note()
        # damxd checks every 2 s; refresh a bit later to show what it remembered.
        GLib.timeout_add(2500, self.refresh_once)

    def refresh_once(self):
        self.refresh()
        return GLib.SOURCE_REMOVE

    def update_mode_note(self):
        on_ac = self.last_status.get("on_ac", True) if self.last_status else True
        self.mode_note.set_visible(not on_ac and self.mode_toggles.get_active_name() == "performance")

    def on_remembered_mode_changed(self, row, _pspec, source):
        if self.updating:
            return
        mode = MODE_NAMES[row.get_selected()]
        try:
            reply = damxd_request({"cmd": "set_power_mode", "source": source, "mode": mode})
            if not reply.get("ok"):
                self.toast(f"Couldn't save mode: {reply.get('error')}")
        except OSError as e:
            self.toast(f"Couldn't save mode: {e}")
        self.refresh()

    # ----- damxd settings -----

    def set_setting(self, name, value):
        try:
            reply = damxd_request({"cmd": "set", "name": name, "value": value})
        except OSError as e:
            self.toast(f"Couldn't save setting: {e}")
            return
        if not reply.get("ok"):
            self.toast(f"Couldn't save setting: {reply.get('error')}")
        self.refresh()

    def on_limit_changed(self, row, _pspec):
        if not self.updating:
            self.set_setting("battery_limiter", "1" if row.get_active() else "0")

    def on_backlight_changed(self, row, _pspec):
        if not self.updating:
            self.set_setting("backlight_timeout", "1" if row.get_active() else "0")

    def on_usb_changed(self, row, _pspec):
        if not self.updating:
            self.set_setting("usb_charging", USB_CHOICES[row.get_selected()][0])

    def on_low_battery_changed(self, _row, _pspec):
        if self.updating:
            return
        # damxd restarts UPower for this, so wait until the value settles.
        if self.low_battery_timer:
            GLib.source_remove(self.low_battery_timer)
        self.low_battery_timer = GLib.timeout_add(800, self.send_low_battery)

    def send_low_battery(self):
        self.low_battery_timer = None
        percent = int(self.low_battery_row.get_value())
        try:
            reply = damxd_request({"cmd": "set_low_battery", "percent": percent})
            if reply.get("ok"):
                self.toast(f"Battery low level set to {percent}%")
            else:
                self.toast(f"Couldn't change it: {reply.get('error')}")
        except OSError as e:
            self.toast(f"Couldn't change it: {e}")
        self.refresh()
        return GLib.SOURCE_REMOVE

    def on_fan_mode_changed(self, toggles, _pspec):
        mode = toggles.get_active_name()
        self.custom_box.set_visible(mode == "custom")
        if self.updating:
            return
        if mode == "auto":
            self.set_setting("fan_speed", "0,0")
        elif mode == "max":
            self.set_setting("fan_speed", "100,100")
        else:
            self.send_custom_fans()

    def on_fan_slider_changed(self, _scale):
        if self.updating:
            return
        # Wait until the slider stops moving before talking to the hardware.
        if self.fan_timer:
            GLib.source_remove(self.fan_timer)
        self.fan_timer = GLib.timeout_add(400, self.send_custom_fans)

    def send_custom_fans(self):
        self.fan_timer = None
        cpu, gpu = int(self.cpu_scale.get_value()), int(self.gpu_scale.get_value())
        self.set_setting("fan_speed", f"{cpu},{gpu}")
        return GLib.SOURCE_REMOVE

    def on_calibrate_clicked(self, _button):
        running = self.last_status and self.last_status.get("battery_calibration") == "1"
        if running:
            self.send_calibrate(False)
            return
        dialog = Adw.AlertDialog(
            heading="Calibrate Battery?",
            body=("The laptop will charge to 100%, drain the battery, and charge again. "
                  "This takes several hours. Keep the charger plugged in the whole time "
                  "and don't put the laptop to sleep."))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("start", "Start")
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "start" and self.send_calibrate(True))
        dialog.present(self)

    def send_calibrate(self, on):
        try:
            reply = damxd_request({"cmd": "calibrate", "on": on})
            if not reply.get("ok"):
                self.toast(f"Calibration failed: {reply.get('error')}")
        except OSError as e:
            self.toast(f"Calibration failed: {e}")
        self.refresh()

    # ----- refreshing everything -----

    def refresh(self):
        self.refresh_sensors()
        try:
            status = damxd_request({"cmd": "status"})
        except (OSError, ValueError):
            status = None
        self.banner.set_revealed(status is None)
        for widget in (self.limit_row, self.calib_row, self.fan_toggles, self.custom_box,
                       self.usb_row, self.backlight_row, self.ac_mode_row,
                       self.battery_mode_row, self.low_battery_row):
            widget.set_sensitive(status is not None)
        if status:
            self.last_status = status
            self.show_status(status)
        return GLib.SOURCE_CONTINUE

    def refresh_sensors(self):
        status = read_file("/sys/class/power_supply/BAT1/status")
        self.battery_card.caption.set_label(f"Battery · {status}" if status else "Battery")
        self.battery_card.push(read_int("/sys/class/power_supply/BAT1/capacity"))
        self.cpu_temp_card.push(read_int(hwmon_file("k10temp", "temp1_input"), 1000))
        self.refresh_nvidia()
        self.cpu_fan_card.push(read_int(hwmon_file("acer", "fan1_input")))
        self.gpu_fan_card.push(read_int(hwmon_file("acer", "fan2_input")))

    def refresh_nvidia(self):
        if not nvidia_awake():
            self.gpu_temp_card.caption.set_label("NVIDIA GPU")
            self.gpu_temp_card.push(None, text="Asleep")
            self.nvidia_idle_reads = 0
            return
        if time.monotonic() < self.nvidia_pause_until:
            # Idle and we're leaving it alone so it can sleep; keep the last number.
            self.gpu_temp_card.caption.set_label("NVIDIA GPU · Idle")
            return
        try:
            proc = Gio.Subprocess.new(
                ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self.gpu_temp_card.push(None)
            return
        proc.communicate_utf8_async(None, None, self.on_nvidia_reply)

    def on_nvidia_reply(self, proc, result):
        try:
            _ok, out, _err = proc.communicate_utf8_finish(result)
            temp, load = (int(x) for x in out.strip().split(","))
        except (GLib.Error, ValueError, AttributeError):
            self.gpu_temp_card.caption.set_label("NVIDIA GPU")
            self.gpu_temp_card.push(None)
            return
        self.gpu_temp_card.caption.set_label(f"NVIDIA GPU · {load}% load")
        self.gpu_temp_card.push(temp)
        self.nvidia_idle_reads = self.nvidia_idle_reads + 1 if load == 0 else 0
        if self.nvidia_idle_reads >= NVIDIA_IDLE_READS:
            self.nvidia_pause_until = time.monotonic() + NVIDIA_PAUSE_SECONDS
            self.nvidia_idle_reads = 0

    def show_status(self, status):
        s = status["settings"]
        self.updating = True
        try:
            self.limit_row.set_active(s.get("battery_limiter") == "1")
            self.backlight_row.set_active(s.get("backlight_timeout") == "1")
            usb_values = [v for v, _ in USB_CHOICES]
            if s.get("usb_charging") in usb_values:
                self.usb_row.set_selected(usb_values.index(s["usb_charging"]))

            fan = s.get("fan_speed") or "0,0"
            if fan == "0,0":
                self.fan_toggles.set_active_name("auto")
            elif fan == "100,100":
                self.fan_toggles.set_active_name("max")
            else:
                self.fan_toggles.set_active_name("custom")
                # Don't yank the sliders while the user is dragging them.
                if not self.fan_timer:
                    cpu, gpu = (int(x) for x in fan.split(","))
                    self.cpu_scale.set_value(cpu)
                    self.gpu_scale.set_value(gpu)
            self.custom_box.set_visible(self.fan_toggles.get_active_name() == "custom")

            # Remembered modes; mark which one is in use right now.
            modes = status.get("power_modes") or {}
            on_ac = status.get("on_ac", True)
            for row, source in ((self.ac_mode_row, "ac"), (self.battery_mode_row, "battery")):
                if modes.get(source) in MODE_NAMES:
                    row.set_selected(MODE_NAMES.index(modes[source]))
                row.set_subtitle("In use now" if (source == "ac") == on_ac else "")

            if status.get("low_battery") is not None and not self.low_battery_timer:
                self.low_battery_row.set_value(status["low_battery"])

            calibrating = status.get("battery_calibration") == "1"
            self.calib_button.set_label("Stop" if calibrating else "Start")
            if calibrating:
                self.calib_button.add_css_class("destructive-action")
            else:
                self.calib_button.remove_css_class("destructive-action")
        finally:
            self.updating = False

        self.update_mode_note()
        self.footer.set_label(
            f"Driver {status.get('driver_version') or '?'} · Settings are remembered across restarts")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))


class DamxApp(Adw.Application):
    def __init__(self):
        # Default flags = single instance: pressing the Nitro key again just
        # brings the existing window to the front.
        super().__init__(application_id=APP_ID)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])
        self.set_accels_for_action("window.close", ["<Ctrl>w"])

    def do_activate(self):
        window = self.get_active_window() or DamxWindow(self)
        window.present()


if __name__ == "__main__":
    sys.exit(DamxApp().run(sys.argv))
