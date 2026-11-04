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

Captures réalisées avec des données fictives (`python3 tests/ui/screenshots.py`).

![Tableau de bord, thème sombre](docs/screenshots/dashboard-dark.png)

| Thème clair | Mobile |
|---|---|
| ![Tableau de bord, thème clair](docs/screenshots/dashboard-light.png) | ![Interface sur mobile](docs/screenshots/mobile.png) |

| Assistant de premier démarrage | Outils | Accès SSH |
|---|---|---|
| ![Assistant](docs/screenshots/wizard.png) | ![Outils](docs/screenshots/tools-dialog.png) | ![SSH](docs/screenshots/ssh-dialog.png) |

L'interface suit le thème clair ou sombre de l'appareil et s'adapte aux petits écrans.

## Installation

### Image prête à flasher (recommandé)

Raspberry Pi OS Lite (Debian 13 « Trixie », 64 bits) avec tout préinstallé, une **partition de données séparée** et
le **système en lecture seule** : on peut couper le courant à tout moment.

```
./build-image.sh        # Docker requis, ~25 Go libres dans la VM Docker ; résultat : dist/*.img.xz + SHA256SUMS
```

1. Flasher avec Raspberry Pi Imager (« Utiliser un fichier personnalisé »). Le compte `pi` est déjà dans l'image
   (voir « Accès SSH ») : **ne règle pas d'utilisateur dans l'Imager**. Vérifie l'empreinte avec
   `shasum -a 256 -c SHA256SUMS`.
