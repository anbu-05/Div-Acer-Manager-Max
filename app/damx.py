#!/usr/bin/python3
"""
DAMX - settings app for the Acer Nitro ANV15-41 (GTK4 + libadwaita).

Where each setting goes:
  - Power mode     -> GNOME's power-profiles-daemon over D-Bus. This is the
                      same switch as the power menu in the top bar, so the two
                      can never disagree.
  - Everything else -> the damxd service through /run/damx.sock. damxd writes
                      the hardware and remembers the value for the next boot.

Read-only info (battery %, CPU temperature, fan RPM) comes straight from
/sys, which any user may read.
"""

import glob
import json
import os
import socket
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.anbu05.DAMX"
# DAMX_SOCKET lets the test harness point the app at a fake service.
SOCKET_PATH = os.environ.get("DAMX_SOCKET", "/run/damx.sock")

PPD_NAME = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"
PPD_IFACE = "org.freedesktop.UPower.PowerProfiles"

# (power-profiles-daemon name, label, icon)
POWER_MODES = [
    ("power-saver", "Power Saver", "power-profile-power-saver-symbolic"),
    ("balanced", "Balanced", "power-profile-balanced-symbolic"),
    ("performance", "Performance", "power-profile-performance-symbolic"),
]

# usb_charging values: keep USB ports powered while the laptop is off,
# until the battery drops to this level. 0 = off.
USB_CHOICES = [("0", "Off"), ("10", "Down to 10%"),
               ("20", "Down to 20%"), ("30", "Down to 30%")]

REFRESH_SECONDS = 2

CSS = """
.stat-card { padding: 14px 8px; }
.stat-value { font-size: 1.6em; font-weight: 800; font-feature-settings: "tnum"; }
.mode-note { margin-top: 6px; }
"""


# --- Talking to damxd -------------------------------------------------------

def damxd_request(request):
    """Send one request to damxd and return its reply (or raise OSError)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
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


def hwmon_dir(name):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_file(os.path.join(d, "name")) == name:
            return d
    return None


def read_sensors():
    """Battery %, battery status, CPU temp in °C, (cpu_rpm, gpu_rpm)."""
    battery = read_file("/sys/class/power_supply/BAT1/capacity")
    status = read_file("/sys/class/power_supply/BAT1/status")
    cpu = hwmon_dir("k10temp")
    temp = read_file(os.path.join(cpu, "temp1_input")) if cpu else None
    acer = hwmon_dir("acer")
    fans = (read_file(os.path.join(acer, "fan1_input")) if acer else None,
            read_file(os.path.join(acer, "fan2_input")) if acer else None)
    return battery, status, (int(temp) // 1000 if temp else None), fans


# --- UI ------------------------------------------------------------------------

class DamxWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DAMX")
        self.set_default_size(480, 760)
        self.set_size_request(360, 400)

        # True while we copy hardware state into the widgets, so the widgets'
        # "changed" signals don't send the same value straight back.
        self.updating = False
        self.fan_timer = None
        self.last_status = None

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="DAMX", subtitle="Acer Nitro ANV15-41"))
        toolbar.add_top_bar(header)

        # Shown when the background service can't be reached.
        self.banner = Adw.Banner(title="The DAMX service isn't running — hardware settings are unavailable")
        toolbar.add_top_bar(self.banner)

        page = Adw.PreferencesPage()
        page.add(self.build_stats())
        page.add(self.build_power())
        page.add(self.build_battery())
        page.add(self.build_fans())
        page.add(self.build_usb())
        page.add(self.build_keyboard())
        page.add(self.build_footer())

        toolbar.set_content(page)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.connect_power_profiles()
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    # ----- building the sections -----

    def stat_card(self, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        box.add_css_class("card")
        box.add_css_class("stat-card")
        value = Gtk.Label(label="–")
        value.add_css_class("stat-value")
        caption = Gtk.Label(label=title)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        box.append(value)
        box.append(caption)
        return box, value, caption

    def build_stats(self):
        group = Adw.PreferencesGroup()
        row = Gtk.Box(spacing=10, homogeneous=True)
        card, self.battery_value, self.battery_caption = self.stat_card("Battery")
        row.append(card)
        card, self.temp_value, _ = self.stat_card("CPU")
        row.append(card)
        card, self.fan_value, _ = self.stat_card("Fans (CPU / GPU)")
        row.append(card)
        group.add(row)
        return group

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
        return group

    def build_battery(self):
        group = Adw.PreferencesGroup(title="Battery")
        self.limit_row = Adw.SwitchRow(
            title="Stop Charging at 80%",
            subtitle="Helps the battery last longer if the laptop is mostly plugged in")
        self.limit_row.connect("notify::active", self.on_limit_changed)
        group.add(self.limit_row)

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
            subtitle="Keeps charging your phone after shutdown, until the battery drops to this level",
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
        group = Adw.PreferencesGroup()
        self.footer = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.footer.add_css_class("caption")
        self.footer.add_css_class("dim-label")
        group.add(self.footer)
        return group

    # ----- power-profiles-daemon (power mode) -----

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

    def update_mode_note(self):
        on_ac = self.last_status.get("on_ac", True) if self.last_status else True
        self.mode_note.set_visible(not on_ac and self.mode_toggles.get_active_name() == "performance")

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
        for widget in (self.limit_row, self.calib_row, self.fan_toggles,
                       self.custom_box, self.usb_row, self.backlight_row):
            widget.set_sensitive(status is not None)
        if status:
            self.last_status = status
            self.show_status(status)
        return GLib.SOURCE_CONTINUE

    def refresh_sensors(self):
        battery, bat_status, temp, (cpu_rpm, gpu_rpm) = read_sensors()
        self.battery_value.set_label(f"{battery}%" if battery else "–")
        self.battery_caption.set_label(f"Battery · {bat_status}" if bat_status else "Battery")
        self.temp_value.set_label(f"{temp}°C" if temp is not None else "–")
        if cpu_rpm is not None:
            self.fan_value.set_label(f"{int(cpu_rpm) // 100 / 10:g}k / {int(gpu_rpm or 0) // 100 / 10:g}k")
        else:
            self.fan_value.set_label("–")

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
