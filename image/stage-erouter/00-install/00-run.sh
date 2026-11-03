#!/bin/bash -e
# Les fichiers du projet sont copiés dans ./files par build-image.sh avant le build.

install -d "${ROOTFS_DIR}/opt/erouter" "${ROOTFS_DIR}/etc/erouter" \
	"${ROOTFS_DIR}/var/lib/erouter" "${ROOTFS_DIR}/etc/NetworkManager/conf.d"

rm -rf "${ROOTFS_DIR}/opt/erouter/erouter"
cp -r files/erouter "${ROOTFS_DIR}/opt/erouter/erouter"
install -m 644 files/systemd/erouter*.service "${ROOTFS_DIR}/etc/systemd/system/"
install -m 644 files/systemd/90-erouter.conf "${ROOTFS_DIR}/etc/NetworkManager/conf.d/90-erouter.conf"
install -m 600 files/config.image.yaml "${ROOTFS_DIR}/etc/erouter/config.yaml"

on_chroot << EOF
systemctl enable erouter.service
systemctl disable hostapd dnsmasq 2>/dev/null || true
EOF
