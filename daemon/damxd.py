#!/usr/bin/python3
"""
damxd - small background service for the Acer Nitro ANV15-41.

What it does:
  1. Remembers the Acer hardware settings (80% battery limit, fan speed,
     USB charging while off, keyboard backlight timeout) and puts them back
     every time the laptop boots.
  2. Lets the DAMX app change those settings through a local socket
     (/run/damx.sock, only usable by the "linuwu_sense" group).
  3. When the charger is plugged in, re-applies the power mode, so a
     "Performance" choice made on battery actually kicks in (the firmware
     only allows it on AC).
  4. Opens the DAMX app when the Nitro key is pressed.

What it deliberately does NOT do:
  - Change the power mode on its own. GNOME's power-profiles-daemon owns the
    power mode (and remembers it across reboots); the app talks to it directly.

Runs as root via systemd (damxd.service).
"""

import grp
import json
import logging
import os
import select
import socket
import socketserver
import subprocess
import threading
import time

import evdev

log = logging.getLogger("damxd")

# --- Fixed paths for this laptop -------------------------------------------

NITRO_DIR = "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/nitro_sense"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
AC_ONLINE = "/sys/class/power_supply/ACAD/online"

STATE_DIR = "/var/lib/damx"
STATE_FILE = os.path.join(STATE_DIR, "settings.json")
SOCKET_PATH = "/run/damx.sock"
SOCKET_GROUP = "linuwu_sense"   # the user is already in this group

APP_COMMAND = "/usr/bin/damx"
NITRO_KEYCODE = 425             # code the Nitro key sends (upstream DAMX default)

# --- The settings we remember ----------------------------------------------
# Each entry: sysfs file name -> function that checks a value is allowed.
# Battery calibration is NOT here on purpose: it is a one-off process
# (charge to 100%, drain, recharge), restarting it on every boot would be bad.

def _valid_fan(value):
    # "cpu,gpu" with each 0..100. 0,0 means automatic.
    try:
        cpu, gpu = (int(x) for x in value.split(","))
    except ValueError:
        return False
    return 0 <= cpu <= 100 and 0 <= gpu <= 100

SETTINGS = {
    "battery_limiter":   lambda v: v in ("0", "1"),            # 1 = stop at 80%
    "fan_speed":         _valid_fan,
    "usb_charging":      lambda v: v in ("0", "10", "20", "30"),  # keep USB powered when off until battery hits N%
    "backlight_timeout": lambda v: v in ("0", "1"),            # 1 = keyboard light off after 30 s idle
}

# Settings are written by several threads (socket clients, startup), so one
# lock keeps the saved file and the hardware in step.
state_lock = threading.Lock()


# --- Small file helpers -----------------------------------------------------

def read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        log.warning("read %s failed: %s", path, e)
        return None


