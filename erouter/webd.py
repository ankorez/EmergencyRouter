"""Interface web, sans privilèges (utilisateur `erouter`). Parle à l'agent via le socket local.

HTTPS (gunicorn) avec le certificat auto-signé créé par l'agent ; le port HTTP ne fait que rediriger vers HTTPS.
Si le certificat est introuvable, l'interface reste accessible en HTTP (mieux vaut une interface en clair qu'aucune
interface quand on dépanne un réseau).
"""
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import gunicorn.http.wsgi as gunicorn_wsgi
from gunicorn.app.base import BaseApplication

from . import config, tls
from .web import _HOST_OK, app, configure, init_secret

log = logging.getLogger("erouter.webd")
_PORT_RE = re.compile(r":\d+$")


class _Redirect(BaseHTTPRequestHandler):
    """Répond uniquement par une redirection 308 vers la même adresse en HTTPS."""
    server_version = "erouter"
    sys_version = ""
    timeout = 10
    https_port = 443

    def _go(self):
        host = self.headers.get("Host", "")
        host = _PORT_RE.sub("", host) if _HOST_OK.fullmatch(host) else ""
        if not host or not self.path.startswith("/"):
            self.send_error(400)
            return
        port = "" if self.https_port == 443 else f":{self.https_port}"
        self.send_response(308)
        self.send_header("Location", f"https://{host}{port}{quote(self.path, safe='/?=&%:@!$()*+,;~-._')}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = _go

    def log_message(self, *a):
        pass


def _serve_redirect(http_port, https_port):
    handler = type("Redirect", (_Redirect,), {"https_port": https_port})
    srv = ThreadingHTTPServer(("0.0.0.0", http_port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="redirect-http").start()
    return srv


class _Gunicorn(BaseApplication):
    def __init__(self, flask_app, options):
        self.flask_app, self.options = flask_app, options     # ne pas appeler « wsgi » : nom réservé par gunicorn
        super().__init__()

    def load_config(self):
        for k, v in self.options.items():
            self.cfg.set(k, v)

    def load(self):
        return self.flask_app


def _wait_for_cert(timeout=30):
    deadline = time.monotonic() + timeout
    while not tls.ready() and time.monotonic() < deadline:
        time.sleep(1)
    return tls.ready()


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    web = config.load()["web"]
    init_secret()
    use_tls = bool(web["tls"]) and _wait_for_cert()
    if web["tls"] and not use_tls:
        log.warning("certificat HTTPS introuvable : interface servie en HTTP")
    configure(use_tls)
    gunicorn_wsgi.SERVER = "erouter"      # ne pas annoncer le logiciel serveur dans l'en-tête « Server »

    options = {
        "workers": 1,                 # un seul processus : la limitation des essais de connexion est en mémoire
        "threads": 8,
        "worker_class": "gthread",
        "timeout": 30,
        "graceful_timeout": 10,
        "keepalive": 5,
        "limit_request_line": 4094,
        "limit_request_fields": 50,
        "limit_request_field_size": 4094,
        "errorlog": "-",
        "loglevel": "warning",
        "proc_name": "erouter-web",
    }
    if use_tls:
        crt, key, _ = tls.paths()
        options.update(bind=f"0.0.0.0:{int(web['port'])}", certfile=crt, keyfile=key)
        if int(web["http_port"]):
            _serve_redirect(int(web["http_port"]), int(web["port"]))
    else:
        options.update(bind=f"0.0.0.0:{int(web['http_port']) or int(web['port'])}")
    _Gunicorn(app, options).run()


if __name__ == "__main__":
    main()
