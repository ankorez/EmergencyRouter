"""Racine du système en lecture seule, avec une partition de données séparée.

Principe : tout ce qui doit survivre (config, Wi-Fi enregistrés, certificat) vit dans /data, une petite partition
séparée (« erdata »). Une fois le démarrage terminé, l'agent remonte `/` en lecture seule : le système n'est plus
jamais écrit. Une coupure de courant ne peut abîmer que /data, qui est vérifiée, réparée ou recréée à chaque
démarrage (scripts/erouter-data) sans jamais empêcher le démarrage.

Le verrouillage n'existe que si /data est une vraie partition séparée (image EmergencyRouter).
Sortie de secours : ajouter `erouter.unlock` à la fin de cmdline.txt (partition de boot, lisible depuis un Mac).

Ligne de commande :  sudo erouter-fs status | lock | unlock
"""
import contextlib
import glob
import logging
import os
import stat
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
RETRY_MIN, RETRY_MAX = 30, 600      # s entre deux tentatives de verrouillage : double à chaque échec
_retry = RETRY_MIN
_last_blockers_log = 0.0


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


@contextlib.contextmanager
def writable():
    """Repasse brièvement le système en écriture (ex. changer un mot de passe), puis le reverrouille.
    Sans effet si le système n'est pas verrouillé. Le verrou interne empêche tout reverrouillage pendant la fenêtre."""
    with _lock:
        was_ro = root_ro()
        if was_ro and not _remount("rw"):
            raise RuntimeError("Impossible de passer le système en écriture")
        try:
            yield
        finally:
            system.run(["sync"])
            if was_ro and not _try_lock():
                log.warning("Reverrouillage impossible pour l'instant : nouvel essai automatique")


def _loop_backings():
    """Fichiers servant de support à un périphérique « loop » (ex. le fichier d'écriture différée du swap)."""
    out = []
    for path in glob.glob("/sys/block/loop*/loop/backing_file"):
        try:
            with open(path) as f:
                out.append(f.read().strip())
        except OSError:
            pass
    return out


def blockers(root_dev=None, limit=8):
    """Ce qui empêche de repasser `/` en lecture seule : (processus, fichier) ouverts en écriture sur la racine,
    et fichiers de la racine utilisés comme support d'un périphérique loop. Doit s'exécuter en root pour tout voir."""
    try:
        root_dev = os.stat("/").st_dev if root_dev is None else root_dev
    except OSError:
        return []
    found = []
    for backing in _loop_backings():
        try:
            if os.stat(backing).st_dev == root_dev:
                found.append(("périphérique loop", backing))
        except OSError:
            pass
    for pid in (d for d in os.listdir("/proc") if d.isdigit()) if os.path.isdir("/proc/self/fd") else ():
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                st = os.stat(f"/proc/{pid}/fd/{fd}")
                if st.st_dev != root_dev or not stat.S_ISREG(st.st_mode):
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    flags = int(next(line for line in f if line.startswith("flags:")).split()[1], 8)
                if flags & (os.O_WRONLY | os.O_RDWR):
                    with open(f"/proc/{pid}/comm") as f:
                        found.append((f.read().strip(), os.readlink(f"/proc/{pid}/fd/{fd}")))
        except (OSError, StopIteration, ValueError):
            continue            # le processus a disparu ou n'est pas lisible
    return list(dict.fromkeys(found))[:limit]


def describe_blockers(items):
    return "; ".join(f"{who} : {path}" for who, path in items) or "cause introuvable (processus non visibles ?)"


def maintain():
    """Appelée régulièrement par l'agent : verrouille la racine une fois le démarrage terminé."""
    global _last_try, _retry, _last_blockers_log
    if not enabled() or rescue() or root_ro():
        return
    with _lock:
        if time.time() - _last_try < _retry:
            return
        _last_try = time.time()
        state = system.run(["systemctl", "is-system-running"]).stdout.strip()
        if state not in ("running", "degraded"):   # démarrage pas fini : on attend
            return
        if _try_lock(2):
            log.info("Système verrouillé (lecture seule)")
            _retry = RETRY_MIN
        else:
            _retry = min(_retry * 2, RETRY_MAX)
            if time.time() - _last_blockers_log > 600:       # on dit QUI bloque, mais pas toutes les 30 secondes
                _last_blockers_log = time.time()
                log.warning("Verrouillage impossible, prochain essai dans %d s. Bloqué par : %s", _retry, describe_blockers(blockers()))


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
