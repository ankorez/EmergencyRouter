#!/usr/bin/env bash
# Construit l'image Raspberry Pi OS Lite (Debian 13 Trixie, 64 bits) avec EmergencyRouter préinstallé.
# Prérequis : Docker en marche, ~25 Go libres dans la VM Docker, accès Internet.
# Résultat : dist/*.img.xz  (à flasher avec Raspberry Pi Imager → « Utiliser un fichier personnalisé »)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PIGEN="${PIGEN_DIR:-$HOME/.cache/erouter-pigen}"      # chemin sans espace
BRANCH="${PIGEN_BRANCH:-arm64}"                         # branche 64 bits de pi-gen
# Version de pi-gen épinglée (reproductible, pas de code inconnu téléchargé) : changer volontairement pour mettre à jour.
PIGEN_REF="${PIGEN_REF:-c851f00178c64b87a86c06c2be216bd89d62e55b}"

# Image Debian utilisée pour le post-traitement (épinglée par empreinte)
DEBIAN_IMAGE="${DEBIAN_IMAGE:-debian:trixie@sha256:2dc419c6d7fe3dad0a2f9fd469c34cc90b0f9ba642d8fde837fe3d7d34a0e7f1}"

command -v docker >/dev/null || { echo "Docker est requis." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Le démon Docker ne répond pas." >&2; exit 1; }

if [ ! -d "$PIGEN/.git" ]; then
  mkdir -p "$(dirname "$PIGEN")"
  git clone --branch "$BRANCH" https://github.com/RPI-Distro/pi-gen.git "$PIGEN"
fi
git -C "$PIGEN" fetch --quiet origin "$BRANCH"
git -C "$PIGEN" checkout --quiet --detach "$PIGEN_REF"
[ "$(git -C "$PIGEN" rev-parse HEAD)" = "$PIGEN_REF" ] || { echo "pi-gen n'est pas à la version attendue ($PIGEN_REF)." >&2; exit 1; }

# Notre étape + les fichiers du projet
rm -rf "$PIGEN/stage-erouter"
cp -R "$ROOT/image/stage-erouter" "$PIGEN/stage-erouter"
FILES="$PIGEN/stage-erouter/00-install/files"
rm -rf "$FILES"
mkdir -p "$FILES/systemd" "$FILES/scripts"
rsync -a --exclude __pycache__ "$ROOT/erouter/" "$FILES/erouter/"
cp "$ROOT"/systemd/* "$FILES/systemd/"
cp "$ROOT"/scripts/* "$FILES/scripts/"
cp "$ROOT/image/config.image.yaml" "$ROOT/image/90-erouter-sshd.conf" "$FILES/"
printf 'ssh:\n  user: %s\n' "${SSH_USER:-pi}" >> "$FILES/config.image.yaml"     # compte dont l'interface règle le mot de passe
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/erouter/__init__.py")"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo inconnue)"
git -C "$ROOT" diff --quiet HEAD -- 2>/dev/null || COMMIT="$COMMIT-modifié"   # modifications non commitées
printf '{"version": "%s", "commit": "%s", "built": "%s", "os": "Debian 13 (trixie) arm64"}\n' \
  "$VERSION" "$COMMIT" "$(date -u +%Y-%m-%d)" > "$FILES/build-info.json"
chmod +x "$PIGEN/stage-erouter/prerun.sh" "$PIGEN/stage-erouter/00-install/00-run.sh"

# Pas d'image « Lite » standard : seule la nôtre est exportée
touch "$PIGEN/stage2/SKIP_IMAGES"
cp "$ROOT/image/config" "$PIGEN/config"

# Compte « pi » sans mot de passe utilisable ; le SSH se règle ensuite depuis l'interface (voir image/ssh-config.sh).
# SSH_USER pour changer le nom, NO_ADMIN_USER=1 pour une image sans compte (à créer avec Raspberry Pi Imager).
if [ "${NO_ADMIN_USER:-0}" != 1 ]; then
  bash "$ROOT/image/ssh-config.sh" "${SSH_USER:-pi}" >> "$PIGEN/config"
  trap 'rm -f "$PIGEN/config"' EXIT          # la config contient le mot de passe aléatoire : on ne la garde pas
  echo "==> Compte « ${SSH_USER:-pi} » créé (SSH éteint ; à activer depuis l'interface)"
fi

cd "$PIGEN"
rm -f deploy/*.img deploy/*.img.xz            # pas de confusion avec un build précédent
./build-docker.sh

IMG="$(ls -t deploy/*.img | head -1)"
NAME="$(basename "$IMG")"

echo "==> Post-traitement : partition de données et retrait de « resize »"
docker run --rm -v "$ROOT/image":/image:ro -v "$PIGEN/deploy":/deploy "$DEBIAN_IMAGE" sh -euc '
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends fdisk e2fsprogs mtools >/dev/null
  sh /image/postprocess.sh "/deploy/$1"' sh "$NAME"

echo "==> Compression"
xz -T0 -6 -f -k "$IMG"

mkdir -p "$ROOT/dist"
rm -f "$ROOT"/dist/*.img.xz "$ROOT/dist/SHA256SUMS"
cp -v "$IMG.xz" "$ROOT/dist/"
(cd "$ROOT/dist" && shasum -a 256 -- "$NAME.xz" | tee SHA256SUMS)
echo "Image prête dans $ROOT/dist/ (empreinte SHA-256 dans dist/SHA256SUMS)"
