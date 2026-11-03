import logging
import threading

from werkzeug.serving import run_simple

from . import agent, config
from .web import app


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    cfg = config.load()
    stop = threading.Event()
    threading.Thread(target=agent.run_forever, args=(stop,), daemon=True).start()
    try:
        run_simple("0.0.0.0", int(cfg["web"]["port"]), app, threaded=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
