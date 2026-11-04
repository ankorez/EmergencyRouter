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

cleanup() { ip netns del lan 2>/dev/null; ip netns del net 2>/dev/null; ip link del br0 2>/dev/null; ip link del wlan0 2>/dev/null; pkill -f "listen.py" 2>/dev/null; true; }
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
python3 /tmp/listen.py 4 22 443 80 8080 5000 &
python3 /tmp/listen.py 6 22 443 &
ip netns exec lan python3 /tmp/listen.py 4 22 &       # un appareil du LAN avec un SSH ouvert
ip netns exec net python3 /tmp/listen.py 4 5000 &     # un serveur « sur Internet »
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

exit "$FAIL"
