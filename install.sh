#!/usr/bin/env bash
# Installe EmergencyRouter sur Raspberry Pi OS Lite / Debian (Bookworm ou Trixie).
# Usage : sudo ./install.sh   (relançable pour mettre à jour)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Lance ce script avec sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  network-manager hostapd dnsmasq-base nftables iw rfkill iproute2 iputils-ping \
  python3 python3-flask python3-yaml python3-jinja2 wireless-regdb

echo "==> Utilisateur sans privilèges pour l'interface web"
id erouter >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin erouter

echo "==> Fichiers"
install -d /opt/erouter /var/lib/erouter
install -d -o erouter -g erouter -m 750 /etc/erouter
rm -rf /opt/erouter/erouter
cp -r "$SRC/erouter" /opt/erouter/erouter
[ -f /etc/erouter/config.yaml ] || install -o erouter -g erouter -m 600 "$SRC/config.example.yaml" /etc/erouter/config.yaml
chown -R erouter:erouter /etc/erouter && chmod 600 /etc/erouter/config.yaml

# Ancien service unique (versions précédentes)
systemctl disable --now erouter.service 2>/dev/null || true
rm -f /etc/systemd/system/erouter.service

install -m 644 "$SRC"/systemd/erouter-*.service /etc/systemd/system/
install -d /etc/NetworkManager/conf.d /etc/systemd/journald.conf.d
install -m 644 "$SRC/systemd/90-erouter.conf" /etc/NetworkManager/conf.d/90-erouter.conf
install -m 644 "$SRC/systemd/journald-erouter.conf" /etc/systemd/journald.conf.d/erouter.conf

echo "==> Ménage anti-usure de la carte SD"
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
systemctl restart systemd-journald

echo "==> Services"
systemctl disable --now hostapd dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true
systemctl daemon-reload
systemctl restart NetworkManager
systemctl enable --now erouter-agent.service erouter-web.service

IP=$(awk -F'[ /]+' '/address:/{print $3; exit}' /etc/erouter/config.yaml)
echo
echo "Installé. Ouvre http://${IP:-192.168.1.1}/ depuis le LAN pour finir la configuration."
echo "Logs : sudo journalctl -u erouter-agent -u erouter-web -u erouter-hostapd -u erouter-dnsmasq -n 80"
