#!/bin/bash -e
# Les fichiers du projet sont copiés dans ./files par build-image.sh avant le build.

install -d -m 755 "${ROOTFS_DIR}/opt/erouter" "${ROOTFS_DIR}/var/lib/erouter" \
	"${ROOTFS_DIR}/etc/NetworkManager/conf.d" "${ROOTFS_DIR}/etc/systemd/journald.conf.d" \
	"${ROOTFS_DIR}/etc/sysctl.d" "${ROOTFS_DIR}/usr/local/sbin"

rm -rf "${ROOTFS_DIR}/opt/erouter/erouter"
cp -r files/erouter "${ROOTFS_DIR}/opt/erouter/erouter"
install -m 644 files/systemd/erouter-*.service "${ROOTFS_DIR}/etc/systemd/system/"
install -m 644 files/systemd/90-erouter.conf "${ROOTFS_DIR}/etc/NetworkManager/conf.d/90-erouter.conf"
install -m 644 files/systemd/journald-erouter.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/erouter.conf"
install -m 644 files/systemd/90-erouter-sysctl.conf "${ROOTFS_DIR}/etc/sysctl.d/90-erouter.conf"
install -m 755 files/scripts/erouter-fs "${ROOTFS_DIR}/usr/local/sbin/erouter-fs"

install -d -m 750 "${ROOTFS_DIR}/etc/erouter"
install -m 640 files/config.image.yaml "${ROOTFS_DIR}/etc/erouter/config.yaml"
# Protection de la carte SD active dès le premier démarrage (verrouillage une fois le démarrage terminé)
touch "${ROOTFS_DIR}/var/lib/erouter/lock-enabled"

on_chroot << CHROOT
id erouter >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin erouter
chown -R root:root /opt/erouter
chown root:erouter /etc/erouter /etc/erouter/config.yaml
chmod -R go-w /opt/erouter
systemctl enable erouter-agent.service erouter-web.service
systemctl disable hostapd dnsmasq 2>/dev/null || true
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
CHROOT
