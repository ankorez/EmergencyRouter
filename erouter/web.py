import functools
import os
import subprocess
import time

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, agent, config, lan, system

STATIC = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=None)
_failures = {}  # ip -> (nb, dernier échec) : frein simple contre le brute-force


def _cfg():
    return config.load()


@app.before_request
def _secret():
    app.secret_key = _cfg()["web"]["secret"]
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")


def auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return jsonify(error="Non authentifié"), 401
        if request.method != "GET" and not request.is_json:
            return jsonify(error="JSON requis"), 400
        return fn(*a, **kw)
    return wrapper


def err(msg, code=400):
    return jsonify(error=str(msg)), code


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/session")
def get_session():
    return jsonify(setup_needed=not _cfg()["web"]["password_hash"], authenticated=bool(session.get("ok")),
                   version=__version__)


@app.post("/api/login")
def login():
    cfg = _cfg()
    data = request.get_json(silent=True) or {}
    pw = data.get("password", "")
    ip = request.remote_addr
    n, last = _failures.get(ip, (0, 0))
    if n >= 5 and time.time() - last < 60:
        return err("Trop d'essais, réessaie dans une minute", 429)
    if not cfg["web"]["password_hash"]:      # premier démarrage : on définit le mot de passe
        if len(pw) < 8:
            return err("8 caractères minimum")
        cfg["web"]["password_hash"] = generate_password_hash(pw)
        config.save(cfg)
    elif not check_password_hash(cfg["web"]["password_hash"], pw):
        _failures[ip] = (n + 1, time.time())
        return err("Mot de passe incorrect", 401)
    _failures.pop(ip, None)
    session["ok"] = True
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/password")
@auth
def change_password():
    cfg = _cfg()
    d = request.get_json()
    if not check_password_hash(cfg["web"]["password_hash"], d.get("current", "")):
        return err("Mot de passe actuel incorrect", 401)
    if len(d.get("new", "")) < 8:
        return err("8 caractères minimum")
    cfg["web"]["password_hash"] = generate_password_hash(d["new"])
    config.save(cfg)
    return jsonify(ok=True)


@app.get("/api/status")
@auth
def status():
    cfg = _cfg()
    s = agent.snapshot()
    s["lan"] = {"address": cfg["lan"]["address"], "clients": len(system.clients(lan.LEASES))}
    s["ap"]["ssid"] = cfg["ap"]["ssid"]
    s["uptime"] = system.uptime()
    return jsonify(s)


@app.get("/api/wifi/scan")
@auth
def scan():
    rescan = request.args.get("rescan") == "1"
    iface = _cfg()["uplink"]["interface"]
    try:
        return jsonify(networks=system.scan(iface, rescan), saved=system.saved_networks())
    except Exception as e:
        return err(e, 500)


@app.post("/api/wifi/connect")
@auth
def wifi_connect():
    d = request.get_json()
    ssid = (d.get("ssid") or "").strip()
    if not ssid:
        return err("SSID manquant")
    try:
        system.connect(_cfg()["uplink"]["interface"], ssid, d.get("password") or None, bool(d.get("hidden")))
    except Exception as e:
        return err(e)
    agent.wake.set()
    return jsonify(ok=True)


@app.post("/api/wifi/forget")
@auth
def wifi_forget():
    try:
        system.forget((request.get_json().get("ssid") or ""))
    except Exception as e:
        return err(e)
    return jsonify(ok=True)


@app.get("/api/settings")
@auth
def get_settings():
    cfg = _cfg()
    return jsonify(ap={k: cfg["ap"][k] for k in ("enabled", "ssid", "password", "country", "fallback_channel")},
                   lan=cfg["lan"])


@app.post("/api/settings")
@auth
def set_settings():
    d = request.get_json()
    cfg = _cfg()
    try:
        if "ap" in d:
            for k in ("enabled", "ssid", "password", "country", "fallback_channel"):
                if k in d["ap"]:
                    cfg["ap"][k] = d["ap"][k]
        if "lan" in d:
            for k in ("address", "dhcp_start", "dhcp_end", "lease", "dns"):
                if k in d["lan"]:
                    cfg["lan"][k] = d["lan"][k]
        lan.validate(cfg)
    except (ValueError, KeyError, TypeError) as e:
        return err(e)
    config.save(cfg)
    agent.request_apply()
    return jsonify(ok=True)


@app.get("/api/clients")
@auth
def clients():
    return jsonify(clients=system.clients(lan.LEASES))


@app.post("/api/reboot")
@auth
def reboot():
    subprocess.Popen(["systemctl", "reboot"])
    return jsonify(ok=True)
