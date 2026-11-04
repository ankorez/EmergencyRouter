#!/usr/bin/env bash
# Installe EmergencyRouter sur Raspberry Pi OS Lite / Debian (Bookworm ou Trixie).
# Usage : sudo ./install.sh   (relançable pour mettre à jour)
# Note : sans l'image EmergencyRouter il n'y a pas de partition de données séparée : /data est un simple dossier
# et la protection de la carte SD (système en lecture seule) n'est pas disponible.
set -euo pipefail
umask 022

[ "$(id -u)" -eq 0 ] || { echo "Lance ce script avec sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
# Système verrouillé (image EmergencyRouter) : on le repasse en écriture le temps de l'installation
RELOCK=0
if findmnt -no OPTIONS / | tr ',' '\n' | grep -qx ro; then
  RELOCK=1
  mount -o remount,rw /
fi
apt-get update
apt-get install -y --no-install-recommends \
  network-manager dnsmasq-base nftables iw rfkill iproute2 iputils-ping openssl \
  python3 python3-flask python3-yaml python3-jinja2 python3-gunicorn curl wireless-regdb

echo "==> Utilisateur sans privilèges pour l'interface web"
id erouter >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin erouter

echo "==> Fichiers"
install -d -m 755 /opt/erouter /usr/share/erouter /var/lib/erouter
rm -rf /opt/erouter/erouter
cp -r "$SRC/erouter" /opt/erouter/erouter
find /opt/erouter -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /opt/erouter && chmod -R go-w /opt/erouter
install -m 644 "$SRC/config.example.yaml" /usr/share/erouter/config.default.yaml
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/erouter/__init__.py")"
printf '{"version": "%s", "built": "%s", "source": "install.sh"}\n' "$VERSION" "$(date -u +%Y-%m-%d)" > /usr/share/erouter/build-info.json
install -m 755 "$SRC/scripts/erouter-fs" "$SRC/scripts/erouter-data" /usr/local/sbin/

# Ancien service unique (versions précédentes)
systemctl disable --now erouter.service 2>/dev/null || true
rm -f /etc/systemd/system/erouter.service

install -m 644 "$SRC"/systemd/erouter-*.service /etc/systemd/system/
install -d /etc/NetworkManager/conf.d /etc/systemd/journald.conf.d /etc/sysctl.d /etc/systemd/system.conf.d /etc/rpi/swap.conf.d
install -m 644 "$SRC/systemd/90-erouter.conf" /etc/NetworkManager/conf.d/90-erouter.conf
install -m 644 "$SRC/systemd/91-erouter-wifi.conf" /etc/NetworkManager/conf.d/91-erouter-wifi.conf
install -m 644 "$SRC/systemd/91-erouter-watchdog.conf" /etc/systemd/system.conf.d/91-erouter-watchdog.conf
install -m 644 "$SRC/systemd/90-erouter-swap.conf" /etc/rpi/swap.conf.d/90-erouter.conf
install -m 644 "$SRC/systemd/journald-erouter.conf" /etc/systemd/journald.conf.d/erouter.conf
install -m 644 "$SRC/systemd/90-erouter-sysctl.conf" /etc/sysctl.d/90-erouter.conf

echo "==> Données (/data) et migration de l'ancienne configuration"
/usr/local/sbin/erouter-data
if [ -f /etc/erouter/config.yaml ] && [ ! -f /data/erouter/config.yaml.migrated ]; then
  install -m 640 -o root -g erouter /etc/erouter/config.yaml /data/erouter/config.yaml
  sed -i 's/^  port: 80$/  port: 443/' /data/erouter/config.yaml   # l'ancien port 80 devient la redirection HTTP
  touch /data/erouter/config.yaml.migrated
  mv /etc/erouter /etc/erouter.old
  echo "Ancienne config migrée vers /data/erouter (sauvegarde : /etc/erouter.old)"
fi
chown root:erouter /data/erouter && chmod 750 /data/erouter
[ -f /data/erouter/config.yaml ] && chown root:erouter /data/erouter/config.yaml && chmod 640 /data/erouter/config.yaml

echo "==> Ménage anti-usure de la carte SD"
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
systemctl restart systemd-journald
sysctl --system >/dev/null

echo "==> Services"
systemctl disable --now dnsmasq 2>/dev/null || true
systemctl daemon-reload
systemctl restart NetworkManager
systemctl enable erouter-data.service erouter-agent.service erouter-web.service
systemctl restart erouter-agent.service erouter-web.service

if [ "$RELOCK" = 1 ]; then
  sync
  mount -o remount,ro / || true
fi

IP=$(awk -F'[ /]+' '/address:/{print $3; exit}' /data/erouter/config.yaml)
echo
echo "Installé. Ouvre https://${IP:-192.168.1.1}/ depuis le LAN pour finir la configuration"
echo "(avertissement du navigateur au premier accès : le certificat est auto-signé, compare son empreinte affichée en bas de page)."
echo "Redémarre le Pi une fois : le swap sur la carte (/var/swap) est supprimé et le chien de garde matériel activé."
echo "Logs : sudo journalctl -u erouter-agent -u erouter-web -u erouter-dnsmasq -n 80"
