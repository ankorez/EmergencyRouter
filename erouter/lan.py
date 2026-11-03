"""Applique la config réseau : pont br0, ap0, hostapd, dnsmasq, nftables."""
import ipaddress
import os

from jinja2 import Environment, FileSystemLoader

from . import system

RUN_DIR = "/run/erouter"
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


def apply_dnsmasq(cfg):
    lan = cfg["lan"]
    ip = ipaddress.ip_interface(lan["address"])
    conf = _env.get_template("dnsmasq.conf.j2").render(
        bridge=BRIDGE, gateway=str(ip.ip), netmask=str(ip.netmask),
        start=lan["dhcp_start"], end=lan["dhcp_end"], lease=lan["lease"],
        dns=lan["dns"], leases=LEASES)
    _write("dnsmasq.conf", conf, 0o644)
    system.run(["systemctl", "restart", "erouter-dnsmasq"])


def apply_firewall(cfg):
    conf = _env.get_template("nftables.conf.j2").render(
        bridge=BRIDGE, uplink=cfg["uplink"]["interface"], web_port=int(cfg["web"]["port"]),
        lan_net=str(ipaddress.ip_interface(cfg["lan"]["address"]).network))
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


def apply_base(cfg):
    system.run(["rfkill", "unblock", "wlan"])
    apply_bridge(cfg)
    apply_dnsmasq(cfg)
    apply_firewall(cfg)


def current_ap():
    return _current_ap
