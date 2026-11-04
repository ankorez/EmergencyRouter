"""Version du logiciel et de l'image (écrite à la construction dans /usr/share/erouter/build-info.json)."""
import json

from . import __version__

DEFAULT_PATH = "/usr/share/erouter/build-info.json"


def load(path=DEFAULT_PATH):
    info = {"version": __version__}
    try:
        with open(path) as f:
            data = json.load(f)
        for key in ("version", "commit", "built", "os", "source"):
            if isinstance(data.get(key), str) and len(data[key]) <= 64:
                info[key] = data[key]
    except (OSError, ValueError, AttributeError):
        pass
    return info
