"""Faux serveur pour tester l'interface dans un vrai navigateur : sert les vrais fichiers statiques et des données fictives.
Enregistre les requêtes POST (consultables sur /__log) pour que les tests vérifient ce que l'interface envoie."""
import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "erouter", "static")
LOG = []
STATE = {"speed_polls": 0, "speed_started": False, "connected": None}
LOCK = threading.Lock()


def history(now=1_800_000_000):
    pts = []
    for i in range(288):
        t = now - (288 - i) * 300
        pts.append({"t": t, "signal": round(70 + 10 * math.sin(i / 20), 1), "ms": round(30 + 15 * math.sin(i / 7) + (40 if i % 97 == 0 else 0), 1),
                    "loss": 5 if i % 97 == 0 else 0, "down": round(20 + 10 * math.sin(i / 11), 1), "up": round(5 + 2 * math.sin(i / 13), 1)})
    return pts


def api(scenario):
    wizard = scenario.startswith("wizard")
    return {
        "/api/session": {"setup_needed": False, "authenticated": True},
        "/api/status": {
            "uplink": {"connected": True, "ssid": "Partage-5G", "channel": 6, "freq": 2437, "signal": 78, "ip": "172.20.10.2/28", "internet": True},
            "ap": {"enabled": True, "channel": 6, "active": True, "ssid": "MaBox"}, "lan": {"address": "192.168.1.1/24", "clients": 2},
            "fs": {"available": True, "enabled": True, "locked": True, "rescue": False, "data": "ok"},
            "tls": {"enabled": True, "ready": True, "fingerprint": "AA:BB:CC"},
            "ssh": {"enabled": False, "active": False, "user": "pi", "password_set": False},
            "build": {"version": "0.1.0", "commit": "abc1234", "built": "2026-11-04", "os": "Debian 13"},
            "uptime": 7500, "warnings": ["Forfait : 82 % de 50 Go consommés depuis le 01/11."]},
        "/api/wifi/scan": {"saved": ["Partage-5G"], "networks": [
            {"in_use": True, "ssid": "Partage-5G", "signal": 78, "secured": True, "freq": 2437},
            {"in_use": False, "ssid": "Routeur-4G", "signal": 55, "secured": True, "freq": 2462}]},
        "/api/settings": {
            "ap": {"enabled": True, "ssid": "MaBox", "password": "mot-de-passe-wifi", "country": "FR", "fallback_channel": 6},
            "lan": {"address": "192.168.1.1/24", "dhcp_start": "192.168.1.100", "dhcp_end": "192.168.1.250", "lease": "12h", "dns": ["1.1.1.1"]},
            "usage": {"plan_gb": 50, "alert_percent": 80, "reset_day": 1},
            "savings": {"block_ads": False, "limits": [{"mac": "02:00:00:00:00:01", "down_mbit": 5, "up_mbit": 0}]},
            "sqm": {"enabled": False, "down_mbit": 0, "up_mbit": 0}, "setup": {"done": not wizard}},
        "/api/clients": {"clients": [
            {"hostname": "portable", "ip": "192.168.1.101", "mac": "02:00:00:00:00:01", "wifi": True},
            {"hostname": "telephone", "ip": "192.168.1.102", "mac": "02:00:00:00:00:02", "wifi": True}]},
        "/api/usage": {"clock": True, "today": {"rx": 1_200_000_000, "tx": 300_000_000}, "period": {"rx": 31_000_000_000, "tx": 10_000_000_000, "start": "2026-11-01"},
                       "plan_bytes": 50_000_000_000, "percent": 82.0, "devices": [
                           {"mac": "02:00:00:00:00:01", "name": "portable", "ip": "192.168.1.101", "down": 30_000_000_000, "up": 8_000_000_000},
                           {"mac": "02:00:00:00:00:02", "name": "", "ip": "192.168.1.102", "down": 1_000_000_000, "up": 2_000_000_000}]},
        "/api/history": {"points": history()},
    }


RESULT = {"at": 1_800_000_000, "ping_ms": 31.2, "jitter_ms": 4.1, "loss_pct": 0.0, "down_mbit": 84.3, "up_mbit": 21.7, "bytes_used": 33_000_000, "error": ""}


class Handler(BaseHTTPRequestHandler):
    scenario = ""

    def _send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                html = f.read().decode()
            here = os.path.dirname(__file__)
            with open(os.path.join(here, "probe.js")) as f:
                probe = f.read()
            scenario = ""
            if self.scenario:
                with open(os.path.join(here, "scenarios", self.scenario + ".js")) as f:
                    scenario = f.read() + "\nscenario().catch(e => report({fatal: String((e && e.stack) || e)}));"
            html = html.replace("<head>", "<head><script>" + probe + "</script>", 1).replace("</body>", f"<script>{scenario}</script></body>")
            return self._send(html, "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name in ("app.js", "features.js", "app.css"):
                with open(os.path.join(STATIC, name), "rb") as f:
                    return self._send(f.read(), "text/javascript" if name.endswith(".js") else "text/css")
        if path == "/__log":
            with LOCK:
                return self._send(LOG)
        if path == "/api/speedtest":
            with LOCK:
                if STATE["speed_started"]:
                    STATE["speed_polls"] += 1
                running = STATE["speed_started"] and STATE["speed_polls"] < 2
                done = STATE["speed_started"] and not running
            return self._send({"running": running, "stage": "téléchargement", "result": RESULT if (done or not STATE["speed_started"]) else None,
                               "history": [RESULT, {**RESULT, "at": RESULT["at"] - 3600}] if (done or not STATE["speed_started"]) else []})
        if path == "/api/qr.svg":
            return self._send('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>', "image/svg+xml")
        if path in ("/api/diagnostic", "/api/backup"):
            return self._send("{}", "application/octet-stream")
        data = api(self.scenario).get(path)
        if path == "/api/status" and STATE["connected"]:               # la connexion demandée est (enfin) établie
            data = json.loads(json.dumps(data))
            data["uplink"]["ssid"] = STATE["connected"]
        return self._send(data) if data is not None else self._send({"error": "inconnu"}, code=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        path = self.path.split("?")[0]
        with LOCK:
            LOG.append({"path": path, "body": body})
            if path == "/api/speedtest":
                STATE["speed_started"], STATE["speed_polls"] = True, 0
            if path == "/api/wifi/connect":
                STATE["connected"] = body["ssid"]
        if path == "/api/wifi/connect" and self.scenario == "wizard-cut":
            self.close_connection = True                                # le Wi-Fi du routeur se coupe : aucune réponse n'arrive
            self.connection.close()
            return
        if path == "/api/speedtest":
            return self._send({"running": True, "stage": "latence", "result": None, "history": []})
        return self._send({"ok": True, "enabled": False, "relogin": False})

    def log_message(self, *a):
        pass


def serve(port, scenario):
    handler = type("H", (Handler,), {"scenario": scenario})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
