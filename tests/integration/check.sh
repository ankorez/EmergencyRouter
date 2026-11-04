#!/bin/bash
# Vérifications dans un vrai Debian 13 (mêmes paquets que sur le Pi). À lancer via : make integration
# (conteneur privilégié : disques virtuels « loop » pour tester la partition de données).
set -u
export DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
SRC=/src
FAIL=0
ok()   { echo "  ✔ $*"; }
bad()  { echo "  ✘ $*"; FAIL=$((FAIL + 1)); }
check() { local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
section() { echo; echo "== $*"; }

section "Installation des outils"
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends python3 python3-flask python3-yaml python3-jinja2 python3-gunicorn \
  openssl nftables dnsmasq-base hostapd systemd shellcheck ca-certificates curl e2fsprogs fdisk mtools dosfstools \
  mount util-linux xz-utils python3-pyflakes procps iproute2 openssh-client openssh-server >/dev/null 2>&1 || { echo "apt a échoué"; exit 1; }
python3 -c "from importlib.metadata import version as v; print('python', __import__('sys').version.split()[0], '| flask', v('flask'), '| gunicorn', v('gunicorn'), '|', end=' ')"
openssl version
id erouter >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin erouter
rm -rf /work && cp -r "$SRC" /work && cd /work && find . -name __pycache__ -prune -exec rm -rf {} +

section "Analyse statique et tests unitaires"
check "pyflakes" python3 -m pyflakes erouter tests
out=$(python3 -B -W ignore -m unittest discover -s tests 2>&1 | tail -4)
echo "$out" | sed 's/^/    /'
echo "$out" | grep -q "^OK" && ok "tests unitaires" || bad "tests unitaires"
echo "$out" | grep -q "skipped" && bad "des tests ont été ignorés (openssl ?)"

section "Pare-feu, dnsmasq, hostapd, systemd, shell"
python3 -B - <<'PY'
from jinja2 import Environment, FileSystemLoader
e = Environment(loader=FileSystemLoader('/work/erouter/templates'))
open('/tmp/nft.conf', 'w').write(e.get_template('nftables.conf.j2').render(bridge='br0', uplink='wlan0', web_ports=[443, 80], lan_net='192.168.1.0/24'))
open('/tmp/dnsmasq.conf', 'w').write(e.get_template('dnsmasq.conf.j2').render(bridge='br0', gateway='192.168.1.1', netmask='255.255.255.0', start='192.168.1.100', end='192.168.1.250', lease='12h', dns=['1.1.1.1'], leases='/tmp/leases'))
PY
check "nft -c (syntaxe du pare-feu)" nft -c -f /tmp/nft.conf
check "dnsmasq --test" dnsmasq --test --conf-file=/tmp/dnsmasq.conf
mkdir -p /opt/erouter && cp -r /work/erouter /opt/erouter/ && touch /usr/local/sbin/erouter-data && chmod +x /usr/local/sbin/erouter-data
verify=$(systemd-analyze verify /work/systemd/erouter-*.service 2>&1 | grep -v "^$")
[ -z "$verify" ] && ok "systemd-analyze verify (5 unités)" || { bad "systemd-analyze verify"; echo "$verify" | sed 's/^/    /'; }
sc=$(shellcheck -S warning -e SC2154 install.sh build-image.sh scripts/erouter-fs scripts/erouter-data image/postprocess.sh \
  image/stage-erouter/prerun.sh image/stage-erouter/00-install/00-run.sh tests/integration/check.sh tests/integration/firewall.sh 2>&1)
[ -z "$sc" ] && ok "shellcheck" || { bad "shellcheck"; echo "$sc" | head -30 | sed 's/^/    /'; }

section "Pare-feu : vrai trafic entre le LAN, le routeur et « Internet »"
fw=$(bash /work/tests/integration/firewall.sh /work 2>&1); fw_rc=$?
echo "$fw" | grep -E "✔|✘|^  --|règles"
[ "$fw_rc" -eq 0 ] || FAIL=$((FAIL + fw_rc))

section "Partition de données (erouter-data) sur de vrais disques"
DEFAULT=/tmp/default.yaml; echo "lan: {}" > $DEFAULT
run_data() { ERDATA_DEV="$1" ERDATA_LABEL="${2:-erdata}" ERDATA_MNT=/tmp/mnt ERDATA_STATE=/tmp/state ERDATA_DEFAULT_CONFIG=$DEFAULT /work/scripts/erouter-data >/tmp/data.log 2>&1; }
state() { cat /tmp/state; }
img=/tmp/data.img; truncate -s 64M $img; dev=$(losetup -f --show $img)
mke2fs -q -t ext4 -L erdata -m 0 "$dev"

run_data "$dev"
[ "$(state)" = ok ] && ok "partition saine : état « ok »" || bad "état attendu ok, obtenu $(state)"
check "partition montée" mountpoint -q /tmp/mnt
check "montée sans exécution ni suid ni périphériques" bash -c "findmnt -no OPTIONS /tmp/mnt | grep -q noexec && findmnt -no OPTIONS /tmp/mnt | grep -q nosuid && findmnt -no OPTIONS /tmp/mnt | grep -q nodev"
[ "$(stat -c '%a %U:%G' /tmp/mnt/erouter/config.yaml)" = "640 root:erouter" ] && ok "config par défaut installée (640 root:erouter)" || bad "droits de config.yaml : $(stat -c '%a %U:%G' /tmp/mnt/erouter/config.yaml)"
[ "$(stat -c '%a' /tmp/mnt/nm-connections)" = 700 ] && ok "dossier des Wi-Fi en 700" || bad "droits nm-connections"
echo "ma config" > /tmp/mnt/erouter/config.yaml; sync; umount /tmp/mnt

run_data "$dev"
grep -q "ma config" /tmp/mnt/erouter/config.yaml && ok "la config existante n'est jamais écrasée" || bad "config écrasée"
umount /tmp/mnt

# Corruption légère : une entrée de répertoire pointe vers un inode libéré (comme après une coupure de courant)
debugfs -w -R "freei /erouter/config.yaml" "$dev" >/dev/null 2>&1
run_data "$dev"
case "$(state)" in
  repaired) ok "corruption légère : réparée automatiquement" ;;
  *) bad "corruption légère : état $(state) (attendu repaired)" ;;
