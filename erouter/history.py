"""Historique de la qualité de la connexion (signal, latence, débit) : en mémoire, sur 24 h."""
import threading
from collections import deque

SAMPLE_EVERY = 30        # s entre deux points
MAX_SAMPLES = 24 * 3600 // SAMPLE_EVERY


class History:
    def __init__(self, maxlen=MAX_SAMPLES):
        self._lock = threading.Lock()
        self._samples = deque(maxlen=maxlen)

    def record(self, t, signal, ms, down_bps, up_bps):
        """ms = None quand le ping n'a pas répondu ; débits en octets par seconde."""
        with self._lock:
            self._samples.append((t, signal, ms, down_bps, up_bps))

    def series(self, now, hours=24, bucket=300):
        """Points moyennés par tranche de `bucket` secondes sur les `hours` dernières heures."""
        start = now - hours * 3600
        with self._lock:
            rows = [r for r in self._samples if r[0] >= start]
        buckets = {}
        for t, signal, ms, down, up in rows:
            b = buckets.setdefault(int(t // bucket) * bucket, {"signal": [], "ms": [], "n": 0, "lost": 0, "down": [], "up": []})
            b["n"] += 1
            if signal is not None:
                b["signal"].append(signal)
            if ms is None:
                b["lost"] += 1
            else:
                b["ms"].append(ms)
            if down is not None:
                b["down"].append(down)
            if up is not None:
                b["up"].append(up)

        def avg(values, scale=1.0):
            return round(sum(values) / len(values) * scale, 1) if values else None

        return [{"t": t, "signal": avg(b["signal"]), "ms": avg(b["ms"]), "loss": round(100 * b["lost"] / b["n"]),
                 "down": avg(b["down"], 8 / 1e6), "up": avg(b["up"], 8 / 1e6)}      # débits en Mbit/s
                for t, b in sorted(buckets.items())]
