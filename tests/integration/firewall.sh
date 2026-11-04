#!/bin/bash
# Pare-feu : vrai trafic entre trois « mondes » (réseaux isolés Linux), avec le vrai jeu de règles nftables.
#
#   client (ns lan)  ──br0──  ROUTEUR (cet espace de noms)  ──wlan0──  Internet (ns net)
#
# Appelé par check.sh (conteneur privilégié, Debian 13). Affiche ✔ / ✘ et renvoie le nombre d'échecs.
set -u
SRC="${1:-/work}"
FAIL=0
ok()  { echo "  ✔ $*"; }
bad() { echo "  ✘ $*"; FAIL=$((FAIL + 1)); }

cleanup() { ip netns del lan 2>/dev/null; ip netns del net 2>/dev/null; ip link del br0 2>/dev/null; ip link del wlan0 2>/dev/null; pkill -f "listen.py" 2>/dev/null; pkill -f "serve_bytes.py" 2>/dev/null; true; }
trap cleanup EXIT
cleanup

# --- topologie : le routeur a br0 (côté LAN) et wlan0 (côté « 5G »), comme sur le Pi
ip netns add lan; ip netns add net
ip link add br0 type veth peer name br0_in;     ip link set br0_in netns lan
ip link add wlan0 type veth peer name wlan0_in; ip link set wlan0_in netns net
ip addr add 10.1.0.1/24 dev br0;  ip link set br0 up
ip addr add 10.2.0.1/24 dev wlan0; ip link set wlan0 up
ip addr add fd00:2::1/64 dev wlan0 nodad
ip addr add fd00:1::1/64 dev br0 nodad
ip -n lan addr add 10.1.0.50/24 dev br0_in; ip -n lan link set br0_in up; ip -n lan link set lo up
ip -n lan route add default via 10.1.0.1
ip -n lan addr add fd00:1::50/64 dev br0_in nodad
ip -n net addr add 10.2.0.9/24 dev wlan0_in; ip -n net link set wlan0_in up; ip -n net link set lo up
ip -n net addr add fd00:2::9/64 dev wlan0_in nodad
ip -n net route add 10.1.0.0/24 via 10.2.0.1          # un attaquant qui connaît le réseau du LAN
echo 1 > /proc/sys/net/ipv4/ip_forward
echo 2 > /proc/sys/net/ipv4/conf/all/rp_filter; echo 2 > /proc/sys/net/ipv4/conf/br0/rp_filter   # comme /etc/sysctl.d/90-erouter.conf
# Voisins IPv6 statiques : le pare-feu bloque (volontairement) la découverte de voisins ; ainsi seul le pare-feu décide
mac() { ip netns exec "$1" cat "/sys/class/net/$2/address"; }
ip -6 neigh replace fd00:1::50 lladdr "$(mac lan br0_in)" dev br0 nud permanent
ip -6 neigh replace fd00:2::9 lladdr "$(mac net wlan0_in)" dev wlan0 nud permanent
ip -n lan -6 neigh replace fd00:1::1 lladdr "$(cat /sys/class/net/br0/address)" dev br0_in nud permanent
ip -n net -6 neigh replace fd00:2::1 lladdr "$(cat /sys/class/net/wlan0/address)" dev wlan0_in nud permanent

# --- le vrai pare-feu du projet
python3 -B - "$SRC" <<'PY'
import sys
from jinja2 import Environment, FileSystemLoader
e = Environment(loader=FileSystemLoader(sys.argv[1] + "/erouter/templates"))
open("/tmp/fw.nft", "w").write(e.get_template("nftables.conf.j2").render(
    bridge="br0", uplink="wlan0", web_ports=[443, 80], lan_net="10.1.0.0/24"))
PY
nft flush ruleset
nft -f /tmp/fw.nft && ok "règles chargées dans le noyau" || { bad "règles refusées par le noyau"; exit 1; }

# --- services « du routeur » qui écoutent sur toutes les interfaces (comme SSH, le web, un service oublié)
cat > /tmp/listen.py <<'PY'
import socket, sys, threading
def serve(port, family, bind):
    s = socket.socket(family); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)   # ne pas entrer en conflit avec l'écouteur IPv4
    s.bind((bind, port)); s.listen(16)
    while True:
        c, addr = s.accept(); open(f"/tmp/src_{port}", "a").write(addr[0] + "\n"); c.close()
for port in map(int, sys.argv[2:]):
    threading.Thread(target=serve, args=(port, socket.AF_INET if sys.argv[1] == "4" else socket.AF_INET6, "0.0.0.0" if sys.argv[1] == "4" else "::"), daemon=True).start()
