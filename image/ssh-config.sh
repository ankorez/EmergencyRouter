#!/bin/bash
# Génère les lignes de config pi-gen du compte d'administration : connexion par clé SSH uniquement.
# Usage : ssh-config.sh <clé-publique.pub> [utilisateur]     (affiche les lignes sur la sortie standard)
#
# - le mot de passe du compte est aléatoire et jamais conservé : il n'existe pas de mot de passe utilisable
#   (pi-gen en exige un pour garder un nom d'utilisateur fixe) ; SSH par mot de passe est désactivé
# - seule la clé (sans son commentaire) est injectée : rien de personnel ne part dans l'image
set -euo pipefail

KEYFILE="${1:?usage : ssh-config.sh clé-publique.pub [utilisateur]}"
USER_NAME="${2:-pi}"

die() { echo "ssh-config : $*" >&2; exit 1; }

[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "nom d'utilisateur invalide : $USER_NAME"
[ -r "$KEYFILE" ] || die "clé introuvable : $KEYFILE"
grep -q "PRIVATE KEY" "$KEYFILE" && die "$KEYFILE est une clé PRIVÉE : donne le fichier .pub"

read -r kind blob _ < "$KEYFILE" || true
case "$kind" in
  ssh-ed25519 | ssh-rsa | ecdsa-sha2-nistp256 | ecdsa-sha2-nistp384 | ecdsa-sha2-nistp521 \
    | sk-ssh-ed25519@openssh.com | sk-ecdsa-sha2-nistp256@openssh.com) ;;
  *) die "type de clé non reconnu : '${kind:-vide}'" ;;
esac
[[ "${blob:-}" =~ ^[A-Za-z0-9+/]+=*$ ]] || die "clé publique illisible"

PASS="$(openssl rand -base64 24)"       # jamais affiché ni conservé en dehors de la config du build

printf 'FIRST_USER_NAME=%q\n' "$USER_NAME"
printf 'FIRST_USER_PASS=%q\n' "$PASS"
printf 'PUBKEY_SSH_FIRST_USER=%q\n' "$kind $blob erouter-admin"
echo 'PUBKEY_ONLY_SSH=1'
echo 'DISABLE_FIRST_BOOT_USER_RENAME=1'
echo 'PASSWORDLESS_SUDO=1'
