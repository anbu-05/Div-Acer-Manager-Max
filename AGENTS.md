<!-- agents-md: v3 · current-state · ≤2000 words · history in AGENTS.archive-*.md · index in AGENTS.archive-index.md -->
# DAMX — nitro-gnome fork

## What this is
Fork of PXDiv/Div-Acer-Manager-Max, cut down and rebuilt for one laptop:
Acer Nitro ANV15-41, Ubuntu 26.04, GNOME 50, kernel 7.0. Branch `nitro-gnome`
(fork remote `origin` = git@github.com:anbu-05/Div-Acer-Manager-Max.git,
`upstream` = PXDiv). The `main` branch still holds the untouched upstream code.

Three parts:
- `driver/` — linuwu_sense kernel module, copied from PXDiv/Div-Linuwu-Sense
  (commit in `driver/UPSTREAM_COMMIT`), installed via DKMS as
  `linuwu-sense/25.701-nitro1`. Our changes are marked `DAMX-nitro-gnome`.
- `daemon/damxd.py` — root systemd service `damxd.service`. Saves and restores
  settings in `/var/lib/damx/settings.json`; remembers one power mode for
  plugged in and one for battery (`/var/lib/damx/power-modes.json`) and switches
  PPD between them; edits `PercentageLow` in `/etc/UPower/UPower.conf`
  (original kept as `UPower.conf.damx-orig`, restarts upower); socket
  `/run/damx.sock` (root:linuwu_sense 0660, JSON lines: `status`, `set`,
  `calibrate`, `set_power_mode`, `set_low_battery`); launches the app on the
  Nitro key.
- `app/damx.py` — GTK4 + libadwaita (Python, PyGObject) app, id
  `io.github.anbu05.DAMX`, installed as `/usr/bin/damx`. Top: clickable stat
  cards (battery, CPU temp, Radeon iGPU temp, CPU/GPU fan RPM) with 1-minute
  mini-graphs; clicking scrolls to the matching section. NVIDIA temp is
  deliberately not read (would wake the dGPU).

## Build / run / test
- Install or update: `sudo ./install.sh` (also removes the original DAMX 1.0.2).
  Remove: `sudo ./uninstall.sh`.
- Compile-test the driver without installing: copy `driver/Makefile` and
  `driver/src` to a temp folder, then `make` (builds for the running kernel).
- UI test without hardware: run damxd's functions against a fake sysfs folder
  (override `NITRO_DIR`, `STATE_DIR`, `STATE_FILE`, `MODES_FILE`,
  `UPOWER_CONF`, `SOCKET_PATH`; stub `os.chown` and `subprocess.run`; keep the
  socket path short, AF_UNIX limit), start the app with
  `DAMX_SOCKET=<fake socket>`. To see it in a browser:
  `gtk4-broadwayd :5`, then `GDK_BACKEND=broadway BROADWAY_DISPLAY=:5`, open
  http://127.0.0.1:8085. Careful: the power mode buttons still change the
  REAL power mode through power-profiles-daemon, and `set_power_mode` for the
  current charger state does too.

## Decisions in force
- GNOME's power-profiles-daemon (PPD) owns the power mode. Neither damxd nor the
  app writes `platform_profile` directly, except damxd re-writing the same value
  when the charger is plugged in.
- Driver patch 1: the firmware has eco/quiet/balanced/performance but no turbo
  (bitmap 0x53). Upstream reported firmware PERFORMANCE as `balanced-performance`,
  so PPD (which needs `performance`) fell back to "placeholder" and only
  controlled the CPU. With no turbo, firmware PERFORMANCE is now reported as
  `performance`.
- Driver patch 2: on battery the firmware refuses performance/quiet. Instead of
  returning an error, the driver runs BALANCED and remembers the request
  (`requested_on_battery`); reading the profile shows the request until the
  firmware mode changes. damxd re-applies it on AC plug-in.
- Per-charger modes (user chose this 2026-11-26): damxd polls every 2 s. On a
  charger change (and at boot) it sets PPD to the remembered mode for the new
  state; any other mode change is remembered for the current state. While PPD
  has an ActiveProfileHold (GNOME's automatic low-battery Power Saver) it
  neither remembers nor overrides, so the low-battery switch never overwrites
  the battery choice.
- Low-battery level range in the app: 10–50% (must stay above
  PercentageCritical=5). It also moves GNOME's low-battery warning. The
  Automatic Power Saver switch in the app is bound to GNOME's own gsettings key
  `power-saver-profile-on-low-battery`.
- Remembered settings: battery_limiter, fan_speed, usb_charging,
  backlight_timeout. Battery calibration is deliberately not remembered
  (it's a one-off multi-hour process).
- First damxd run (no settings file yet) saves the hardware's current values
  instead of resetting them.
- Nitro key = keycode 425 (upstream default). damxd watches every input device
  that can send it (input-remapper creates a second keyboard), de-duplicates
  within 1 s, and starts the app with `systemd-run --user --machine=<user>@.host`.
- Upstream C# GUI, Python daemon, scripts, docs and CI were removed on this
  branch (still in git history and on `main`).

## Known issues / not yet verified
- Installed 2026-11-26. Verified: DKMS install for 7.0.0-44, PPD shows
  `PlatformDriver: platform_profile`, GNOME modes map to firmware
  (power-saver→low-power, balanced→balanced, performance→performance),
  damxd running and saved the current settings on first run.
- User confirmed 2026-11-26: app, Nitro key, restore at boot and
  Performance-on-battery all work.
- Still unverified: per-charger mode switching and the low-battery level on
  real hardware (tested only against a fake service), automatic DKMS rebuild
  on the next kernel update.
- The driver's mode-cycling key can put the firmware in Quiet, which isn't one
  of GNOME's three modes.
- The `damxfix` alias (`~/.bash_aliases`, see `~/modifications/damxfix-alias/`)
  reinstalls the ORIGINAL DAMX and would undo this fork. Retire it once this
  install is confirmed working.
