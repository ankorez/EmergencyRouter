"""Petit RPC local (socket Unix, JSON ligne par ligne) entre le web (sans privilèges) et l'agent (root).

Le web ne peut demander que les méthodes explicitement exposées par l'agent ; chacune valide ses paramètres.
"""
import grp
import json
import os
import socket
import socketserver
import threading

SOCK = "/run/erouter/agent.sock"
MAX_LINE = 65536


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline(MAX_LINE))
            fn = self.server.methods.get(req.get("method"))
            if fn is None:
                raise ValueError("méthode inconnue")
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("paramètres invalides")
            out = {"result": fn(**params)}
        except Exception as e:  # l'erreur est renvoyée au web, jamais fatale pour l'agent
            out = {"error": str(e)}
        self.wfile.write((json.dumps(out) + "\n").encode())


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(methods, sock=None, group="erouter"):
    path = sock or SOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = _Server(path, _Handler)
    srv.methods = methods
    try:
        os.chown(path, 0, grp.getgrnam(group).gr_gid)
        os.chmod(path, 0o660)
    except KeyError:   # groupe absent : on reste en 0755 (root seul peut se connecter)
        os.chmod(path, 0o755)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(method, timeout=70, sock=None, **params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock or SOCK)
        s.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    res = json.loads(data)
    if "error" in res:
        raise RuntimeError(res["error"])
    return res["result"]
