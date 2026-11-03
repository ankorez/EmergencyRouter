"""Interface web, sans privilèges (utilisateur `erouter`). Parle à l'agent via le socket local."""
import logging

from werkzeug.serving import run_simple

from . import config
from .web import app


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    cfg = config.load()
    run_simple("0.0.0.0", int(cfg["web"]["port"]), app, threaded=True)


if __name__ == "__main__":
    main()
