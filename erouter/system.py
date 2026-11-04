"""Accès au système : nmcli, iw, ip, ping. Aucune commande n'est passée par un shell."""
import json
import os
import re
import subprocess


def run(cmd, timeout=20, check=False, input_text=None):
    """Exécute une commande (liste d'arguments, jamais de shell) avec un délai maximal."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except subprocess.TimeoutExpired:
        p = subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]} : délai dépassé")
    except FileNotFoundError:
        p = subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} : commande introuvable")
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f"{cmd[0]} a échoué")
    return p


def iface_exists(name):
    return bool(name) and os.path.exists(f"/sys/class/net/{name}")


def freq_to_channel(freq):
    if freq == 2484:
        return 14
    if freq < 2484:
        return (freq - 2407) // 5
    if 5000 <= freq < 5900:
        return (freq - 5000) // 5
    return 0


def wifi_link(iface):
    """État de l'association Wi-Fi client (via iw)."""
    out = {"connected": False, "ssid": "", "freq": 0, "channel": 0, "signal": None}
    if not iface_exists(iface):
        return out
    p = run(["iw", "dev", iface, "link"])
    if p.returncode != 0 or "Not connected" in p.stdout:
        return out
    out["connected"] = True
    m = re.search(r"SSID: (.*)", p.stdout)
    out["ssid"] = m.group(1).strip() if m else ""
    m = re.search(r"freq: (\d+)", p.stdout)
    if m:
        out["freq"] = int(m.group(1))
        out["channel"] = freq_to_channel(out["freq"])
    m = re.search(r"signal: (-?\d+)", p.stdout)
    if m:
        dbm = int(m.group(1))
        out["signal"] = max(0, min(100, 2 * (dbm + 100)))  # approx. en %
    return out


def power_save(iface):
    """True/False selon l'économie d'énergie Wi-Fi de l'interface (« Power save: on/off »), None si inconnu."""
    p = run(["iw", "dev", iface, "get", "power_save"])
    m = re.search(r"Power save:\s*(on|off)", p.stdout)
    return None if p.returncode != 0 or not m else m.group(1) == "on"


def set_power_save(iface, on):
    return run(["iw", "dev", iface, "set", "power_save", "on" if on else "off"]).returncode == 0


def iface_ip(iface):
    p = run(["ip", "-j", "-4", "addr", "show", "dev", iface])
    try:
        for entry in json.loads(p.stdout or "[]"):
            for a in entry.get("addr_info", []):
                return f"{a['local']}/{a['prefixlen']}"
    except (ValueError, KeyError):
        pass
    return ""


def ping_ms(iface, host="1.1.1.1"):
    """Latence (ms) d'un ping via l'interface, ou None s'il n'y a pas de réponse."""
    p = run(["ping", "-c", "1", "-W", "2", "-I", iface, host], timeout=5)
    if p.returncode != 0:
        return None
    m = re.search(r"time[=<]([0-9.]+)\s*ms", p.stdout)
    return float(m.group(1)) if m else 0.0


def ping(iface, host="1.1.1.1"):
    return ping_ms(iface, host) is not None


def iface_bytes(iface):
    """(octets reçus, octets émis) cumulés de l'interface, ou (None, None)."""
    try:
        base = f"/sys/class/net/{iface}/statistics/"
        with open(base + "rx_bytes") as f:
            rx = int(f.read())
        with open(base + "tx_bytes") as f:
            tx = int(f.read())
        return rx, tx
    except (OSError, ValueError):
        return None, None


def _split_nmcli(line):
    """Découpe une ligne `nmcli -t` en gérant les ':' échappés."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def scan(iface, rescan=False):
    cmd = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list",
           "ifname", iface, "--rescan", "yes" if rescan else "no"]
    p = run(cmd, timeout=40)
    seen = {}
    for line in p.stdout.splitlines():
        f = _split_nmcli(line)
        if len(f) < 5 or not f[1]:
            continue
        net = {
            "in_use": f[0].strip() == "*",
            "ssid": f[1],
            "signal": int(f[2] or 0),
            "secured": f[3].strip() not in ("", "--"),
            "freq": int(re.sub(r"\D", "", f[4]) or 0),
        }
        old = seen.get(net["ssid"])
        if not old or net["signal"] > old["signal"]:
            seen[net["ssid"]] = net
    return sorted(seen.values(), key=lambda n: (not n["in_use"], -n["signal"]))


def saved_networks():
    p = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    return [_split_nmcli(l)[0] for l in p.stdout.splitlines()
            if len(_split_nmcli(l)) > 1 and _split_nmcli(l)[1] == "802-11-wireless"]


def connect(iface, ssid, password=None, hidden=False):
    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        cmd += ["password", password]
    if hidden:
        cmd += ["hidden", "yes"]
    existed = ssid in saved_networks()
    p = run(cmd, timeout=60)
    if p.returncode != 0:
        if not existed:   # on ne supprime que le profil qu'on vient de créer (mauvais mot de passe)
            run(["nmcli", "connection", "delete", "id", ssid])
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or "Connexion impossible")


def forget(ssid):
    run(["nmcli", "connection", "delete", "id", ssid], check=True)


def reconnect(iface):
    run(["nmcli", "device", "disconnect", iface])
    run(["nmcli", "device", "connect", iface], timeout=40)


def carrier(iface):
    """True si un câble est branché et actif sur l'interface, False sinon, None si l'interface n'existe pas."""
    try:
        with open(f"/sys/class/net/{iface}/carrier") as f:
            return f.read().strip() == "1"
    except OSError:
        return None if not iface_exists(iface) else False     # interface éteinte : « carrier » illisible


def clients(leases_file):
    """Appareils du réseau local (baux DHCP) : mac, ip, nom."""
    out = []
    try:
        with open(leases_file) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    exp, mac, ip, host = parts[:4]
                    out.append({"mac": mac.lower(), "ip": ip, "hostname": "" if host == "*" else host})
    except FileNotFoundError:
        pass
    return sorted(out, key=lambda c: tuple(int(x) for x in c["ip"].split(".")))


def uptime():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except OSError:
        return 0
