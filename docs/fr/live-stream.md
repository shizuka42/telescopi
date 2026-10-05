#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/live-stream.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/live-stream.md)

# Flux vidéo en direct (optionnel, `CAMERA_BACKEND=usb` uniquement)

TeleScoPi peut diffuser un flux RTSP de la caméra à la demande, démarré et arrêté depuis Telegram avec `/live_start` et `/live_stop` (voir [Utilisation](./utilisation.md)). Cette fonctionnalité est **désactivée par défaut** et uniquement prise en charge avec le backend webcam USB.

**À utiliser uniquement via un VPN.** Le flux ne dispose d'aucun chiffrement TLS natif ; il ne doit jamais être exposé directement sur internet (pas de redirection de port sur le routeur). Ce guide utilise [Tailscale](https://tailscale.com) (VPN maillé gratuit basé sur WireGuard) afin que le Raspberry Pi et vos appareils de visionnage (téléphone, ordinateur portable) rejoignent un réseau privé, le port RTSP n'étant accessible qu'à l'intérieur de ce tunnel.

> `scripts/telescopi_setup.sh` automatise les étapes 1 à 4 ci-dessous (installation de Tailscale, installation/configuration de MediaMTX, règle de pare-feu, variables `.env`) si vous répondez "y" à la question sur le flux en direct lors d'une installation en `CAMERA_BACKEND=usb`. Ces étapes restent documentées ici pour référence, pour une exécution manuelle, ou pour configurer les appareils de visionnage (étape 1, deuxième partie).

## Architecture

