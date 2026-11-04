#!/bin/bash
# Génère les lignes de config pi-gen du compte d'administration « pi ».
# Usage : ssh-config.sh [utilisateur]      (affiche les lignes sur la sortie standard)
#
# Le compte existe dans l'image mais n'a AUCUN mot de passe utilisable : pi-gen en exige un pour garder un nom
# d'utilisateur fixe, on met donc une valeur aléatoire que personne ne connaît (jamais affichée ni conservée) et le SSH
# est éteint. Le vrai mot de passe se définit depuis l'interface web (carte « Accès SSH »), qui active aussi le SSH.
set -euo pipefail

USER_NAME="${1:-pi}"
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || { echo "ssh-config : nom d'utilisateur invalide : $USER_NAME" >&2; exit 1; }

printf 'FIRST_USER_NAME=%q\n' "$USER_NAME"
printf 'FIRST_USER_PASS=%q\n' "$(openssl rand -base64 33)"
echo 'DISABLE_FIRST_BOOT_USER_RENAME=1'
echo 'ENABLE_SSH=0'
