#!/usr/bin/env bash
# Installe EmergencyRouter sur Raspberry Pi OS Lite / Debian (Bookworm ou Trixie).
# Usage : sudo ./install.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Lance ce script avec sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  network-manager hostapd dnsmasq-base nftables iw rfkill iproute2 iputils-ping \
  python3 python3-flask python3-yaml python3-jinja2 wireless-regdb

echo "==> Fichiers"
install -d /opt/erouter /etc/erouter /var/lib/erouter
rm -rf /opt/erouter/erouter
cp -r "$SRC/erouter" /opt/erouter/erouter
install -m 644 "$SRC"/systemd/erouter*.service /etc/systemd/system/
install -d /etc/NetworkManager/conf.d
install -m 644 "$SRC/systemd/90-erouter.conf" /etc/NetworkManager/conf.d/90-erouter.conf
[ -f /etc/erouter/config.yaml ] || install -m 600 "$SRC/config.example.yaml" /etc/erouter/config.yaml

echo "==> Services"
# Le hostapd/dnsmasq "système" ne doivent pas tourner : on a nos propres unités.
systemctl disable --now hostapd dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true
systemctl daemon-reload
systemctl restart NetworkManager
systemctl enable --now erouter.service

IP=$(awk -F'[ /]+' '/address:/{print $3; exit}' /etc/erouter/config.yaml)
echo
echo "Installé. Ouvre http://${IP:-192.168.1.1}/ depuis le LAN pour finir la configuration."
echo "Pense à éditer /etc/erouter/config.yaml (SSID/mot de passe de ta box) ou à le faire depuis l'interface."
