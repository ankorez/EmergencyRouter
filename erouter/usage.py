"""Consommation de données de l'uplink : total, par jour et par appareil. Persistée dans /data (écriture espacée)."""
import datetime
import json
import logging
import os
import threading
import time

from . import config, system
from .tls import CLOCK_FLOOR

log = logging.getLogger("erouter.usage")

STORE = os.environ.get("EROUTER_USAGE", os.path.join(config.DATA_DIR, "erouter", "usage.json"))
KEEP_DAYS = 100          # jours conservés
SAVE_EVERY = 600         # s entre deux écritures : une coupure de courant perd au plus 10 minutes de comptage
TOP_DEVICES = 20


def parse_counters(text):
    """Sortie JSON de `nft -j list set` -> {adresse IP: octets}."""
    out = {}
    try:
        items = json.loads(text).get("nftables", [])
    except (ValueError, AttributeError):
        return out
    for item in items:
        for entry in (item.get("set") or {}).get("elem", []):
            el = entry.get("elem") if isinstance(entry, dict) else None
            if isinstance(el, dict) and isinstance(el.get("val"), str) and isinstance(el.get("counter"), dict):
                out[el["val"]] = int(el["counter"].get("bytes", 0))
    return out


def read_counters(name):
    p = system.run(["nft", "-j", "list", "set", "inet", "erouter", name])
    return parse_counters(p.stdout) if p.returncode == 0 else {}


def period_start(today, reset_day):
    """Premier jour de la période de facturation en cours."""
    if today.day >= reset_day:
        return today.replace(day=reset_day)
    first = today.replace(day=1) - datetime.timedelta(days=1)       # dernier jour du mois précédent
    return first.replace(day=reset_day)


class Usage:
    def __init__(self, path=None):
        self.path = path or STORE
        self._lock = threading.RLock()
        self.data = self._load()
        self._last_if = None
        self._last_dev = {"up": {}, "down": {}}
        self._dirty = False
        self._saved = time.monotonic()

    # ---- stockage
    @staticmethod
    def _empty():
        return {"v": 1, "days": {}, "pending": {"rx": 0, "tx": 0}, "devices": {}}

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("days"), dict) and isinstance(data.get("devices"), dict):
                data.setdefault("pending", {"rx": 0, "tx": 0})
                return data
        except (OSError, ValueError):
            pass
        return self._empty()

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved = time.monotonic()

    def maybe_save(self, force=False):
        with self._lock:
            if self._dirty and (force or time.monotonic() - self._saved >= SAVE_EVERY):
                try:
                    self.save()
                except OSError:
                    log.exception("impossible d'enregistrer la consommation")

    def reset(self):
        with self._lock:
            self.data = self._empty()
            self._last_dev = {"up": {}, "down": {}}
            self._dirty = True
            self.maybe_save(force=True)

    # ---- comptage
    @staticmethod
    def day(now):
        """Date locale du jour, ou None tant que l'horloge n'est pas fiable (pas de pile : avant la synchro NTP)."""
        return time.strftime("%Y-%m-%d", time.localtime(now)) if now > CLOCK_FLOOR else None

    def _credit(self, now, rx, tx):
        day = self.day(now)
        pending = self.data["pending"]
        if day is None:
            bucket = pending
        else:
            bucket = self.data["days"].setdefault(day, {"rx": 0, "tx": 0})
            if pending["rx"] or pending["tx"]:          # l'heure est enfin correcte : on verse ce qui n'avait pas de date
                bucket["rx"] += pending["rx"]
                bucket["tx"] += pending["tx"]
                pending["rx"] = pending["tx"] = 0
            self._prune(day)
        bucket["rx"] += rx
        bucket["tx"] += tx
        self._dirty = True

    def _prune(self, today):
        limit = (datetime.date.fromisoformat(today) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        for day in [d for d in self.data["days"] if d < limit]:
            del self.data["days"][day]
        for mac, rec in list(self.data["devices"].items()):
            for day in [d for d in rec["days"] if d < limit]:
                del rec["days"][day]
            if not rec["days"]:
                del self.data["devices"][mac]

    def sample_iface(self, rx, tx, now):
        """Compteurs cumulés de l'interface (reçu = téléchargement, émis = envoi)."""
        with self._lock:
            last, self._last_if = self._last_if, (rx, tx)
            if last is None:
                return
            drx = rx - last[0] if rx >= last[0] else rx          # compteur remis à zéro (redémarrage de l'interface)
            dtx = tx - last[1] if tx >= last[1] else tx
            if drx or dtx:
                self._credit(now, drx, dtx)

    def sample_devices(self, up, down, who, now):
        """up/down : {ip: octets cumulés} lus dans le pare-feu ; who : {ip: (mac, nom)}."""
        day = self.day(now)
        with self._lock:
            for direction, current in (("up", up), ("down", down)):
                last = self._last_dev[direction]
                for ip, total in current.items():
                    prev = last.get(ip)
                    delta = 0 if prev is None else (total - prev if total >= prev else total)
                    last[ip] = total
                    if delta and day:
                        mac, name = who.get(ip, (ip, ""))
                        rec = self.data["devices"].setdefault(mac, {"name": name, "ip": ip, "days": {}})
                        rec["ip"] = ip
                        rec["name"] = name or rec.get("name", "")
                        rec["days"].setdefault(day, {"down": 0, "up": 0})[direction] += delta
                        self._dirty = True
                for ip in [i for i in last if i not in current]:
                    del last[ip]

    # ---- lecture
    def summary(self, cfg_usage, now):
        day = self.day(now)
        with self._lock:
            if day is None:
                return {"clock": False, "pending": dict(self.data["pending"]), "today": None, "period": None,
                        "plan_bytes": 0, "percent": None, "devices": []}
            today = datetime.date.fromisoformat(day)
            start = period_start(today, cfg_usage["reset_day"]).isoformat()
            rx = tx = 0
            for d, v in self.data["days"].items():
                if d >= start:
                    rx, tx = rx + v["rx"], tx + v["tx"]
            t = self.data["days"].get(day, {"rx": 0, "tx": 0})
            devices = []
            for mac, rec in self.data["devices"].items():
                down = sum(v["down"] for d, v in rec["days"].items() if d >= start)
                up = sum(v["up"] for d, v in rec["days"].items() if d >= start)
                if down or up:
                    devices.append({"mac": mac, "name": rec.get("name", ""), "ip": rec.get("ip", ""), "down": down, "up": up})
            devices.sort(key=lambda r: r["down"] + r["up"], reverse=True)
            plan = int(cfg_usage["plan_gb"]) * 1_000_000_000
            total = rx + tx
            return {"clock": True, "today": {"rx": t["rx"], "tx": t["tx"]}, "period": {"rx": rx, "tx": tx, "start": start},
                    "plan_bytes": plan, "percent": round(100 * total / plan, 1) if plan else None,
                    "devices": devices[:TOP_DEVICES]}
