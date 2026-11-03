"""Tests d'EmergencyRouter. Lancer :  python3 -m unittest discover -s tests -v

Aucun accès au système réel : l'agent est simulé (sauf la validation/enregistrement de config, qui est réel).
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="erouter-test-")
os.environ["EROUTER_CONFIG"] = os.path.join(_TMP, "config.yaml")
os.environ["EROUTER_LOCK_FLAG"] = os.path.join(_TMP, "lock-enabled")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from erouter import agent, agentd, config, fs, lan, rpc, system, web  # noqa: E402

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
        out = self.env.get_template("nftables.conf.j2").render(bridge="br0", uplink="wlan0", web_port=80,
                                                              lan_net="192.168.1.0/24")
        self.assertIn("policy drop", out)
        self.assertIn('ip saddr != 192.168.1.0/24 drop', out)
        self.assertIn("5353", out)
        self.assertNotIn('iifname "wlan0"', out, "rien n'est ouvert depuis l'uplink")

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
        fs._depth, fs._need_relock, fs._last_try = 0, False, 0.0
        if os.path.exists(fs.FLAG):
            os.remove(fs.FLAG)

        def remount(mode):
            self.remounts.append(mode)
            self.ro = mode == "ro"
            return True

        self.patches = [
            mock.patch.object(fs, "root_ro", lambda: self.ro),
            mock.patch.object(fs, "_remount", remount),
            mock.patch.object(fs, "rescue", lambda: False),
            mock.patch.object(fs.system, "run", lambda *a, **k: mock.Mock(returncode=0, stdout="running\n")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_writable_window_relocks(self):
        with fs.writable():
            self.assertFalse(self.ro)
        self.assertTrue(self.ro)
        self.assertEqual(self.remounts, ["rw", "ro"])

    def test_nested_windows_relock_once(self):
        with fs.writable():
            with fs.writable():
                pass
            self.assertFalse(self.ro, "reste en écriture tant que la fenêtre externe est ouverte")
        self.assertEqual(self.remounts, ["rw", "ro"])

    def test_window_is_noop_when_not_locked(self):
        self.ro = False
        with fs.writable():
            pass
        self.assertEqual(self.remounts, [])

    def test_maintain_locks_only_when_enabled_and_booted(self):
        self.ro = False
        fs.maintain()
        self.assertEqual(self.remounts, [], "désactivé : on ne verrouille pas")
        fs.set_enabled(True)
        self.assertTrue(self.ro)
        self.assertTrue(os.path.exists(fs.FLAG))

    def test_rescue_never_locks(self):
        self.ro = False
        fs.set_enabled(True)
        self.ro = False
        with mock.patch.object(fs, "rescue", lambda: True):
            fs._last_try = 0.0
            fs.maintain()
        self.assertFalse(self.ro)

    def test_disable_leaves_card_writable(self):
        fs.set_enabled(True)
        fs.set_enabled(False)
        self.assertFalse(self.ro)
        self.assertFalse(os.path.exists(fs.FLAG))


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

        def m_connect(ssid, password=None, hidden=False):
            cls.calls.append(("connect", ssid))
            if ssid == "boom":
                raise ValueError("Connexion impossible")
            return True

        methods = dict(agentd.METHODS)
        methods.update({
            "status": lambda: {"uplink": {"connected": True, "internet": True},
                               "ap": {"enabled": True, "channel": 6, "active": True}, "warnings": [],
                               "fs": {"enabled": True, "locked": True, "rescue": False}, "clients": 3, "uptime": 99},
            "scan": lambda rescan=False: {"networks": [], "saved": []},
            "connect": m_connect,
            "forget": lambda ssid: True,
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
