"""Interface web, sans privilèges (utilisateur `erouter`). Parle à l'agent via le socket local."""
import logging

from waitress import serve

from . import config
from .web import app, init_secret


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    port = int(config.load()["web"]["port"])
    init_secret()
    serve(app, host="0.0.0.0", port=port, threads=6, ident="erouter",
          max_request_body_size=64 * 1024, connection_limit=100, channel_timeout=30,
          clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
