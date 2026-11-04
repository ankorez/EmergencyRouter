"""Applique la config réseau : pont br0, ap0, hostapd, dnsmasq, nftables."""
import gzip
import ipaddress
import logging
import os
import shutil

from jinja2 import Environment, FileSystemLoader

from . import system

log = logging.getLogger("erouter.lan")

RUN_DIR = "/run/erouter"
BLOCKLIST_GZ = os.path.join(os.path.dirname(__file__), "data", "blocklist.hosts.gz")
BLOCKLIST_RUN = os.path.join(RUN_DIR, "blocklist.hosts")
LEASES = "/run/erouter/dnsmasq.leases"   # en RAM : pas d'écriture sur la carte SD
BRIDGE = "br0"
AP_IF = "ap0"
_env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                   keep_trailing_newline=True)

_current_ap = None  # (enabled, ssid, password, country, channel) réellement appliqué


def _write(name, content, mode=0o600):
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def apply_bridge(cfg):
    lan = cfg["lan"]
    if not system.iface_exists(BRIDGE):
        system.run(["ip", "link", "add", BRIDGE, "type", "bridge"], check=True)
    if system.iface_ip(BRIDGE) != str(ipaddress.ip_interface(lan["address"])):
        system.run(["ip", "addr", "flush", "dev", BRIDGE])
        system.run(["ip", "addr", "add", lan["address"], "dev", BRIDGE], check=True)
    system.run(["ip", "link", "set", BRIDGE, "up"], check=True)
    eth = lan.get("interface")
    if system.iface_exists(eth):
        system.run(["ip", "link", "set", eth, "master", BRIDGE])
        system.run(["ip", "link", "set", eth, "up"])


def ensure_ap_iface(cfg):
    if system.iface_exists(AP_IF):
        return
    up = cfg["uplink"]["interface"]
    system.run(["iw", "dev", up, "interface", "add", AP_IF, "type", "__ap"], check=True)


def prepare_blocklist(cfg):
    """Chemin de la liste de blocage décompressée en mémoire (/run), ou None si le blocage est désactivé."""
    if not cfg["savings"]["block_ads"]:
        return None
    if not os.path.isfile(BLOCKLIST_GZ):
        log.warning("liste de blocage absente : blocage désactivé")
        return None
    os.makedirs(RUN_DIR, exist_ok=True)
    if not os.path.isfile(BLOCKLIST_RUN) or os.path.getmtime(BLOCKLIST_RUN) < os.path.getmtime(BLOCKLIST_GZ):
        tmp = BLOCKLIST_RUN + ".tmp"
        with gzip.open(BLOCKLIST_GZ, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, BLOCKLIST_RUN)
    return BLOCKLIST_RUN


def limit_rules(cfg):
    """Plafonds de débit par appareil, traduits en règles : on retrouve l'adresse IP actuelle de chaque MAC (baux DHCP)."""
    ips = {c["mac"]: c["ip"] for c in system.clients(LEASES)}
    rules = []
    for lim in cfg["savings"]["limits"]:
        if lim["mac"] in ips:
            rules.append({"mac": lim["mac"], "ip": ips[lim["mac"]],
                          "up_kbytes": int(lim["up_mbit"]) * 125, "down_kbytes": int(lim["down_mbit"]) * 125})   # Mbit/s -> ko/s
    return rules


def apply_dnsmasq(cfg):
    lan = cfg["lan"]
    ip = ipaddress.ip_interface(lan["address"])
    conf = _env.get_template("dnsmasq.conf.j2").render(
        bridge=BRIDGE, gateway=str(ip.ip), netmask=str(ip.netmask),
        start=lan["dhcp_start"], end=lan["dhcp_end"], lease=lan["lease"],
        dns=lan["dns"], leases=LEASES, blocklist=prepare_blocklist(cfg))
    _write("dnsmasq.conf", conf, 0o644)
    system.run(["systemctl", "restart", "erouter-dnsmasq"])


def web_ports(cfg):
    ports = [int(cfg["web"]["port"])]
    if int(cfg["web"]["http_port"]):
        ports.append(int(cfg["web"]["http_port"]))
    return ports


def apply_firewall(cfg):
    conf = _env.get_template("nftables.conf.j2").render(
        bridge=BRIDGE, uplink=cfg["uplink"]["interface"], web_ports=web_ports(cfg),
        lan_net=str(ipaddress.ip_interface(cfg["lan"]["address"]).network), limits=limit_rules(cfg))
    path = _write("nftables.conf", conf, 0o600)
    system.run(["nft", "-f", path], check=True)


def apply_ap(cfg, channel):
    """(Re)démarre hostapd sur le canal demandé si quelque chose a changé."""
    global _current_ap
    ap = cfg["ap"]
    wanted = (bool(ap["enabled"]), ap["ssid"], ap["password"], ap["country"], int(channel))
    if wanted == _current_ap and (not ap["enabled"] or system.hostapd_active()):
        return False
    _current_ap = wanted
    if not ap["enabled"]:
        system.run(["systemctl", "stop", "erouter-hostapd"])
        return True
    ensure_ap_iface(cfg)
    conf = _env.get_template("hostapd.conf.j2").render(
        iface=AP_IF, bridge=BRIDGE, ssid=ap["ssid"], password=ap["password"],
        country=ap["country"], channel=int(channel), hw_mode="g" if int(channel) <= 14 else "a")
    _write("hostapd.conf", conf, 0o600)
    system.run(["systemctl", "restart", "erouter-hostapd"])
    return True


def apply_base(cfg, parts=("bridge", "dnsmasq", "firewall")):
    """Applique les parties demandées, dans l'ordre : le DNS a besoin du pont, le pare-feu des interfaces."""
    if "bridge" in parts:
        system.run(["rfkill", "unblock", "wlan"])
        apply_bridge(cfg)
    if "dnsmasq" in parts:
        apply_dnsmasq(cfg)
    if "firewall" in parts:
        apply_firewall(cfg)


def current_ap():
    return _current_ap
