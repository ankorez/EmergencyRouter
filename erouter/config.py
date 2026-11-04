import copy
import ipaddress
import logging
import os
import re
import secrets
import shutil
import threading

import yaml

log = logging.getLogger("erouter.config")

DATA_DIR = os.environ.get("EROUTER_DATA", "/data")         # partition de données (ou simple dossier)
CONFIG_PATH = os.environ.get("EROUTER_CONFIG", os.path.join(DATA_DIR, "erouter", "config.yaml"))
_lock = threading.Lock()

DEFAULTS = {
    "lan": {
        "address": "192.168.1.1/24",   # idéalement la même IP que ta box fibre
        "interface": "eth0",           # port Ethernet vers le switch ("" pour désactiver)
        "dhcp_start": "192.168.1.100",
        "dhcp_end": "192.168.1.250",
        "lease": "12h",
        "dns": ["1.1.1.1", "9.9.9.9"],
    },
    "ap": {
        "enabled": True,
        "ssid": "MaBox",               # même SSID que la box fibre
        "password": "changeme123",     # même mot de passe que la box fibre
        "country": "FR",
        "fallback_channel": 6,         # utilisé tant que l'uplink n'est pas connecté
    },
    "uplink": {
        "interface": "wlan0",
    },
    "ssh": {
        "enabled": False,              # éteint par défaut ; s'active depuis l'interface (réseau du routeur uniquement)
        "user": "pi",                  # compte existant (uid >= 1000) dont on règle le mot de passe
        "password_set": False,
    },
    "web": {
        "port": 443,                   # HTTPS
        "http_port": 80,               # redirige vers HTTPS (0 = désactivé)
        "tls": True,                   # HTTPS avec certificat auto-signé
        "password_hash": "",           # vide => écran de premier démarrage
        "secret": "",
    },
}

_IFACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")   # jamais de « - » en tête : pas pris pour une option
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
_LEASE = re.compile(r"^\d{1,5}[smhd]?$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(cfg):
    """Lève ValueError si la config est invalide.

    Appelée par le web avant d'enregistrer, ET par l'agent (root) à chaque lecture :
    le fichier est modifiable par le service web, il ne faut jamais lui faire confiance.
    """
    try:
        _validate(cfg)
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"Configuration invalide ({e})")


def _validate(cfg):
    lan, ap, up, web, ssh = cfg["lan"], cfg["ap"], cfg["uplink"], cfg["web"], cfg["ssh"]

    iface = ipaddress.IPv4Interface(lan["address"])
    net = iface.network
    if not 8 <= net.prefixlen <= 29 or iface.ip in (net.network_address, net.broadcast_address):
        raise ValueError("Adresse LAN invalide")
    start, end = ipaddress.IPv4Address(lan["dhcp_start"]), ipaddress.IPv4Address(lan["dhcp_end"])
    if start not in net or end not in net or start > end:
        raise ValueError("Plage DHCP incohérente avec le réseau LAN")
    if not _LEASE.fullmatch(str(lan["lease"])):
        raise ValueError("Durée de bail invalide (ex. 12h)")
    dns = lan["dns"]
    if not isinstance(dns, list) or not 1 <= len(dns) <= 4:
        raise ValueError("DNS : 1 à 4 adresses")
    for d in dns:
        ipaddress.IPv4Address(d)
    if lan["interface"] and not _IFACE.fullmatch(lan["interface"]):
        raise ValueError("Nom d'interface LAN invalide")
    if not _IFACE.fullmatch(up["interface"]):
        raise ValueError("Nom d'interface uplink invalide")

    if not isinstance(ssh["enabled"], bool) or not isinstance(ssh["password_set"], bool):
        raise ValueError("Réglages SSH invalides")
    if not isinstance(ssh["user"], str) or not _USER.fullmatch(ssh["user"]):
        raise ValueError("Nom d'utilisateur SSH invalide")

    ssid, pw = ap["ssid"], ap["password"]
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32 \
            or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("SSID : 1 à 32 caractères")
    if not isinstance(pw, str) or not 8 <= len(pw) <= 63 or not all(32 <= ord(c) < 127 for c in pw):
        raise ValueError("Mot de passe Wi-Fi : 8 à 63 caractères ASCII")
    if not isinstance(ap["country"], str) or not _COUNTRY.fullmatch(ap["country"]):
        raise ValueError("Pays : code à 2 lettres majuscules (ex. FR)")
    if not 1 <= int(ap["fallback_channel"]) <= 13:
        raise ValueError("Canal de repli : 1 à 13")
    port, http_port = int(web["port"]), int(web["http_port"])
    reserved = (22, 53, 67, 5353)
    if not 1 <= port <= 65535 or port in reserved:
        raise ValueError("Port web invalide")
    if http_port and (not 1 <= http_port <= 65535 or http_port in reserved or http_port == port):
        raise ValueError("Port HTTP de redirection invalide")
    if not isinstance(web["tls"], bool):
        raise ValueError("Option HTTPS invalide")
    if not isinstance(web["password_hash"], str) or not isinstance(web["secret"], str) \
            or len(web["password_hash"]) > 300 or len(web["secret"]) > 200:
        raise ValueError("Champs web invalides")


def sanitize(cfg):
    """Ne garde que les clés connues (complétées par les défauts) : rien d'inattendu n'est jamais écrit."""
    if not isinstance(cfg, dict):
        raise ValueError("Configuration invalide")
    out = {}
    for section, defaults in DEFAULTS.items():
        given = cfg.get(section) if isinstance(cfg.get(section), dict) else {}
        out[section] = {k: copy.deepcopy(given.get(k, v)) for k, v in defaults.items()}
    return out


def _read():
    try:
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _merge(DEFAULTS, data)


def load():
    """Lecture seule (service web). Le secret de session est créé par l'agent."""
    with _lock:
        return _read()


def new_secret():
    return secrets.token_hex(32)


_last_good = None


def load_for_agent():
    """Lecture pour l'agent root : ne modifie jamais le fichier, valide toujours,
    et retombe sur la dernière config valide (ou les défauts) en cas de problème."""
    global _last_good
    with _lock:
        try:
            cfg = _read()
            validate(cfg)
            _last_good = cfg
        except Exception as e:
            log.error("config.yaml invalide, dernière config valide conservée : %s", e)
            cfg = _last_good or copy.deepcopy(DEFAULTS)
        return copy.deepcopy(cfg)


def save(cfg):
    with _lock:
        _write(cfg)


def _write(cfg):
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
        f.flush()
        os.fsync(f.fileno())          # la carte SD peut perdre le courant : on force l'écriture
    try:                              # lisible par le service web (groupe erouter), jamais modifiable par lui
        shutil.chown(tmp, user=0, group="erouter")
        os.chmod(tmp, 0o640)
    except (LookupError, PermissionError, OSError):
        pass                          # poste de dev sans utilisateur erouter
    os.replace(tmp, CONFIG_PATH)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
