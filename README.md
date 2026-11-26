# DAMX — nitro-gnome fork

A personal fork of [Div Acer Manager Max](https://github.com/PXDiv/Div-Acer-Manager-Max),
rebuilt for one laptop: an **Acer Nitro ANV15-41 on Ubuntu with GNOME**.

## What's different from upstream

| Problem with upstream DAMX | What this fork does |
|---|---|
| Driver breaks after every kernel update | Driver is installed with **DKMS**, so it rebuilds itself automatically |
| GNOME's power menu and DAMX fight over the power mode | The driver now offers the modes GNOME expects, so **GNOME's power menu controls the Acer firmware modes**. The app uses the same switch. |
| Settings are forgotten after a reboot | `damxd` **remembers** battery limit, fans, USB charging and keyboard timeout and restores them at boot. GNOME remembers the power mode. |
| Switches modes on its own, and forgets your choice | Remembers **one mode for plugged in and one for battery** and switches between them; GNOME's low-battery Power Saver never overwrites them |
| 100 MB .NET app | Small GTK4/libadwaita app that looks like the rest of GNOME |

## Layout

```
driver/   linuwu_sense kernel driver (from PXDiv/Div-Linuwu-Sense, patched) + dkms.conf
daemon/   damxd.py - root service: remembers settings, Nitro key, socket for the app
app/      damx.py  - the GTK4/libadwaita app, launcher entry and icon
install.sh / uninstall.sh
```

## Install / update

```bash
sudo ./install.sh
```

This also removes the original DAMX 1.0.2 install (`/opt/damx`, `damx-daemon.service`,
and the loose driver files under `/lib/modules`). To remove this fork, run `sudo ./uninstall.sh`.

## Credits

Driver: Linuwu-Sense by 0x7375646F, modified by PXDiv (Div Sharp) for DAMX; based on
the kernel `acer-wmi` driver. Original DAMX by PXDiv. Licensed GPL-3.0 (see `LICENSE`).
