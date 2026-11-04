"""Régénère les captures d'écran du README (docs/screenshots) depuis le vrai code de l'interface et des données fictives.
Usage : python3 tests/ui/screenshots.py"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import smoke  # noqa: E402  (retrouve Chrome)
import mock_server  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "screenshots")
# (fichier, scénario, largeur, hauteur, thème sombre forcé)
SHOTS = [
    ("dashboard-dark.png", "dashboard-shot", 1200, 2650, True),
    ("dashboard-light.png", "light", 1200, 2650, False),
    ("mobile.png", "dashboard-shot", 500, 3900, True),
    ("ssh-dialog.png", "ssh-shot", 900, 760, True),
    ("tools-dialog.png", "tools-shot", 900, 760, True),
    ("wizard.png", "wizard-shot", 900, 760, True),
]
PORT = 8791


def main():
    if not smoke.CHROME:
        sys.exit("Chrome introuvable (variable CHROME)")
    for name, scenario, w, h, dark in SHOTS:
        srv = mock_server.serve(PORT, scenario)
        try:
            cmd = [smoke.CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", f"--window-size={w},{h}",
                   "--virtual-time-budget=9000", f"--screenshot={os.path.join(OUT, name)}"] + (["--force-dark-mode"] if dark else []) + [f"http://127.0.0.1:{PORT}/"]
            subprocess.run(cmd, capture_output=True, timeout=90)
        finally:
            srv.shutdown()
            srv.server_close()
        print("ok", name)


if __name__ == "__main__":
    main()
