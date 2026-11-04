#!/bin/bash -e
# Les fichiers du projet sont copiés dans ./files par build-image.sh avant le build.

install -d -m 755 "${ROOTFS_DIR}/opt/erouter" "${ROOTFS_DIR}/usr/share/erouter" "${ROOTFS_DIR}/data" \
	"${ROOTFS_DIR}/etc/NetworkManager/conf.d" "${ROOTFS_DIR}/etc/systemd/journald.conf.d" \
	"${ROOTFS_DIR}/etc/sysctl.d" "${ROOTFS_DIR}/etc/ssh/sshd_config.d" "${ROOTFS_DIR}/usr/local/sbin" \
	"${ROOTFS_DIR}/etc/systemd/system.conf.d" "${ROOTFS_DIR}/etc/rpi/swap.conf.d"

rm -rf "${ROOTFS_DIR}/opt/erouter/erouter"
cp -r files/erouter "${ROOTFS_DIR}/opt/erouter/erouter"
install -m 644 files/systemd/erouter-*.service "${ROOTFS_DIR}/etc/systemd/system/"
install -m 644 files/systemd/90-erouter.conf "${ROOTFS_DIR}/etc/NetworkManager/conf.d/90-erouter.conf"
install -m 644 files/systemd/journald-erouter.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/erouter.conf"
install -m 644 files/systemd/91-erouter-wifi.conf "${ROOTFS_DIR}/etc/NetworkManager/conf.d/91-erouter-wifi.conf"
install -m 644 files/systemd/91-erouter-watchdog.conf "${ROOTFS_DIR}/etc/systemd/system.conf.d/91-erouter-watchdog.conf"
install -m 644 files/systemd/90-erouter-swap.conf "${ROOTFS_DIR}/etc/rpi/swap.conf.d/90-erouter.conf"
install -m 644 files/systemd/90-erouter-sysctl.conf "${ROOTFS_DIR}/etc/sysctl.d/90-erouter.conf"
install -m 644 files/90-erouter-sshd.conf "${ROOTFS_DIR}/etc/ssh/sshd_config.d/90-erouter.conf"
install -m 755 files/scripts/erouter-fs files/scripts/erouter-data "${ROOTFS_DIR}/usr/local/sbin/"
# Config par défaut : copiée dans la partition de données au premier démarrage (erouter-data)
install -m 644 files/config.image.yaml "${ROOTFS_DIR}/usr/share/erouter/config.default.yaml"
install -m 644 files/build-info.json "${ROOTFS_DIR}/usr/share/erouter/build-info.json"

on_chroot << CHROOT
id erouter >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin erouter
chown -R root:root /opt/erouter
chmod -R go-w /opt/erouter
# Wi-Fi enregistrés et état de NetworkManager : sur la partition de données (le reste du système est en lecture seule)
rm -rf /etc/NetworkManager/system-connections /var/lib/NetworkManager
ln -s /data/nm-connections /etc/NetworkManager/system-connections
ln -s /data/nm-state /var/lib/NetworkManager
systemctl enable erouter-data.service erouter-agent.service erouter-web.service
systemctl disable hostapd dnsmasq 2>/dev/null || true
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
CHROOT
