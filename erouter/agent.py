"""Boucle de fond : applique la config, suit le canal de l'uplink, garde l'AP aligné, surveille Internet."""
import copy
import logging
import threading
import time

from . import config, fs, lan, system

log = logging.getLogger("erouter.agent")

wake = threading.Event()   # réveille la boucle après un changement de config
_lock = threading.Lock()
state = {
    "uplink": {"connected": False, "ssid": "", "channel": 0, "freq": 0, "signal": None,
               "ip": "", "internet": False},
    "ap": {"enabled": False, "channel": 0, "active": False},
    "warnings": [],
    "fs": {"enabled": False, "locked": False, "rescue": False},
}

TICK = 5
OFFLINE_RECONNECT_AFTER = 90   # s sans Internet (alors qu'associé) avant de relancer le Wi-Fi
RECONNECT_COOLDOWN = 300

_pending = None                # (canal candidat, nb de lectures consécutives)
_offline_since = None
_last_reconnect = 0.0
_applied_base = None           # (lan, uplink, port web) réellement appliqué
_base_failed_at = 0.0
BASE_RETRY = 30                # s entre deux essais si l'application de la config échoue


def snapshot():
    with _lock:
        return copy.deepcopy(state)


def request_apply():
    wake.set()


def _apply_base_if_changed(cfg):
    """Pont, DHCP/DNS et pare-feu : uniquement si leur config a changé (évite de couper les clients)."""
    global _applied_base, _base_failed_at
    key = (copy.deepcopy(cfg["lan"]), cfg["uplink"]["interface"], cfg["web"]["port"])
    if key == _applied_base or time.monotonic() - _base_failed_at < BASE_RETRY:
        return
    try:
        lan.apply_base(cfg)
    except Exception:
        _base_failed_at = time.monotonic()    # pas de boucle d'erreurs toutes les 5 s
        log.exception("application de la config réseau impossible, nouvel essai dans %ds", BASE_RETRY)
        return
    _applied_base = key


def _desired_channel(cfg, link):
    # une seule puce Wi-Fi : l'AP doit être sur le canal de l'uplink ; sinon canal de repli
    if link["connected"] and link["channel"]:
        return link["channel"]
    return int(cfg["ap"]["fallback_channel"])


def _sync_ap(cfg, desired):
    """Applique l'AP ; un changement de canal doit être confirmé par deux lectures (anti-rebond)."""
    global _pending
    ap = cfg["ap"]
    cur = lan.current_ap()
    same_settings = cur is not None and cur[:4] == (bool(ap["enabled"]), ap["ssid"], ap["password"], ap["country"])
    if cur is None or (same_settings and cur[4] == desired) or not same_settings:
        _pending = None
        lan.apply_ap(cfg, desired)    # 1er démarrage, réglages modifiés, ou relance si hostapd est tombé
        return
    if _pending and _pending[0] == desired:
        lan.apply_ap(cfg, desired)
        _pending = None
    else:
        _pending = (desired, 1)


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
    cfg = config.load_for_agent()
    up_if = cfg["uplink"]["interface"]
    _apply_base_if_changed(cfg)

    link = system.wifi_link(up_if)
    internet = link["connected"] and system.ping(up_if)
    _watchdog(up_if, link, internet)

    desired = _desired_channel(cfg, link)
    _sync_ap(cfg, desired)

    ap_active = system.hostapd_active()
    warnings = []
    if cfg["ap"]["enabled"] and not ap_active:
        warnings.append("Le hotspot n'a pas pu démarrer sur le canal %d. Si la 5G est en 5 GHz sur un "
                        "canal DFS (52-144) ou si ta puce est 2,4 GHz seulement, passe le partage de "
                        "connexion du téléphone en 2,4 GHz." % desired)
    if link["connected"] and system.is_dfs(link["freq"]):
        warnings.append("L'uplink est sur un canal DFS : le hotspot risque de ne pas démarrer.")

    with _lock:
        state["uplink"] = {
            "connected": link["connected"], "ssid": link["ssid"], "channel": link["channel"],
            "freq": link["freq"], "signal": link["signal"],
            "ip": system.iface_ip(up_if) if link["connected"] else "", "internet": internet,
        }
        state["ap"] = {"enabled": bool(cfg["ap"]["enabled"]), "channel": desired, "active": ap_active}
        state["warnings"] = warnings
        state["fs"] = fs.status()


def run_forever(stop):
    while not stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception("erreur dans la boucle de l'agent")
        try:
            fs.maintain()
        except Exception:
            log.exception("erreur de verrouillage de la carte")
        wake.wait(TICK)
        wake.clear()
