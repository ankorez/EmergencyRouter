#!/usr/bin/env bash
# Construit l'image Raspberry Pi OS Lite (Debian 13 Trixie, 64 bits) avec EmergencyRouter préinstallé.
# Prérequis : Docker en marche, ~25 Go libres dans la VM Docker, accès Internet.
# Résultat : dist/*.img.xz  (à flasher avec Raspberry Pi Imager → « Utiliser un fichier personnalisé »)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PIGEN="${PIGEN_DIR:-$HOME/.cache/erouter-pigen}"      # chemin sans espace
BRANCH="${PIGEN_BRANCH:-arm64}"                         # branche 64 bits de pi-gen

command -v docker >/dev/null || { echo "Docker est requis." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Le démon Docker ne répond pas." >&2; exit 1; }

if [ ! -d "$PIGEN/.git" ]; then
  mkdir -p "$(dirname "$PIGEN")"
  git clone --depth 1 --branch "$BRANCH" https://github.com/RPI-Distro/pi-gen.git "$PIGEN"
fi

# Notre étape + les fichiers du projet
rm -rf "$PIGEN/stage-erouter"
cp -R "$ROOT/image/stage-erouter" "$PIGEN/stage-erouter"
FILES="$PIGEN/stage-erouter/00-install/files"
mkdir -p "$FILES/systemd"
rsync -a --exclude __pycache__ "$ROOT/erouter/" "$FILES/erouter/"
cp "$ROOT"/systemd/* "$FILES/systemd/"
cp "$ROOT/image/config.image.yaml" "$FILES/config.image.yaml"
chmod +x "$PIGEN/stage-erouter/prerun.sh" "$PIGEN/stage-erouter/00-install/00-run.sh"

# Pas d'image « Lite » standard : seule la nôtre est exportée
touch "$PIGEN/stage2/SKIP_IMAGES"
cp "$ROOT/image/config" "$PIGEN/config"

cd "$PIGEN"
./build-docker.sh

mkdir -p "$ROOT/dist"
cp -v deploy/*.img.xz "$ROOT/dist/"
echo "Image prête dans $ROOT/dist/"
