#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/configuration.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/configuration.md)

# Configuration de TeleScoPi

Toute la configuration de TeleScoPi passe par des variables d'environnement, lues au démarrage du processus `telescopi.py` depuis le fichier `~/telescopi/config/.env` (chargé par le service telescopi).

Les deux variables obligatoires (`BOT_TOKEN` et `ALLOWED_USER_IDS`) sont demandées et écrites dans ce fichier par le script d'installation. Toutes les autres sont optionnelles : si elles sont absentes du fichier, la valeur par défaut indiquée ci-dessous s'applique.

Pour modifier une variable, il faut éditer `~/telescopi/config/.env` en ajoutant/modifiant la ligne souhaitée au format `NOM_VARIABLE=valeur` (par exemple `PING_TIME=10:00`), puis redémarrer le service avec `sudo systemctl daemon-reload && sudo systemctl restart telescopi`.

## Variables obligatoires

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `BOT_TOKEN` | *(aucun, obligatoire)* | jeton d'authentification du bot Telegram, obtenu auprès de [@BotFather](https://t.me/BotFather) | chaîne de caractères fournie par BotFather, ex. `BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `ALLOWED_USER_IDS` | *(aucun, obligatoire)* | identifiants Telegram des utilisateurs autorisés à piloter le bot ; toute personne hors de cette liste est ignorée | un ou plusieurs identifiants numériques séparés par des virgules, sans espace, ex. `ALLOWED_USER_IDS=111111111,222222222` |
| `CAMERA_BACKEND` | `picam` | Définition du type de caméra à utiliser (module caméra de Raspberry Pi ou caméra USB) | `picam` ou `usb` |

## Variables d'environnement optionnelles

### Réglage vidéo et photo

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `VIDEO_WIDTH` | `1280` | largeur (px) de la vidéo **et** des photos (image capturée sur le flux vidéo) | entier positif, ex. `VIDEO_WIDTH=1920` |
| `VIDEO_HEIGHT` | `720` | hauteur (px) de la vidéo et des photos | entier positif, ex. `VIDEO_HEIGHT=1080` |
| `VIDEO_FPS` | `20` | nombre d'images par seconde de la vidéo enregistrée | entier positif, ex. `VIDEO_FPS=25` |
| `VIDEO_BITRATE` | `2000000` | débit de l'encodage H.264, en bits/seconde | entier positif, ex. `VIDEO_BITRATE=4000000` |
| `CAMERA_ROTATE_180` | `1` (activé) | retourne l'image à 180° (caméra montée à l'envers) | booléen : `1`/`true`/`yes`/`on` pour activer, `0`/`false`/`no`/`off` pour désactiver |

### Réglage vidéo pour caméra USB

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `WEBCAM_DEVICE` | `0` | Chemin V4L2 de la caméra de préférence par id (exemple `/dev/v4l/by-id/abc-video0`) ou index numérique de la caméra | ex. `WEBCAM_DEVICE=/dev/v4l/by-id/abc-video0` |
| `WEBCAM_FOURCC` | `MJPG` | Codec vidéo ? | `MJPG` ou `YUYV` |

