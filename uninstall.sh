#!/bin/bash
# Removes DAMX (nitro-gnome fork): service, app and DKMS driver.
# Leaves /var/lib/damx/settings.json in place in case you reinstall.
# To go back to the original DAMX afterwards, run its installer (damxfix).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo ./uninstall.sh"
    exit 1
fi

systemctl disable --now damxd.service 2>/dev/null || true
rm -f /etc/systemd/system/damxd.service
systemctl daemon-reload

rm -rf /usr/lib/damx
rm -f /usr/bin/damx
rm -f /usr/share/applications/io.github.anbu05.DAMX.desktop
rm -f /usr/share/icons/hicolor/256x256/apps/io.github.anbu05.DAMX.png

modprobe -r linuwu_sense 2>/dev/null || true
for v in $(dkms status linuwu-sense 2>/dev/null | sed -n 's|^[^/]*/\([^,:]*\).*|\1|p' | sort -u); do
    dkms remove "linuwu-sense/$v" --all || true
    rm -rf "/usr/src/linuwu-sense-$v"
done
rm -f /etc/modules-load.d/linuwu_sense.conf /etc/modprobe.d/blacklist-acer_wmi.conf

# Give the laptop the stock kernel driver back.
modprobe acer_wmi 2>/dev/null || true
systemctl restart power-profiles-daemon.service 2>/dev/null || true
echo "DAMX removed."
