"""Chien de garde systemd : l'agent signale régulièrement qu'il fonctionne ; s'il se fige, systemd le relance."""
import os
import socket


def notify(message):
    """Envoie un message au gestionnaire (READY=1, WATCHDOG=1, STATUS=...). Sans effet hors de systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    addr = "\0" + path[1:] if path.startswith("@") else path     # « @ » = socket abstrait
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode(), addr)
        return True
    except OSError:
        return False
