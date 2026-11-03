# EmergencyRouter

Routeur de secours tout-en-un pour Raspberry Pi (3, 4, 5) quand la fibre tombe.
Le Pi se connecte en Wi-Fi à ta 5G, et **avec la même puce Wi-Fi** diffuse un hotspot portant le
SSID/mot de passe de ta box (interface virtuelle `ap0`). Il fait aussi DHCP/DNS et NAT pour le switch en Ethernet.

```
Internet ← 5G ← wlan0 ┐
                      [ Pi ] ── br0 (192.168.1.1) ── eth0 → switch → clients
                  ap0 ┘  (hotspot, même SSID que la box)
```

## Aperçu

Captures réalisées avec des données fictives.

![Tableau de bord, thème sombre](docs/screenshots/dashboard-dark.png)

| Thème clair | Mobile | Premier démarrage |
|---|---|---|
| ![Tableau de bord, thème clair](docs/screenshots/dashboard-light.png) | ![Interface sur mobile](docs/screenshots/mobile.png) | ![Écran de premier démarrage](docs/screenshots/login.png) |

L'interface suit le thème clair ou sombre de l'appareil et s'adapte aux petits écrans.

## Installation

### Image prête à flasher (recommandé)

Raspberry Pi OS Lite (Debian 13 « Trixie », 64 bits) avec tout préinstallé, une **partition de données séparée** et
le **système en lecture seule** : on peut couper le courant à tout moment.

```
./build-image.sh        # Docker requis, ~25 Go libres dans la VM Docker ; résultat : dist/*.img.xz + SHA256SUMS
```

1. Flasher avec Raspberry Pi Imager (« Utiliser un fichier personnalisé ») ; utilisateur et clé SSH se règlent dans l'Imager,
   l'image n'embarque aucun mot de passe. Vérifie l'empreinte avec `shasum -a 256 -c SHA256SUMS`.
