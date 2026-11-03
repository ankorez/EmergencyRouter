#!/usr/bin/env bash
# Installe EmergencyRouter sur Raspberry Pi OS Lite / Debian (Bookworm ou Trixie).
# Usage : sudo ./install.sh   (relançable pour mettre à jour)
set -euo pipefail
umask 022

[ "$(id -u)" -eq 0 ] || { echo "Lance ce script avec sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  network-manager hostapd dnsmasq-base nftables iw rfkill iproute2 iputils-ping \
  python3 python3-flask python3-yaml python3-jinja2 python3-waitress wireless-regdb

echo "==> Utilisateur sans privilèges pour l'interface web"
id erouter >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin erouter

# Carte SD protégée ? Alors on repasse en écriture le temps de l'installation.
if [ -f /var/lib/erouter/lock-enabled ] && command -v erouter-fs >/dev/null; then
  RELOCK=1
  mount -o remount,rw /
else
  RELOCK=0
fi

echo "==> Fichiers"
install -d -m 755 /opt/erouter /var/lib/erouter
install -d -o root -g erouter -m 750 /etc/erouter
rm -rf /opt/erouter/erouter
cp -r "$SRC/erouter" /opt/erouter/erouter
find /opt/erouter -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /opt/erouter && chmod -R go-w /opt/erouter
# Config : écrite par l'agent (root), lisible par le service web, jamais modifiable par lui
[ -f /etc/erouter/config.yaml ] || install -o root -g erouter -m 640 "$SRC/config.example.yaml" /etc/erouter/config.yaml
chown root:erouter /etc/erouter /etc/erouter/config.yaml
chmod 750 /etc/erouter && chmod 640 /etc/erouter/config.yaml
install -m 755 "$SRC/scripts/erouter-fs" /usr/local/sbin/erouter-fs

# Ancien service unique (versions précédentes)
systemctl disable --now erouter.service 2>/dev/null || true
rm -f /etc/systemd/system/erouter.service

install -m 644 "$SRC"/systemd/erouter-*.service /etc/systemd/system/
install -d /etc/NetworkManager/conf.d /etc/systemd/journald.conf.d /etc/sysctl.d
install -m 644 "$SRC/systemd/90-erouter.conf" /etc/NetworkManager/conf.d/90-erouter.conf
install -m 644 "$SRC/systemd/journald-erouter.conf" /etc/systemd/journald.conf.d/erouter.conf
install -m 644 "$SRC/systemd/90-erouter-sysctl.conf" /etc/sysctl.d/90-erouter.conf

echo "==> Ménage anti-usure de la carte SD"
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
systemctl restart systemd-journald
sysctl --system >/dev/null

echo "==> Services"
systemctl disable --now hostapd dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true
systemctl daemon-reload
systemctl restart NetworkManager
systemctl enable erouter-agent.service erouter-web.service
systemctl restart erouter-agent.service erouter-web.service

[ "$RELOCK" = 1 ] && { sync; mount -o remount,ro / || true; }

IP=$(awk -F'[ /]+' '/address:/{print $3; exit}' /etc/erouter/config.yaml)
echo
echo "Installé. Ouvre http://${IP:-192.168.1.1}/ depuis le LAN pour finir la configuration."
echo "Quand tout est réglé : sudo erouter-fs lock   (protège la carte SD des coupures de courant)"
echo "Logs : sudo journalctl -u erouter-agent -u erouter-web -u erouter-hostapd -u erouter-dnsmasq -n 80"
