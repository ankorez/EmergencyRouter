"""Interface web (service sans privilèges). Tout ce qui touche au système passe par l'agent (rpc.py)."""
import copy
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, buildinfo, config, rpc

log = logging.getLogger("erouter.web")

STATIC = os.path.join(os.path.dirname(__file__), "static")
BUILD_INFO = os.environ.get("EROUTER_BUILD_INFO", "/usr/share/erouter/build-info.json")
PASSWORD_MIN, PASSWORD_MAX = 8, 128      # le maximum évite de faire calculer un hash sur une entrée géante
MAX_FAILURES, FAILURE_WINDOW = 5, 60     # 5 échecs par minute et par adresse
_BOOT_SECRET = secrets.token_hex(32)     # secours si l'agent n'a pas encore créé le secret persistant
_failures = {}                           # ip -> (échecs, instant du dernier échec)

# Accepte une IP, `erouter`, `erouter.local`, `localhost` : refuse tout autre nom (anti DNS-rebinding)
_HOST_OK = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:]+\]|[a-z0-9-]+(?:\.local)?)(?::\d{1,5})?$", re.I)

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

app = Flask(__name__, static_folder=None)
app.secret_key = _BOOT_SECRET            # remplacée par le secret persistant au démarrage (init_secret)
app.config.update(
    MAX_CONTENT_LENGTH=64 * 1024,
    SESSION_COOKIE_NAME="erouter",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def configure(secure):
    """Cookie de session : `__Host-` + Secure dès que l'interface est servie en HTTPS."""
    app.config.update(SESSION_COOKIE_SECURE=bool(secure),
                      SESSION_COOKIE_NAME="__Host-erouter" if secure else "erouter")


def init_secret(timeout=30):
    """Fixe la clé de session persistante. Le secret est créé par l'agent : on l'attend quelques secondes."""
    deadline = time.monotonic() + timeout
    secret = config.load()["web"]["secret"]
    while not secret and time.monotonic() < deadline:
        time.sleep(1)
        secret = config.load()["web"]["secret"]
    if not secret:
        log.warning("secret de session absent : clé temporaire (les sessions ne survivront pas à un redémarrage)")
    app.secret_key = secret or _BOOT_SECRET


def build_info():
    """Version du logiciel et de l'image : affichée seulement après connexion."""
    return buildinfo.load(BUILD_INFO)


def _cfg():
    if "cfg" not in g:
        g.cfg = config.load()
    return g.cfg


def _fingerprint(cfg):
    """Lie la session au mot de passe courant : changer le mot de passe invalide toutes les sessions."""
    return hashlib.sha256(cfg["web"]["password_hash"].encode()).hexdigest()[:16]


def err(msg, code=400):
    return jsonify(error=str(msg)), code


@app.before_request
def _guard():
    if not _HOST_OK.fullmatch(request.host):
        return err("Hôte non autorisé", 400)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return err("Origine refusée", 403)


@app.after_request
def _headers(resp):
    resp.headers.update({
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    })
    if request.path.startswith("/api/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(OSError)
def _agent_down(_):
    return err("Agent injoignable (service erouter-agent arrêté ?)", 503)


@app.errorhandler(Exception)
def _unhandled(e):
    if isinstance(e, HTTPException):
        return err(e.description, e.code)
    log.exception("erreur interne")
    return err("Erreur interne", 500)


def auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        cfg = _cfg()
        if not session.get("ok") or session.get("pv") != _fingerprint(cfg):
            return err("Non authentifié", 401)
        if request.method not in ("GET", "HEAD") and not request.is_json:
            return err("JSON requis", 415)
        return fn(*a, **kw)
    return wrapper


def _save(cfg):
    rpc.call("save_config", cfg=cfg)


def _login_session(cfg):
    session.clear()                      # nouvelle session à chaque connexion (pas de fixation)
    session.permanent = True
    session["ok"] = True
    session["pv"] = _fingerprint(cfg)


def _throttled(ip):
    now = time.monotonic()
    for k in [k for k, (_, t) in _failures.items() if now - t > FAILURE_WINDOW]:
        del _failures[k]                 # purge : la table ne grossit pas indéfiniment
    n, _ = _failures.get(ip, (0, 0))
    return n >= MAX_FAILURES


# ---------- pages ----------

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    if name not in ("app.js", "features.js", "app.css"):
        return err("Introuvable", 404)
    return send_from_directory(STATIC, name)


# ---------- authentification ----------

@app.get("/api/session")
def get_session():
    cfg = _cfg()
    ok = bool(session.get("ok")) and session.get("pv") == _fingerprint(cfg)
    return jsonify(setup_needed=not cfg["web"]["password_hash"], authenticated=ok)


@app.post("/api/login")
def login():
    cfg = _cfg()
    pw = (request.get_json(silent=True) or {}).get("password", "")
    ip = request.remote_addr or "?"
    if not isinstance(pw, str) or len(pw) > PASSWORD_MAX:
        return err("Mot de passe invalide")
    if _throttled(ip):
        return err("Trop d'essais, réessaie dans une minute", 429)
    if not cfg["web"]["password_hash"]:      # premier démarrage : on définit le mot de passe
        if len(pw) < PASSWORD_MIN:
            return err(f"{PASSWORD_MIN} caractères minimum")
        cfg["web"]["password_hash"] = generate_password_hash(pw)
        _save(cfg)
        log.info("mot de passe administrateur défini")
    elif not check_password_hash(cfg["web"]["password_hash"], pw):
        n, _ = _failures.get(ip, (0, 0))
        _failures[ip] = (n + 1, time.monotonic())
        log.warning("échec de connexion depuis %s", ip)
        return err("Mot de passe incorrect", 401)
    _failures.pop(ip, None)
    _login_session(cfg)
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
    cur, new = d.get("current", ""), d.get("new", "")
    if not isinstance(cur, str) or not isinstance(new, str) or len(cur) > PASSWORD_MAX:
        return err("Mot de passe invalide")
    if not check_password_hash(cfg["web"]["password_hash"], cur):
        return err("Mot de passe actuel incorrect", 401)
    if not PASSWORD_MIN <= len(new) <= PASSWORD_MAX:
        return err(f"Mot de passe : {PASSWORD_MIN} à {PASSWORD_MAX} caractères")
    cfg["web"]["password_hash"] = generate_password_hash(new)
    _save(cfg)
    _login_session(cfg)                  # toutes les autres sessions sont invalidées
    log.info("mot de passe administrateur modifié")
    return jsonify(ok=True)


# ---------- état et actions ----------

@app.get("/api/status")
@auth
def status():
    cfg = _cfg()
    s = rpc.call("status")
    s["lan"] = {**s.get("lan", {}), "address": cfg["lan"]["address"], "clients": s.pop("clients")}
    s["build"] = build_info()
    return jsonify(s)


@app.get("/api/wifi/scan")
@auth
def scan():
    try:
        return jsonify(rpc.call("scan", rescan=request.args.get("rescan") == "1"))
    except RuntimeError as e:
        return err(e, 500)


@app.post("/api/wifi/connect")
@auth
def wifi_connect():
    d = request.get_json()
    try:
        rpc.call("connect", ssid=d.get("ssid") or "", password=d.get("password") or None,
                 hidden=bool(d.get("hidden")))
    except RuntimeError as e:
        return err(e)
    return jsonify(ok=True)


@app.post("/api/wifi/forget")
@auth
def wifi_forget():
    try:
        rpc.call("forget", ssid=request.get_json().get("ssid") or "")
    except RuntimeError as e:
        return err(e)
    return jsonify(ok=True)


SETTINGS = {                     # section -> clés que l'interface a le droit de modifier
    "lan": ("address", "dhcp_start", "dhcp_end", "lease", "dns"),
    "usage": ("plan_gb", "alert_percent", "reset_day"),
    "savings": ("block_ads", "limits"),
    "sqm": ("enabled", "down_mbit", "up_mbit"),
    "setup": ("done",),
}


@app.get("/api/settings")
@auth
def get_settings():
    cfg = _cfg()
    return jsonify({section: {k: cfg[section][k] for k in keys} for section, keys in SETTINGS.items()})


@app.post("/api/settings")
@auth
def set_settings():
    d = request.get_json()
    cfg = _cfg()
    for section, keys in SETTINGS.items():
        if isinstance(d.get(section), dict):
            for k in keys:
                if k in d[section]:
                    cfg[section][k] = d[section][k]
    try:
        _save(cfg)                       # l'agent nettoie et valide : c'est lui qui fait foi
    except RuntimeError as e:
        return err(e)
    return jsonify(ok=True)


@app.post("/api/ssh")
@auth
def set_ssh():
    d = request.get_json()
    password = d.get("password") or None
    if password is not None and not isinstance(password, str):
        return err("Mot de passe invalide")
    try:
        return jsonify(rpc.call("set_ssh", enabled=bool(d.get("enabled")), password=password))
    except RuntimeError as e:
        return err(e)


@app.get("/api/usage")
@auth
def get_usage():
    return jsonify(rpc.call("usage"))


@app.post("/api/usage/reset")
@auth
def reset_usage():
    rpc.call("usage_reset")
    return jsonify(ok=True)


@app.get("/api/history")
@auth
def get_history():
    return jsonify(points=rpc.call("history"))


@app.get("/api/speedtest")
@auth
def speedtest_status():
    return jsonify(rpc.call("speedtest_status"))


@app.post("/api/speedtest")
@auth
def speedtest_start():
    try:
        return jsonify(rpc.call("speedtest_start"))
    except RuntimeError as e:
        return err(e)


def _download(body, mimetype, filename):
    resp = Response(body, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.get("/api/diagnostic")
@auth
def diagnostic():
    return _download(rpc.call("diagnostic"), "text/plain; charset=utf-8", time.strftime("erouter-diagnostic-%Y%m%d-%H%M.txt"))


@app.get("/api/backup")
@auth
def backup():
    cfg = copy.deepcopy(_cfg())
    cfg["web"]["secret"] = ""            # le secret de session ne se sauvegarde pas
    data = {"erouter_backup": 1, "version": __version__, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "config": config.sanitize(cfg)}
    return _download(json.dumps(data, indent=2, ensure_ascii=False), "application/json", time.strftime("erouter-config-%Y%m%d.json"))


@app.post("/api/restore")
@auth
def restore():
    d = request.get_json()
    if not isinstance(d, dict) or d.get("erouter_backup") != 1 or not isinstance(d.get("config"), dict):
        return err("Ce fichier n'est pas une sauvegarde EmergencyRouter")
    current = _cfg()
    try:
        new = config.sanitize(d["config"])
        new["web"]["secret"] = current["web"]["secret"]
        new["ssh"] = copy.deepcopy(current["ssh"])   # le mot de passe SSH vit dans le système : il n'est pas restauré
        _save(new)                                   # l'agent revalide tout
    except (ValueError, RuntimeError) as e:
        return err(e)
    return jsonify(ok=True, relogin=new["web"]["password_hash"] != current["web"]["password_hash"])


@app.get("/api/clients")
@auth
def clients():
    return jsonify(clients=rpc.call("clients"))


@app.post("/api/reboot")
@auth
def reboot():
    rpc.call("reboot")
    return jsonify(ok=True)
