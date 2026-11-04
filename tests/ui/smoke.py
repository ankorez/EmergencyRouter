"""Tests de l'interface dans un vrai Chrome (sans écran) contre un faux serveur. Lancer : python3 tests/ui/smoke.py
Nécessite Chrome ou Chromium (variable CHROME pour indiquer son chemin) ; sinon les tests sont ignorés."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import mock_server  # noqa: E402

CANDIDATES = [os.environ.get("CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("chromium") or "", shutil.which("chromium-browser") or "", shutil.which("google-chrome") or ""]
CHROME = next((c for c in CANDIDATES if c and os.path.exists(c)), None)
PORT = 8790


@unittest.skipUnless(CHROME, "Chrome introuvable (définir CHROME)")
class UiTests(unittest.TestCase):
    def run_scenario(self, scenario, width=1200):
        mock_server.LOG.clear()
        mock_server.STATE.update(speed_polls=0, speed_started=False, connected=None)
        srv = mock_server.serve(PORT, scenario)
        try:
            p = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", f"--window-size={width},1800",
                                "--virtual-time-budget=12000", "--dump-dom", f"http://127.0.0.1:{PORT}/"],
                               capture_output=True, text=True, timeout=90)
        finally:
            srv.shutdown()
            srv.server_close()
        m = re.search(r'<pre id="__result">(.*?)</pre>', p.stdout, re.S)
        self.assertIsNotNone(m, "le scénario n'a rien rapporté : " + p.stderr[-300:])
        import html
        result = json.loads(html.unescape(m.group(1)))
        self.assertNotIn("fatal", result, "le scénario a levé une exception")
        self.assertEqual(result.pop("errors"), [], "erreurs JavaScript dans la page")
        return result, list(mock_server.LOG)

    def test_dashboard_renders_usage_charts_speedtest_and_limits(self):
        r, _ = self.run_scenario("dashboard")
        self.assertEqual(r["usToday"], "1,50 Go")
        self.assertEqual(r["usPeriod"], "41,00 Go")
        self.assertIn("sur 50,00 Go (82 %)", r["usPeriodSub"])
        self.assertEqual((r["progress"], r["progressClass"], r["progressHidden"]), (82, "warn", False), "82 % > seuil de 80 % : barre orange")
        self.assertEqual(r["devices"], 2)
        self.assertEqual((r["latLines"], r["rateLines"]), (1, 2), "latence : 1 courbe ; débit : reçu + envoyé")
        self.assertGreater(r["lossBars"], 0, "les pertes de paquets sont repérées sur la courbe de latence")
        self.assertIn("ms", r["latNow"])
        self.assertEqual((r["spDown"], r["spUp"], r["spPing"]), ("84,3 Mbit/s", "21,7 Mbit/s", "31 ms"))
        self.assertEqual(r["spHistory"], 1)
        self.assertFalse(r["speedSqmHidden"], "après un test, on propose d'utiliser les débits pour l'anti-latence")
        self.assertIn("portable", r["limList"])
        self.assertEqual(r["limOptions"], 1, "l'appareil déjà plafonné n'est plus proposé")
        self.assertEqual(r["buttons"], ["Outils", "SSH : éteint", "Mot de passe", "Déconnexion"])
        self.assertFalse(r["wizardOpen"], "l'assistant ne s'ouvre pas quand la configuration est terminée")
        self.assertIn("Forfait : 82 %", r["warnings"])

    def test_tools_dialog(self):
        r, _ = self.run_scenario("tools")
        self.assertTrue(r["toolsOpen"] and r["toolsClosed"])
        self.assertEqual(sorted(r["links"]), ["/api/backup", "/api/diagnostic"])
        self.assertNotIn("QR code du Wi-Fi", r["buttons"], "plus de hotspot, plus de QR code")

    def test_first_run_wizard_has_two_steps_and_saves_only_at_the_end(self):
        r, log = self.run_scenario("wizard")
        self.assertTrue(r["openedByItself"], "l'assistant s'ouvre tout seul au premier démarrage")
        self.assertTrue(r["title1"].startswith("1/2 · La connexion 5G"))
        self.assertEqual(r["networks"], 2)
        self.assertIn("mot de passe", r["errNoPw"].lower())
        self.assertTrue(r["title2"].startswith("2/2 · Terminé"))
        self.assertIn("borne", r["body2"], "on rappelle que la borne Wi-Fi se configure à part")
        self.assertTrue(r["connectPosted"])
        self.assertEqual(r["settingsBeforeFinish"], 0)
        self.assertTrue(r["closed"])
        posts = [(e["path"], e["body"]) for e in log]
        self.assertIn(("/api/wifi/connect", {"ssid": "Routeur-4G", "password": "motdepasse-5g"}), posts)
        self.assertEqual([b for p, b in posts if p == "/api/settings"], [{"setup": {"done": True}}])

    def test_speedtest_runs_polls_and_prefills_the_anti_latency_rates(self):
        r, log = self.run_scenario("speedtest")
        self.assertEqual(r["disabled"], True)
        self.assertIn("en cours", r["runningLabel"])
        self.assertEqual(r["after"], "84,3 Mbit/s")
        self.assertEqual(r["finalLabel"], "Lancer le test")
        self.assertEqual((r["sqmDown"], r["sqmUp"], r["sqmOn"]), ("71", "18", True), "85 % de 84,3 et de 21,7 Mbit/s")
        self.assertIn("/api/speedtest", [e["path"] for e in log])

    def test_saving_limits_ads_antilatency_and_plan_sends_the_right_settings(self):
        r, log = self.run_scenario("savings")
        self.assertEqual(r["options"], ["02:00:00:00:00:02"])
        self.assertEqual((r["limitsShown"], r["afterRemove"]), (2, 1))
        settings = [e["body"] for e in log if e["path"] == "/api/settings"]
        self.assertIn({"savings": {"block_ads": True, "limits": [{"mac": "02:00:00:00:00:02", "down_mbit": 3, "up_mbit": 1}]},
                       "sqm": {"enabled": True, "down_mbit": 80, "up_mbit": 20}}, settings)
        self.assertIn({"usage": {"plan_gb": 100, "alert_percent": 90, "reset_day": 15}}, settings)

    def test_phone_width_has_no_script_errors(self):
        self.run_scenario("dashboard", width=500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
