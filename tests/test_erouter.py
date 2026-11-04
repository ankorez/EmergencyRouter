"""Tests d'EmergencyRouter. Lancer :  python3 -m unittest discover -s tests -v

Aucun accès au système réel : l'agent est simulé (sauf la validation/enregistrement de config, qui est réel).
"""
import contextlib
import copy
import http.client
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="erouter-test-")
os.environ["EROUTER_DATA"] = os.path.join(_TMP, "data")
os.environ["EROUTER_CONFIG"] = os.path.join(_TMP, "data", "erouter", "config.yaml")
os.environ["EROUTER_DATA_STATE"] = os.path.join(_TMP, "data.state")
os.environ["EROUTER_TLS_DIR"] = os.path.join(_TMP, "tls")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from erouter import agent, agentd, config, fs, lan, rpc, system, tls, web, webd  # noqa: E402

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
            "ssid": lambda c: c["ap"].__setitem__("ssid", "a\nwpa=0"),
            "mot de passe court": lambda c: c["ap"].__setitem__("password", "short"),
            "pays minuscule": lambda c: c["ap"].__setitem__("country", "fr"),
            "interface": lambda c: c["lan"].__setitem__("interface", "eth0; reboot"),
            "interface option": lambda c: c["uplink"].__setitem__("interface", "-h"),
            "adresse réseau": lambda c: c["lan"].__setitem__("address", "10.0.0.0/24"),
            "dhcp hors réseau": lambda c: c["lan"].__setitem__("dhcp_start", "10.9.9.9"),
            "canal": lambda c: c["ap"].__setitem__("fallback_channel", 40),
            "port SSH": lambda c: c["web"].__setitem__("port", 22),
            "ports identiques": lambda c: c["web"].__setitem__("http_port", 443),
            "port de redirection réservé": lambda c: c["web"].__setitem__("http_port", 53),
            "tls non booléen": lambda c: c["web"].__setitem__("tls", "oui"),
            "utilisateur ssh avec option": lambda c: c["ssh"].__setitem__("user", "-oProxyCommand=x"),
            "ssh activé non booléen": lambda c: c["ssh"].__setitem__("enabled", "oui"),
            "section absente": lambda c: c.pop("lan"),
        }
        for name, mutate in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                config.validate(_bad(mutate))

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

    def test_nftables(self):
        out = self.env.get_template("nftables.conf.j2").render(bridge="br0", uplink="wlan0", web_ports=[443, 80],
                                                              lan_net="192.168.1.0/24")
        self.assertIn("policy drop", out)
        self.assertIn('ip saddr != 192.168.1.0/24 drop', out)
        self.assertIn("5353", out)
        self.assertNotIn('iifname "wlan0"', out, "rien n'est ouvert depuis l'uplink")
        self.assertIn("{ 53, 443, 80 }", out)

    def test_dnsmasq_and_hostapd(self):
        d = self.env.get_template("dnsmasq.conf.j2").render(bridge="br0", gateway="g", netmask="n", start="a", end="b",
                                                           lease="12h", dns=["1.1.1.1"], leases="/x")
        self.assertIn("stop-dns-rebind", d)
        self.assertIn("pid-file=", d)
        h = self.env.get_template("hostapd.conf.j2").render(iface="ap0", bridge="br0", ssid="S", password="p",
                                                           country="FR", channel=6, hw_mode="g")
        self.assertIn("wpa=2", h)
        self.assertIn("rsn_pairwise=CCMP", h)


class SystemTests(unittest.TestCase):
    def test_channels(self):
        self.assertEqual(system.freq_to_channel(2437), 6)
        self.assertEqual(system.freq_to_channel(5180), 36)
        self.assertEqual(system.freq_to_channel(2484), 14)
        self.assertTrue(system.is_dfs(5500))
        self.assertFalse(system.is_dfs(5180))

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


class AgentBaseTests(unittest.TestCase):
    def test_failed_apply_is_retried_later_not_every_tick(self):
        cfg = config.sanitize(config.DEFAULTS)
        agent._applied_base, agent._base_failed_at = None, 0.0
        boom = mock.Mock(side_effect=RuntimeError("nft a échoué"))
        with mock.patch.object(lan, "apply_base", boom), self.assertLogs("erouter.agent", "ERROR"):
            agent._apply_base_if_changed(cfg)
            agent._apply_base_if_changed(cfg)
        self.assertEqual(boom.call_count, 1, "pas de nouvel essai avant le délai")
        agent._base_failed_at = 0.0
        ok = mock.Mock()
        with mock.patch.object(lan, "apply_base", ok):
            agent._apply_base_if_changed(cfg)
            agent._apply_base_if_changed(cfg)
        self.assertEqual(ok.call_count, 1, "appliquée une seule fois tant que la config ne change pas")


