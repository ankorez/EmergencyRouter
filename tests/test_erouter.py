"""Tests d'EmergencyRouter. Lancer :  python3 -m unittest discover -s tests -v

Aucun accès au système réel : l'agent est simulé (sauf la validation/enregistrement de config, qui est réel).
"""
import contextlib
import copy
import datetime
import gzip
import http.client
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="erouter-test-")
os.environ["EROUTER_DATA"] = os.path.join(_TMP, "data")
os.environ["EROUTER_CONFIG"] = os.path.join(_TMP, "data", "erouter", "config.yaml")
os.environ["EROUTER_DATA_STATE"] = os.path.join(_TMP, "data.state")
os.environ["EROUTER_TLS_DIR"] = os.path.join(_TMP, "tls")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from erouter import agent, agentd, config, diagnostic, fs, history, lan, rpc, speedtest, sqm, system, tls, usage, wd, web, webd  # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "erouter", "templates")


def _bad(mutate):
    cfg = copy.deepcopy(config.DEFAULTS)
    mutate(cfg)
    return cfg


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        config.validate(copy.deepcopy(config.DEFAULTS))

    def test_rejects_injection_and_garbage(self):
        cases = {
            "bail": lambda c: c["lan"].__setitem__("lease", "12h\ndhcp-script=/bin/sh"),
            "dns": lambda c: c["lan"].__setitem__("dns", ["1.1.1.1\nconf-file=/x"]),
            "interface": lambda c: c["lan"].__setitem__("interface", "eth0; reboot"),
            "interface option": lambda c: c["uplink"].__setitem__("interface", "-h"),
            "adresse réseau": lambda c: c["lan"].__setitem__("address", "10.0.0.0/24"),
            "dhcp hors réseau": lambda c: c["lan"].__setitem__("dhcp_start", "10.9.9.9"),
            "port SSH": lambda c: c["web"].__setitem__("port", 22),
            "ports identiques": lambda c: c["web"].__setitem__("http_port", 443),
            "port de redirection réservé": lambda c: c["web"].__setitem__("http_port", 53),
            "tls non booléen": lambda c: c["web"].__setitem__("tls", "oui"),
            "forfait négatif": lambda c: c["usage"].__setitem__("plan_gb", -1),
            "forfait en texte": lambda c: c["usage"].__setitem__("plan_gb", "50"),
            "forfait booléen": lambda c: c["usage"].__setitem__("plan_gb", True),
            "seuil à 0": lambda c: c["usage"].__setitem__("alert_percent", 0),
            "jour 29": lambda c: c["usage"].__setitem__("reset_day", 29),
            "blocage non booléen": lambda c: c["savings"].__setitem__("block_ads", "oui"),
            "limites : trop nombreuses": lambda c: c["savings"].__setitem__("limits", [{"mac": f"aa:bb:cc:dd:ee:{i:02x}", "down_mbit": 1, "up_mbit": 1} for i in range(17)]),
            "limite : MAC en majuscules": lambda c: c["savings"].__setitem__("limits", [{"mac": "AA:BB:CC:DD:EE:01", "down_mbit": 1, "up_mbit": 1}]),
            "limite : MAC en double": lambda c: c["savings"].__setitem__("limits", [{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 1, "up_mbit": 1}] * 2),
            "limite : aucun débit": lambda c: c["savings"].__setitem__("limits", [{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 0, "up_mbit": 0}]),
            "limite : clé en trop": lambda c: c["savings"].__setitem__("limits", [{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 1, "up_mbit": 1, "x": 1}]),
            "anti-latence sans débits": lambda c: c["sqm"].update(enabled=True, down_mbit=0, up_mbit=20),
            "assistant non booléen": lambda c: c["setup"].__setitem__("done", 1),
            "utilisateur ssh avec option": lambda c: c["ssh"].__setitem__("user", "-oProxyCommand=x"),
            "ssh activé non booléen": lambda c: c["ssh"].__setitem__("enabled", "oui"),
            "section absente": lambda c: c.pop("lan"),
        }
        for name, mutate in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                config.validate(_bad(mutate))

    def test_wizard_is_not_reopened_on_an_already_configured_install(self):
        path = config.CONFIG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:                      # config d'une version sans assistant : mot de passe admin déjà défini
            f.write("web: {password_hash: 'scrypt:abc'}\nap: {ssid: Ouifi}\n")
        self.assertTrue(config.load()["setup"]["done"])
        with open(path, "w") as f:                      # première installation : pas de mot de passe admin
            f.write("ap: {ssid: Ouifi}\n")
        self.assertFalse(config.load()["setup"]["done"])
        with open(path, "w") as f:                      # l'utilisateur a explicitement rouvert l'assistant
            f.write("web: {password_hash: 'scrypt:abc'}\nsetup: {done: false}\n")
        self.assertFalse(config.load()["setup"]["done"])
        os.remove(path)

    def test_valid_extras_are_accepted(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["usage"].update(plan_gb=50, alert_percent=80, reset_day=15)
        cfg["savings"].update(block_ads=True, limits=[{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 5, "up_mbit": 0}])
        cfg["sqm"].update(enabled=True, down_mbit=80, up_mbit=20)
        config.validate(cfg)

    def test_sanitize_drops_unknown_keys(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["evil"] = {"x": 1}
        cfg["lan"]["extra"] = "y"
        clean = config.sanitize(cfg)
        self.assertNotIn("evil", clean)
        self.assertNotIn("extra", clean["lan"])
        with self.assertRaises(ValueError):
            config.sanitize("pas un dict")

    def test_agent_never_writes_and_keeps_last_good(self):
        path = config.CONFIG_PATH
        if os.path.exists(path):
            os.remove(path)
        good = config.load_for_agent()
        self.assertFalse(os.path.exists(path), "l'agent ne doit pas créer le fichier")
        config.save(config.sanitize(good))
        with open(path, "a") as f:
            f.write("\nlan: {lease: 'x\\ny'}\n")
        with self.assertLogs("erouter.config", "ERROR"):
            kept = config.load_for_agent()
        self.assertEqual(kept["lan"]["lease"], "12h")

    def test_file_mode_is_private(self):
        config.save(config.sanitize(config.DEFAULTS))
        self.assertEqual(os.stat(config.CONFIG_PATH).st_mode & 0o007, 0, "ni lisible ni modifiable par 'autres'")


class TemplateTests(unittest.TestCase):
    env = Environment(loader=FileSystemLoader(TEMPLATES))

    @staticmethod
    def _chain(text, name):
        start = text.index(f"chain {name} {{")
        return text[start:text.index("\n    }\n", start)]

    def render_fw(self, **kw):
        args = dict(lan_if="eth0", uplink="wlan0", web_ports=[443, 80], lan_net="192.168.1.0/24", limits=[])
        args.update(kw)
        return self.env.get_template("nftables.conf.j2").render(**args)

    def test_nftables_opens_nothing_from_the_uplink(self):
        out = self.render_fw()
        self.assertIn("policy drop", out)
        self.assertIn("ip saddr != 192.168.1.0/24 drop", out)
        self.assertIn("{ 53, 443, 80 }", out)
        self.assertIn("5353", out)
        self.assertNotIn("wlan0", self._chain(out, "input"), "aucune règle d'entrée n'accepte quoi que ce soit depuis l'uplink")
        for line in self._chain(out, "forward").splitlines():
            if 'iifname "wlan0"' in line:
                self.assertIn("ct state established,related", line, "depuis l'uplink : uniquement les réponses à des connexions du LAN")

    def test_nftables_counts_per_device_only_what_is_allowed(self):
        out = self.render_fw()
        self.assertIn("set usage_up", out)
        self.assertIn("set usage_down", out)
        self.assertIn("add @usage_up { ip saddr counter }", out)
        fwd = self._chain(out, "forward")
        self.assertLess(fwd.index("ip saddr !="), fwd.index("@usage_up"), "une adresse usurpée n'est jamais comptée")
        self.assertLess(fwd.index("@usage_down"), fwd.index("ct state established,related accept"))
        self.assertIn("ct state established,related add @usage_down", fwd, "les connexions entrantes refusées ne sont pas comptées")

    def test_nftables_speed_limits_per_device(self):
        out = self.render_fw(limits=[{"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.101", "up_kbytes": 250, "down_kbytes": 625},
                                     {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.1.102", "up_kbytes": 0, "down_kbytes": 125}])
        fwd = self._chain(out, "forward")
        self.assertIn("ip saddr 192.168.1.101 limit rate over 250 kbytes/second burst 64 kbytes drop", fwd)
        self.assertIn("ip daddr 192.168.1.101 limit rate over 625 kbytes/second burst 64 kbytes drop", fwd)
        self.assertIn("ip daddr 192.168.1.102 limit rate over 125 kbytes/second", fwd)
        self.assertNotIn("ip saddr 192.168.1.102", fwd, "pas de plafond montant s'il n'est pas demandé")
        self.assertLess(fwd.index("limit rate over"), fwd.index("@usage_up"), "les paquets jetés par un plafond ne sont pas comptés")

    def test_dnsmasq_template(self):
        d = self.env.get_template("dnsmasq.conf.j2").render(lan_if="eth0", gateway="g", netmask="n", start="a", end="b",
                                                           lease="12h", dns=["1.1.1.1"], leases="/x", blocklist=None)
        self.assertIn("stop-dns-rebind", d)
        self.assertNotIn("addn-hosts", d)
        blocked = self.env.get_template("dnsmasq.conf.j2").render(lan_if="eth0", gateway="g", netmask="n", start="a", end="b",
                                                                  lease="12h", dns=["1.1.1.1"], leases="/x", blocklist="/run/erouter/blocklist.hosts")
        self.assertIn("addn-hosts=/run/erouter/blocklist.hosts", blocked)
        self.assertIn("pid-file=", d)


class SystemTests(unittest.TestCase):
    def test_channels(self):
        self.assertEqual(system.freq_to_channel(2437), 6)
        self.assertEqual(system.freq_to_channel(5180), 36)
        self.assertEqual(system.freq_to_channel(2484), 14)

    def test_split_nmcli_handles_escaped_colons(self):
        self.assertEqual(system._split_nmcli(r"*:Wi\:Fi:80:WPA2:2437 MHz"), ["*", "Wi:Fi", "80", "WPA2", "2437 MHz"])

    def test_run_survives_missing_command_and_timeout(self):
        self.assertEqual(system.run(["commande-qui-n-existe-pas"]).returncode, 127)
        self.assertEqual(system.run(["sleep", "5"], timeout=0.2).returncode, 124)

    def test_connect_keeps_existing_profile_on_failure(self):
        calls = []

        def fake_run(cmd, timeout=20, check=False):
            calls.append(cmd)
            rc = 1 if cmd[:3] == ["nmcli", "device", "wifi"] else 0
            out = "Profil\n"
            return mock.Mock(returncode=rc, stdout="Maison:802-11-wireless\n" if cmd[1] == "-t" else out, stderr="échec")

        with mock.patch.object(system, "run", fake_run), self.assertRaises(RuntimeError):
            system.connect("wlan0", "Maison", "motdepasse")
        self.assertFalse(any(c[:3] == ["nmcli", "connection", "delete"] for c in calls),
                         "un profil déjà enregistré ne doit jamais être supprimé")


class FsTests(unittest.TestCase):
    def setUp(self):
        self.ro = True
        self.remounts = []
        self.partition = True
        self.boot = "running"
        fs._last_try = 0.0
        if os.path.exists(fs.DISABLED):
            os.remove(fs.DISABLED)

        def remount(mode):
            self.remounts.append(mode)
            self.ro = mode == "ro"
            return True

        self.patches = [
            mock.patch.object(fs, "root_ro", lambda: self.ro),
            mock.patch.object(fs, "_remount", remount),
            mock.patch.object(fs, "rescue", lambda: False),
            mock.patch.object(fs, "has_data_partition", lambda: self.partition),
            mock.patch.object(fs.system, "run", lambda *a, **k: mock.Mock(returncode=0, stdout=self.boot + "\n")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_enabled_by_default_with_data_partition(self):
        self.assertTrue(fs.enabled())
        self.assertEqual(set(fs.status()), {"available", "enabled", "locked", "rescue", "data"})

    def test_unavailable_without_data_partition(self):
        self.partition = False
        self.assertFalse(fs.enabled())
        with self.assertRaises(RuntimeError):
            fs.set_enabled(True)
        self.ro = False
        fs.maintain()
        self.assertEqual(self.remounts, [], "sans partition de données, on ne verrouille jamais")

    def test_maintain_locks_once_booted(self):
        self.ro = False
        fs.maintain()
        self.assertTrue(self.ro)
        self.assertEqual(self.remounts, ["ro"])

    def test_maintain_waits_for_end_of_boot(self):
        self.ro, self.boot = False, "starting"
        fs.maintain()
        self.assertEqual(self.remounts, [])

    def test_disable_unlocks_and_persists(self):
        fs.set_enabled(False)
        self.assertFalse(self.ro)
        self.assertTrue(os.path.exists(fs.DISABLED))
        self.assertFalse(fs.enabled())
        fs._last_try = 0.0
        fs.maintain()
        self.assertFalse(self.ro, "désactivé : on ne reverrouille pas")

    def test_enable_locks_and_forgets_the_opt_out(self):
        fs.set_enabled(False)
        fs.set_enabled(True)
        self.assertTrue(self.ro)
        self.assertFalse(os.path.exists(fs.DISABLED))

    def test_rescue_never_locks(self):
        self.ro = False
        with mock.patch.object(fs, "rescue", lambda: True):
            fs.maintain()
        self.assertFalse(self.ro)

    def test_writable_window_unlocks_then_relocks(self):
        with fs.writable():
            self.assertFalse(self.ro)
        self.assertTrue(self.ro)
        self.assertEqual(self.remounts, ["rw", "ro"])

    def test_writable_window_is_a_noop_when_not_locked(self):
        self.ro = False
        with fs.writable():
            pass
        self.assertEqual(self.remounts, [])

    def test_window_relocks_even_if_the_block_fails(self):
        with self.assertRaises(ValueError), fs.writable():
            raise ValueError("boum")
        self.assertTrue(self.ro)


class TlsHelperTests(unittest.TestCase):
    def test_validity_is_long_even_with_a_wrong_clock(self):
        days, not_after = tls._validity(0.0)               # horloge à 1970 (pas de pile, pas encore de NTP)
        self.assertGreaterEqual(not_after, tls.CLOCK_FLOOR + 825 * 86400)
        self.assertGreater(days, 20000)
        days, _ = tls._validity(tls.CLOCK_FLOOR + 10 * 86400)   # horloge correcte
        self.assertEqual(days, 826)

    def test_renewal_rules(self):
        with mock.patch.object(tls, "ready", lambda: True), \
                mock.patch.object(tls, "_meta", lambda: {"ip": "192.168.1.1", "not_after": tls.CLOCK_FLOOR + 800 * 86400}):
            now = tls.CLOCK_FLOOR + 10 * 86400
            self.assertFalse(tls.needs_renewal("192.168.1.1", now))
            self.assertTrue(tls.needs_renewal("10.0.0.1", now), "nouvelle IP : nouveau certificat")
            self.assertTrue(tls.needs_renewal("192.168.1.1", tls.CLOCK_FLOOR + 760 * 86400), "proche de l'échéance")
            self.assertFalse(tls.needs_renewal("192.168.1.1", 100.0), "horloge fausse : pas de renouvellement")


def _openssl_ok():
    try:
        out = subprocess.run(["openssl", "req", "-help"], capture_output=True, text=True, timeout=10)
        return "-addext" in out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_openssl_ok(), "openssl avec -addext requis")
class TlsCertTests(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(tls.TLS_DIR, ignore_errors=True)

    def test_certificate_is_created_once_and_is_valid(self):
        self.assertTrue(tls.ensure("192.168.1.1/24"))
        self.assertTrue(tls.ready())
        fp = tls.fingerprint()
        self.assertRegex(fp, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertFalse(tls.ensure("192.168.1.1/24"), "pas de nouveau certificat si rien n'a changé")
        self.assertEqual(tls.fingerprint(), fp)
        text = subprocess.run(["openssl", "x509", "-in", tls.paths()[0], "-noout", "-text"],
                              capture_output=True, text=True).stdout
        for expected in ("DNS:erouter.local", "DNS:erouter", "IP Address:192.168.1.1", "CA:FALSE", "prime256v1"):
            self.assertIn(expected, text)
        self.assertEqual(os.stat(tls.paths()[1]).st_mode & 0o007, 0, "clé privée illisible pour 'autres'")

    def test_new_address_gives_new_certificate(self):
        tls.ensure("192.168.1.1/24")
        fp = tls.fingerprint()
        self.assertTrue(tls.ensure("10.0.0.1/24"))
        self.assertNotEqual(tls.fingerprint(), fp)


class RedirectTests(unittest.TestCase):
    def start(self, https_port=443):
        srv = webd._serve_redirect(0, https_port)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return srv.server_address[1]

    def get(self, port, host, path="/"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": host})
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status, r.getheader("Location")

    def test_redirects_to_https_on_the_same_host(self):
        port = self.start()
        self.assertEqual(self.get(port, "erouter.local", "/x?y=1"), (308, "https://erouter.local/x?y=1"))
        self.assertEqual(self.get(port, "192.168.1.1:80"), (308, "https://192.168.1.1/"))

    def test_non_default_https_port_is_kept(self):
        port = self.start(8443)
        self.assertEqual(self.get(port, "erouter"), (308, "https://erouter:8443/"))

    def test_foreign_host_is_refused_no_open_redirect(self):
        port = self.start()
        status, location = self.get(port, "evil.example.com")
        self.assertEqual(status, 400)
        self.assertIsNone(location)


class AgentTlsTests(unittest.TestCase):
    def setUp(self):
        agent._tls_failed_at = 0.0
        self.cfg = config.sanitize(config.DEFAULTS)

    def test_web_restarted_only_when_certificate_changed(self):
        with mock.patch.object(tls, "ensure", return_value=True), mock.patch.object(system, "run") as run:
            agent._maintain_tls(self.cfg)
        self.assertIn(["systemctl", "try-restart", "--no-block", "erouter-web"], [c.args[0] for c in run.call_args_list])
        with mock.patch.object(tls, "ensure", return_value=False), mock.patch.object(system, "run") as run:
            agent._maintain_tls(self.cfg)
        run.assert_not_called()

    def test_failure_is_contained_and_backed_off(self):
        boom = mock.Mock(side_effect=RuntimeError("openssl absent"))
        with mock.patch.object(tls, "ensure", boom), self.assertLogs("erouter.agent", "ERROR"):
            agent._maintain_tls(self.cfg)
            agent._maintain_tls(self.cfg)
        self.assertEqual(boom.call_count, 1)

    def test_disabled_tls_does_nothing(self):
        self.cfg["web"]["tls"] = False
        with mock.patch.object(tls, "ensure") as ensure:
            agent._maintain_tls(self.cfg)
        ensure.assert_not_called()


def _account(uid=1000, shell="/bin/bash"):
    return pwd.struct_passwd(("pi", "x", uid, uid, "", "/home/pi", shell))


class AgentSshTests(unittest.TestCase):
    """Le mot de passe SSH se change depuis l'interface : jamais en argument, jamais pour root, jamais faible."""

    def setUp(self):
        self.runs = []
        self.account = _account()
        self.chpasswd_rc = 0

        def fake_run(cmd, timeout=20, check=False, input_text=None):
            self.runs.append((cmd, input_text))
            return mock.Mock(returncode=self.chpasswd_rc if cmd[0] == "chpasswd" else 0, stdout="", stderr="")

        def getpwnam(name):
            if self.account is None:
                raise KeyError(name)
            return self.account

        for p in (mock.patch.object(agentd.pwd, "getpwnam", getpwnam), mock.patch.object(agentd.system, "run", fake_run),
                  mock.patch.object(agentd.fs, "writable", contextlib.nullcontext), mock.patch.object(agentd.agent, "request_apply")):
            p.start()
            self.addCleanup(p.stop)
        config.save(config.sanitize(config.DEFAULTS))

    GOOD = "Une-phrase-secrete 42 \"!"

    def test_password_goes_through_stdin_never_argv(self):
        r = agentd.m_set_ssh(True, self.GOOD)
        self.assertEqual(r, {"enabled": True, "password_set": True, "user": "pi"})
        self.assertIn((["chpasswd"], f"pi:{self.GOOD}\n"), self.runs)
        self.assertFalse(any(self.GOOD in " ".join(cmd) for cmd, _ in self.runs), "le mot de passe ne doit pas être dans la ligne de commande")
        self.assertTrue(config.load_for_agent()["ssh"]["enabled"])
        self.assertTrue(config.load_for_agent()["ssh"]["password_set"])

    def test_rejects_weak_or_malformed_passwords(self):
        for bad in ("court", "x" * 65, "avec\nretour-a-la-ligne", " espace-au-debut-xx", "motdepasse-12345",
                    "ma-phrase-pi-12345", "accentué-12345678", "ERouter-123456789"):
            with self.subTest(bad), self.assertRaises(ValueError):
                agentd.m_set_ssh(True, bad)
        self.assertFalse(any(cmd == ["chpasswd"] for cmd, _ in self.runs), "rien n'est modifié si le mot de passe est refusé")

    def test_cannot_enable_without_a_password(self):
        with self.assertRaises(ValueError):
            agentd.m_set_ssh(True)
        agentd.m_set_ssh(True, self.GOOD)
        agentd.m_set_ssh(False)
        self.assertFalse(config.load_for_agent()["ssh"]["enabled"])
        agentd.m_set_ssh(True)           # le mot de passe déjà défini est conservé
        self.assertTrue(config.load_for_agent()["ssh"]["enabled"])

    def test_refuses_root_and_system_accounts(self):
        for acct in (_account(uid=0), _account(uid=105, shell="/usr/sbin/nologin"), _account(uid=1001, shell="/bin/false")):
            self.account = acct
            with self.subTest(f"uid {acct.pw_uid} {acct.pw_shell}"), self.assertRaises(ValueError):
                agentd.m_set_ssh(True, self.GOOD)
        self.assertFalse(any(cmd == ["chpasswd"] for cmd, _ in self.runs))

    def test_unknown_user(self):
        self.account = None
        with self.assertRaisesRegex(ValueError, "n'existe pas"):
            agentd.m_set_ssh(True, self.GOOD)

    def test_chpasswd_failure_is_reported_and_not_recorded(self):
        self.chpasswd_rc = 1
        with self.assertRaises(RuntimeError):
            agentd.m_set_ssh(True, self.GOOD)
        self.assertFalse(config.load_for_agent()["ssh"]["password_set"])


class AgentSshSyncTests(unittest.TestCase):
    def setUp(self):
        self.active = False
        self.start_ok = True
        self.calls = []
        agent._ssh_failed_at = 0.0

        def fake_run(cmd, timeout=20, check=False, input_text=None):
            self.calls.append(cmd)
            if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
                return mock.Mock(returncode=0 if self.active else 3)
            if cmd[:2] == ["systemctl", "start"]:
                return mock.Mock(returncode=0 if self.start_ok else 1)
            return mock.Mock(returncode=0)

        p = mock.patch.object(agent.system, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = config.sanitize(config.DEFAULTS)

    def test_starts_ssh_after_stopping_the_socket(self):
        self.cfg["ssh"]["enabled"] = True
        self.assertTrue(agent._sync_ssh(self.cfg))
        self.assertLess(self.calls.index(["systemctl", "stop", "ssh.socket"]), self.calls.index(["systemctl", "start", "ssh"]))

    def test_stops_ssh_when_disabled(self):
        self.active = True
        self.assertFalse(agent._sync_ssh(self.cfg))
        self.assertIn(["systemctl", "stop", "ssh.socket", "ssh"], self.calls)

    def test_nothing_to_do_when_already_in_the_right_state(self):
        self.assertFalse(agent._sync_ssh(self.cfg))
        self.active, self.cfg["ssh"]["enabled"] = True, True
        self.assertTrue(agent._sync_ssh(self.cfg))
        self.assertFalse(any(c[:2] in (["systemctl", "start"], ["systemctl", "stop"]) for c in self.calls))

    def test_failed_start_is_not_retried_every_tick(self):
        self.cfg["ssh"]["enabled"], self.start_ok = True, False
        with self.assertLogs("erouter.agent", "ERROR"):
            self.assertFalse(agent._sync_ssh(self.cfg))
        self.calls.clear()
        agent._sync_ssh(self.cfg)
        self.assertNotIn(["systemctl", "start", "ssh"], self.calls)


NFT_SET_JSON = ('{"nftables": [{"metainfo": {"version": "1.1.3"}}, {"set": {"family": "inet", "name": "up", "table": "t", '
                '"type": "ipv4_addr", "handle": 2, "size": 64, "flags": ["dynamic"], "elem": [{"elem": {"val": "10.1.0.50", '
                '"counter": {"packets": 3, "bytes": 1584}}}, {"elem": {"val": "10.1.0.51", "counter": {"packets": 1, "bytes": 66}}}], '
                '"stmt": [{"counter": null}]}}]}')


class UsageTests(unittest.TestCase):
    T0 = tls.CLOCK_FLOOR + 40 * 86400            # une date « sûre » (horloge fiable)

    def setUp(self):
        self.path = os.path.join(_TMP, "usage-test.json")
        for f in (self.path, self.path + ".tmp"):
            if os.path.exists(f):
                os.remove(f)
        self.u = usage.Usage(self.path)
        self.cfg = {"plan_gb": 50, "alert_percent": 80, "reset_day": 1}

    def test_parses_the_real_nft_json(self):
        self.assertEqual(usage.parse_counters(NFT_SET_JSON), {"10.1.0.50": 1584, "10.1.0.51": 66})
        for junk in ("", "pas du json", "[]", '{"nftables": []}', '{"nftables": [{"set": {}}]}'):
            self.assertEqual(usage.parse_counters(junk), {})

    def test_counts_deltas_and_survives_counter_resets(self):
        self.u.sample_iface(1000, 500, self.T0)                # première lecture : base, rien n'est compté
        self.u.sample_iface(3000, 900, self.T0 + 30)           # +2000 reçus, +400 émis
        self.u.sample_iface(100, 50, self.T0 + 60)             # l'interface a redémarré : on compte depuis zéro
        s = self.u.summary(self.cfg, self.T0 + 60)
        self.assertEqual((s["today"]["rx"], s["today"]["tx"]), (2100, 450))

    def test_no_dated_bucket_while_clock_is_wrong_then_credited(self):
        self.u.sample_iface(0, 0, 100.0)
        self.u.sample_iface(5000, 1000, 130.0)                 # 1970 : l'heure n'est pas fiable
        self.assertEqual(self.u.data["days"], {})
        self.assertEqual(self.u.data["pending"], {"rx": 5000, "tx": 1000})
        self.u.sample_iface(5100, 1100, self.T0)               # NTP a corrigé l'heure
        s = self.u.summary(self.cfg, self.T0)
        self.assertEqual((s["today"]["rx"], s["today"]["tx"]), (5100, 1100), "le comptage sans date est versé au jour courant")
        self.assertEqual(self.u.data["pending"], {"rx": 0, "tx": 0})

    def test_billing_period_and_plan_percent(self):
        day = lambda n: time.strftime("%Y-%m-%d", time.localtime(self.T0 + n * 86400))   # noqa: E731
        self.u.data["days"] = {day(-40): {"rx": 9 * 10**9, "tx": 0}, day(0): {"rx": 20 * 10**9, "tx": 5 * 10**9}}
        s = self.u.summary({**self.cfg, "reset_day": 1}, self.T0)
        start = s["period"]["start"]
        self.assertTrue(start.endswith("-01"))
        self.assertEqual(s["period"]["rx"] + s["period"]["tx"], 25 * 10**9, "le mois précédent n'est pas compté")
        self.assertEqual(s["percent"], 50.0)
        self.assertEqual(usage.period_start(datetime.date(2026, 11, 3), 15), datetime.date(2026, 10, 15))
        self.assertEqual(usage.period_start(datetime.date(2026, 1, 3), 15), datetime.date(2025, 12, 15))
        self.assertEqual(usage.period_start(datetime.date(2026, 11, 20), 15), datetime.date(2026, 11, 15))

    def test_per_device_accounting_by_mac(self):
        who = {"10.1.0.50": ("aa:aa:aa:aa:aa:01", "portable"), "10.1.0.51": ("aa:aa:aa:aa:aa:02", "")}
        self.u.sample_devices({"10.1.0.50": 1000}, {"10.1.0.50": 5000}, who, self.T0)
        self.u.sample_devices({"10.1.0.50": 1600, "10.1.0.51": 40}, {"10.1.0.50": 9000, "10.1.0.51": 70}, who, self.T0 + 30)
        devices = self.u.summary(self.cfg, self.T0 + 30)["devices"]
        self.assertEqual(devices[0]["mac"], "aa:aa:aa:aa:aa:01")
        self.assertEqual((devices[0]["up"], devices[0]["down"], devices[0]["name"]), (600, 4000, "portable"))
        self.assertEqual(len(devices), 1, "un appareil vu pour la première fois n'a pas de delta")

    def test_persistence_roundtrip_and_atomic_file(self):
        self.u.sample_iface(0, 0, self.T0)
        self.u.sample_iface(10, 20, self.T0 + 30)
        self.u.maybe_save(force=True)
        self.assertEqual(os.stat(self.path).st_mode & 0o077, 0, "fichier privé")
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        again = usage.Usage(self.path)
        self.assertEqual(again.summary(self.cfg, self.T0 + 30)["today"], {"rx": 10, "tx": 20})

    def test_corrupt_or_foreign_file_does_not_crash(self):
        for content in ("", "{", "[1,2]", '{"days": 3}'):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertEqual(usage.Usage(self.path).data["days"], {})

    def test_reset_and_old_days_are_pruned(self):
        old = time.strftime("%Y-%m-%d", time.localtime(self.T0 - 200 * 86400))
        self.u.data["days"][old] = {"rx": 1, "tx": 1}
        self.u.sample_iface(0, 0, self.T0)
        self.u.sample_iface(5, 5, self.T0 + 1)
        self.assertNotIn(old, self.u.data["days"])
        self.u.reset()
        self.assertEqual(self.u.data["days"], {})


class HistoryAndWatchdogTests(unittest.TestCase):
    def test_series_buckets_losses_and_rates(self):
        h = history.History()
        now = 100000.0
        for i, ms in enumerate((20.0, 40.0, None, 30.0)):       # 4 points dans la même tranche de 5 minutes
            h.record(now - 100 + i * 10, 70 + i, ms, 1_250_000, 125_000)      # 10 Mbit/s descendant, 1 Mbit/s montant
        (point,) = h.series(now, hours=1, bucket=300)
        self.assertEqual(point["ms"], 30.0)
        self.assertEqual(point["loss"], 25)
        self.assertEqual(point["down"], 10.0)
        self.assertEqual(point["up"], 1.0)
        self.assertEqual(h.series(now + 7200, hours=1), [], "hors fenêtre")

    def test_history_is_bounded(self):
        h = history.History(maxlen=5)
        for i in range(50):
            h.record(float(i), 50, 10.0, 0, 0)
        self.assertEqual(sum(1 for _ in h._samples), 5)

    def test_sd_notify_writes_to_the_unix_socket(self):
        import socket
        path = os.path.join(_TMP, "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(path)
        srv.settimeout(2)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
            self.assertTrue(wd.notify("WATCHDOG=1"))
        self.assertEqual(srv.recv(64), b"WATCHDOG=1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            self.assertFalse(wd.notify("WATCHDOG=1"), "sans systemd : sans effet, sans erreur")
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": os.path.join(_TMP, "absent.sock")}):
            self.assertFalse(wd.notify("READY=1"))


class SpeedtestTests(unittest.TestCase):
    PING = ("PING 1.1.1.1 56(84) bytes of data.\n--- 1.1.1.1 ping statistics ---\n"
            "8 packets transmitted, 8 received, 0% packet loss, time 2103ms\n"
            "rtt min/avg/max/mdev = 20.115/25.310/40.002/6.201 ms\n")

    def test_parsers(self):
        self.assertEqual(speedtest.parse_ping(self.PING), (20.115, 25.31, 40.002, 6.201, 0.0))
        self.assertEqual(speedtest.parse_ping("4 packets transmitted, 0 received, 100% packet loss, time 3000ms"), (None, None, None, None, 100.0))
        self.assertIsNone(speedtest.parse_ping("n'importe quoi"))
        self.assertEqual(speedtest.parse_curl("25000000 2.5 200"), (25000000, 2.5, 200))
        for junk in ("", "a b c", "1 2", "1 2 3 4"):
            self.assertIsNone(speedtest.parse_curl(junk))
        self.assertEqual(speedtest.mbit(25_000_000, 2.0), 100.0)
        self.assertIsNone(speedtest.mbit(1, 0))

    def run_sync(self, curl_outputs):
        """Exécute un test complet de façon synchrone avec des sorties de ping et de curl simulées."""
        class SyncThread:
            def __init__(self, target, args=(), **kw):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        def fake_run(cmd, **kw):
            if cmd[0] == "ping":
                return mock.Mock(stdout=self.PING)
            return mock.Mock(stdout=curl_outputs["up" if "-X" in cmd else "down"].encode())

        st = speedtest.Speedtest()
        with mock.patch.object(speedtest.threading, "Thread", SyncThread), mock.patch.object(speedtest.subprocess, "run", fake_run):
            st.start("wlan0", now=1000.0)
        return st

    def test_full_run_gives_rates_latency_and_data_used(self):
        st = self.run_sync({"down": "25000000 2.5 200", "up": "8000000 1.0 200"})
        r = st.status()["result"]
        self.assertEqual((r["down_mbit"], r["up_mbit"], r["ping_ms"], r["jitter_ms"], r["loss_pct"]), (80.0, 64.0, 25.31, 6.201, 0.0))
        self.assertEqual(r["bytes_used"], 33_000_000)
        self.assertEqual(r["error"], "")
        self.assertFalse(st.status()["running"])
        self.assertEqual(len(st.status()["history"]), 1)

    def test_server_error_is_reported_not_raised(self):
        st = self.run_sync({"down": "0 0.3 503", "up": "0 0.3 503"})
        r = st.status()["result"]
        self.assertIsNone(r["down_mbit"])
        self.assertIn("Aucune réponse", r["error"])

    def test_refuses_concurrent_and_too_frequent_tests(self):
        st = speedtest.Speedtest()
        with mock.patch.object(speedtest.threading, "Thread"):
            st.start("wlan0", now=5000.0)
            with self.assertRaisesRegex(ValueError, "déjà en cours"):
                st.start("wlan0", now=5001.0)
        st._state.update(running=False, finished=5100.0)
        with self.assertRaisesRegex(ValueError, "Attends encore"):
            st.start("wlan0", now=5110.0)
        with mock.patch.object(speedtest.threading, "Thread"):
            st.start("wlan0", now=5131.0)

    def test_history_is_bounded(self):
        st = speedtest.Speedtest()

        def fake_run(cmd, **kw):
            return mock.Mock(stdout=self.PING if cmd[0] == "ping" else b"1000 1.0 200")

        with mock.patch.object(speedtest.subprocess, "run", fake_run):
            for _ in range(speedtest.HISTORY + 10):
                st._run("wlan0")
        self.assertEqual(len(st.status()["history"]), speedtest.HISTORY)


class SqmTests(unittest.TestCase):
    def test_plan_builds_egress_and_ingress_cake(self):
        cmds = [c for c, _ in sqm.plan("wlan0", 80, 20)]
        flat = [" ".join(c) for c in cmds]
        self.assertIn("tc qdisc replace dev wlan0 root cake bandwidth 20mbit besteffort dual-srchost nat", flat)
        self.assertIn("tc qdisc replace dev ifb4wlan0 root cake bandwidth 80mbit besteffort dual-dsthost nat ingress", flat)
        self.assertTrue(any("mirred egress redirect dev ifb4wlan0" in c for c in flat))
        self.assertLessEqual(len(sqm.ifb_name("wlan0")), 15)
        self.assertLessEqual(len(sqm.ifb_name("une-interface-tres-longue")), 15)

    def test_apply_reports_the_first_required_failure_only(self):
        def fake_run(cmd, **kw):
            fail = cmd[:3] == ["ip", "link", "add"] or cmd[:4] == ["tc", "qdisc", "del", "dev"]       # facultatives
            return mock.Mock(returncode=1 if fail else 0, stdout="", stderr="")
        with mock.patch.object(sqm.system, "run", fake_run):
            self.assertEqual(sqm.apply("wlan0", 80, 20), (True, ""))

        def no_cake(cmd, **kw):
            return mock.Mock(returncode=2 if "cake" in cmd else 0, stdout="", stderr="Unknown qdisc \"cake\"")
        with mock.patch.object(sqm.system, "run", no_cake):
            ok, msg = sqm.apply("wlan0", 80, 20)
        self.assertFalse(ok)
        self.assertIn("cake", msg)


class AgentSqmSyncTests(unittest.TestCase):
    def setUp(self):
        agent._sqm_applied, agent._sqm_failed_at, agent._sqm_error = None, 0.0, ""
        self.cfg = config.sanitize(config.DEFAULTS)
        self.active = False
        self.applied = []
        self.cleared = []
        for p in (mock.patch.object(agent.system, "iface_exists", lambda i: True),
                  mock.patch.object(agent.sqm, "active", lambda i: self.active),
                  mock.patch.object(agent.sqm, "apply", lambda i, d, u: (self.applied.append((d, u)) or (True, ""))),
                  mock.patch.object(agent.sqm, "clear", lambda i: self.cleared.append(i))):
            p.start()
            self.addCleanup(p.stop)

    def test_applies_once_then_only_when_the_kernel_forgets_it(self):
        self.cfg["sqm"].update(enabled=True, down_mbit=80, up_mbit=20)
        agent._sync_sqm(self.cfg, "wlan0")
        self.active = True
        agent._sync_sqm(self.cfg, "wlan0")
        self.assertEqual(self.applied, [(80, 20)])
        self.active = False                       # l'interface a été recréée : la file d'attente a disparu
        agent._sync_sqm(self.cfg, "wlan0")
        self.assertEqual(len(self.applied), 2)

    def test_removed_once_when_disabled(self):
        agent._sync_sqm(self.cfg, "wlan0")
        agent._sync_sqm(self.cfg, "wlan0")
        self.assertEqual(self.cleared, ["wlan0"])

    def test_failure_is_remembered_and_not_retried_every_tick(self):
        self.cfg["sqm"].update(enabled=True, down_mbit=80, up_mbit=20)
        with mock.patch.object(agent.sqm, "apply", return_value=(False, "Unknown qdisc cake")) as ap, self.assertLogs("erouter.agent", "ERROR"):
            agent._sync_sqm(self.cfg, "wlan0")
            agent._sync_sqm(self.cfg, "wlan0")
        self.assertEqual(ap.call_count, 1)
        self.assertEqual(agent._sqm_error, "Unknown qdisc cake")


class DiagnosticTests(unittest.TestCase):
    def test_config_dump_hides_every_secret(self):
        cfg = config.sanitize(config.DEFAULTS)
        cfg["web"]["password_hash"], cfg["web"]["secret"] = "scrypt:abc123hash", "s" * 64
        dumped = json.dumps(diagnostic.redact_config(cfg))
        for secret in ("scrypt:abc123hash", "s" * 64):
            self.assertNotIn(secret, dumped)
        self.assertEqual(cfg["web"]["secret"], "s" * 64, "l'original n'est pas modifié")

    def test_command_output_is_scrubbed(self):
        text = "wpa_passphrase=Mot-de-passe-wifi-42\nligne normale\nreseau Mot-de-passe-wifi-42 connecté\npsk=abcdef"
        out = diagnostic.redact_text(text, ["Mot-de-passe-wifi-42"])
        self.assertNotIn("Mot-de-passe-wifi-42", out)
        self.assertNotIn("abcdef", out)
        self.assertIn("ligne normale", out)

    def test_collect_contains_no_password_even_if_a_command_leaks_it(self):
        cfg = config.sanitize(config.DEFAULTS)
        cfg["web"]["secret"] = "Mot-de-passe-wifi-42"
        leaky = mock.Mock(returncode=0, stdout="ssid=MaBox pass=Mot-de-passe-wifi-42\n", stderr="")
        with mock.patch.object(diagnostic.system, "run", return_value=leaky):
            text = diagnostic.collect(cfg, {"uplink": {"ssid": "Partage"}}, {"version": "0.1.0"})
        self.assertNotIn("Mot-de-passe-wifi-42", text)
        self.assertIn("Version :", text)
        self.assertIn("secret: '***'", text)


class LanSavingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="erouter-lan-")
        self.cfg = config.sanitize(config.DEFAULTS)

    def test_limits_follow_the_current_dhcp_address_of_each_device(self):
        self.cfg["savings"]["limits"] = [{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 5, "up_mbit": 2},
                                         {"mac": "aa:bb:cc:dd:ee:09", "down_mbit": 1, "up_mbit": 1}]
        clients = [{"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.120", "hostname": "x"}]
        with mock.patch.object(lan.system, "clients", return_value=clients):
            rules = lan.limit_rules(self.cfg)
        self.assertEqual(rules, [{"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.120", "up_kbytes": 250, "down_kbytes": 625}],
                         "5 Mbit/s = 625 ko/s ; un appareil sans bail est ignoré")

    def test_blocklist_is_extracted_to_memory_only_when_enabled(self):
        gz = os.path.join(self.tmp, "list.hosts.gz")
        with gzip.open(gz, "wt") as f:
            f.write("0.0.0.0 ads.example.net\n0.0.0.0 tracker.example.org\n")
        run_copy = os.path.join(self.tmp, "run", "blocklist.hosts")
        with mock.patch.object(lan, "BLOCKLIST_GZ", gz), mock.patch.object(lan, "BLOCKLIST_RUN", run_copy), \
                mock.patch.object(lan, "RUN_DIR", os.path.dirname(run_copy)):
            self.assertIsNone(lan.prepare_blocklist(self.cfg), "désactivé par défaut")
            self.cfg["savings"]["block_ads"] = True
            self.assertEqual(lan.prepare_blocklist(self.cfg), run_copy)
            with open(run_copy) as f:
                self.assertIn("0.0.0.0 ads.example.net", f.read())
        with mock.patch.object(lan, "BLOCKLIST_GZ", os.path.join(self.tmp, "absente.gz")), \
                self.assertLogs("erouter.lan", "WARNING"):
            self.assertIsNone(lan.prepare_blocklist(self.cfg), "liste absente : on désactive au lieu de planter")

    def test_bundled_blocklist_is_sane(self):
        path = lan.BLOCKLIST_GZ
        self.assertTrue(os.path.isfile(path), "la liste de blocage est livrée avec le logiciel")
        with gzip.open(path, "rt") as f:
            lines = f.read().splitlines()
        self.assertGreater(len(lines), 50000)
        for line in lines[:2000] + lines[-2000:]:
            self.assertRegex(line, r"^0\.0\.0\.0 [a-z0-9.-]+\.[a-z]{2,}$")
        domains = {line.split()[1] for line in lines}
        for essential in ("localhost", "github.com", "debian.org", "apple.com", "google.com", "cloudflare.com"):
            self.assertNotIn(essential, domains, f"{essential} ne doit jamais être bloqué")


class AgentRecordTests(unittest.TestCase):
    def setUp(self):
        path = os.path.join(_TMP, "usage-agent.json")
        if os.path.exists(path):
            os.remove(path)
        agent._usage = usage.Usage(path)
        agent._last_sample, agent._prev_bytes = 0.0, None
        agent.HISTORY = history.History()
        self.counters = {"usage_up": {}, "usage_down": {}}
        self.bytes = (1000, 500)
        for p in (mock.patch.object(agent.system, "iface_bytes", lambda i: self.bytes),
                  mock.patch.object(agent.usage, "read_counters", lambda name: self.counters[name]),
                  mock.patch.object(agent.system, "clients", lambda f: [{"mac": "aa:aa:aa:aa:aa:01", "ip": "10.1.0.50", "hostname": "portable"}])):
            p.start()
            self.addCleanup(p.stop)

    def test_interface_and_per_device_bytes_are_recorded(self):
        link = {"connected": True, "signal": 70}
        with mock.patch.object(agent.time, "time", return_value=tls.CLOCK_FLOOR + 5 * 86400):
            agent._record(link, "wlan0", 25.0)
            self.counters["usage_up"], self.counters["usage_down"] = {"10.1.0.50": 100}, {"10.1.0.50": 900}
            self.bytes = (6000, 1500)
            agent._last_sample = 0.0
            agent._record(link, "wlan0", 30.0)
            summary = agent.get_usage().summary({"plan_gb": 0, "alert_percent": 80, "reset_day": 1}, tls.CLOCK_FLOOR + 5 * 86400)
        self.assertEqual((summary["today"]["rx"], summary["today"]["tx"]), (5000, 1000))
        self.assertEqual(len(agent.HISTORY.series(tls.CLOCK_FLOOR + 5 * 86400)), 1)

    def test_nothing_is_recorded_when_the_interface_has_no_counters(self):
        self.bytes = (None, None)
        agent._record({"connected": False, "signal": None}, "wlan0", None)    # ne doit pas lever d'exception
        self.assertEqual(agent.get_usage().data["days"], {})


class PowerSaveParsingTests(unittest.TestCase):
    def test_reads_the_driver_state(self):
        for out, rc, expected in (("Power save: on\n", 0, True), ("Power save: off\n", 0, False), ("", 1, None), ("n'importe quoi", 0, None)):
            with self.subTest(out), mock.patch.object(system, "run", return_value=mock.Mock(stdout=out, returncode=rc)):
                self.assertIs(system.power_save("wlan0"), expected)


class PowerSaveTests(unittest.TestCase):
    """L'économie d'énergie endort la radio et rend la liaison avec le téléphone instable."""

    def setUp(self):
        self.state = {"wlan0": True}
        self.sets = []
        agent._last_ps_check = 0.0
        for p in (mock.patch.object(agent.system, "iface_exists", lambda i: i in self.state),
                  mock.patch.object(agent.system, "power_save", lambda i: self.state[i]),
                  mock.patch.object(agent.system, "set_power_save", lambda i, on: (self.sets.append((i, on)) or self.state.__setitem__(i, on) or True))):
            p.start()
            self.addCleanup(p.stop)

    def test_turned_off_on_the_wifi_interface(self):
        agent._sync_power_save("wlan0")
        self.assertEqual(sorted(self.sets), [("wlan0", False)])

    def test_nothing_to_do_when_already_off_or_interface_missing(self):
        self.state = {"wlan0": False}
        agent._sync_power_save("wlan0")
        self.assertEqual(self.sets, [])

    def test_rechecked_but_not_more_than_every_30_seconds(self):
        agent._sync_power_save("wlan0")
        self.state["wlan0"] = True                  # NetworkManager l'a remise après une reconnexion
        agent._sync_power_save("wlan0")
        self.assertEqual(len(self.sets), 1, "trop tôt : pas de nouvelle vérification")
        agent._last_ps_check -= agent.POWER_SAVE_EVERY + 1
        agent._sync_power_save("wlan0")
        self.assertEqual(len(self.sets), 2)

    def test_a_refusal_is_logged_not_raised(self):
        with mock.patch.object(agent.system, "set_power_save", return_value=False), self.assertLogs("erouter.agent", "WARNING"):
            agent._sync_power_save("wlan0")

    def test_image_config_forbids_power_save_for_networkmanager(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "systemd", "91-erouter-wifi.conf")) as f:
            self.assertRegex(f.read(), r"(?m)^wifi\.powersave\s*=\s*2\s*$")


class FsBlockersTests(unittest.TestCase):
    LINUX = unittest.skipUnless(os.path.isdir("/proc/self/fd"), "Linux requis (/proc)")

    @LINUX
    def test_reports_a_file_open_for_writing_but_not_one_open_for_reading(self):
        with tempfile.NamedTemporaryFile("w", dir=_TMP) as w:
            self.assertIn(w.name, [path for _, path in fs.blockers(root_dev=os.stat(w.name).st_dev)])
        with tempfile.NamedTemporaryFile("w", dir=_TMP) as f:
            ro = open(f.name)
            self.addCleanup(ro.close)
            f.close()
            self.assertNotIn(f.name, [path for _, path in fs.blockers(root_dev=os.stat(_TMP).st_dev)])

    def test_reports_a_loop_device_backed_by_a_file_of_the_root_filesystem(self):
        with tempfile.NamedTemporaryFile(dir=_TMP) as f, mock.patch.object(fs, "_loop_backings", return_value=[f.name, "/n/existe/pas"]):
            found = fs.blockers(root_dev=os.stat(f.name).st_dev)
        self.assertEqual(found[0], ("périphérique loop", f.name))

    def test_description_is_readable_even_with_nothing_found(self):
        self.assertEqual(fs.describe_blockers([("loop", "/var/swap"), ("python3", "/var/x")]), "loop : /var/swap; python3 : /var/x")
        self.assertIn("introuvable", fs.describe_blockers([]))


class FsLockFailureTests(unittest.TestCase):
    """Quand le verrouillage échoue : délai croissant, et on dit QUI bloque, une seule fois (pas toutes les 30 s)."""

    def setUp(self):
        self.now = 10_000.0
        fs._last_try, fs._retry, fs._last_blockers_log = 0.0, fs.RETRY_MIN, 0.0
        self.attempts = []
        for p in (mock.patch.object(fs, "root_ro", lambda: False), mock.patch.object(fs, "enabled", lambda: True),
                  mock.patch.object(fs, "rescue", lambda: False), mock.patch.object(fs.time, "time", lambda: self.now),
                  mock.patch.object(fs.system, "run", lambda *a, **k: mock.Mock(stdout="running\n", returncode=0)),
                  mock.patch.object(fs, "_try_lock", lambda retries=5: self.attempts.append(self.now) or False),
                  mock.patch.object(fs, "blockers", lambda: [("périphérique loop", "/var/swap")])):
            p.start()
            self.addCleanup(p.stop)

    def test_backoff_and_a_single_explanatory_message(self):
        with self.assertLogs("erouter.fs", "WARNING") as logs:
            fs.maintain()
            self.now += 10
            fs.maintain()                                  # trop tôt
            self.now += 60
            fs.maintain()
            self.now += 130
            fs.maintain()
        self.assertEqual(len(self.attempts), 3, "la 2e tentative était trop rapprochée")
        self.assertEqual(len([m for m in logs.output if "/var/swap" in m]), 1, "le blocage est expliqué une seule fois")
        self.assertIn("prochain essai dans 60 s", logs.output[0])
        self.assertEqual(fs._retry, 240)

    def test_message_is_repeated_after_ten_minutes_and_delay_is_capped(self):
        with self.assertLogs("erouter.fs", "WARNING") as logs:
            for _ in range(12):
                fs.maintain()
                self.now += 700
        self.assertGreater(len([m for m in logs.output if "/var/swap" in m]), 1)
        self.assertEqual(fs._retry, fs.RETRY_MAX)


class AgentBaseTests(unittest.TestCase):
    def test_failed_apply_is_retried_later_not_every_tick(self):
        cfg = config.sanitize(config.DEFAULTS)
        agent._applied_base, agent._base_failed_at = {}, 0.0
        boom = mock.Mock(side_effect=RuntimeError("nft a échoué"))
        with mock.patch.object(lan, "apply_base", boom), self.assertLogs("erouter.agent", "ERROR"):
            agent._apply_base_if_changed(cfg)
            agent._apply_base_if_changed(cfg)
        self.assertEqual(boom.call_count, 1, "pas de nouvel essai avant le délai")
        self.assertEqual(agent._applied_base, {}, "rien n'est marqué appliqué après un échec")
        agent._base_failed_at = 0.0
        ok = mock.Mock()
        with mock.patch.object(lan, "apply_base", ok):
            agent._apply_base_if_changed(cfg)
            agent._apply_base_if_changed(cfg)
        self.assertEqual(ok.call_count, 1, "appliquée une seule fois tant que la config ne change pas")


class LanInterfaceTests(unittest.TestCase):
    """Le réseau local passe par le port Ethernet directement (la borne Wi-Fi est configurée à part)."""

    def setUp(self):
        self.cfg = config.sanitize(config.DEFAULTS)
        self.cmds = []
        self.existing = {"eth0"}
        self.ip = ""
        for p in (mock.patch.object(lan.system, "iface_exists", lambda i: i in self.existing),
                  mock.patch.object(lan.system, "iface_ip", lambda i: self.ip),
                  mock.patch.object(lan.system, "run", lambda cmd, **kw: self.cmds.append(cmd))):
            p.start()
            self.addCleanup(p.stop)

    def test_address_is_set_on_the_ethernet_port(self):
        lan.apply_interface(self.cfg)
        self.assertIn(["ip", "addr", "add", "192.168.1.1/24", "dev", "eth0"], self.cmds)
        self.assertIn(["ip", "link", "set", "eth0", "up"], self.cmds)

    def test_nothing_is_flushed_when_the_address_is_already_right(self):
        self.ip = "192.168.1.1/24"
        lan.apply_interface(self.cfg)
        self.assertNotIn(["ip", "-4", "addr", "flush", "dev", "eth0"], self.cmds)

    def test_old_bridge_and_hotspot_interface_are_removed_on_upgrade(self):
        self.existing |= {"br0", "ap0"}
        lan.apply_interface(self.cfg)
        self.assertIn(["ip", "link", "del", "br0"], self.cmds)
        self.assertIn(["iw", "dev", "ap0", "del"], self.cmds)

    def test_missing_interface_is_reported(self):
        self.existing = set()
        with self.assertRaises(RuntimeError):
            lan.apply_interface(self.cfg)


class CarrierTests(unittest.TestCase):
    def test_cable_state(self):
        for content, expected in (("1\n", True), ("0\n", False)):
            with mock.patch("builtins.open", mock.mock_open(read_data=content)):
                self.assertIs(system.carrier("eth0"), expected)
        with mock.patch("builtins.open", side_effect=OSError), mock.patch.object(system, "iface_exists", return_value=False):
            self.assertIsNone(system.carrier("eth9"))
        with mock.patch("builtins.open", side_effect=OSError), mock.patch.object(system, "iface_exists", return_value=True):
            self.assertIs(system.carrier("eth0"), False)


class WebTests(unittest.TestCase):
    """API web de bout en bout : web -> socket -> agent (validation et écriture de config réelles)."""

    @classmethod
    def setUpClass(cls):
        cls.sock = os.path.join(_TMP, "agent.sock")
        cls.calls = []

        def fake_set_ssh(enabled, password=None):
            cls.calls.append(("ssh", enabled, password))
            if enabled and not password:
                raise ValueError("Définis d'abord un mot de passe SSH")
            return {"enabled": enabled, "password_set": True, "user": "pi"}

        cls._fake_set_ssh = staticmethod(fake_set_ssh)
        cls.speed_error = None

        def fake_speed_start():
            if cls.speed_error:
                raise ValueError(cls.speed_error)
            cls.calls.append(("speedtest",))
            return {"running": True, "stage": "latence", "result": None, "history": []}

        cls._fake_speed_start = staticmethod(fake_speed_start)

        def m_connect(ssid, password=None, hidden=False):
            cls.calls.append(("connect", ssid))
            if ssid == "boom":
                raise ValueError("Connexion impossible")
            return True

        methods = dict(agentd.METHODS)
        methods.update({
            "status": lambda: {"uplink": {"connected": True, "internet": True},
                               "lan": {"interface": "eth0", "link": True}, "warnings": [],
                               "fs": {"available": True, "enabled": True, "locked": True, "rescue": False, "data": "ok"},
                               "tls": {"enabled": True, "ready": True, "fingerprint": "AA:BB"},
                               "ssh": {"enabled": False, "active": False, "user": "pi", "password_set": False},
                               "clients": 3, "uptime": 99},
            "scan": lambda rescan=False: {"networks": [], "saved": []},
            "connect": m_connect,
            "forget": lambda ssid: True,
            "set_ssh": cls._fake_set_ssh,
            "usage": lambda: {"clock": True, "today": {"rx": 5, "tx": 1}, "period": {"rx": 50, "tx": 10, "start": "2026-11-01"},
                              "plan_bytes": 0, "percent": None, "devices": [{"mac": "aa:aa:aa:aa:aa:01", "name": "portable", "ip": "10.1.0.50", "down": 40, "up": 10}]},
            "usage_reset": lambda: cls.calls.append(("usage_reset",)) or True,
            "history": lambda: [{"t": 1000, "signal": 70.0, "ms": 25.0, "loss": 0, "down": 10.0, "up": 2.0}],
            "speedtest_start": cls._fake_speed_start,
            "speedtest_status": lambda: {"running": False, "stage": "", "result": {"down_mbit": 80.0}, "history": []},
            "diagnostic": lambda: "diagnostic fictif\n",
            "clients": lambda: [],
            "reboot": lambda: True,
        })
        cls.srv = rpc.serve(methods, sock=cls.sock, group="groupe-inexistant")
        cls._orig_sock = rpc.SOCK
        rpc.SOCK = cls.sock

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        rpc.SOCK = cls._orig_sock

    def setUp(self):
        if os.path.exists(config.CONFIG_PATH):
            os.remove(config.CONFIG_PATH)
        web._failures.clear()
        self.c = web.app.test_client()

    def login(self, client=None, pw="motdepasse1"):
        return (client or self.c).post("/api/login", json={"password": pw})

    def test_first_run_sets_password_then_requires_it(self):
        self.assertTrue(self.c.get("/api/session").get_json()["setup_needed"])
        self.assertEqual(self.c.post("/api/login", json={"password": "abc"}).status_code, 400)
        self.assertEqual(self.login().status_code, 200)
        other = web.app.test_client()
        self.assertFalse(other.get("/api/session").get_json()["setup_needed"])
        self.assertEqual(self.login(other, "mauvaismdp1").status_code, 401)
        self.assertEqual(self.login(other).status_code, 200)

    def test_requires_auth_and_json(self):
        self.assertEqual(self.c.get("/api/status").status_code, 401)
        self.login()
        self.assertEqual(self.c.post("/api/reboot", data="x").status_code, 415)

    def test_session_cookie_is_host_prefixed_and_secure_in_https_mode(self):
        web.configure(True)
        try:
            r = self.login()
            cookie = r.headers["Set-Cookie"]
            for part in ("__Host-erouter=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
                self.assertIn(part, cookie)
            self.assertNotIn("Domain=", cookie)
        finally:
            web.configure(False)

    def test_version_is_only_shown_after_login(self):
        self.assertNotIn("version", self.c.get("/api/session").get_json(), "pas de version pour un visiteur anonyme")
        self.login()
        info = os.path.join(_TMP, "build-info.json")
        with open(info, "w") as f:
            json.dump({"version": "9.9.9", "commit": "abc1234", "built": "2026-11-04", "os": "Debian 13", "evil": "x"}, f)
        with mock.patch.object(web, "BUILD_INFO", info):
            b = self.c.get("/api/status").get_json()["build"]
        self.assertEqual((b["version"], b["commit"], b["built"], b["os"]), ("9.9.9", "abc1234", "2026-11-04", "Debian 13"))
        self.assertNotIn("evil", b, "seules les clés connues sont exposées")

    def test_build_info_falls_back_to_package_version(self):
        for content in (None, "pas du json", "[1, 2]", json.dumps({"version": "x" * 500})):
            with self.subTest(content):
                path = os.path.join(_TMP, "bi.json")
                if content is None:
                    path = os.path.join(_TMP, "absent.json")
                else:
                    with open(path, "w") as f:
                        f.write(content)
                with mock.patch.object(web, "BUILD_INFO", path):
                    self.assertEqual(web.build_info()["version"], web.__version__)

    def test_status_exposes_tls_fingerprint_and_data_state(self):
        self.login()
        s = self.c.get("/api/status").get_json()
        self.assertEqual(s["tls"]["fingerprint"], "AA:BB")
        self.assertEqual(s["fs"]["data"], "ok")

    def test_status_merges_agent_and_config(self):
        self.login()
        s = self.c.get("/api/status").get_json()
        self.assertEqual(s["lan"]["clients"], 3)
        self.assertTrue(s["fs"]["locked"])
        self.assertEqual(s["lan"]["address"], "192.168.1.1/24")
        self.assertTrue(s["lan"]["link"])

    def test_password_change_invalidates_other_sessions(self):
        self.login()
        other = web.app.test_client()
        self.login(other)
        r = self.c.post("/api/password", json={"current": "motdepasse1", "new": "nouveaumdp99"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/status").status_code, 200, "la session qui change le mot de passe continue")
        self.assertEqual(other.get("/api/status").status_code, 401, "les autres sessions sont révoquées")

    def test_login_is_rate_limited(self):
        self.login()
        other = web.app.test_client()
        for _ in range(5):
            self.assertEqual(self.login(other, "faux12345").status_code, 401)
        self.assertEqual(self.login(other, "faux12345").status_code, 429)
        self.assertEqual(self.login(other).status_code, 429, "même le bon mot de passe est refusé pendant le blocage")

    def test_rejects_foreign_host_and_origin(self):
        self.assertEqual(self.c.get("/api/session", headers={"Host": "evil.example.com"}).status_code, 400)
        self.assertEqual(self.c.get("/api/session", headers={"Host": "192.168.1.1"}).status_code, 200)
        self.assertEqual(self.c.get("/api/session", headers={"Host": "erouter.local"}).status_code, 200)
        r = self.c.post("/api/login", json={"password": "motdepasse1"}, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_security_headers_and_no_cache(self):
        r = self.c.get("/")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"<script>", r.data, "aucun script en ligne")
        self.assertNotIn(b"style=", r.data, "aucun style en ligne")

    def test_ssh_lives_in_a_dialog_opened_from_the_header(self):
        html = self.c.get("/").data
        self.assertIn(b'id="sshBtn"', html)
        self.assertIn(b'<dialog id="sshDlg">', html)
        self.assertNotIn("Accès SSH</h2>".encode(), html, "plus de carte SSH dans la grille")

    def test_static_whitelist(self):
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)
        self.assertEqual(self.c.get("/static/features.js").status_code, 200)
        self.assertEqual(self.c.get("/static/app.css").status_code, 200)
        self.assertEqual(self.c.get("/static/../config.py").status_code, 404)
        self.assertEqual(self.c.get("/static/index.html").status_code, 404)

    def test_oversized_body_is_refused(self):
        self.login()
        r = self.c.post("/api/settings", data=json.dumps({"lan": {"dns": ["x" * 100000]}}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_settings_validated_by_agent_and_persisted(self):
        self.login()
        bad = self.c.post("/api/settings", json={"lan": {"lease": "1h\nx"}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("bail", bad.get_json()["error"].lower())
        ok = self.c.post("/api/settings", json={"usage": {"plan_gb": 80}})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(config.load()["usage"]["plan_gb"], 80)
        self.assertEqual(config.load()["web"]["password_hash"] != "", True, "le hash du mot de passe est conservé")

    def test_secret_and_hash_cannot_be_injected_via_settings(self):
        self.login()
        before = config.load()["web"]["password_hash"]
        self.c.post("/api/settings", json={"web": {"password_hash": "x"}, "usage": {"plan_gb": 10}})
        self.assertEqual(config.load()["web"]["password_hash"], before)

    def test_ssh_toggle_goes_through_the_agent(self):
        self.assertEqual(self.c.post("/api/ssh", json={"enabled": True, "password": "x" * 12}).status_code, 401)
        self.login()
        r = self.c.post("/api/ssh", json={"enabled": True, "password": "Une-phrase-secrete-42"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(("ssh", True, "Une-phrase-secrete-42"), self.calls)
        err = self.c.post("/api/ssh", json={"enabled": True})
        self.assertEqual(err.status_code, 400)
        self.assertIn("mot de passe", err.get_json()["error"])
        self.assertEqual(self.c.post("/api/ssh", json={"enabled": True, "password": 12345678901234}).status_code, 400)

    def test_status_exposes_ssh_state(self):
        self.login()
        self.assertEqual(self.c.get("/api/status").get_json()["ssh"]["user"], "pi")

    def test_new_features_require_login(self):
        for method, path in (("get", "/api/usage"), ("post", "/api/usage/reset"), ("get", "/api/history"), ("get", "/api/speedtest"),
                             ("post", "/api/speedtest"), ("get", "/api/diagnostic"), ("get", "/api/backup"), ("post", "/api/restore")):
            with self.subTest(path):
                kw = {"json": {}} if method == "post" else {}
                self.assertEqual(getattr(self.c, method)(path, **kw).status_code, 401)

    def test_usage_history_and_speedtest_routes(self):
        self.login()
        self.assertEqual(self.c.get("/api/usage").get_json()["devices"][0]["name"], "portable")
        self.assertEqual(self.c.post("/api/usage/reset", json={}).status_code, 200)
        self.assertIn(("usage_reset",), self.calls)
        self.assertEqual(self.c.get("/api/history").get_json()["points"][0]["ms"], 25.0)
        self.assertEqual(self.c.get("/api/speedtest").get_json()["result"]["down_mbit"], 80.0)
        self.assertTrue(self.c.post("/api/speedtest", json={}).get_json()["running"])
        type(self).speed_error = "Attends encore 12 s avant un nouveau test"
        try:
            r = self.c.post("/api/speedtest", json={})
        finally:
            type(self).speed_error = None
        self.assertEqual(r.status_code, 400)
        self.assertIn("Attends encore", r.get_json()["error"])

    def test_extended_settings_are_validated_by_the_agent(self):
        self.login()
        good = {"usage": {"plan_gb": 50, "alert_percent": 90, "reset_day": 12},
                "savings": {"block_ads": True, "limits": [{"mac": "aa:bb:cc:dd:ee:01", "down_mbit": 5, "up_mbit": 2}]},
                "sqm": {"enabled": True, "down_mbit": 80, "up_mbit": 20}, "setup": {"done": True}}
        self.assertEqual(self.c.post("/api/settings", json=good).status_code, 200)
        saved = self.c.get("/api/settings").get_json()
        self.assertEqual(saved["usage"], good["usage"])
        self.assertEqual(saved["savings"]["limits"][0]["mac"], "aa:bb:cc:dd:ee:01")
        self.assertTrue(saved["sqm"]["enabled"])
        self.assertTrue(saved["setup"]["done"])
        for bad in ({"usage": {"plan_gb": "50"}}, {"usage": {"reset_day": 31}},
                    {"savings": {"limits": [{"mac": "pas-une-mac", "down_mbit": 1, "up_mbit": 1}]}},
                    {"sqm": {"enabled": True, "down_mbit": 0, "up_mbit": 0}}):
            with self.subTest(bad):
                self.assertEqual(self.c.post("/api/settings", json=bad).status_code, 400)
        self.assertEqual(self.c.get("/api/settings").get_json()["usage"], good["usage"], "un refus ne change rien")

    def test_settings_cannot_touch_ssh_or_web_sections(self):
        self.login()
        before = config.load()
        self.c.post("/api/settings", json={"ssh": {"enabled": True, "user": "root"}, "web": {"password_hash": "x", "port": 22}})
        after = config.load()
        self.assertEqual((after["ssh"], after["web"]["password_hash"], after["web"]["port"]),
                         (before["ssh"], before["web"]["password_hash"], before["web"]["port"]))

    def test_diagnostic_is_a_downloadable_text_file(self):
        self.login()
        r = self.c.get("/api/diagnostic")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertTrue(r.headers["Content-Disposition"].endswith('.txt"'))
        self.assertIn("text/plain", r.headers["Content-Type"])
        self.assertEqual(r.data, b"diagnostic fictif\n")

    def test_backup_then_restore_keeps_secret_and_ssh_state(self):
        self.login()
        cfg = config.load()
        cfg["ssh"].update(enabled=True, password_set=True)
        cfg["web"]["secret"] = "x" * 64
        config.save(cfg)
        r = self.c.get("/api/backup")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        data = json.loads(r.data)
        self.assertEqual(data["erouter_backup"], 1)
        self.assertEqual(data["config"]["web"]["secret"], "", "le secret de session n'est jamais sauvegardé")
        data["config"]["usage"]["plan_gb"] = 123
        data["config"]["ssh"]["enabled"] = False                      # une sauvegarde ne doit pas éteindre/allumer le SSH
        data["config"]["web"]["secret"] = "pirate"
        ok = self.c.post("/api/restore", json=data)
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(ok.get_json()["relogin"], "même mot de passe admin : pas de nouvelle connexion")
        after = config.load()
        self.assertEqual(after["usage"]["plan_gb"], 123)
        self.assertEqual(after["web"]["secret"], "x" * 64)
        self.assertTrue(after["ssh"]["enabled"])

    def test_restore_rejects_foreign_or_invalid_files(self):
        self.login()
        before = config.load()["usage"]["plan_gb"]
        for bad in ({}, {"erouter_backup": 2, "config": {}}, {"erouter_backup": 1}, {"erouter_backup": 1, "config": "x"},
                    {"erouter_backup": 1, "config": {"usage": {"plan_gb": -1}}}, {"erouter_backup": 1, "config": {"lan": {"address": "n'importe quoi"}}}):
            with self.subTest(bad):
                self.assertEqual(self.c.post("/api/restore", json=bad).status_code, 400)
        self.assertEqual(config.load()["usage"]["plan_gb"], before)

    def test_wifi_errors_are_reported(self):
        self.login()
        self.assertEqual(self.c.post("/api/wifi/connect", json={"ssid": "Tel", "password": "abcdefgh"}).status_code, 200)
        r = self.c.post("/api/wifi/connect", json={"ssid": "boom"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("impossible", r.get_json()["error"])

    def test_agent_ssid_validation(self):
        for bad in ("", "-oProxy", "x" * 40, "a\x00b"):
            with self.subTest(bad), self.assertRaises(ValueError):
                agentd._check_ssid(bad)

    def test_agent_down_gives_503(self):
        self.login()
        saved = rpc.SOCK
        rpc.SOCK = os.path.join(_TMP, "absent.sock")
        try:
            self.assertEqual(self.c.get("/api/status").status_code, 503)
        finally:
            rpc.SOCK = saved


class RpcTests(unittest.TestCase):
    def test_unknown_method_and_params_are_refused(self):
        sock = os.path.join(_TMP, "rpc.sock")
        srv = rpc.serve({"ping": lambda: "pong"}, sock=sock, group="groupe-inexistant")
        try:
            self.assertEqual(rpc.call("ping", sock=sock), "pong")
            with self.assertRaises(RuntimeError):
                rpc.call("rm_rf", sock=sock)
            with self.assertRaises(RuntimeError):
                rpc.call("ping", sock=sock, evil=1)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
