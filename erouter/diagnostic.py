"""Diagnostic exportable : état, configuration SANS secrets, réseau, journaux. Pensé pour être joint à une demande d'aide."""
import copy
import json
import re
import time

import yaml

from . import __version__, system

SECRET_KEYS = frozenset({"password", "password_hash", "secret"})
UNITS = ("erouter-data", "erouter-agent", "erouter-web", "erouter-hostapd", "erouter-dnsmasq")
MAX_SECTION = 20000
_SECRETISH = re.compile(r"(passw|passphrase|psk|secret)", re.I)


def redact_config(cfg):
    """Copie de la config où chaque mot de passe, empreinte de mot de passe et secret est remplacé par ***."""
    out = copy.deepcopy(cfg)
    for section in out.values():
        if isinstance(section, dict):
            for key in SECRET_KEYS & set(section):
                if section[key]:
                    section[key] = "***"
    return out


def secret_values(cfg):
    values = [cfg["ap"]["password"], cfg["web"]["password_hash"], cfg["web"]["secret"]]
    return [v for v in values if isinstance(v, str) and len(v) >= 4]


def redact_text(text, secrets):
    """Masque les valeurs secrètes connues, et toute ligne qui évoque un mot de passe, une clé ou un secret."""
    for value in secrets:
        text = text.replace(value, "***")
    return "\n".join("[ligne masquée : évoque un secret]" if _SECRETISH.search(line) else line for line in text.splitlines())


def _section(title, cmd, secrets):
    p = system.run(cmd, timeout=15)
    body = redact_text((p.stdout + p.stderr)[:MAX_SECTION], secrets)
    return f"### {title}\n$ {' '.join(cmd)}\n{body}"


def collect(cfg, snapshot, build):
    """Texte du diagnostic. `snapshot` = état de l'agent, `build` = version de l'image."""
    secrets = secret_values(cfg)
    parts = [
        "EmergencyRouter : diagnostic\n"
        f"Date : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\nVersion : {__version__}\nImage : {json.dumps(build, ensure_ascii=False)}\n"
        "Ce fichier ne contient aucun mot de passe. Il contient en revanche des noms de réseaux Wi-Fi, des adresses IP et MAC :\n"
        "relis-le avant de le partager.",
        "### Configuration (secrets masqués)\n" + yaml.safe_dump(redact_config(cfg), sort_keys=False, allow_unicode=True),
        "### État de l'agent\n" + json.dumps(snapshot, indent=2, ensure_ascii=False),
    ]
    parts += [_section(t, c, secrets) for t, c in (
        ("Système", ["uname", "-a"]),
        ("Montages", ["findmnt", "-no", "TARGET,SOURCE,OPTIONS", "/", "/data"]),
        ("Espace disque", ["df", "-h", "/", "/data"]),
        ("Adresses", ["ip", "-br", "addr"]),
        ("Routes", ["ip", "route"]),
        ("Wi-Fi (interfaces)", ["iw", "dev"]),
        ("NetworkManager (appareils)", ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]),
        ("Pare-feu", ["nft", "list", "ruleset"]),
        ("Files d'attente (anti-latence)", ["tc", "qdisc", "show"]),
        ("Services", ["systemctl", "--no-pager", "-l", "--no-legend", "list-units", *[u + ".service" for u in UNITS], "ssh.service"]),
        ("Journaux (150 dernières lignes)", ["journalctl", "--no-pager", "-n", "150", *[a for u in UNITS for a in ("-u", u)]]),
    )]
    return "\n\n".join(parts) + "\n"