### Flux en direct (RTSP, backend USB uniquement - voir [Flux en direct](./live-stream.md))

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `LIVE_STREAM_ENABLED` | `0` (désactivé) | active les commandes Telegram `/live_start`/`/live_stop` et le diffuseur RTSP | booléen : `1`/`true`/`yes`/`on` pour activer |
| `LIVE_STREAM_QUALITY` | `reduced` | `full` reprend `VIDEO_WIDTH`/`VIDEO_HEIGHT`/`VIDEO_FPS`/`VIDEO_BITRATE` ; `reduced` utilise les valeurs allégées ci-dessous (surchargeables individuellement) | `full` ou `reduced` |
| `LIVE_STREAM_WIDTH` | `640` (reduced) / `VIDEO_WIDTH` (full) | largeur (px) du flux en direct | entier positif, ex. `LIVE_STREAM_WIDTH=854` |
| `LIVE_STREAM_HEIGHT` | `360` (reduced) / `VIDEO_HEIGHT` (full) | hauteur (px) du flux en direct | entier positif, ex. `LIVE_STREAM_HEIGHT=480` |
| `LIVE_STREAM_FPS` | `10` (reduced) / `VIDEO_FPS` (full) | images par seconde du flux en direct | entier positif, ex. `LIVE_STREAM_FPS=15` |
| `LIVE_STREAM_BITRATE` | `600000` (reduced) / `VIDEO_BITRATE` (full) | débit de l'encodage H.264 du flux en direct, en bits/seconde | entier positif, ex. `LIVE_STREAM_BITRATE=1000000` |
| `LIVE_STREAM_IDLE_TIMEOUT_SECONDS` | `600` | arrête le flux après ce nombre de secondes sans spectateur RTSP | entier positif (secondes), ex. `LIVE_STREAM_IDLE_TIMEOUT_SECONDS=300` |
| `LIVE_STREAM_RTSP_URL` | `rtsp://127.0.0.1:8554/cam` | adresse locale de MediaMTX vers laquelle telescopi diffuse le flux ; à ne jamais exposer directement | URL RTSP |
| `LIVE_STREAM_API_URL` | `http://127.0.0.1:9997` | API de contrôle locale de MediaMTX, utilisée pour détecter l'absence de spectateur | URL HTTP, boucle locale uniquement |
| `LIVE_STREAM_PUBLISH_USER` / `LIVE_STREAM_PUBLISH_PASSWORD` | *(aucun)* | identifiants utilisés par telescopi pour publier vers MediaMTX (doivent correspondre à `mediamtx.yml`) | chaîne, ex. `LIVE_STREAM_PUBLISH_USER=telescopi` |
| `LIVE_STREAM_READ_USER` / `LIVE_STREAM_READ_PASSWORD` | *(aucun)* | identifiants que vos spectateurs RTSP doivent fournir (doivent correspondre à `mediamtx.yml`) ; le nom d'utilisateur est affiché par `/live_start`, le mot de passe ne l'est jamais - votre client RTSP le demande | chaîne |
| `LIVE_STREAM_VIEWER_URL` | *(repli sur `LIVE_STREAM_RTSP_URL`)* | URL envoyée par `/live_start` (les identifiants sont toujours retirés avant l'envoi), en général le nom d'hôte MagicDNS du Pi via Tailscale | URL RTSP, ex. `rtsp://homepi.tailnet-name.ts.net:8554/cam` |

### Détection de mouvement

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `THRESHOLD_DAY` | `500` | taille minimale (en pixels de l'image d'analyse, ~320 px de large) de la zone en mouvement pour déclencher une alerte, de jour | entier positif ; augmenter si trop de fausses alertes de jour, ex. `THRESHOLD_DAY=450` |
| `THRESHOLD_NIGHT` | `100` | idem `THRESHOLD_DAY` mais de nuit (le bruit IR / faible lumière impose souvent un seuil plus élevé) | entier positif, ex. `THRESHOLD_NIGHT=150` |
| `BRIGHTNESS_DAY_NIGHT_THRESHOLD` | `30` | niveau de gris moyen (0-255) de l'image d'analyse en dessous duquel la scène est considérée comme nocturne | entier entre 0 et 255, ex. `BRIGHTNESS_DAY_NIGHT_THRESHOLD=50` |
| `PIXEL_DIFF_THRESHOLD` | `25` | écart de niveau de gris (0-255) à partir duquel un pixel est considéré comme "en mouvement" | entier entre 0 et 255, ex. `PIXEL_DIFF_THRESHOLD=30` |
| `MOTION_CONSECUTIVE_FRAMES` | `2` | nombre d'images consécutives en mouvement nécessaires avant déclenchement (filtre les faux positifs isolés) | entier positif, ex. `MOTION_CONSECUTIVE_FRAMES=3` |
| `DETECTION_FPS` | `10` | fréquence (images/seconde) à laquelle le flux d'analyse est examiné pour détecter le mouvement | entier positif, ex. `DETECTION_FPS=15` |
| `BACKGROUND_ALPHA` | `0.03` | vitesse d'adaptation de l'image de référence aux changements lents de la scène | nombre décimal entre 0 et 1, ex. `BACKGROUND_ALPHA=0.05` |
| `EXPOSURE_JUMP` | `15` | saut de luminosité globale (dû à l'auto-exposition ou à un changement de lumière) au-delà duquel l'image de référence est réinitialisée | nombre décimal, ex. `EXPOSURE_JUMP=20` |
| `MOTION_SNAPSHOT_DRAW_BOX` | `1` (activé) | dessine un cadre rouge autour de la zone en mouvement sur la photo d'alerte envoyée sur Telegram | booléen : `1`/`true`/`yes`/`on` pour activer, `0`/`false`/`no`/`off` pour désactiver |

### Enregistrement des clips

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `PRE_ROLL_SECONDS` | `5` | secondes de vidéo conservées dans le buffer **avant** le déclenchement d'une alerte mouvement | entier positif (secondes), ex. `PRE_ROLL_SECONDS=10` |
| `MOTION_VIDEO_DURATION` | `30` | durée (secondes) enregistrée **après** le déclenchement d'une alerte mouvement | entier positif (secondes), ex. `MOTION_VIDEO_DURATION=45` |
| `MANUAL_VIDEO_DURATION` | `30` | durée (secondes) d'un enregistrement demandé manuellement via `/video` | entier positif (secondes), ex. `MANUAL_VIDEO_DURATION=15` |
| `DELAY_AFTER_MOTION` | `5` | pause de détection (secondes) une fois un clip de mouvement terminé, pour éviter les déclenchements en rafale | entier positif (secondes), ex. `DELAY_AFTER_MOTION=10` |

### Notifications et stockage

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `PING_TIME` | `07:30` | heure du message quotidien "système OK" envoyé sur Telegram, toujours interprétée en heure de Paris (gère automatiquement l'heure d'été/hiver) | heure au format 24h `HH:MM`, ex. `PING_TIME=08:15` |
| `CAPTURES_DIR` | `captures` (relatif au dossier de travail du service) | dossier où sont stockées les photos/vidéos capturées ainsi que l'outbox des envois en attente | chemin de dossier, absolu ou relatif, ex. `CAPTURES_DIR=/home/pi/telescopi_captures` |

### Avancé

| Variable | Défaut | Rôle | Comment la remplir |
|---|---|---|---|
| `LIBCAMERA_LOG_LEVELS` | `*:WARN` | niveau de verbosité des logs de la bibliothèque `libcamera`/`picamera2` sous-jacente (variable technique, à ne modifier qu'en cas de diagnostic matériel) | chaîne au format `libcamera`, ex. `LIBCAMERA_LOG_LEVELS=*:INFO` ou `LIBCAMERA_LOG_LEVELS=*:DEBUG` |

## Conseils

### Réglage de la détection

Chaque activité proche du seuil est notée dans les logs (`Activity level 872 (threshold 150, day)`) : `journalctl -u telescopi -f`.

- si vous avez trop de fausses alertes : montez `THRESHOLD_DAY`/`THRESHOLD_NIGHT` au-dessus des niveaux observés sans mouvement réel, ou `MOTION_CONSECUTIVE_FRAMES=3` ;
- si au contraire vous avez des mouvements non détectés (personne lointaine) : baisser le seuil ;
- fausses alertes uniquement à la tombée de la nuit : ajuster `BRIGHTNESS_DAY_NIGHT_THRESHOLD` à la luminosité où la caméra bascule en infrarouge.