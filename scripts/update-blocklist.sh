#!/usr/bin/env bash
# Régénère erouter/data/blocklist.hosts.gz (blocage DNS des publicités et traqueurs) depuis StevenBlack/hosts (licence MIT),
# à un commit précis pour que le résultat soit reproductible. L'empreinte est enregistrée dans blocklist.SOURCE.
# Usage : scripts/update-blocklist.sh [commit]      (sans argument : dernier commit du fichier « hosts »)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/erouter/data"
REPO="StevenBlack/hosts"
SHA="${1:-$(curl -sSf "https://api.github.com/repos/$REPO/commits?path=hosts&per_page=1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["sha"])')}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "commit invalide : $SHA" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -sSfL "https://raw.githubusercontent.com/$REPO/$SHA/hosts" -o "$TMP/hosts"
curl -sSfL "https://raw.githubusercontent.com/$REPO/$SHA/license.txt" -o "$TMP/license"

python3 - "$TMP/hosts" "$TMP/blocklist.hosts" <<'PY'
import re, sys
domain = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
reserved = {"localhost", "local", "broadcasthost", "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
            "ip6-allnodes", "ip6-allrouters", "ip6-allhosts", "0.0.0.0"}
seen = set()
for line in open(sys.argv[1], encoding="utf-8"):
    parts = line.split("#", 1)[0].split()
    if len(parts) == 2 and parts[0] == "0.0.0.0" and parts[1].lower() not in reserved and domain.match(parts[1].lower()):
        seen.add(parts[1].lower())
with open(sys.argv[2], "w") as f:
    for d in sorted(seen):
        f.write(f"0.0.0.0 {d}\n")
print(len(seen), "domaines", file=sys.stderr)
PY

N="$(wc -l < "$TMP/blocklist.hosts" | tr -d ' ')"
[ "$N" -gt 1000 ] || { echo "liste suspecte ($N domaines) : abandon" >&2; exit 1; }
mkdir -p "$OUT"
gzip -9n -c "$TMP/blocklist.hosts" > "$OUT/blocklist.hosts.gz"
cp "$TMP/license" "$OUT/blocklist.LICENSE"
cat > "$OUT/blocklist.SOURCE" <<SRC
Source    : https://github.com/$REPO (licence MIT, voir blocklist.LICENSE)
Commit    : $SHA
Domaines  : $N
SHA-256   : $(shasum -a 256 "$TMP/blocklist.hosts" | cut -d' ' -f1)   (fichier décompressé, une ligne « 0.0.0.0 domaine » par entrée)
Mise à jour : scripts/update-blocklist.sh $SHA
SRC
cat "$OUT/blocklist.SOURCE"