2. Au premier démarrage, le hotspot **`ERouter-Setup`** (mot de passe `erouter-setup`) sert à la configuration.
3. Ouvre **https://192.168.1.1/** (ou `https://erouter.local/`) : le navigateur avertit que le certificat est auto-signé
   (normal : compare l'empreinte SHA-256 affichée en bas de la page), choisis le mot de passe admin, puis règle le vrai
   SSID/mot de passe du hotspot et le Wi-Fi de la 5G.

### Installation manuelle sur Raspberry Pi OS Lite

Copier ce dossier sur le Pi puis `sudo ./install.sh`. Fais-le **avant** d'en avoir besoin (il télécharge des paquets).
Sans l'image il n'y a pas de partition de données séparée : la protection de la carte SD n'est pas disponible.

## Limites à connaître

- **Même puce = même canal.** Le hotspot est calé sur le canal de la connexion 5G ; l'agent le suit automatiquement
  (le hotspot redémarre brièvement si le canal change).
- Si le partage de connexion du téléphone est en **5 GHz sur un canal DFS (52-144)**, le hotspot ne peut pas démarrer.
  Mets le partage en **2,4 GHz** (le plus fiable), ou en 5 GHz sur un canal bas (36-48).
- Pi 3B et Zero 2 W : 2,4 GHz uniquement. Pi 3B+/4/5 : 2,4 et 5 GHz.
- **Ne branche pas le Pi en même temps que la box fibre sur le même réseau** (deux serveurs DHCP, même IP).
- Les débits d'un Wi-Fi unique partagé entre uplink et hotspot sont réduits ; pour de meilleurs résultats, mets les
  appareils fixes sur Ethernet via le switch.
- Pas de pile dans le Pi : au démarrage hors-ligne l'heure peut être fausse jusqu'à la synchronisation (sans effet sur le routage).

## Architecture

| Service | Droits | Rôle |
|---|---|---|
| `erouter-data` (`scripts/erouter-data`) | root, au démarrage | vérifie, répare ou recrée la partition de données `/data`, puis la monte |
| `erouter-agent` (`agent.py`, `agentd.py`, `lan.py`, `tls.py`, `fs.py`) | root | pont `br0`, `ap0`, hostapd, dnsmasq, nftables ; suit le canal de l'uplink ; surveille Internet ; certificat HTTPS ; verrouillage du système ; seul à écrire la config |
| `erouter-web` (`web.py`, `webd.py`, `static/`) | utilisateur `erouter`, système en lecture seule, seul droit : ports 80/443 | API Flask + interface (sans dépendance externe, fonctionne hors-ligne), servie en HTTPS par gunicorn ; le port 80 redirige vers HTTPS |
| `rpc.py` | | socket local `/run/erouter/agent.sock` (0660 root:erouter) ; l'agent n'expose que 7 méthodes et valide chaque paramètre |

Services supplémentaires : `erouter-hostapd`, `erouter-dnsmasq`. Détails et limites : [SECURITY.md](SECURITY.md).

Config : `/data/erouter/config.yaml` (voir `config.example.yaml`), modifiable depuis l'interface.
Logs : `sudo journalctl -u erouter-data -u erouter-agent -u erouter-web -u erouter-hostapd -u erouter-dnsmasq -n 80`

## Protection de la carte SD

Un routeur de secours dort longtemps puis subit une coupure de courant. L'image a donc trois partitions :

| Partition | Contenu | Écrite ? |
|---|---|---|
| `boot` (FAT, 512 Mo) | noyau, `cmdline.txt` | jamais en fonctionnement |
| `rootfs` (ext4, ~2,4 Go) | le système | **lecture seule** une fois le démarrage terminé |
| `erdata` (ext4, 256 Mo) | config, Wi-Fi enregistrés, état de NetworkManager, certificat HTTPS | oui, quand tu enregistres un réglage |

- Une coupure de courant ne peut donc abîmer que `erdata`, qui est **vérifiée à chaque démarrage** (`e2fsck -f`) :
  réparée si possible, **recréée avec la config par défaut** sinon (l'interface t'en avertit). Le démarrage n'est jamais bloqué.
- Le système n'est écrit que pendant le démarrage (le temps que tout soit lancé), puis verrouillé.
- La racine n'est **pas** agrandie au premier démarrage (le mot `resize` est retiré de `cmdline.txt`) : le reste de la carte est inutilisé.
- Journaux et baux DHCP en RAM, pas de swap, pas de mises à jour automatiques.
- L'état se voit et se règle en ligne de commande : `sudo erouter-fs status | lock | unlock`. L'interface n'affiche que les
  avertissements (partition de données réparée ou recréée).
- **Mettre à jour le système** : `sudo erouter-fs unlock && sudo reboot`, puis `sudo apt update && sudo apt upgrade`,
  puis `sudo erouter-fs lock`.
- **Sortie de secours** : retire la carte, ouvre `cmdline.txt` sur la partition de boot (visible depuis un Mac) et ajoute
  `erouter.unlock` à la fin de la ligne : le système reste alors en écriture.
- Contrepartie : un dossier personnel (`~`) n'est pas modifiable quand le système est verrouillé.

## HTTPS

Certificat auto-signé créé par l'agent au premier démarrage (EC P-256, valable 825 jours, renouvelé 60 jours avant
l'échéance ou si l'adresse IP du routeur change), stocké dans `/data` : il reste le même d'un démarrage à l'autre, donc
l'avertissement du navigateur n'apparaît qu'**une fois par appareil**. Il couvre `erouter.local`, `erouter` et l'IP du routeur.
L'empreinte SHA-256 s'affiche en bas de l'interface pour la comparer lors de la première visite.
Si le certificat ne peut pas être créé, l'interface reste servie en HTTP (avec un avertissement) plutôt que d'être inaccessible.

## Développement

```
make check          # pyflakes + shellcheck + 51 tests unitaires
make integration    # vérifications dans un vrai Debian 13 (Docker, conteneur privilégié) :
                    # vrais disques pour la partition de données (saine, corrompue, détruite), post-traitement d'image,
                    # HTTPS de bout en bout avec gunicorn, pare-feu (nft -c), dnsmasq, unités systemd, shellcheck
make image          # image flashable
```
La CI GitHub exécute les vérifications statiques et les tests unitaires. Ce qui n'est pas testé automatiquement :
le hotspot et l'uplink Wi-Fi (matériel), le démarrage réel de l'image et le verrouillage du système sur un Pi.

## Feuille de route

- [ ] Image construite automatiquement par GitHub Actions
- [ ] Compteur de données 5G
