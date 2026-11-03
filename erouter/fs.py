"""Racine du système en lecture seule, avec une partition de données séparée.

Principe : tout ce qui doit survivre (config, Wi-Fi enregistrés, certificat) vit dans /data, une petite partition
séparée (« erdata »). Une fois le démarrage terminé, l'agent remonte `/` en lecture seule : le système n'est plus
jamais écrit. Une coupure de courant ne peut abîmer que /data, qui est vérifiée, réparée ou recréée à chaque
démarrage (scripts/erouter-data) sans jamais empêcher le démarrage.

Le verrouillage n'existe que si /data est une vraie partition séparée (image EmergencyRouter).
Sortie de secours : ajouter `erouter.unlock` à la fin de cmdline.txt (partition de boot, lisible depuis un Mac).

Ligne de commande :  sudo erouter-fs status | lock | unlock
"""
import logging
import os
import sys
import threading
import time

from . import system

log = logging.getLogger("erouter.fs")

DATA = os.environ.get("EROUTER_DATA", "/data")
DISABLED = os.path.join(DATA, "erouter", "lock-disabled")     # présent => verrouillage désactivé par l'utilisateur
DATA_STATE = os.environ.get("EROUTER_DATA_STATE", "/run/erouter-data.state")
_lock = threading.RLock()
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


def has_data_partition():
    return os.path.ismount(DATA)


def data_state():
    """ok | repaired | reset | none : résultat de la vérification de /data au démarrage."""
    try:
        with open(DATA_STATE) as f:
            return f.read().strip() or "none"
    except OSError:
        return "none"


def enabled():
    return has_data_partition() and not os.path.exists(DISABLED)


def status():
    return {"available": has_data_partition(), "enabled": enabled(), "locked": root_ro(),
            "rescue": rescue(), "data": data_state()}


def _remount(mode):
    return system.run(["mount", "-o", f"remount,{mode}", "/"]).returncode == 0


def _try_lock(retries=5):
    """Repasse `/` en lecture seule (peut échouer tant qu'un fichier est ouvert en écriture)."""
    system.run(["sync"])
    for _ in range(retries):
        if _remount("ro"):
            return True
        time.sleep(1)
    return False


def maintain():
    """Appelée régulièrement par l'agent : verrouille la racine une fois le démarrage terminé."""
    global _last_try
    if not enabled() or rescue() or root_ro():
        return
    with _lock:
        if time.time() - _last_try < 30:
            return
        _last_try = time.time()
        state = system.run(["systemctl", "is-system-running"]).stdout.strip()
        if state not in ("running", "degraded"):   # démarrage pas fini : on attend
            return
        if _try_lock(2):
            log.info("Système verrouillé (lecture seule)")
        else:
            log.warning("Verrouillage impossible pour l'instant (fichier ouvert en écriture)")


def set_enabled(on):
    """Active/désactive le verrouillage de façon durable (le choix est mémorisé dans /data)."""
    if on and not has_data_partition():
        raise RuntimeError("Protection indisponible : il faut la partition de données (image EmergencyRouter)")
    with _lock:
        if on:
            try:
                os.remove(DISABLED)
            except FileNotFoundError:
                pass
            if not rescue():
                _try_lock(3)
        else:
            os.makedirs(os.path.dirname(DISABLED), exist_ok=True)
            with open(DISABLED, "w") as f:
                f.flush()
                os.fsync(f.fileno())
            if root_ro() and not _remount("rw"):
                raise RuntimeError("Impossible de passer le système en écriture")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "lock":
            set_enabled(True)
        elif cmd == "unlock":
            set_enabled(False)
        elif cmd != "status":
            print("usage : erouter-fs status|lock|unlock", file=sys.stderr)
            return 2
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    s = status()
    print(f"partition de données : {s['available']} (état : {s['data']}) | verrouillage activé : {s['enabled']}"
          f" | racine en lecture seule : {s['locked']}" + (" | mode secours (erouter.unlock)" if s["rescue"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
