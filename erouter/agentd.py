"""Agent privilégié (root) : applique la config, suit le canal, exécute les actions demandées par le web.

C'est le SEUL processus qui écrit sur la carte SD (via fs.writable) ; le service web est en lecture seule
et ne peut demander que les méthodes de METHODS, dont chacune valide ses paramètres.
"""
import logging
import subprocess
import threading

from . import agent, config, fs, lan, rpc, system

log = logging.getLogger("erouter.agentd")
WIFI_PROFILE_LINGER = 4   # s : laisse NetworkManager finir d'écrire le profil avant de reverrouiller la carte


def _uplink():
    return config.load_for_agent()["uplink"]["interface"]


def _check_ssid(ssid):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32 or "\x00" in ssid or ssid.startswith("-"):
        raise ValueError("SSID invalide")


def _persist(cfg):
    with fs.writable():
        config.save(cfg)


def m_status():
    s = agent.snapshot()
    s["clients"] = len(system.clients(lan.LEASES))
    s["uptime"] = system.uptime()
    return s


def m_scan(rescan=False):
    return {"networks": system.scan(_uplink(), bool(rescan)), "saved": system.saved_networks()}


def m_connect(ssid, password=None, hidden=False):
    _check_ssid(ssid)
    password = password or None
    if password is not None and (not isinstance(password, str) or not 8 <= len(password) <= 63):
        raise ValueError("Mot de passe Wi-Fi : 8 à 63 caractères")
    with fs.writable(linger=WIFI_PROFILE_LINGER):    # NetworkManager enregistre le profil dans /etc
        system.connect(_uplink(), ssid, password, bool(hidden))
    agent.wake.set()
    return True


def m_forget(ssid):
    _check_ssid(ssid)
    with fs.writable(linger=WIFI_PROFILE_LINGER):
        system.forget(ssid)
    return True


def m_clients():
    return system.clients(lan.LEASES)


def m_save_config(cfg):
    """Enregistre une config proposée par le web, après nettoyage et validation complète."""
    new = config.sanitize(cfg)
    config.validate(new)
    _persist(new)
    agent.request_apply()
    return True


def m_fs_set(enabled):
    fs.set_enabled(bool(enabled))
    return fs.status()


def m_reboot():
    subprocess.Popen(["systemctl", "reboot"])
    return True


METHODS = {"status": m_status, "scan": m_scan, "connect": m_connect, "forget": m_forget,
           "clients": m_clients, "save_config": m_save_config, "fs_set": m_fs_set, "reboot": m_reboot}


def _ensure_secret():
    """Crée le secret de session au premier démarrage (le web n'a pas le droit d'écrire)."""
    cfg = config.load()
    if not cfg["web"]["secret"]:
        cfg["web"]["secret"] = config.new_secret()
        _persist(config.sanitize(cfg))


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        _ensure_secret()
    except Exception:
        log.exception("impossible de créer le secret de session")
    rpc.serve(METHODS)
    agent.run_forever(threading.Event())


if __name__ == "__main__":
    main()
