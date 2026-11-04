"""Anti-latence (SQM) : CAKE sur l'uplink, en envoi directement et en réception via une interface IFB.

Sans cela, une connexion 5G partagée devient très lente (latence qui explose) dès que quelqu'un télécharge.
Les débits indiqués doivent être un peu inférieurs à ce que la 5G fournit vraiment, sinon la file d'attente se forme
chez l'opérateur et CAKE ne peut rien contrôler (l'interface propose 85 % du dernier test de débit).
"""
from . import system


def ifb_name(iface):
    return ("ifb4" + iface)[:15]


def plan(iface, down_mbit, up_mbit):
    """Commandes à exécuter (dans l'ordre) : (commande, obligatoire)."""
    ifb = ifb_name(iface)
    return [
        (["tc", "qdisc", "replace", "dev", iface, "root", "cake", "bandwidth", f"{up_mbit}mbit",
          "besteffort", "dual-srchost", "nat"], True),
        (["ip", "link", "add", ifb, "type", "ifb"], False),                       # existe déjà : sans importance
        (["ip", "link", "set", ifb, "up"], True),
        (["tc", "qdisc", "del", "dev", iface, "ingress"], False),
        (["tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress"], True),
        (["tc", "filter", "add", "dev", iface, "parent", "ffff:", "protocol", "all", "matchall",
          "action", "mirred", "egress", "redirect", "dev", ifb], True),
        (["tc", "qdisc", "replace", "dev", ifb, "root", "cake", "bandwidth", f"{down_mbit}mbit",
          "besteffort", "dual-dsthost", "nat", "ingress"], True),
    ]


def apply(iface, down_mbit, up_mbit):
    """Retourne (ok, message d'erreur)."""
    for cmd, required in plan(iface, int(down_mbit), int(up_mbit)):
        p = system.run(cmd)
        if p.returncode != 0 and required:
            return False, (p.stderr or p.stdout).strip()[:200] or f"{cmd[0]} a échoué"
    return True, ""


def clear(iface):
    ifb = ifb_name(iface)
    system.run(["tc", "qdisc", "del", "dev", iface, "root"])
    system.run(["tc", "qdisc", "del", "dev", iface, "ingress"])
    system.run(["ip", "link", "del", ifb])


def active(iface):
    p = system.run(["tc", "qdisc", "show", "dev", iface])
    return p.returncode == 0 and "cake" in p.stdout
