"""Boucle de fond : applique la config, surveille l'uplink Wi-Fi (5G partagée) et Internet."""
import copy
import logging
import threading
import time

from . import config, fs, history, lan, sqm, system, tls, usage, wd

log = logging.getLogger("erouter.agent")

wake = threading.Event()   # réveille la boucle après un changement de config
_lock = threading.Lock()
state = {
    "uplink": {"connected": False, "ssid": "", "channel": 0, "freq": 0, "signal": None,
               "ip": "", "internet": False},
    "lan": {"interface": "eth0", "link": None},
    "warnings": [],
    "fs": {"available": False, "enabled": False, "locked": False, "rescue": False, "data": "none"},
    "tls": {"enabled": False, "ready": False, "fingerprint": ""},
    "ssh": {"enabled": False, "active": False, "user": "pi", "password_set": False},
}

TICK = 5
OFFLINE_RECONNECT_AFTER = 90   # s sans Internet (alors qu'associé) avant de relancer le Wi-Fi
RECONNECT_COOLDOWN = 300

_offline_since = None
_last_reconnect = 0.0
_applied_base = {}             # partie -> valeur appliquée ("interface", "dnsmasq", "firewall")
_base_failed_at = 0.0
BASE_RETRY = 30                # s entre deux essais si l'application de la config échoue
_tls_failed_at = 0.0
_ssh_failed_at = 0.0
SSH_RETRY = 30                 # s entre deux tentatives de démarrage de sshd

HISTORY = history.History()
_usage = None
_last_sample = 0.0             # (horloge monotone) dernier échantillon d'historique et de comptage par appareil
_prev_bytes = None             # (horloge monotone, reçus, émis) pour calculer les débits
POWER_SAVE_EVERY = 30          # s entre deux vérifications de l'économie d'énergie Wi-Fi
_last_ps_check = 0.0
_sqm_applied = None            # None : jamais vu ; () : retiré ; sinon (activé, descendant, montant, interface)
_sqm_failed_at = 0.0
_sqm_error = ""


def snapshot():
    with _lock:
        return copy.deepcopy(state)


def request_apply():
    wake.set()


def _apply_base_if_changed(cfg):
    """Interface LAN, DHCP/DNS et pare-feu : chaque partie n'est réappliquée que si SA config a changé (évite de couper les clients).
    Un nouveau bail DHCP d'un appareil plafonné ne met à jour que le pare-feu."""
    global _base_failed_at
    lan_cfg = copy.deepcopy(cfg["lan"])
    wanted = {
        "interface": (lan_cfg,),
        "dnsmasq": (lan_cfg, bool(cfg["savings"]["block_ads"])),
        "firewall": (lan_cfg, cfg["uplink"]["interface"], lan.web_ports(cfg), lan.limit_rules(cfg)),
    }
    todo = [part for part in ("interface", "dnsmasq", "firewall") if _applied_base.get(part) != wanted[part]]
    if not todo or time.monotonic() - _base_failed_at < BASE_RETRY:
        return
    try:
        lan.apply_base(cfg, todo)
    except Exception:
        _base_failed_at = time.monotonic()    # pas de boucle d'erreurs toutes les 5 s
        log.exception("application de la config réseau impossible, nouvel essai dans %ds", BASE_RETRY)
        return
    _applied_base.update({part: wanted[part] for part in todo})


def _sync_power_save(up_if):
    """Avec l'économie d'énergie, la radio s'endort et la liaison avec le téléphone devient instable (latence, pertes).
    On la coupe, et on le revérifie car NetworkManager peut la remettre."""
    global _last_ps_check
    if time.monotonic() - _last_ps_check < POWER_SAVE_EVERY:
        return
    _last_ps_check = time.monotonic()
    if system.iface_exists(up_if) and system.power_save(up_if):
        if system.set_power_save(up_if, False):
            log.info("économie d'énergie Wi-Fi désactivée sur %s", up_if)
        else:
            log.warning("impossible de désactiver l'économie d'énergie Wi-Fi sur %s", up_if)


def get_usage():
    global _usage
    if _usage is None:
        _usage = usage.Usage()
    return _usage


def _record(link, up_if, ms):
    """Consommation (compteurs de l'interface et par appareil) et historique de la qualité de la connexion."""
    global _last_sample, _prev_bytes
    now, mono = time.time(), time.monotonic()
    u = get_usage()
    rx, tx = system.iface_bytes(up_if)
    if rx is not None:
        u.sample_iface(rx, tx, now)
    if mono - _last_sample >= history.SAMPLE_EVERY:
        who = {c["ip"]: (c["mac"], c["hostname"]) for c in system.clients(lan.LEASES)}
        u.sample_devices(usage.read_counters("usage_up"), usage.read_counters("usage_down"), who, now)
        down = up = None
        if rx is not None:
            if _prev_bytes and mono > _prev_bytes[0] and rx >= _prev_bytes[1] and tx >= _prev_bytes[2]:
                dt = mono - _prev_bytes[0]
                down, up = (rx - _prev_bytes[1]) / dt, (tx - _prev_bytes[2]) / dt
            _prev_bytes = (mono, rx, tx)
        HISTORY.record(now, link["signal"] if link["connected"] else None, ms, down, up)
        _last_sample = mono
    u.maybe_save()


def _sync_sqm(cfg, up_if):
    """Anti-latence : appliquée quand elle est activée, retirée sinon, revérifiée régulièrement (le noyau peut l'oublier)."""
    global _sqm_applied, _sqm_failed_at, _sqm_error
    if not system.iface_exists(up_if) or time.monotonic() - _sqm_failed_at < 60:
        return
    sq = cfg["sqm"]
    want = (bool(sq["enabled"]), int(sq["down_mbit"]), int(sq["up_mbit"]), up_if)
    if want[0]:
        if _sqm_applied != want or not sqm.active(up_if):
            ok, message = sqm.apply(up_if, want[1], want[2])
            if ok:
                _sqm_applied, _sqm_error = want, ""
            else:
                _sqm_failed_at, _sqm_error = time.monotonic(), message
                log.error("anti-latence impossible : %s", message)
    elif _sqm_applied != ():
        sqm.clear(up_if)
        _sqm_applied, _sqm_error = (), ""


