"""Boucle de fond : suit le canal de l'uplink, garde l'AP aligné, surveille Internet."""
import logging
import threading
import time

from . import config, lan, system

log = logging.getLogger("erouter.agent")

wake = threading.Event()   # réveille la boucle après un changement de config
_lock = threading.Lock()
state = {
    "uplink": {"connected": False, "ssid": "", "channel": 0, "freq": 0, "signal": None,
               "ip": "", "internet": False},
    "ap": {"enabled": False, "channel": 0, "active": False},
    "warnings": [],
}

TICK = 5
OFFLINE_RECONNECT_AFTER = 90   # s sans Internet (alors qu'associé) avant de relancer le Wi-Fi
RECONNECT_COOLDOWN = 300

_pending = None                # (canal candidat, nb de lectures consécutives)
_offline_since = None
_last_reconnect = 0.0
_base_applied = False


def snapshot():
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in state.items()}


def request_apply():
    global _base_applied
    _base_applied = False
    wake.set()


def _tick():
    global _pending, _offline_since, _last_reconnect, _base_applied
    cfg = config.load_for_agent()
    up_if = cfg["uplink"]["interface"]

    if not _base_applied:
        lan.apply_base(cfg)
        _base_applied = True
        lan._current_ap = None   # force la réécriture hostapd

    link = system.wifi_link(up_if)
    internet = link["connected"] and system.ping(up_if)
    now = time.time()

    warnings = []
    if link["connected"] and not internet:
        _offline_since = _offline_since or now
        if now - _offline_since > OFFLINE_RECONNECT_AFTER and now - _last_reconnect > RECONNECT_COOLDOWN:
            log.warning("Pas d'Internet depuis %ds : reconnexion du Wi-Fi", now - _offline_since)
            _last_reconnect = now
            system.reconnect(up_if)
    else:
        _offline_since = None

    # canal voulu pour l'AP : celui de l'uplink (puce Wi-Fi unique), sinon le canal de repli
    desired = link["channel"] if link["connected"] and link["channel"] else int(cfg["ap"]["fallback_channel"])
    cur = lan.current_ap()
    if cur is None or cur[4] != desired or cur[:4] != (bool(cfg["ap"]["enabled"]), cfg["ap"]["ssid"],
                                                       cfg["ap"]["password"], cfg["ap"]["country"]):
        # anti-rebond : deux lectures identiques avant de redémarrer l'AP (sauf premier démarrage)
        if cur is None or _pending and _pending[0] == desired and _pending[1] >= 1:
            lan.apply_ap(cfg, desired)
            _pending = None
        else:
            _pending = (desired, (_pending[1] + 1) if _pending and _pending[0] == desired else 1)
    else:
        _pending = None
        lan.apply_ap(cfg, desired)  # relance hostapd s'il est tombé

    ap_active = system.hostapd_active()
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


def run_forever(stop):
    while not stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception("erreur dans la boucle de l'agent")
        wake.wait(TICK)
        wake.clear()