- [MediaMTX](https://github.com/bluenviron/mediamtx), un serveur RTSP autonome et léger, tourne comme son propre service systemd sur le Raspberry Pi, lié à `127.0.0.1:8554`/`127.0.0.1:9997` ainsi qu'à toute interface autorisée par votre pare-feu (l'interface Tailscale, `tailscale0`).
- `telescopi.py` encode le flux de la webcam USB avec `ffmpeg` et le transmet à MediaMTX uniquement pendant qu'un flux est actif (`/live_start`) ; il s'arrête automatiquement après `LIVE_STREAM_IDLE_TIMEOUT_SECONDS` sans spectateur, ou via `/live_stop`.
- Votre client RTSP (VLC, Home Assistant, etc.) se connecte à MediaMTX via le réseau Tailscale.

## 1. Installer Tailscale

Sur le Raspberry Pi, connecté en SSH :

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Suivez l'URL affichée pour authentifier le Pi sur votre tailnet. Son adresse IP Tailscale reste stable à travers les redémarrages (le vôtre et celui de votre box internet), puisqu'elle ne dépend pas de votre adresse LAN/WAN ; elle ne change que si le nœud est supprimé, réinitialisé ou réinstallé. Préférez son nom d'hôte MagicDNS à l'IP brute - plus lisible, et il continue de fonctionner même dans le cas rare où l'IP est réattribuée :

```bash
tailscale status --self
```

La première colonne de votre propre ligne est l'IP Tailscale, la deuxième est le nom d'hôte MagicDNS (ex. `homepi`, utilisable sous la forme `homepi.<votre-tailnet>.ts.net` depuis n'importe quel appareil du tailnet).

Installez Tailscale sur chaque appareil que vous utiliserez pour regarder le flux (téléphone, ordinateur) via les applications officielles : https://tailscale.com/download, et connectez-vous au même tailnet.

### Restreindre l'accès au tailnet au seul flux (ACL)

Par défaut, la politique d'un nouveau tailnet autorise chaque appareil à joindre tout autre appareil sur tous les ports - cela exposerait SSH et tout ce qui tourne sur le Pi à chaque appareil du tailnet. Les tags sont réservés aux **appareils de service** (ne jamais tagger téléphone/ordinateur portable - cela remplace leur identité utilisateur et casse le SSH Tailscale vers leurs propres appareils, selon les recommandations de Tailscale elles-mêmes). Taggez uniquement le Pi, et restreignez l'ACL au port RTSP :

1. Ouvrez la page [Access Controls](https://login.tailscale.com/admin/acl/file) de la console d'administration.
2. Définissez un tag pour le Pi et une règle ACL qui n'autorise que le port TCP `8554` vers celui-ci, en remplaçant `you@example.com` par votre identifiant Tailscale :
   ```json
   {
     "tagOwners": {
       "tag:pi": ["you@example.com"],
     },
     "acls": [
       {
         "action": "accept",
         "src":    ["autogroup:member"],
         "proto":  "tcp",
         "dst":    ["tag:pi:8554"],
       },
     ],
   }
   ```
   Sans autre entrée `acls`, tout autre port (y compris SSH, 22) devient inaccessible depuis le reste du tailnet - Tailscale refuse par défaut, seules les règles `accept` explicites ouvrent un accès.
3. Appliquez le tag sur le Pi (nécessite une ré-authentification) :
   ```bash
   sudo tailscale up --advertise-tags=tag:pi --force-reauth
   ```
4. Vérifiez : depuis votre téléphone/ordinateur, la connexion à `<ip-tailscale-du-pi>:8554` fonctionne, mais `ssh <ip-tailscale-du-pi>` ou tout autre port expire sans réponse.

Ceci restreint uniquement l'accès **via le tailnet** ; cela n'affecte pas l'accessibilité SSH sur votre réseau local (protégez le Pi séparément par pare-feu si cela doit aussi être verrouillé).

## 2. Installer MediaMTX

1. Sur le Raspberry Pi, récupérez la dernière version et téléchargez l'archive `linux_arm64` depuis la [page des Releases](https://github.com/bluenviron/mediamtx/releases) (Raspberry Pi OS Lite 64 bits = `arm64`), par exemple :
   ```bash
   cd /tmp
   curl -LO https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep -oP '"tag_name": "\K[^"]+')_linux_arm64.tar.gz
   tar xzf mediamtx_*_linux_arm64.tar.gz
   sudo mv mediamtx /usr/local/bin/mediamtx
   sudo chmod +x /usr/local/bin/mediamtx
   ```
2. Générez deux mots de passe aléatoires forts et distincts (un pour la publication par telescopi, un pour la lecture par vos spectateurs RTSP), par exemple :
   ```bash
   openssl rand -base64 24   # à exécuter deux fois, conservez les deux résultats
   ```
3. Copiez [config/mediamtx.yml.template](../../config/mediamtx.yml.template) vers `/usr/local/etc/mediamtx.yml` et remplacez `${LIVE_STREAM_PUBLISH_USER}`, `${LIVE_STREAM_PUBLISH_PASSWORD}`, `${LIVE_STREAM_READ_USER}`, `${LIVE_STREAM_READ_PASSWORD}` par vos propres valeurs (les noms d'utilisateur publish/read peuvent être n'importe quoi, par ex. `telescopi`/`viewer`).
4. Copiez [config/mediamtx.service.template](../../config/mediamtx.service.template) vers `/etc/systemd/system/mediamtx.service`, en remplaçant `${USER}` par votre nom d'utilisateur Raspberry Pi.
5. Activez et démarrez-le :
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now mediamtx
   sudo systemctl status mediamtx
   ```

## 3. Restreindre l'accès réseau (défense en profondeur)

Même si le flux est destiné à être accessible via Tailscale, MediaMTX écoute par défaut sur toutes les interfaces. Si vous utilisez `ufw`, restreignez le port RTSP à l'interface Tailscale et à la boucle locale uniquement :

```bash
sudo ufw allow in on tailscale0 to any port 8554 proto tcp
sudo ufw deny 8554/tcp
```

## 4. Configurer TeleScoPi

Modifiez `~/telescopi/config/.env` et ajoutez (en reprenant les identifiants définis dans `mediamtx.yml`) :

```bash
LIVE_STREAM_ENABLED=1
LIVE_STREAM_QUALITY=reduced   # ou "full" ; voir docs/fr/configuration.md
LIVE_STREAM_PUBLISH_USER=telescopi
LIVE_STREAM_PUBLISH_PASSWORD=<mot de passe publish généré ci-dessus>
LIVE_STREAM_READ_USER=viewer
LIVE_STREAM_READ_PASSWORD=<mot de passe read généré ci-dessus>
LIVE_STREAM_VIEWER_URL=rtsp://<nom-hote-du-pi>.<votre-tailnet>.ts.net:8554/cam
```

Redémarrez le service : `sudo systemctl daemon-reload && sudo systemctl restart telescopi`.

Voir [Configuration](./configuration.md) pour la liste complète des variables `LIVE_STREAM_*` et leurs valeurs par défaut.

## 5. Regarder le flux

1. Sur Telegram, envoyez `/live_start`. Le bot répond avec l'URL RTSP et le nom d'utilisateur en lecture ; il n'envoie jamais le mot de passe sur Telegram.
2. Ouvrez l'URL dans VLC (`Média` → `Ouvrir un flux réseau`) ou tout autre client/NVR compatible RTSP, depuis un appareil connecté au même tailnet. Le client demandera le nom d'utilisateur/mot de passe (MediaMTX répond `401` tant qu'ils ne sont pas fournis) ; saisissez les valeurs `LIVE_STREAM_READ_USER`/`LIVE_STREAM_READ_PASSWORD` depuis `~/telescopi/config/.env`.
3. Envoyez `/live_stop` une fois terminé, ou laissez l'arrêt automatique se déclencher après `LIVE_STREAM_IDLE_TIMEOUT_SECONDS` sans spectateur.

## Compromis sur les ressources

Sur un Raspberry Pi 3B, un flux en direct actif en même temps qu'un enregistrement (mouvement ou manuel) signifie **deux** encodages logiciels H.264 simultanés. `LIVE_STREAM_QUALITY=reduced` (par défaut) maintient le flux en direct à une résolution/fréquence/débit plus faible que les enregistrements afin de limiter cette charge CPU supplémentaire ; ne passez à `full` que si vous avez vérifié que le Pi supporte les deux charges en même temps.