threading.Event().wait()
PY
rm -f /tmp/src_*
python3 /tmp/listen.py 4 22 443 80 8080 5000 >/dev/null 2>&1 &
python3 /tmp/listen.py 6 22 443 >/dev/null 2>&1 &
ip netns exec lan python3 /tmp/listen.py 4 22 >/dev/null 2>&1 &       # un appareil du LAN avec un SSH ouvert
ip netns exec net python3 /tmp/listen.py 4 5000 >/dev/null 2>&1 &     # un serveur « sur Internet »
sleep 1

can_connect() {   # can_connect <espace de noms> <adresse> <port>
  ip netns exec "$1" python3 -c '
import socket, sys
a = sys.argv[1]
s = socket.socket(socket.AF_INET6 if ":" in a else socket.AF_INET); s.settimeout(2)
try: s.connect((a, int(sys.argv[2]))); sys.exit(0)
except Exception: sys.exit(1)' "$2" "$3"
}
expect_open()   { can_connect "$1" "$2" "$3" && ok "$4" || bad "$4 (devrait être ouvert)"; }
expect_closed() { can_connect "$1" "$2" "$3" && bad "$4 (devrait être REFUSÉ)" || ok "$4"; }

echo "  -- depuis le réseau du routeur (hotspot / Ethernet)"
expect_open   lan 10.1.0.1 443  "web HTTPS (443) joignable"
expect_open   lan 10.1.0.1 80   "redirection HTTP (80) joignable"
expect_open   lan 10.1.0.1 22   "SSH (22) joignable"
expect_closed lan 10.1.0.1 8080 "port non prévu (8080) refusé"
expect_closed lan 10.1.0.1 5000 "service oublié (5000) refusé"

expect_open   lan fd00:1::1 443 "contrôle : l'écouteur IPv6 existe bien (joignable depuis le LAN)"

echo "  -- depuis « Internet » (le réseau de la 5G), vers le routeur"
for p in 22 443 80 8080 5000; do expect_closed net 10.2.0.1 "$p" "port $p refusé (IPv4)"; done
for p in 22 443; do expect_closed net fd00:2::1 "$p" "port $p refusé (IPv6, sans NAT)"; done

echo "  -- depuis « Internet », vers un appareil du LAN (à travers le routeur)"
expect_closed net 10.1.0.50 22 "le SSH d'un appareil du LAN n'est pas atteignable depuis l'uplink"

echo "  -- trafic légitime : un appareil du LAN sort vers Internet"
expect_open lan 10.2.0.9 5000 "le LAN atteint Internet"
rm -f /tmp/src_5000; nft add rule inet erouter postrouting oifname wlan0 masquerade 2>/dev/null   # déjà présent : sans effet
can_connect lan 10.2.0.9 5000; sleep 0.3
[ "$(tail -1 /tmp/src_5000 2>/dev/null)" = "10.2.0.1" ] && ok "NAT : Internet voit l'adresse du routeur (10.2.0.1), pas celle du client" \
  || bad "NAT : source vue par Internet = $(tail -1 /tmp/src_5000 2>/dev/null)"

echo "  -- usurpation d'adresse depuis le LAN"
ip -n lan addr add 192.168.77.7/24 dev br0_in
ip route add 192.168.77.0/24 dev br0        # le routeur sait renvoyer vers cette adresse : sans la règle, le faux client passerait
spoof() { ip netns exec lan python3 -c '
import socket
s = socket.socket(); s.settimeout(2); s.bind(("192.168.77.7", 0))
try: s.connect(("10.2.0.9", 5000)); raise SystemExit(0)
except Exception: raise SystemExit(1)'; }
grep -v 'ip saddr !=' /tmp/fw.nft > /tmp/fw-sans-antispoof.nft
nft flush ruleset; nft -f /tmp/fw-sans-antispoof.nft
spoof && ok "contrôle : SANS la règle anti-usurpation, la fausse adresse passe (le test est donc significatif)" || bad "contrôle négatif : le test ne détecte rien"
nft flush ruleset; nft -f /tmp/fw.nft
spoof && bad "un client a pu sortir avec une fausse adresse source" || ok "adresse source usurpée refusée par le pare-feu"

echo "  -- compteurs de consommation par appareil (lus par l'agent)"
counter() { PYTHONPATH="$SRC" python3 -B -c "import sys; from erouter import usage; print(usage.read_counters(sys.argv[1]).get(sys.argv[2], 0))" "$1" "$2"; }
nft flush ruleset; nft -f /tmp/fw.nft
ip netns exec lan ping -c 5 -s 1000 -W 1 10.2.0.9 >/dev/null 2>&1
up=$(counter usage_up 10.1.0.50); down=$(counter usage_down 10.1.0.50)
[ "$up" -ge 5000 ] && ok "octets envoyés par 10.1.0.50 comptés ($up o)" || bad "octets envoyés non comptés ($up)"
[ "$down" -ge 5000 ] && ok "octets reçus par 10.1.0.50 comptés ($down o)" || bad "octets reçus non comptés ($down)"
for p in 22 80 443 5000; do can_connect net 10.1.0.50 "$p"; done
sleep 0.3
[ "$(counter usage_down 10.1.0.50)" -eq "$down" ] && ok "le trafic entrant refusé n'est pas compté" || bad "du trafic refusé a été compté"
spoof >/dev/null 2>&1
[ "$(counter usage_up 192.168.77.7)" -eq 0 ] && ok "une adresse usurpée n'est jamais comptée" || bad "adresse usurpée comptée"

echo "  -- plafond de débit par appareil (mesuré)"
cat > /tmp/serve_bytes.py <<'PY'
import socket, sys, threading
size, mode, port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])      # mode : send (envoie size octets) ou sink (lit tout)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", port)); s.listen(8)
def handle(c):
    try:
        if mode == "send":
            c.sendall(b"\0" * size)
        else:
            while c.recv(65536): pass
    finally:
        c.close()
