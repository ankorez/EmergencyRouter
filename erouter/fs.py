"""Racine du système en lecture seule, avec de brèves fenêtres d'écriture.

Principe : une fois le démarrage terminé, l'agent remonte `/` en lecture seule. Seul l'agent (root) peut la
repasser en écriture, quelques secondes, pour enregistrer la config ou un Wi-Fi, puis la reverrouille.
On peut donc couper le courant à tout moment (sauf pendant ces quelques secondes) sans abîmer la carte SD.

Sortie de secours : ajouter `erouter.unlock` à la fin de cmdline.txt (partition de boot, lisible depuis un Mac).

Utilisation en ligne de commande :  sudo erouter-fs status | lock | unlock
"""
import contextlib
import logging
import os
import sys
import threading
import time

from . import system

log = logging.getLogger("erouter.fs")

FLAG = os.environ.get("EROUTER_LOCK_FLAG", "/var/lib/erouter/lock-enabled")
_lock = threading.RLock()
_depth = 0
_need_relock = False
_last_try = 0.0


def root_ro():
    """True si `/` est actuellement monté en lecture seule."""
    opts = None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4 and p[1] == "/":
                    opts = p[3].split(",")   # on garde la dernière entrée de `/`
    except OSError:
        return False
    return opts is not None and "ro" in opts


def rescue():
    try:
        with open("/proc/cmdline") as f:
            return "erouter.unlock" in f.read().split()
    except OSError:
        return False


def enabled():
    return os.path.exists(FLAG)


def status():
    return {"enabled": enabled(), "locked": root_ro(), "rescue": rescue()}


def _remount(mode):
    return system.run(["mount", "-o", f"remount,{mode}", "/"]).returncode == 0


def _try_lock(retries=5):
    """Repasse `/` en lecture seule (peut échouer tant qu'un fichier est ouvert en écriture)."""
    system.run(["sync"])
    for i in range(retries):
        if _remount("ro"):
            return True
        time.sleep(1)
    return False


@contextlib.contextmanager
def writable(linger=0):
    """Fenêtre d'écriture : `/` est en écriture dans le bloc, puis reverrouillée (sans effet si non verrouillée)."""
    global _depth, _need_relock
    with _lock:
        if root_ro():
            if not _remount("rw"):
                raise RuntimeError("Impossible de passer la carte en écriture")
            _need_relock = True
        _depth += 1
    try:
        yield
    finally:
        if linger:
            time.sleep(linger)   # laisse NetworkManager finir d'écrire son profil
        with _lock:
            _depth -= 1
            if _depth == 0 and _need_relock:
                _need_relock = False
                if not _try_lock():
                    log.warning("Impossible de reverrouiller la carte pour l'instant, nouvel essai plus tard")


def maintain():
    """Appelée régulièrement par l'agent : verrouille la racine une fois le démarrage terminé."""
    global _last_try
    if not enabled() or rescue() or root_ro():
        return
    with _lock:
        if _depth:
            return
        if time.time() - _last_try < 30:
            return
        _last_try = time.time()
        state = system.run(["systemctl", "is-system-running"]).stdout.strip()
        if state not in ("running", "degraded"):   # démarrage pas fini : on attend
            return
        if _try_lock(2):
            log.info("Carte SD verrouillée (lecture seule)")
        else:
            log.warning("Verrouillage impossible pour l'instant (fichier ouvert en écriture)")


def set_enabled(on):
    """Active/désactive le verrouillage de façon durable."""
    with _lock:
        if root_ro() and not _remount("rw"):
            raise RuntimeError("Impossible de passer la carte en écriture")
        if on:
            os.makedirs(os.path.dirname(FLAG), exist_ok=True)
            with open(FLAG, "w") as f:
                f.flush()
                os.fsync(f.fileno())
        else:
            try:
                os.remove(FLAG)
            except FileNotFoundError:
                pass
        system.run(["sync"])
        if on and not rescue():
            _try_lock(3)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "lock":
        set_enabled(True)
    elif cmd == "unlock":
        set_enabled(False)
    elif cmd != "status":
        print("usage : erouter-fs status|lock|unlock", file=sys.stderr)
        return 2
    s = status()
    print(f"verrouillage activé : {s['enabled']} | racine en lecture seule : {s['locked']}"
          + (" | mode secours (erouter.unlock)" if s["rescue"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