def write_sysfs(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as e:
        log.error("write %s=%s failed: %s", path, value, e)
        return False


def load_saved():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {k: str(v) for k, v in data.items() if k in SETTINGS}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("could not read %s: %s", STATE_FILE, e)
        return {}


def save(saved):
    # Write to a temp file then rename, so a crash never leaves half a file.
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(saved, f, indent=2)
    os.replace(tmp, STATE_FILE)


# --- Settings logic ---------------------------------------------------------

def wait_for_driver(timeout=60):
    """The driver can take a moment to appear at boot; wait for it."""
    for _ in range(timeout):
        if os.path.isdir(NITRO_DIR):
            return True
        time.sleep(1)
    return False


def restore_settings():
    """Boot-time: put every remembered setting back on the hardware."""
    with state_lock:
        saved = load_saved()
        if not saved:
            # First run: nothing saved yet, so remember what the hardware has
            # now. This keeps the user's current setup instead of resetting it.
            for name in SETTINGS:
                value = read_sysfs(os.path.join(NITRO_DIR, name))
                if value is not None and SETTINGS[name](value):
                    saved[name] = value
            save(saved)
            log.info("first run, saved current hardware settings: %s", saved)
            return
        for name, value in saved.items():
            if SETTINGS[name](value):
                write_sysfs(os.path.join(NITRO_DIR, name), value)
        log.info("restored settings: %s", saved)


def set_setting(name, value):
    if name not in SETTINGS:
        return False, f"unknown setting {name}"
    value = str(value)
    if not SETTINGS[name](value):
        return False, f"invalid value {value!r} for {name}"
    with state_lock:
        if not write_sysfs(os.path.join(NITRO_DIR, name), value):
            return False, "hardware write failed"
        saved = load_saved()
        saved[name] = value
        save(saved)
    log.info("set %s=%s", name, value)
    return True, None


def current_status():
    values = {name: read_sysfs(os.path.join(NITRO_DIR, name)) for name in SETTINGS}
    return {
        "settings": values,
        "battery_calibration": read_sysfs(os.path.join(NITRO_DIR, "battery_calibration")),
        "driver_version": read_sysfs(os.path.join(NITRO_DIR, "version")),
        "platform_profile": read_sysfs(PLATFORM_PROFILE),
        "on_ac": read_sysfs(AC_ONLINE) == "1",
    }


# --- Socket server (the app talks to us here) -------------------------------
# Protocol: the client sends one JSON object per line, we answer one per line.
#   {"cmd": "status"}
#   {"cmd": "set", "name": "battery_limiter", "value": "1"}
#   {"cmd": "calibrate", "on": true}

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            try:
                req = json.loads(line)
                reply = self.dispatch(req)
            except Exception as e:  # never let one bad request kill the service
                log.exception("bad request")
                reply = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(reply) + "\n").encode())

    def dispatch(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **current_status()}
        if cmd == "set":
            ok, err = set_setting(req.get("name"), req.get("value"))
            return {"ok": ok, "error": err}
        if cmd == "calibrate":
            ok = write_sysfs(os.path.join(NITRO_DIR, "battery_calibration"),
                             "1" if req.get("on") else "0")
            return {"ok": ok, "error": None if ok else "hardware write failed"}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve_socket():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = Server(SOCKET_PATH, Handler)
    # Only root and the linuwu_sense group may talk to us.
    os.chown(SOCKET_PATH, 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
    os.chmod(SOCKET_PATH, 0o660)
    log.info("listening on %s", SOCKET_PATH)
    server.serve_forever()


# --- Charger watcher ---------------------------------------------------------

def watch_charger():
    """
    On battery the firmware runs Balanced even if Performance is selected
    (our driver remembers the choice). When the charger comes back, writing
    the selected mode again makes the firmware actually switch.
    This never picks a different mode - it only re-applies the user's one.
    """
    last = read_sysfs(AC_ONLINE)
    while True:
        time.sleep(2)
        now = read_sysfs(AC_ONLINE)
        if now == "1" and last == "0":
            profile = read_sysfs(PLATFORM_PROFILE)
            if profile:
                write_sysfs(PLATFORM_PROFILE, profile)
                log.info("charger plugged in, re-applied mode %s", profile)
        last = now


# --- Nitro key ----------------------------------------------------------------

def find_key_devices():
    """All input devices that can send the Nitro key code.
    input-remapper re-creates the keyboard as a second device, so there may
    be more than one; we watch them all and de-duplicate presses."""
    found = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
            if NITRO_KEYCODE in keys:
                found.append(dev)
            else:
                dev.close()
        except OSError:
            pass
    return found


def desktop_user():
    """User of the active graphical session on seat0."""
    try:
        out = subprocess.run(["loginctl", "list-sessions", "--json=short"],
                             capture_output=True, text=True, timeout=5).stdout
        for s in json.loads(out):
            if s.get("seat") == "seat0" and s.get("state", "active") == "active" and s.get("uid", 0) >= 1000:
                return s["user"]
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("could not find desktop user: %s", e)
    return None


def launch_app():
    user = desktop_user()
    if not user:
        log.warning("Nitro key pressed but no desktop session found")
        return
    # Starting it inside the user's own systemd session gives it the right
    # display, D-Bus and environment, on Wayland and X11 alike.
    subprocess.Popen(["systemd-run", "--user", f"--machine={user}@.host",
                      "--collect", "--quiet", APP_COMMAND],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Nitro key: opened app for %s", user)


def watch_nitro_key():
    devices = {}
    last_press = 0.0
    last_scan = 0.0
    while True:
        # Re-scan now and then: input-remapper can re-create its device.
        if time.monotonic() - last_scan > 10:
            for dev in find_key_devices():
                if dev.path in devices:
                    dev.close()
                else:
                    devices[dev.path] = dev
                    log.info("watching %s (%s) for the Nitro key", dev.path, dev.name)
            last_scan = time.monotonic()
        if not devices:
            time.sleep(5)
            continue
        ready, _, _ = select.select(list(devices.values()), [], [], 5)
        for dev in ready:
            try:
                for event in dev.read():
                    if (event.type == evdev.ecodes.EV_KEY and event.code == NITRO_KEYCODE
                            and event.value == 1):
                        # Same press can arrive from two devices; ignore repeats within 1 s.
                        if time.monotonic() - last_press > 1:
                            launch_app()
                        last_press = time.monotonic()
            except OSError:
                # Device went away (unplugged / input-remapper restarted).
                devices.pop(dev.path, None)
                dev.close()


# --- Main ---------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log.info("damxd starting")
    if not wait_for_driver():
        log.error("linuwu_sense driver not found at %s", NITRO_DIR)
        raise SystemExit(1)
    restore_settings()
    for target in (watch_charger, watch_nitro_key):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
    serve_socket()


if __name__ == "__main__":
    main()
