"""Certificat HTTPS auto-signé : créé et renouvelé par l'agent (root), lu par le service web (groupe erouter).

Le certificat vit dans /data (il reste donc le même d'un démarrage à l'autre) et couvre erouter.local, erouter et
l'adresse IP du routeur. Il est valable 825 jours et renouvelé 60 jours avant l'échéance, ou si l'adresse IP change.
"""
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import ssl
import tempfile
import time

from . import config, system

log = logging.getLogger("erouter.tls")

TLS_DIR = os.environ.get("EROUTER_TLS_DIR", os.path.join(config.DATA_DIR, "erouter", "tls"))
VALID_DAYS = 825
RENEW_BEFORE_DAYS = 60
# Le Pi n'a pas de pile : avant la synchro NTP l'horloge peut être très en retard. On ne laisse jamais un
# certificat expirer avant cette date plancher + 825 jours, quelle que soit l'horloge au moment de la création.
CLOCK_FLOOR = 1_793_000_000   # ~ 26 oct. 2026 (date de la version)


def paths():
    return (os.path.join(TLS_DIR, "server.crt"), os.path.join(TLS_DIR, "server.key"),
            os.path.join(TLS_DIR, "meta.json"))


def ready():
    crt, key, _ = paths()
    return os.path.isfile(crt) and os.path.isfile(key)


def _meta():
    try:
        with open(paths()[2]) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def needs_renewal(ip, now=None):
    now = time.time() if now is None else now
    if not ready():
        return True
    meta = _meta()
    if meta.get("ip") != ip:
        return True
    clock_ok = now > CLOCK_FLOOR
    return clock_ok and now > meta.get("not_after", 0) - RENEW_BEFORE_DAYS * 86400


def _validity(now):
    """(jours à demander à openssl, instant d'expiration). Jamais d'expiration avant le plancher + 825 jours."""
    not_after = max(now, CLOCK_FLOOR) + VALID_DAYS * 86400
    return int((not_after - now) / 86400) + 1, not_after


def ensure(lan_address):
    """Crée ou renouvelle le certificat si nécessaire. Retourne True si un nouveau certificat a été écrit."""
    ip = str(ipaddress.ip_interface(lan_address).ip)
    if not needs_renewal(ip):
        return False
    os.makedirs(TLS_DIR, exist_ok=True)
    try:                                  # le service web (groupe erouter) doit pouvoir traverser le dossier
        shutil.chown(TLS_DIR, user=0, group="erouter")
    except (LookupError, PermissionError, OSError):
        pass
    os.chmod(TLS_DIR, 0o750)
    now = time.time()
    days, not_after = _validity(now)
    with tempfile.TemporaryDirectory(dir=TLS_DIR) as tmp:
        crt, key = os.path.join(tmp, "server.crt"), os.path.join(tmp, "server.key")
        system.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", key, "-out", crt, "-days", str(days), "-subj", "/CN=erouter.local/O=EmergencyRouter",
            "-addext", f"subjectAltName=DNS:erouter.local,DNS:erouter,IP:{ip}",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature",
            "-addext", "extendedKeyUsage=serverAuth",
        ], timeout=60, check=True)
        for f in (crt, key):
            try:
                shutil.chown(f, user=0, group="erouter")
            except (LookupError, PermissionError, OSError):
                pass                      # poste de dev sans utilisateur erouter
            os.chmod(f, 0o640)
        with open(os.path.join(tmp, "meta.json"), "w") as f:
            json.dump({"ip": ip, "created": now, "not_after": not_after}, f)
        for name in ("server.key", "server.crt", "meta.json"):   # la clé d'abord : jamais de certificat sans clé
            os.replace(os.path.join(tmp, name), os.path.join(TLS_DIR, name))
    log.info("certificat HTTPS créé pour %s (valable %d jours)", ip, days)
    return True


def fingerprint():
    """Empreinte SHA-256 du certificat (à comparer lors de la première visite)."""
    try:
        with open(paths()[0]) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return ""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def status(enabled):
    return {"enabled": bool(enabled), "ready": ready(), "fingerprint": fingerprint() if ready() else ""}
