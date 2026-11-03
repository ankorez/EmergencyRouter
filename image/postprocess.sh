#!/bin/sh
# Post-traitement de l'image produite par pi-gen (exécuté dans un conteneur Debian par build-image.sh) :
#   1. ajoute une 3e partition « erdata » (données persistantes) à la fin de l'image ;
#   2. retire le mot « resize » de cmdline.txt : la racine garde sa taille et ne mange pas la partition de données.
# Usage : postprocess.sh image.img      (outils : fdisk/sfdisk, e2fsprogs, mtools)
set -eu

IMG="${1:?usage: postprocess.sh image.img}"
DATA_MIB="${DATA_MIB:-256}"
export MTOOLS_SKIP_CHECK=1

parts="$(sfdisk -d "$IMG")"
BOOT_START="$(echo "$parts" | awk -F'[=,]+' '/start=/ {print $2+0; exit}')"
END="$(echo "$parts" | awk -F'[=,]+' '/start=/ {e=$2+$4; if (e>m) m=e} END {print m+0}')"
[ -n "$BOOT_START" ] && [ "$END" -gt 0 ] || { echo "table des partitions illisible" >&2; exit 1; }
NPARTS="$(echo "$parts" | grep -c 'start=')"
[ "$NPARTS" -eq 2 ] || { echo "l'image doit avoir exactement 2 partitions (trouvé : $NPARTS)" >&2; exit 1; }

START=$(( (END + 2047) / 2048 * 2048 ))       # alignement 1 Mio
SIZE=$(( DATA_MIB * 2048 ))                   # en secteurs de 512 octets

echo "== partition de données : début $START, taille $DATA_MIB Mio"
truncate -s $(( (START + SIZE) * 512 )) "$IMG"
echo "start=$START, size=$SIZE, type=83" | sfdisk --append --no-reread --no-tell-kernel -q "$IMG"
mke2fs -q -t ext4 -L erdata -m 0 -E "offset=$(( START * 512 ))" "$IMG" $(( SIZE / 2 ))

echo "== cmdline.txt : retrait de « resize »"
TMP="$(mktemp)"
BOOT="$IMG@@$(( BOOT_START * 512 ))"
mcopy -n -i "$BOOT" ::cmdline.txt "$TMP"
sed -i 's/ resize\( \|$\)/\1/' "$TMP"
if grep -qE '(^| )resize( |$)' "$TMP"; then echo "« resize » toujours présent" >&2; exit 1; fi
mcopy -o -i "$BOOT" "$TMP" ::cmdline.txt
cat "$TMP"
rm -f "$TMP"

echo "== vérification"
sfdisk -d "$IMG" | grep start=
e2fsck -fn "$IMG?offset=$(( START * 512 ))" | tail -1
mcopy -n -i "$BOOT" ::cmdline.txt - | grep -qE '(^| )resize( |$)' && { echo "échec : resize encore présent" >&2; exit 1; }
echo "OK"