esac
check "partition montée après réparation" mountpoint -q /tmp/mnt
umount /tmp/mnt; check "système de fichiers propre après réparation" e2fsck -fn "$dev"

# Destruction totale : superblocs écrasés => recréation, démarrage jamais bloqué
dd if=/dev/urandom of="$dev" bs=1M count=8 status=none
run_data "$dev"; rc=$?
[ "$rc" -eq 0 ] && ok "le script ne bloque jamais le démarrage (code 0)" || bad "code de sortie $rc"
[ "$(state)" = reset ] && ok "partition détruite : recréée, état « reset »" || bad "état attendu reset, obtenu $(state)"
check "partition utilisable après recréation" bash -c "mountpoint -q /tmp/mnt && test -f /tmp/mnt/erouter/config.yaml"
umount /tmp/mnt; losetup -d "$dev"

run_data "" nonexistent-label
[ "$(state)" = none ] && ok "sans partition : /data est un dossier ordinaire (état « none »)" || bad "état attendu none, obtenu $(state)"
check "dossiers créés quand même" test -d /tmp/mnt/erouter

section "Post-traitement de l'image (postprocess.sh) sur une image synthétique"
syn=/tmp/syn.img; truncate -s 200M $syn
printf 'label: dos\nlabel-id: 0x0f49105d\nunit: sectors\n\nstart=16384, size=131072, type=c\nstart=147456, size=204800, type=83\n' | sfdisk -q $syn
mkfs.vfat -F 32 --offset 16384 $syn 65536 >/dev/null
echo "console=serial0,115200 root=PARTUUID=0f49105d-02 rootfstype=ext4 fsck.repair=yes rootwait resize cfg80211.ieee80211_regdom=FR" > /tmp/cmd.txt
MTOOLS_SKIP_CHECK=1 mcopy -i "$syn@@$((16384*512))" /tmp/cmd.txt ::cmdline.txt
mke2fs -q -t ext4 -L rootfs -E "offset=$((147456*512))" $syn 102400
echo "contenu racine" > /tmp/rootfile
debugfs -w -R "write /tmp/rootfile marker.txt" "$syn?offset=$((147456*512))" >/dev/null 2>&1
DATA_MIB=16 sh /work/image/postprocess.sh $syn >/tmp/post.log 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "postprocess.sh réussit" || { bad "postprocess.sh a échoué"; sed 's/^/    /' /tmp/post.log; }
[ "$(sfdisk -d $syn | grep -c start=)" -eq 3 ] && ok "3 partitions" || bad "nombre de partitions"
start3=$(sfdisk -d $syn | awk -F'[=,]+' '/start=/ {s=$2+0} END {print s}')
[ $((start3 % 2048)) -eq 0 ] && ok "partition de données alignée sur 1 Mio" || bad "alignement ($start3)"
label=$(e2label "$syn?offset=$((start3*512))" 2>&1)
[ "$label" = erdata ] && ok "système de fichiers « erdata »" || bad "label erdata (obtenu : $label)"
newcmd=$(MTOOLS_SKIP_CHECK=1 mcopy -n -i "$syn@@$((16384*512))" ::cmdline.txt -)
echo "$newcmd" | grep -qE '(^| )resize( |$)' && bad "« resize » toujours dans cmdline.txt" || ok "« resize » retiré de cmdline.txt"
echo "$newcmd" | grep -q "root=PARTUUID=0f49105d-02 rootfstype=ext4 fsck.repair=yes rootwait cfg80211.ieee80211_regdom=FR" && ok "le reste de cmdline.txt est intact" || bad "cmdline.txt altéré : $newcmd"
debugfs -R "cat marker.txt" "$syn?offset=$((147456*512))" 2>/dev/null | grep -q "contenu racine" && ok "la racine est intacte" || bad "racine altérée"
check "racine : système de fichiers propre" e2fsck -fn "$syn?offset=$((147456*512))"
sh /work/image/postprocess.sh $syn >/dev/null 2>&1 && bad "un second passage aurait dû être refusé" || ok "un second passage est refusé (pas de double partition)"

