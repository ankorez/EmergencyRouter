# Sécurité

## Modèle de menace

EmergencyRouter est un routeur domestique de secours. Il protège contre :
- un appareil du réseau local (ou le Wi-Fi du voisin) qui chercherait à administrer le routeur ;
- un appareil du réseau de la 5G (le téléphone, ou quiconque y est connecté) qui chercherait à atteindre le Pi ;
- une faille dans l'interface web, qui ne doit pas donner le contrôle du système ;
- une coupure de courant qui abîmerait la carte SD.

Il ne protège pas contre un attaquant qui a un accès physique au Pi (carte SD, port série).

## Mesures

| Sujet | Mesure |
|---|---|
| Pare-feu | nftables, politique « drop » : rien n'est joignable depuis l'uplink (5G) ; depuis le LAN seulement DHCP, DNS, mDNS, web et SSH (SSH limité en débit) ; anti-usurpation d'adresse source |
| Privilèges | l'interface web tourne sous un utilisateur dédié, sans droits (seul le port 80 est autorisé), avec un système de fichiers en lecture seule et un filtre d'appels système |
| Agent root | n'écoute que sur un socket Unix (0660 `root:erouter`), expose 7 méthodes, valide chaque paramètre ; aucune commande n'est lancée via un shell |
| Configuration | le web ne peut pas écrire le fichier : il demande à l'agent, qui nettoie, valide (anti-injection dans dnsmasq/hostapd) et écrit ; la config est revalidée à chaque lecture |
| HTTPS | certificat auto-signé EC P-256 créé par l'agent, clé privée `640 root:erouter`, TLS 1.2 minimum, redirection du port 80, cookie de session `__Host-` + `Secure` ; l'empreinte du certificat est affichée dans l'interface |
| Web | en-têtes CSP stricte (aucun script ni style en ligne), `X-Frame-Options`, `nosniff`, sessions `SameSite=Strict`, contrôle du `Host` (anti DNS-rebinding) et de l'`Origin` (anti CSRF), 5 essais de connexion par minute, hash de mot de passe scrypt, sessions invalidées au changement de mot de passe, taille des requêtes limitée, aucune version de serveur annoncée |
| SSH | **éteint par défaut**, activable depuis l'interface avec un mot de passe choisi par l'utilisateur (12 caractères minimum, mots de passe prévisibles refusés, transmis à `chpasswd` par l'entrée standard) ; uniquement depuis le réseau du routeur, limité en débit ; `root` et les comptes système jamais modifiables, 3 essais, pas de redirection de ports ni d'agent |
| Carte SD | système en lecture seule une fois démarré, données sur une partition séparée vérifiée/réparée/recréée à chaque démarrage (voir README), journaux en RAM |
| Chaîne d'approvisionnement | uniquement des paquets Debian signés (aucun `pip`) ; `pi-gen` épinglé sur un commit précis ; actions GitHub épinglées par empreinte ; image publiée avec son SHA-256 |

## Limites connues

- **Certificat auto-signé.** Le navigateur avertit à la première visite : compare l'empreinte SHA-256 affichée en bas de l'interface avec celle du certificat présenté. Sans cette vérification, un appareil malveillant déjà présent sur le LAN pourrait se faire passer pour le routeur à ce moment-là (risque de « première confiance »).
- **Partition de données.** Elle contient la config et le certificat en clair, sans chiffrement (mêmes droits que la config : `root` et le groupe `erouter`). Quiconque a la carte physiquement y a accès.
- **Mot de passe par défaut du hotspot de configuration** (`erouter-setup`) dans l'image : connu de tous. À changer dès le premier accès (l'interface le rappelle). Le premier visiteur de l'interface choisit le mot de passe admin.
- **Mot de passe Wi-Fi visible** dans l'interface (nécessaire pour le reproduire sur le même SSID que la box) et stocké en clair dans `/etc/erouter/config.yaml` (lisible par `root` et le groupe `erouter` uniquement).
- **Mot de passe du Wi-Fi 5G passé à `nmcli` en argument** : visible brièvement dans la liste des processus, donc seulement par un utilisateur local du Pi.
- **mDNS (Avahi)** : `erouter.local` peut être annoncé sur le réseau de la 5G ; il n'ouvre aucun service (le pare-feu bloque tout).
- **SSH par mot de passe** : un mot de passe peut être deviné. Choisis-en un long ; l'accès n'est possible que depuis le hotspot ou l'Ethernet du routeur (le hotspot de configuration a un mot de passe public tant que tu ne l'as pas changé : change-le avant d'activer le SSH). `sudo` y utilise ce même mot de passe.
- **Fenêtre d'écriture** : changer le mot de passe SSH écrit le système pendant une seconde ; une coupure de courant à cet instant précis peut l'abîmer.

## Signaler une faille

Ouvre une « Security advisory » privée sur le dépôt GitHub (onglet *Security*), ou écris au propriétaire du dépôt. Merci de ne pas publier de détails avant correction.
