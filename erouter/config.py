import copy
import os
import secrets
import threading

import yaml

CONFIG_PATH = os.environ.get("EROUTER_CONFIG", "/etc/erouter/config.yaml")
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
    "web": {
        "port": 80,
        "password_hash": "",           # vide => écran de premier démarrage
        "secret": "",
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        try:
            with open(CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        cfg = _merge(DEFAULTS, data)
        if not cfg["web"]["secret"]:
            cfg["web"]["secret"] = secrets.token_hex(32)
            _write(cfg)
        return cfg


def save(cfg):
    with _lock:
        _write(cfg)


def _write(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, CONFIG_PATH)