2. Au premier démarrage, le hotspot **`ERouter-Setup`** (mot de passe `erouter-setup`) sert à la configuration.
3. Ouvre **https://192.168.1.1/** (ou `https://erouter.local/`) : le navigateur avertit que le certificat est auto-signé
   (normal : compare l'empreinte SHA-256 affichée en bas de la page), choisis le mot de passe admin, puis règle le vrai
   SSID/mot de passe du hotspot et le Wi-Fi de la 5G.

### Accès SSH

Éteint par défaut. Pour l'activer, dans l'interface, bouton **« SSH »** en haut de la page : choisis un mot de passe
(12 caractères minimum), coche « Activer le SSH » et enregistre. Puis :

```
ssh pi@192.168.1.1        # depuis le hotspot ERouter-Setup ou le câble Ethernet du Pi
```

- Le SSH n'est joignable que depuis le **réseau du routeur** (hotspot ou Ethernet), jamais depuis l'uplink (Wi-Fi de la box, 5G).
- Le compte est `pi` (réglable au build avec `SSH_USER=nom`) ; l'image n'a ni mot de passe par défaut ni clé : tant que tu n'as
  rien défini dans l'interface, personne ne peut se connecter. `sudo` demande ce même mot de passe.
- Le mot de passe est refusé s'il est trop court ou prévisible (contient le nom du compte, « password », « azerty »…).
- Changer le mot de passe repasse le système en écriture **une seconde**, puis le reverrouille. Le réglage « activé ou non »
  est mémorisé dans `/data` : il survit aux redémarrages, sans toucher au système.
- Protections : `root` interdit, 3 essais maximum, 12 nouvelles connexions par minute, pas de redirection de ports.
- `NO_ADMIN_USER=1 ./build-image.sh` construit une image sans compte (à créer alors avec Raspberry Pi Imager).
- Après un reflash, `ssh-keygen -R 192.168.1.1` (et `-R erouter.local`) évite l'avertissement « clé d'hôte modifiée ».

### Installation manuelle sur Raspberry Pi OS Lite

Copier ce dossier sur le Pi puis `sudo ./install.sh`. Fais-le **avant** d'en avoir besoin (il télécharge des paquets).
Sans l'image il n'y a pas de partition de données séparée : la protection de la carte SD n'est pas disponible.

## Fonctions

| Fonction | Où | Ce qu'elle fait | Coût |
|---|---|---|---|
| **Consommation de données** | carte « Consommation » | octets reçus/envoyés sur la 5G : aujourd'hui, depuis le renouvellement du forfait, par appareil ; barre de progression et alerte à un seuil | aucune donnée ; une écriture dans `/data` toutes les 10 min |
| **Qualité de la connexion** | carte « Qualité » | latence, pertes, signal et débit sur 24 h | en mémoire seulement (perdu au redémarrage) |
| **Test de débit** | carte « Test de débit » | débit descendant/montant, latence, gigue, pertes ; propose d'utiliser les débits pour l'anti-latence | environ **35 Mo** par test, sur demande ; contacte `speed.cloudflare.com` |
| **Blocage des publicités** | carte « Économies » | bloque ~77 000 domaines publicitaires et de traçage au niveau DNS ([StevenBlack/hosts](https://github.com/StevenBlack/hosts), licence MIT, version épinglée dans `erouter/data/blocklist.SOURCE`) | décompressée en mémoire ; peut gêner quelques sites |
| **Anti-latence (CAKE)** | carte « Économies » | évite que la connexion devienne très lente quand quelqu'un télécharge (envoi + réception, équité par appareil) | à régler un peu sous les débits réels de la 5G |
| **Plafond par appareil** | carte « Économies » | limite le débit d'un appareil (suivi par son adresse MAC, même si son IP change) | jusqu'à 16 appareils ; plafond **approximatif** (à ± 30 % près) |
| **QR code du Wi-Fi** | Outils | connecte un téléphone au hotspot en scannant | — |
| **Diagnostic** | Outils | un fichier avec l'état, la configuration **sans mot de passe**, le réseau et les journaux, à joindre à une demande d'aide | relis-le : il contient des noms de réseaux, des adresses IP et MAC |
| **Sauvegarde / restauration** | Outils | exporte et réimporte la configuration (hotspot, forfait, plafonds, mot de passe admin) | le fichier contient le mot de passe du hotspot : garde-le en lieu sûr ; le Wi-Fi 5G et le SSH ne sont pas inclus |
| **Assistant de premier démarrage** | s'ouvre seul | 3 étapes : hotspot, Wi-Fi de la 5G, terminé | — |
| **Chien de garde** | automatique | le Pi redémarre seul s'il se fige ; l'agent est relancé s'il ne répond plus | — |

## Limites à connaître

- **Même puce = même canal.** Le hotspot est calé sur le canal de la connexion 5G ; l'agent le suit automatiquement
  (le hotspot redémarre brièvement si le canal change).
- Si le partage de connexion du téléphone est en **5 GHz sur un canal DFS (52-144)**, le hotspot ne peut pas démarrer.
  Mets le partage en **2,4 GHz** (le plus fiable), ou en 5 GHz sur un canal bas (36-48).
- Pi 3B et Zero 2 W : 2,4 GHz uniquement. Pi 3B+/4/5 : 2,4 et 5 GHz.
- **Ne branche pas le Pi en même temps que la box fibre sur le même réseau** (deux serveurs DHCP, même IP).
- Les débits d'un Wi-Fi unique partagé entre uplink et hotspot sont réduits ; pour de meilleurs résultats, mets les
  appareils fixes sur Ethernet via le switch.
- Pas de pile dans le Pi : au démarrage hors-ligne l'heure peut être fausse jusqu'à la synchronisation (sans effet sur le routage ; la consommation est comptée mais « non datée » jusque-là).
- **Hotspot et uplink sur une seule puce : l'économie d'énergie Wi-Fi doit rester coupée**, sinon la radio s'endort, le hotspot n'émet plus ses balises de façon fiable et les nouveaux appareils ne le voient plus (les appareils déjà connectés restent connectés). L'agent la coupe et le revérifie toutes les 30 s ; NetworkManager est configuré dans le même sens (`91-erouter-wifi.conf`).
- **Le hotspot est en 2,4 GHz** tant que ton iPhone l'est : une seule radio, donc un seul canal pour les deux. Garde le partage en 2,4 GHz (« Maximiser la compatibilité ») : beaucoup d'objets connectés n'ont pas de 5 GHz.
- **Pas de swap sur la carte SD** : Raspberry Pi OS crée par défaut un fichier `/var/swap` (écriture différée du swap compressé) qui empêche le verrouillage en lecture seule et use la carte. EmergencyRouter impose un swap compressé en mémoire seulement (`Mechanism=zram`).

## Architecture

| Service | Droits | Rôle |
|---|---|---|
| `erouter-data` (`scripts/erouter-data`) | root, au démarrage | vérifie, répare ou recrée la partition de données `/data`, puis la monte |
| `erouter-agent` (`agent.py`, `agentd.py`, `lan.py`, `tls.py`, `fs.py`) | root | pont `br0`, `ap0`, hostapd, dnsmasq, nftables ; suit le canal de l'uplink ; surveille Internet ; certificat HTTPS ; verrouillage du système ; seul à écrire la config |
| `erouter-web` (`web.py`, `webd.py`, `static/`) | utilisateur `erouter`, système en lecture seule, seul droit : ports 80/443 | API Flask + interface (sans dépendance externe, fonctionne hors-ligne), servie en HTTPS par gunicorn ; le port 80 redirige vers HTTPS |
| `rpc.py` | | socket local `/run/erouter/agent.sock` (0660 root:erouter) ; l'agent n'expose que 14 méthodes et valide chaque paramètre |

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
- Le système n'est écrit que pendant le démarrage (le temps que tout soit lancé), puis verrouillé ; seule exception : une seconde quand tu changes le mot de passe SSH.
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
make check          # pyflakes + shellcheck + tests unitaires (122)
make ui-test        # l'interface jouée dans un vrai Chrome sans écran (6 scénarios : tableau de bord, outils, assistant, test de débit, économies, mobile)
make integration    # vérifications dans un vrai Debian 13 (Docker, conteneur privilégié) :
                    # vrais disques pour la partition de données (saine, corrompue, détruite), post-traitement d'image,
                    # HTTPS de bout en bout avec gunicorn, pare-feu avec du vrai trafic (compteurs, plafonds mesurés, usurpation),
                    # CAKE sur une vraie interface, test de débit contre un serveur local, blocage des pubs avec un vrai dnsmasq,
                    # QR code relu par un lecteur, diagnostic sans secret, unités systemd, shellcheck
make image          # image flashable
```
La CI GitHub exécute les vérifications statiques et les tests unitaires. Ce qui n'est pas testé automatiquement :
le hotspot et l'uplink Wi-Fi (matériel), le démarrage réel de l'image et le verrouillage du système sur un Pi.

## Feuille de route

- [ ] Image construite automatiquement par GitHub Actions
- [ ] IPv6
- [ ] Mise à jour de l'application sans reflasher
