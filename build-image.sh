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
cp "$ROOT/image/config.image.yaml" "$FILES/config.image.yaml"
chmod +x "$PIGEN/stage-erouter/prerun.sh" "$PIGEN/stage-erouter/00-install/00-run.sh"

# Pas d'image « Lite » standard : seule la nôtre est exportée
touch "$PIGEN/stage2/SKIP_IMAGES"
cp "$ROOT/image/config" "$PIGEN/config"

cd "$PIGEN"
./build-docker.sh

mkdir -p "$ROOT/dist"
cp -v deploy/*.img.xz "$ROOT/dist/"
(cd "$ROOT/dist" && shasum -a 256 -- *.img.xz | tee SHA256SUMS)
echo "Image prête dans $ROOT/dist/ (empreinte SHA-256 dans dist/SHA256SUMS)"
