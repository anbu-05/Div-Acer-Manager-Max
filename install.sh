#!/bin/bash
# Installs DAMX (nitro-gnome fork) and removes the original DAMX 1.0.2 install.
# Run from the repo folder:   sudo ./install.sh
# Safe to run again (e.g. after pulling changes) - it just reinstalls.
set -euo pipefail

cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo ./install.sh"
    exit 1
fi

DRV_NAME="linuwu-sense"
DRV_VERSION="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' driver/dkms.conf)"
KVER="$(uname -r)"

step() { echo -e "\n\033[1;34m==> $*\033[0m"; }

# ---------------------------------------------------------------------------
step "Removing the original DAMX install"
# Stopping linuwu_sense.service unloads the driver (that's its job); we load
# the new one further down.
for unit in damx-daemon.service nitro-key-detection.service linuwu_sense.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit"
done
rm -rf /opt/damx /etc/DAMX_Daemon
rm -f /usr/local/bin/DAMX /usr/local/bin/DAMX-Daemon /usr/local/bin/nitro-key-detection.sh
rm -f /usr/share/applications/damx.desktop /usr/share/applications/DAMX.desktop
rm -f /usr/share/icons/hicolor/256x256/apps/damx.png
# The old installer copied the driver straight into each kernel's folder,
# where nothing ever cleaned it up. DKMS manages it from now on.
rm -f /lib/modules/*/kernel/drivers/platform/x86/linuwu_sense.ko
for k in /lib/modules/*/; do
    depmod -a "$(basename "$k")" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
step "Installing the driver with DKMS ($DRV_NAME $DRV_VERSION)"
# Remove any earlier version of our DKMS package first.
for old in $(dkms status "$DRV_NAME" 2>/dev/null | sed -n 's|^[^/]*/\([^,:]*\).*|\1|p' | sort -u); do
    dkms remove "$DRV_NAME/$old" --all || true
    rm -rf "/usr/src/$DRV_NAME-$old"
done
SRC="/usr/src/$DRV_NAME-$DRV_VERSION"
rm -rf "$SRC"
mkdir -p "$SRC"
cp -r driver/Makefile driver/dkms.conf driver/src driver/LICENSE "$SRC/"
dkms add "$DRV_NAME/$DRV_VERSION"
dkms install "$DRV_NAME/$DRV_VERSION" -k "$KVER"

# Load the driver at boot, instead of the stock acer_wmi.
echo "linuwu_sense" > /etc/modules-load.d/linuwu_sense.conf
echo "blacklist acer_wmi" > /etc/modprobe.d/blacklist-acer_wmi.conf
getent group linuwu_sense >/dev/null || groupadd linuwu_sense
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    usermod -aG linuwu_sense "$SUDO_USER"
fi

# Swap the running driver for the new one.
modprobe -r linuwu_sense 2>/dev/null || true
modprobe linuwu_sense

# ---------------------------------------------------------------------------
step "Installing the background service (damxd)"
install -d /usr/lib/damx
install -m 755 daemon/damxd.py /usr/lib/damx/damxd.py
install -m 644 daemon/damxd.service /etc/systemd/system/damxd.service
systemctl daemon-reload
systemctl enable damxd.service
systemctl restart damxd.service

# ---------------------------------------------------------------------------
step "Installing the app"
install -m 755 app/damx.py /usr/lib/damx/damx.py
ln -sf /usr/lib/damx/damx.py /usr/bin/damx
install -m 644 app/io.github.anbu05.DAMX.desktop /usr/share/applications/
install -m 644 app/io.github.anbu05.DAMX.png /usr/share/icons/hicolor/256x256/apps/
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

# ---------------------------------------------------------------------------
step "Letting GNOME's power modes take over the Acer firmware modes"
# power-profiles-daemon only checks the driver when it starts.
systemctl restart power-profiles-daemon.service
sleep 1

step "Done"
echo "Driver modes : $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)"
powerprofilesctl list 2>/dev/null | grep -E '^\*|PlatformDriver' || true
systemctl --no-pager --lines=0 status damxd.service | head -3
echo
echo "Open 'DAMX' from the app grid (or press the Nitro key)."
