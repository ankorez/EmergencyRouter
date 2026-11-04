"""QR code de connexion au Wi-Fi du hotspot (format reconnu par les appareils photo d'iOS et d'Android)."""
import io
import re

_SPECIAL = frozenset(["\\", ";", ",", ":", '"'])


def wifi_payload(ssid, password):
    """WIFI:T:WPA;S:<ssid>;P:<mot de passe>;;  — les caractères \\ ; , : " doivent être précédés d'un antislash."""
    def esc(text):
        return "".join("\\" + c if c in _SPECIAL else c for c in text)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def svg(text):
    import qrcode
    import qrcode.image.svg
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=10)
    qr.add_data(text)
    qr.make(fit=True)
    out = io.BytesIO()
    qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(out)
    return _with_white_background(out.getvalue())


def _with_white_background(svg_bytes):
    """Le SVG de la bibliothèque est transparent : noir sur transparent devient illisible (fond sombre, impression,
    lecteurs de QR). On ajoute un fond blanc de la taille du dessin."""
    text = svg_bytes.decode()
    m = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', text)
    tag = re.search(r"<svg[^>]*>", text)
    if not m or not tag:
        return svg_bytes
    rect = f'<rect width="{m.group(1)}" height="{m.group(2)}" fill="#ffffff"/>'
    return (text[:tag.end()] + rect + text[tag.end():]).encode()