def _maintain_tls(cfg):
    """Certificat HTTPS : créé au premier démarrage, renouvelé avant échéance ou si l'IP du routeur change."""
    global _tls_failed_at
    if not cfg["web"]["tls"] or time.monotonic() - _tls_failed_at < 60:
        return
    try:
        if tls.ensure(cfg["lan"]["address"]):
            system.run(["systemctl", "try-restart", "--no-block", "erouter-web"])   # recharge le certificat
    except Exception:
        _tls_failed_at = time.monotonic()
        log.exception("impossible de créer le certificat HTTPS (l'interface restera en HTTP)")


def _sync_ssh(cfg):
    """Démarre ou arrête sshd selon la config (réglage persistant dans /data, sans toucher au système en lecture seule).
    Retourne True si sshd tourne."""
    global _ssh_failed_at
    want = bool(cfg["ssh"]["enabled"])
    active = system.run(["systemctl", "is-active", "--quiet", "ssh"]).returncode == 0
    if want and not active and time.monotonic() - _ssh_failed_at >= SSH_RETRY:
        system.run(["systemctl", "stop", "ssh.socket"])        # l'activation par socket occuperait le port 22
        started = system.run(["systemctl", "start", "ssh"]).returncode == 0
        if not started:
            _ssh_failed_at = time.monotonic()
            log.error("sshd n'a pas pu démarrer")
        active = started
    elif not want and active:
        system.run(["systemctl", "stop", "ssh.socket", "ssh"])
        active = False
    return active


def _watchdog(up_if, link, internet):
    global _offline_since, _last_reconnect
    now = time.time()
    if link["connected"] and not internet:
        _offline_since = _offline_since or now
        if now - _offline_since > OFFLINE_RECONNECT_AFTER and now - _last_reconnect > RECONNECT_COOLDOWN:
            log.warning("Pas d'Internet depuis %ds : reconnexion du Wi-Fi", now - _offline_since)
            _last_reconnect = now
            system.reconnect(up_if)
    else:
        _offline_since = None


def _tick():
    wd.notify("WATCHDOG=1")
    cfg = config.load_for_agent()
    up_if = cfg["uplink"]["interface"]
    _maintain_tls(cfg)
    _apply_base_if_changed(cfg)

    link = system.wifi_link(up_if)
    ms = system.ping_ms(up_if) if link["connected"] else None
    internet = ms is not None
    _watchdog(up_if, link, internet)
    _record(link, up_if, ms)
    _sync_sqm(cfg, up_if)

    _sync_power_save(up_if)

    lan_if = lan.lan_if(cfg)
    carrier = system.carrier(lan_if)
    warnings = []
    if carrier is False:
        warnings.append(f"Aucun câble réseau détecté sur {lan_if} : branche le switch ou la borne Wi-Fi.")

    if _sqm_error and cfg["sqm"]["enabled"]:
        warnings.append(f"Anti-latence indisponible : {_sqm_error}")
    summary = get_usage().summary(cfg["usage"], time.time())
    if summary["percent"] is not None:
        if summary["percent"] >= 100:
            warnings.append(f"Forfait dépassé : {summary['percent']} % de {cfg['usage']['plan_gb']} Go consommés depuis le {summary['period']['start']}.")
        elif summary["percent"] >= cfg["usage"]["alert_percent"]:
            warnings.append(f"Forfait : {summary['percent']} % de {cfg['usage']['plan_gb']} Go consommés depuis le {summary['period']['start']}.")
    ssh_active = _sync_ssh(cfg)
    if cfg["ssh"]["enabled"] and not ssh_active:
        warnings.append("Le SSH est activé mais le serveur n'a pas pu démarrer.")
    fs_state = fs.status()
    if fs_state["data"] == "reset":
        warnings.append("La partition de données était abîmée (coupure de courant ?) : elle a été recréée. "
                        "Ta configuration a été remise aux valeurs par défaut : refais les réglages.")
    elif fs_state["data"] == "repaired":
        warnings.append("La partition de données a été réparée automatiquement au démarrage.")
    if cfg["web"]["tls"] and not tls.ready():
        warnings.append("HTTPS indisponible (certificat non créé) : l'interface est servie en HTTP.")

    with _lock:
        state["uplink"] = {
            "connected": link["connected"], "ssid": link["ssid"], "channel": link["channel"],
            "freq": link["freq"], "signal": link["signal"],
            "ip": system.iface_ip(up_if) if link["connected"] else "", "internet": internet,
        }
        state["lan"] = {"interface": lan_if, "link": carrier}
        state["warnings"] = warnings
        state["fs"] = fs_state
        state["tls"] = tls.status(cfg["web"]["tls"])
        state["ssh"] = {"enabled": bool(cfg["ssh"]["enabled"]), "active": ssh_active, "user": cfg["ssh"]["user"],
                        "password_set": bool(cfg["ssh"]["password_set"])}


def shutdown():
    """À l'arrêt propre : enregistre le comptage en cours."""
    get_usage().maybe_save(force=True)


def run_forever(stop):
    while not stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception("erreur dans la boucle de l'agent")
        wd.notify("WATCHDOG=1")
        try:
            fs.maintain()
        except Exception:
            log.exception("erreur de verrouillage de la carte")
        wake.wait(TICK)
        wake.clear()