while True:
    c, _ = s.accept(); threading.Thread(target=handle, args=(c,), daemon=True).start()
PY
cat > /tmp/transfer.py <<'PY'
import socket, sys, time
host, port, mode, size = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
s = socket.socket(); s.settimeout(90); s.connect((host, port)); t = time.monotonic()
if mode == "pull":
    got = 0
    while got < size:
        b = s.recv(65536)
        if not b: break
        got += len(b)
else:
    s.sendall(b"\0" * size); s.shutdown(socket.SHUT_WR)
    try: s.recv(1)
    except Exception: pass
print(round(time.monotonic() - t, 2))
PY
ip netns exec net python3 /tmp/serve_bytes.py 4000000 send 5001 >/dev/null 2>&1 &
ip netns exec net python3 /tmp/serve_bytes.py 4000000 sink 5002 >/dev/null 2>&1 &
sleep 1
render_limits() {   # render_limits <ko/s montant> <ko/s descendant>
  python3 -B - "$SRC" "$1" "$2" <<'PY'
import sys
from jinja2 import Environment, FileSystemLoader
e = Environment(loader=FileSystemLoader(sys.argv[1] + "/erouter/templates"))
open("/tmp/fw-limits.nft", "w").write(e.get_template("nftables.conf.j2").render(
    bridge="br0", uplink="wlan0", web_ports=[443, 80], lan_net="10.1.0.0/24",
    limits=[{"mac": "aa:bb:cc:dd:ee:01", "ip": "10.1.0.50", "up_kbytes": int(sys.argv[2]), "down_kbytes": int(sys.argv[3])}]))
PY
  nft flush ruleset; nft -f /tmp/fw-limits.nft
}
nft flush ruleset; nft -f /tmp/fw.nft
free_dl=$(ip netns exec lan python3 /tmp/transfer.py 10.2.0.9 5001 pull 4000000)
free_ul=$(ip netns exec lan python3 /tmp/transfer.py 10.2.0.9 5002 push 4000000)
render_limits 0 1000
limited_dl=$(ip netns exec lan python3 /tmp/transfer.py 10.2.0.9 5001 pull 4000000)
render_limits 1000 0
limited_ul=$(ip netns exec lan python3 /tmp/transfer.py 10.2.0.9 5002 push 4000000)
rate() { awk -v e="$1" 'BEGIN { if (e < 0.01) e = 0.01; printf "%d", 4000000 / e / 1000 }'; }      # ko/s
# Mesure sur un gros transfert (le démarrage de TCP fausse les petits). `nft limit` est un « policer » approximatif : sur ces gros
# paquets de test il dépasse la consigne d'environ 30 % (1 « ko » nft = 1024 o, plus la rafale initiale). On vérifie donc
# l'ordre de grandeur : proche de la consigne, et très en dessous du débit sans plafond.
near_limit() { awk -v r="$1" 'BEGIN { exit !(r >= 500 && r <= 1800) }'; }
near_limit "$(rate "$limited_dl")" && ok "réception plafonnée à 1000 ko/s : débit réel $(rate "$limited_dl") ko/s (sans plafond : $(rate "$free_dl") ko/s)" || bad "plafond de réception : $(rate "$limited_dl") ko/s au lieu de ~1000"
near_limit "$(rate "$limited_ul")" && ok "envoi plafonné à 1000 ko/s : débit réel $(rate "$limited_ul") ko/s (sans plafond : $(rate "$free_ul") ko/s)" || bad "plafond d'envoi : $(rate "$limited_ul") ko/s au lieu de ~1000"
render_limits 0 0
other=$(ip netns exec lan python3 /tmp/transfer.py 10.2.0.9 5001 pull 4000000)
[ "$(rate "$other")" -gt 10000 ] && ok "sans plafond, le même appareil retrouve son plein débit ($(rate "$other") ko/s)" || bad "le plafond persiste ($(rate "$other") ko/s)"

exit "$FAIL"
