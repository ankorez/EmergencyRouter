"""Applique la config réseau : adresse du routeur sur le port Ethernet, dnsmasq (DHCP/DNS), nftables.

Le Wi-Fi du Pi ne sert qu'à se connecter à la 5G (uplink). Le réseau local passe par le port Ethernet, vers un switch et
une borne Wi-Fi indépendante (par exemple UniFi) configurée à part.
"""
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
_env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                   keep_trailing_newline=True)


def _write(name, content, mode=0o600):
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def lan_if(cfg):
    return cfg["lan"]["interface"]


def apply_interface(cfg):
    """Donne l'adresse du routeur au port Ethernet et le monte."""
    eth = lan_if(cfg)
    if not system.iface_exists(eth):
        raise RuntimeError(f"interface réseau local « {eth} » introuvable")
    wanted = str(ipaddress.ip_interface(cfg["lan"]["address"]))
    if system.iface_ip(eth) != wanted:
        system.run(["ip", "-4", "addr", "flush", "dev", eth])
        system.run(["ip", "addr", "add", wanted, "dev", eth], check=True)
    system.run(["ip", "link", "set", eth, "up"], check=True)


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
        lan_if=lan_if(cfg), gateway=str(ip.ip), netmask=str(ip.netmask),
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
        lan_if=lan_if(cfg), uplink=cfg["uplink"]["interface"], web_ports=web_ports(cfg),
        lan_net=str(ipaddress.ip_interface(cfg["lan"]["address"]).network), limits=limit_rules(cfg))
    path = _write("nftables.conf", conf, 0o600)
    system.run(["nft", "-f", path], check=True)


def apply_base(cfg, parts=("interface", "dnsmasq", "firewall")):
    """Applique les parties demandées, dans l'ordre : le DNS a besoin de l'adresse, le pare-feu des interfaces."""
    if "interface" in parts:
        system.run(["rfkill", "unblock", "wlan"])
        apply_interface(cfg)
    if "dnsmasq" in parts:
        apply_dnsmasq(cfg)
    if "firewall" in parts:
        apply_firewall(cfg)
