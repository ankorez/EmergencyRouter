"""Agent privilégié (root) : applique la config, suit le canal, exécute les actions demandées par le web.

C'est le SEUL processus qui écrit la configuration (dans /data) ; le service web est en lecture seule
et ne peut demander que les méthodes de METHODS, dont chacune valide ses paramètres.
"""
import logging
import pwd
import signal
import subprocess
import threading
import time

from . import agent, buildinfo, config, diagnostic, fs, lan, rpc, speedtest, system, wd

log = logging.getLogger("erouter.agentd")


def _uplink():
    return config.load_for_agent()["uplink"]["interface"]


def _check_ssid(ssid):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32 or "\x00" in ssid or ssid.startswith("-"):
        raise ValueError("SSID invalide")


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
    system.connect(_uplink(), ssid, password, bool(hidden))   # le profil est enregistré dans /data
    agent.wake.set()
    return True


def m_forget(ssid):
    _check_ssid(ssid)
    system.forget(ssid)
    return True


def m_clients():
    return system.clients(lan.LEASES)


def m_save_config(cfg):
    """Enregistre une config proposée par le web, après nettoyage et validation complète."""
    new = config.sanitize(cfg)
    config.validate(new)
    config.save(new)
    agent.request_apply()
    return True


SSH_PASSWORD_MIN, SSH_PASSWORD_MAX = 12, 64
_WEAK = ("password", "motdepasse", "changeme", "erouter", "raspberry", "azerty", "qwerty", "123456")


def _check_ssh_user(user):
    """Jamais root ni un compte système : seul un vrai compte utilisateur peut recevoir un mot de passe SSH."""
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise ValueError(f"L'utilisateur « {user} » n'existe pas sur ce système")
    if entry.pw_uid < 1000 or entry.pw_shell.endswith(("nologin", "false")):
        raise ValueError(f"« {user} » est un compte système : refusé")


def _check_ssh_password(password, user):
    if not isinstance(password, str) or not SSH_PASSWORD_MIN <= len(password) <= SSH_PASSWORD_MAX:
        raise ValueError(f"Mot de passe SSH : {SSH_PASSWORD_MIN} à {SSH_PASSWORD_MAX} caractères")
    if not all(32 <= ord(c) < 127 for c in password) or password != password.strip():
        raise ValueError("Mot de passe SSH : caractères ASCII imprimables, sans espace au début ni à la fin")
    lower = password.lower()
    for weak in (user, *_WEAK):
        if weak in lower:
            raise ValueError(f"Mot de passe SSH trop prévisible (contient « {weak} »)")


def m_set_ssh(enabled, password=None):
    """Active/désactive le SSH et/ou change le mot de passe du compte. Le SSH n'est joignable que depuis le LAN."""
    cfg = config.load_for_agent()
    ssh = cfg["ssh"]
    user = ssh["user"]
    _check_ssh_user(user)
    if password:
        _check_ssh_password(password, user)
        with fs.writable():     # /etc/shadow est sur le système (lecture seule) : fenêtre d'écriture d'une seconde
            r = system.run(["chpasswd"], input_text=f"{user}:{password}\n")
        if r.returncode != 0:
            raise RuntimeError("Impossible de changer le mot de passe")
        ssh["password_set"] = True
    if enabled and not ssh["password_set"]:
        raise ValueError("Définis d'abord un mot de passe SSH")
    ssh["enabled"] = bool(enabled)
    config.save(cfg)
    agent.request_apply()
    return {"enabled": ssh["enabled"], "password_set": ssh["password_set"], "user": user}


_SPEEDTEST = speedtest.Speedtest()


def m_usage():
    return agent.get_usage().summary(config.load_for_agent()["usage"], time.time())


def m_usage_reset():
    agent.get_usage().reset()
    return True


def m_history():
    return agent.HISTORY.series(time.time())


def m_speedtest_start():
    if not agent.snapshot()["uplink"]["internet"]:
        raise ValueError("Pas d'Internet : impossible de tester le débit")
    _SPEEDTEST.start(_uplink())
    return _SPEEDTEST.status()


def m_speedtest_status():
    return _SPEEDTEST.status()


def m_diagnostic():
    return diagnostic.collect(config.load_for_agent(), agent.snapshot(), buildinfo.load())


def m_reboot():
    subprocess.Popen(["systemctl", "reboot"])
    return True


METHODS = {"status": m_status, "scan": m_scan, "connect": m_connect, "forget": m_forget,
           "clients": m_clients, "save_config": m_save_config, "set_ssh": m_set_ssh, "reboot": m_reboot,
           "usage": m_usage, "usage_reset": m_usage_reset, "history": m_history, "speedtest_start": m_speedtest_start,
           "speedtest_status": m_speedtest_status, "diagnostic": m_diagnostic}


def _ensure_secret():
    """Crée le secret de session au premier démarrage (le web n'a pas le droit d'écrire)."""
    cfg = config.load()
    if not cfg["web"]["secret"]:
        cfg["web"]["secret"] = config.new_secret()
        config.save(config.sanitize(cfg))


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        _ensure_secret()
    except Exception:
        log.exception("impossible de créer le secret de session")
    rpc.serve(METHODS)
    stop = threading.Event()

    def _stop(*_):
        stop.set()
        agent.wake.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    wd.notify("READY=1")
    agent.run_forever(stop)
    agent.shutdown()


if __name__ == "__main__":
    main()