section "HTTPS de bout en bout (vrai gunicorn, utilisateur sans privilèges)"
W=/tmp/web; rm -rf $W; mkdir -p $W/erouter
cat > $W/erouter/config.yaml <<'CFG'
web: {port: 8443, http_port: 8080, tls: true, secret: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
CFG
chown root:root $W; chmod 755 $W
chown root:erouter $W/erouter $W/erouter/config.yaml; chmod 750 $W/erouter; chmod 640 $W/erouter/config.yaml
export EROUTER_DATA=$W EROUTER_CONFIG=$W/erouter/config.yaml EROUTER_TLS_DIR=$W/erouter/tls PYTHONPATH=/work
# Comme l'agent en production : UMask=0027, propriétaire root
( umask 027; python3 -B -c "from erouter import tls; print('certificat créé :', tls.ensure('127.0.0.1/8'))" )
[ "$(stat -c '%a %U:%G' $W/erouter/tls)" = "750 root:erouter" ] && ok "dossier du certificat : 750 root:erouter" || bad "dossier du certificat : $(stat -c '%a %U:%G' $W/erouter/tls)"
[ "$(stat -c '%a %U:%G' $W/erouter/tls/server.key)" = "640 root:erouter" ] && ok "clé privée : 640 root:erouter" || bad "clé privée : $(stat -c '%a %U:%G' $W/erouter/tls/server.key)"
(setpriv --reuid=erouter --regid=erouter --clear-groups python3 -s -B -m erouter.webd >/tmp/web.log 2>&1 &)
for _ in $(seq 1 20); do curl -ksf https://127.0.0.1:8443/api/session >/dev/null 2>&1 && break; sleep 0.5; done
CRT=$W/erouter/tls/server.crt
check "HTTPS : certificat vérifié par curl (SAN 127.0.0.1)" curl -sf --cacert $CRT https://127.0.0.1:8443/api/session
check "HTTPS : TLS 1.2 accepté" bash -c "echo | openssl s_client -tls1_2 -connect 127.0.0.1:8443 >/dev/null 2>&1"
check "HTTPS : TLS 1.3 accepté" bash -c "echo | openssl s_client -tls1_3 -connect 127.0.0.1:8443 >/dev/null 2>&1"
check "HTTPS : TLS 1.1 refusé" bash -c "! (echo | openssl s_client -tls1_1 -connect 127.0.0.1:8443 >/dev/null 2>&1)"
hdr=$(curl -sI --cacert $CRT https://127.0.0.1:8443/)
for h in "Content-Security-Policy: default-src 'none'" "X-Frame-Options: DENY" "X-Content-Type-Options: nosniff" "Cache-Control: no-store"; do
  echo "$hdr" | grep -qi "$h" && ok "en-tête $h" || bad "en-tête manquant : $h"
done
echo "$hdr" | grep -qi "^server: erouter" && ok "en-tête Server neutre (« erouter »)" || bad "en-tête Server : $(echo "$hdr" | grep -i '^server')"
loc=$(curl -sI http://127.0.0.1:8080/api/x?y=1 | tr -d '\r' | awk 'tolower($1)=="location:" {print $2}')
[ "$loc" = "https://127.0.0.1:8443/api/x?y=1" ] && ok "HTTP redirige vers HTTPS : $loc" || bad "redirection : '$loc'"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example.com' http://127.0.0.1:8080/)" = 400 ] && ok "redirection : hôte étranger refusé" || bad "redirection ouverte"
[ "$(curl -s -o /dev/null -w '%{http_code}' --cacert $CRT -H 'Host: evil.example.com' https://127.0.0.1:8443/api/session)" = 400 ] && ok "HTTPS : hôte étranger refusé" || bad "hôte étranger accepté"
[ "$(curl -s -o /dev/null -w '%{http_code}' --cacert $CRT https://127.0.0.1:8443/api/status)" = 401 ] && ok "API protégée (401 sans connexion)" || bad "API non protégée"
ps -eo user,args | grep -v grep | grep -q "^erouter .*erouter.webd" && ok "le serveur tourne sous l'utilisateur erouter, pas root" || bad "utilisateur du serveur"
grep -i -E "traceback|error" /tmp/web.log | head -3 | sed 's/^/    /'

section "Accès SSH par mot de passe, depuis l'interface"
bash /work/image/ssh-config.sh pi > /tmp/cfg1 2>/tmp/cfg.err && ok "ssh-config.sh : config du build générée" || { bad "ssh-config.sh a échoué"; cat /tmp/cfg.err; }
bash /work/image/ssh-config.sh pi > /tmp/cfg2
(
  # shellcheck disable=SC1091
  . /tmp/cfg1
  [ "$FIRST_USER_NAME" = pi ] && [ "$DISABLE_FIRST_BOOT_USER_RENAME" = 1 ] && [ "$ENABLE_SSH" = 0 ] \
    && [ "${#FIRST_USER_PASS}" -ge 40 ] && [[ "$FIRST_USER_PASS" =~ ^[A-Za-z0-9+/=]+$ ]]
) && ok "la config relue par bash est exacte (SSH éteint, mot de passe aléatoire sans caractère spécial)" || bad "config relue incorrecte"
[ "$(grep FIRST_USER_PASS /tmp/cfg1)" != "$(grep FIRST_USER_PASS /tmp/cfg2)" ] && ok "mot de passe aléatoire différent à chaque build" || bad "mot de passe identique"
for u in "Bad User" 'x;rm -rf /' '-oProxy' 'Root'; do
  bash /work/image/ssh-config.sh "$u" >/dev/null 2>&1 && bad "nom d'utilisateur accepté : '$u'" || ok "nom d'utilisateur refusé : '$u'"
done
grep -q "pwned\|SSH_PASSWORD" /tmp/cfg1 && bad "fuite dans la config" || ok "aucun secret ni clé de l'utilisateur dans la config du build"

ssh-keygen -A >/dev/null 2>&1; mkdir -p /etc/ssh/sshd_config.d; cp /work/image/90-erouter-sshd.conf /etc/ssh/sshd_config.d/90-erouter.conf
check "sshd -t : configuration valide avec le durcissement" /usr/sbin/sshd -t
eff=$(/usr/sbin/sshd -T 2>/dev/null)
for want in "permitrootlogin no" "passwordauthentication yes" "kbdinteractiveauthentication no" "maxauthtries 3" "allowtcpforwarding no" "x11forwarding no"; do
  echo "$eff" | grep -qx "$want" && ok "sshd effectif : $want" || bad "sshd effectif : $want absent"
done

# Vrai chpasswd + vraie vérification du hash (libcrypt), sur un vrai compte
useradd -m -u 1000 -s /bin/bash pi 2>/dev/null
W2=/tmp/sshtest; rm -rf $W2; mkdir -p $W2
export EROUTER_DATA=$W2 EROUTER_CONFIG=$W2/erouter/config.yaml
cat > /tmp/crypt_check.py <<'PY'
import ctypes, ctypes.util
_c = ctypes.CDLL(ctypes.util.find_library("crypt") or "libcrypt.so.1")
_c.crypt.restype, _c.crypt.argtypes = ctypes.c_char_p, [ctypes.c_char_p, ctypes.c_char_p]
def hash_of(user):
    for line in open("/etc/shadow"):
        f = line.split(":")
        if f[0] == user:
            return f[1]
def verify(user, password):
    h = hash_of(user)
    return h not in ("", "*", "!", None) and _c.crypt(password.encode(), h.encode()).decode() == h
PY
cat > /tmp/sshtest.py <<'PY'
import sys
sys.path[:0] = ["/work", "/tmp"]
from erouter import agentd, config
import crypt_check as cc
SPECIAL = 'Une-phrase "secrète" $HOME \\ `x` : fin!'
SPECIAL = SPECIAL.replace("è", "e")
results = []
def check(name, ok): results.append((name, ok))
config.save(config.sanitize(config.DEFAULTS))
r = agentd.m_set_ssh(True, SPECIAL)
check("m_set_ssh renvoie l'état attendu", r == {"enabled": True, "password_set": True, "user": "pi"})
check("le mot de passe (avec guillemets, $, \\, backticks, deux-points) fonctionne vraiment", cc.verify("pi", SPECIAL))
check("un autre mot de passe est refusé par le hash", not cc.verify("pi", SPECIAL + "x"))
check("hash moderne (yescrypt)", cc.hash_of("pi").startswith("$y$"))
check("réglage persistant écrit dans la config", config.load_for_agent()["ssh"] == {"enabled": True, "user": "pi", "password_set": True})
before = cc.hash_of("pi")
for bad_pw in ("court", "motdepasse-12345", "pi-pi-pi-pi-pi-pi"):
    try: agentd.m_set_ssh(True, bad_pw); check(f"refusé : {bad_pw}", False)
    except ValueError: check(f"refusé : {bad_pw}", True)
check("un mot de passe refusé ne modifie rien", cc.hash_of("pi") == before)
cfg = config.load_for_agent(); cfg["ssh"]["user"] = "root"; config.save(cfg)
rootbefore = cc.hash_of("root")
try: agentd.m_set_ssh(True, SPECIAL); check("root refusé", False)
except ValueError: check("root refusé", True)
check("le mot de passe de root n'a pas été touché", cc.hash_of("root") == rootbefore)
for name, ok in results:
    print(("OK  " if ok else "KO  ") + name)
sys.exit(0 if all(ok for _, ok in results) else 1)
PY
out=$(python3 -B /tmp/sshtest.py 2>&1); rc=$?
echo "$out" | grep -v "^KO" | sed 's/^OK  /  ✔ /'
echo "$out" | grep "^KO" | sed 's/^KO  /  ✘ /'
[ "$rc" -eq 0 ] || FAIL=$((FAIL + 1))
unset EROUTER_DATA EROUTER_CONFIG

section "Bilan"
if [ "$FAIL" -eq 0 ]; then echo "TOUT EST OK"; else echo "$FAIL vérification(s) en échec"; fi
exit "$FAIL"