class AgentApTests(unittest.TestCase):
    """Le hotspot ne doit être redémarré que pour une bonne raison (changement de canal confirmé, réglages)."""

    def setUp(self):
        self.applied = []
        self.cur = None
        agent._pending = None

        def apply_ap(cfg, ch):          # idempotent comme lan.apply_ap : rien n'est redémarré si rien n'a changé
            ap = cfg["ap"]
            wanted = (bool(ap["enabled"]), ap["ssid"], ap["password"], ap["country"], ch)
            if wanted != self.cur:
                self.cur = wanted
                self.applied.append(ch)

        for p in (mock.patch.object(lan, "apply_ap", apply_ap), mock.patch.object(lan, "current_ap", lambda: self.cur)):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = config.sanitize(config.DEFAULTS)

    def test_first_start_applies_immediately(self):
        agent._sync_ap(self.cfg, 6)
        self.assertEqual(self.applied, [6])

    def test_channel_change_needs_two_consistent_reads(self):
        agent._sync_ap(self.cfg, 6)
        agent._sync_ap(self.cfg, 11)
        self.assertEqual(self.applied, [6], "un seul relevé ne suffit pas")
        agent._sync_ap(self.cfg, 11)
        self.assertEqual(self.applied, [6, 11])

    def test_flapping_channel_is_ignored(self):
        agent._sync_ap(self.cfg, 6)
        for ch in (11, 6, 11, 6):
            agent._sync_ap(self.cfg, ch)
        self.assertEqual(self.applied, [6])

    def test_settings_change_applies_immediately(self):
        agent._sync_ap(self.cfg, 6)
        self.cfg["ap"]["ssid"] = "Autre"
        agent._sync_ap(self.cfg, 6)
        self.assertEqual(len(self.applied), 2, "nouveau SSID => hotspot reconfiguré")


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

        def m_connect(ssid, password=None, hidden=False):
            cls.calls.append(("connect", ssid))
            if ssid == "boom":
                raise ValueError("Connexion impossible")
            return True

        methods = dict(agentd.METHODS)
        methods.update({
            "status": lambda: {"uplink": {"connected": True, "internet": True},
                               "ap": {"enabled": True, "channel": 6, "active": True}, "warnings": [],
                               "fs": {"available": True, "enabled": True, "locked": True, "rescue": False, "data": "ok"},
                               "tls": {"enabled": True, "ready": True, "fingerprint": "AA:BB"},
                               "ssh": {"enabled": False, "active": False, "user": "pi", "password_set": False},
                               "clients": 3, "uptime": 99},
            "scan": lambda rescan=False: {"networks": [], "saved": []},
            "connect": m_connect,
            "forget": lambda ssid: True,
            "set_ssh": cls._fake_set_ssh,
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

    def test_warns_about_default_hotspot_password(self):
        self.login()
        self.assertTrue(any("mot de passe par défaut" in w for w in self.c.get("/api/status").get_json()["warnings"]))
        self.c.post("/api/settings", json={"ap": {"password": "un-vrai-secret-1"}})
        self.assertFalse(any("mot de passe par défaut" in w for w in self.c.get("/api/status").get_json()["warnings"]))

    def test_status_merges_agent_and_config(self):
        self.login()
        s = self.c.get("/api/status").get_json()
        self.assertEqual(s["lan"]["clients"], 3)
        self.assertTrue(s["fs"]["locked"])
        self.assertEqual(s["ap"]["ssid"], "MaBox")

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
        self.assertEqual(self.c.get("/static/app.css").status_code, 200)
        self.assertEqual(self.c.get("/static/../config.py").status_code, 404)
        self.assertEqual(self.c.get("/static/index.html").status_code, 404)

    def test_oversized_body_is_refused(self):
        self.login()
        r = self.c.post("/api/settings", data=json.dumps({"ap": {"ssid": "x" * 100000}}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_settings_validated_by_agent_and_persisted(self):
        self.login()
        bad = self.c.post("/api/settings", json={"lan": {"lease": "1h\nx"}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("bail", bad.get_json()["error"].lower())
        ok = self.c.post("/api/settings", json={"ap": {"ssid": "MaBox2", "enabled": True}})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(config.load()["ap"]["ssid"], "MaBox2")
        self.assertEqual(config.load()["web"]["password_hash"] != "", True, "le hash du mot de passe est conservé")

    def test_secret_and_hash_cannot_be_injected_via_settings(self):
        self.login()
        before = config.load()["web"]["password_hash"]
        self.c.post("/api/settings", json={"web": {"password_hash": "x"}, "ap": {"ssid": "A"}})
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
