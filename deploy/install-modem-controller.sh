#!/usr/bin/env bash
# Idempotent installer for isales modem-controller on a Linux host.
#
# Spec: device-hardware § modem-controller 三大职责 + § udev 自动检测流程.
#
# Run as root. Safe to re-run; existing artifacts are overwritten only when
# they actually changed.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> apt deps"
apt-get update -y
apt-get install -y libasound2-dev libudev1 udev

echo "==> isales user / group"
id -u isales >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home /var/lib/isales isales
for grp in dialout audio plugdev; do
  if getent group "${grp}" >/dev/null 2>&1; then
    usermod -aG "${grp}" isales
  fi
done

echo "==> directories"
install -d -m 0750 -o isales -g isales /var/lib/isales
install -d -m 0750 -o isales -g isales /var/lib/isales/recordings
install -d -m 0750 -o root -g root /etc/isales

echo "==> udev rules"
install -m 0644 "${REPO_ROOT}/deploy/99-isales-modem.rules" /etc/udev/rules.d/99-isales-modem.rules
udevadm control --reload
udevadm trigger

echo "==> systemd unit"
install -m 0644 "${REPO_ROOT}/deploy/isales-modem-controller.service" /etc/systemd/system/isales-modem-controller.service
systemctl daemon-reload

echo "==> done. Edit /etc/isales/modem-controller.env then:"
echo "    systemctl enable --now isales-modem-controller"
