"""Test de débit de l'uplink (à la demande). Consomme des données : environ 35 Mo avec les réglages par défaut."""
import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("erouter.speedtest")

# Serveur de test public. Seuls les tests peuvent le rediriger (variable d'environnement) ; jamais depuis l'interface.
BASE = os.environ.get("EROUTER_SPEEDTEST_URL", "https://speed.cloudflare.com")
DOWN_BYTES = 25_000_000
UP_BYTES = 8_000_000
COOLDOWN = 30            # s entre deux tests
HISTORY = 20


def parse_ping(text):
    """Sortie de `ping` -> (min, moyenne, max, gigue en ms, pertes en %), ou None."""
    loss = re.search(r"([0-9.]+)% packet loss", text)
    rtt = re.search(r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", text)
    if not loss:
        return None
    if not rtt:
        return (None, None, None, None, float(loss.group(1)))
    mn, avg, mx, jitter = (float(x) for x in rtt.groups())
    return (mn, avg, mx, jitter, float(loss.group(1)))


def parse_curl(text):
    """`-w "%{size} %{time_total} %{http_code}"` -> (octets, secondes, code HTTP), ou None."""
    parts = text.strip().split()
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), float(parts[1]), int(parts[2])
    except ValueError:
        return None


def mbit(size, seconds):
    return round(size * 8 / seconds / 1e6, 1) if seconds > 0 else None


class Speedtest:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"running": False, "stage": "", "started": 0, "finished": 0, "result": None, "history": []}

    def status(self):
        with self._lock:
            return {**self._state, "history": list(self._state["history"])}

    def start(self, iface, now=None):
        """Lance un test en arrière-plan. Lève ValueError s'il est refusé (déjà en cours, trop rapproché)."""
        now = time.time() if now is None else now
        with self._lock:
            if self._state["running"]:
                raise ValueError("Un test est déjà en cours")
            wait = COOLDOWN - (now - self._state["finished"])
            if wait > 0:
                raise ValueError(f"Attends encore {int(wait) + 1} s avant un nouveau test")
            self._state.update(running=True, stage="latence", started=now, result=None)
        threading.Thread(target=self._run, args=(iface,), daemon=True, name="speedtest").start()

    def _set_stage(self, stage):
        with self._lock:
            self._state["stage"] = stage

    def _run(self, iface):
        result = {"at": time.time(), "ping_ms": None, "jitter_ms": None, "loss_pct": None,
                  "down_mbit": None, "up_mbit": None, "bytes_used": 0, "error": ""}
        try:
            p = subprocess.run(["ping", "-c", "8", "-i", "0.3", "-W", "2", "-I", iface, "1.1.1.1"],
                               capture_output=True, text=True, timeout=30)
            ping = parse_ping(p.stdout)
            if ping:
                result["ping_ms"], result["jitter_ms"], result["loss_pct"] = ping[1], ping[3], ping[4]
            self._set_stage("téléchargement")
            down = self._curl(iface, "%{size_download}", ["-o", "/dev/null", f"{BASE}/__down?bytes={DOWN_BYTES}"], None)
            if down:
                result["down_mbit"] = mbit(down[0], down[1])
                result["bytes_used"] += down[0]
            self._set_stage("envoi")
            up = self._curl(iface, "%{size_upload}", ["-o", "/dev/null", "-X", "POST", "--data-binary", "@-", "-H",
                                                      "Content-Type: application/octet-stream", f"{BASE}/__up"], b"\0" * UP_BYTES)
            if up:
                result["up_mbit"] = mbit(up[0], up[1])
                result["bytes_used"] += up[0]
            if down is None and up is None:
                result["error"] = "Aucune réponse du serveur de test (pas d'Internet ?)"
        except Exception as e:   # un test raté ne doit jamais faire tomber l'agent
            log.exception("test de débit en échec")
            result["error"] = str(e)[:200]
        with self._lock:
            self._state.update(running=False, stage="", finished=time.time(), result=result)
            self._state["history"] = ([result] + self._state["history"])[:HISTORY]

    @staticmethod
    def _curl(iface, size_field, args, stdin):
        """Lance curl lié à l'interface ; retourne (octets, secondes, code HTTP) si la réponse est un succès."""
        cmd = ["curl", "-sS", "--interface", iface, "--connect-timeout", "8", "--max-time", "45",
               "-w", size_field + " %{time_total} %{http_code}"] + args
        p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=60)
        parsed = parse_curl(p.stdout.decode(errors="replace"))
        return parsed if parsed and parsed[2] == 200 and parsed[0] > 0 else None
