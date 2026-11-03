# EmergencyRouter

Routeur de secours tout-en-un pour Raspberry Pi (3, 4, 5) quand la fibre tombe.
Le Pi se connecte en Wi-Fi à ta 5G, et **avec la même puce Wi-Fi** diffuse un hotspot portant le
SSID/mot de passe de ta box (interface virtuelle `ap0`). Il fait aussi DHCP/DNS et NAT pour le switch en Ethernet.

```
Internet ← 5G ← wlan0 ┐
                      [ Pi ] ── br0 (192.168.1.1) ── eth0 → switch → clients
                  ap0 ┘  (hotspot, même SSID que la box)
```

## Installation

1. Flasher **Raspberry Pi OS Lite** (64 bits, Bookworm/Trixie) avec Raspberry Pi Imager (SSH activé, accès réseau pour l'installation).
2. Copier ce dossier sur le Pi, puis : `sudo ./install.sh`
3. Ouvrir `http://192.168.1.1/` depuis le LAN, choisir un mot de passe admin, puis choisir le Wi-Fi de la 5G et régler le hotspot.

Fais l'installation **avant** d'en avoir besoin (elle télécharge des paquets).

## Image prête à flasher (Debian 13 Trixie 64 bits, Lite)

Construite avec [pi-gen](https://github.com/RPi-Distro/pi-gen) (branche `arm64`) dans Docker, sans émulation sur un Mac Apple Silicon :

```
./build-image.sh        # ~25 Go libres dans la VM Docker ; résultat dans dist/*.img.xz
```

Flasher avec Raspberry Pi Imager (« Utiliser un fichier personnalisé ») ; utilisateur et clé SSH se règlent dans l'Imager,
l'image n'embarque aucun mot de passe. Au premier démarrage, le hotspot **`ERouter-Setup`** (mot de passe `erouter-setup`)
sert à la configuration : ouvre `http://192.168.1.1/` (ou `http://erouter.local/`), choisis le mot de passe admin, puis règle
le vrai SSID/mot de passe et le Wi-Fi de la 5G. Change ces valeurs tout de suite.

## Limites à connaître

- **Même puce = même canal.** Le hotspot est calé sur le canal de la connexion 5G ; l'agent le suit automatiquement
  (le hotspot redémarre brièvement si le canal change).
- Si le partage de connexion du téléphone est en **5 GHz sur un canal DFS (52-144)**, le hotspot ne peut pas démarrer.
  Mets le partage en **2,4 GHz** (le plus fiable), ou en 5 GHz sur un canal bas (36-48).
- Pi 3B et Zero 2 W : 2,4 GHz uniquement. Pi 3B+/4/5 : 2,4 et 5 GHz.
- **Ne branche pas le Pi en même temps que la box fibre sur le même réseau** (deux serveurs DHCP, même IP).
- Les débits d'un Wi-Fi unique partagé entre uplink et hotspot sont réduits ; pour de meilleurs résultats, mets les
  appareils fixes sur Ethernet via le switch.

## Architecture

Deux services, pour qu'une faille dans la page web ne donne pas root :

| Service | Droits | Rôle |
|---|---|---|
| `erouter-agent` (`erouter/agent.py`, `agentd.py`, `lan.py`) | root | pont `br0`, `ap0`, hostapd, dnsmasq, nftables ; suit le canal de l'uplink ; surveille Internet |
| `erouter-web` (`erouter/web.py`, `webd.py`, `static/index.html`) | utilisateur `erouter`, seul privilège : port 80 | API Flask + interface (sans dépendance externe, fonctionne hors-ligne) |
| `erouter/rpc.py` | | socket local `/run/erouter/agent.sock` (0660 root:erouter) ; l'agent n'expose que quelques méthodes et valide chaque paramètre |

L'agent **revalide toute la config à chaque lecture** (le fichier est modifiable par le service web) et garde la dernière config valide
si le fichier est abîmé. Services supplémentaires : `erouter-hostapd`, `erouter-dnsmasq`.

Config : `/etc/erouter/config.yaml` (voir `config.example.yaml`), modifiable depuis l'interface.
Logs : `sudo journalctl -u erouter-agent -u erouter-web -u erouter-hostapd -u erouter-dnsmasq -n 80`

## Protection de la carte SD

Un routeur de secours dort longtemps puis subit une coupure de courant : on évite donc d'écrire.
- Journaux **en RAM** (`Storage=volatile`), baux DHCP dans `/run` (RAM), `/tmp` en tmpfs (défaut de Debian 13), pas de swap ni de mises à jour automatiques.
- La config est écrite de façon **atomique** (fichier temporaire + `fsync` + renommage).
- Ce n'est **pas** un système de fichiers strictement en lecture seule : un overlay perdrait au redémarrage la config et le
  Wi-Fi 5G enregistré depuis l'interface, sauf à ajouter une partition de données séparée (piste future).

## Feuille de route

- [ ] Overlay en lecture seule avec partition de données séparée
- [ ] Image construite automatiquement par GitHub Actions
