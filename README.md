# HomeCamera - Raspberry project

Projet initial https://github.com/justdaniele/rpisecuritycamerabot modifié pour :

- utiliser des librairies maintenues et stables
- gérer les problèmes de connexion internet
- ajouter un ping quotidien pour vérifier que le système est en ligne
- autoriser plusieurs utilisateurs Telegram

## Installation

### Configuration du Raspberry Pi

Matériel nécessaire :

- Une carte microSD (16 Go minimum, 32 Go conseillé, classe 10 / A1)
- Un module caméra Raspberry Pi compatible (Camera Module v1/v2/v3 ou HQ Camera) — le Pi 3B a un port caméra CSI intégré
- Une alimentation micro-USB 5V/2.5A
- Un ordinateur (Windows/Mac/Linux) avec un lecteur de carte SD, pour préparer la carte

#### Etape 1 : Flasher la carte avec Raspberry Pi Imager

Choix de l'OS pour ce projet : Raspberry Pi OS Lite (64-bit)

- Téléchargez et installe Raspberry Pi Imager depuis le site officiel : raspberrypi.com/software
- Lancez l'appli, insère ta carte microSD dans le lecteur
- Cliquez sur "Choisir l'appareil" → sélectionne "Raspberry Pi 3"
- Cliquez sur "Choisir le système d'exploitation" → "Raspberry Pi OS (other)" → "Raspberry Pi OS Lite (64-bit)"
- Cliquez sur "Choisir le stockage" → sélectionne ta carte SD
- Choisir un nom de host (homepi dans cette doc)
- Configurez le réseau wifi
- Configurez l'utilisateur admin de l'OS (pi dans cette doc)
- Activer la connexion via ssh
- Lancez l'écriture sur la carte

#### Etape 2 : Initilisation du projet

Si votre réseau wifi local est configuré avec une restriction par liste blanche d'adresse MAC, il faut que le raspberry pi utilise toujours la même adresse.
Pour cela il faut d'abord connaitre son adresse MAC, donc il va falloir désactiver cette limitation sur votre réseau lors de cette initialisation, pour que le raspberry puisse se connecter une première fois, avant de pouvoir la réactiver ensuite.

- Insérez la carte dans le raspberry et branchez le. Attendez quelques minutes qu'il ait démarré et soit connecté au wifi.
- Depuis votre ordinateur (sur le même réseau WiFi), connectez-vous en SSH : `ssh utilisateur_admin@homepi.local`
- Mise à jour du système : `sudo apt update && sudo apt full-upgrade -y`
- Activation de la caméra : `sudo raspi-config` > Interface Options > Camera
- Redémmarez le raspberry : `sudo reboot`
- Après redémarrage, se reconnecter en ssh puis vérifier les caméras disponibles : `rpicam-hello --list-cameras`

Si votre réseau wifi local est configuré avec une restriction par liste blanche d'adresse MAC, il faut que le raspberry pi utilise toujours la même adresse :

- vérifier la vraie adresse MAC avec `ip -br link show wlan0`
- `sudo nano /etc/NetworkManager/conf.d/100-disable-wifi-mac-randomization.conf`
- Collez ce contenu dans le fichier
```
[connection]
wifi.cloned-mac-address=preserve

[device]
wifi.scan-rand-mac-address=no
```
- `sudo nmcli general reload conf` puis `sudo systemctl restart NetworkManager`
- ajoutez l'adresse MAC dans la liste blanche de votre réseau et réactivez les restrictions du réseau

#### Mises à jour du système

Les mises à jour du raspberry pi ne sont PAS automatiques. Il faut donc régulièrement faire les mises à jour manuellement via ssh :

```
sudo apt update && sudo apt upgrade -y
sudo apt full-upgrade -y
```

### Configuration avec Telegram

Pour initialiser un token pour le bot, il faut utiliser Telegram :

1. Ouvrez Telegram et recherchez le bot nommé "BotFather".
2. Démarrez une conversation avec BotFather et utilisez la commande `/newbot` pour créer un nouveau bot.
3. Suivez les instructions pour donner un nom et un identifiant unique à votre bot.
4. BotFather vous fournira un token d'accès que vous devrez copier.
5. Utilisez ce token pour initialiser la variable d'environnement `BOT_TOKEN` comme indiqué dans la section d'installation.

Pour récupérer son identifiant utilisateur Telegram, il faut utiliser le bot nommé "userinfobot" et suivre les instructions pour obtenir votre ID.
Ensuite, utilisez cet ID pour compléter la variable d'environnement `ALLOWED_USER_IDS` (plusieurs ids autorisés si séparés par une virgule) comme indiqué dans la section d'installation.

### Connexion internet

Une connexion internet est nécessaire pour que le script puisse communiquer avec Telegram et envoyer des notifications. Il faut donc configurer le Raspberry Pi pour qu'il soit connecté à internet, soit via Ethernet, soit via Wi-Fi et qu'il conserve ses authentifications (à configurer lors de l'installation de l'OS Raspberry Pi).

### Configuration du service

Envoyer des fichiers via ssh (exemple) : `scp file.txt remote_username@10.10.0.2:/remote/directory`

Pour que le script soit démarré automatiquement avec les bonnes informations, il faut créer un service systemd.

Il faut d'abord créer le fichiers des variables d'environnement (ne jamais le mettre en dur dans le code) :

```
touch /home/pi/homecamera/.env
chmod 600 /home/pi/homecamera/.env
chown pi:pi /home/pi/homecamera/.env
## éditer le fichier pour ajouter les variables d'environnement
## Exemple de contenu du fichier .env :
# BOT_TOKEN=votre_token_ici
# ALLOWED_USER_IDS=111111,222222
# PING_TIME=08:30 #si besoin de changer l'heure de la vérification quotidienne
# CAPTURES_DIR=/chemin/vers/le/dossier/des/captures #si besoin de changer le dossier des captures
# THRESHOLD_DAY=100 #seuil de détection de mouvement de jour
# THRESHOLD_NIGHT=2000 #seuil de détection de mouvement de nuit (souvent plus haut, bruit IR/faible lumière)
# BRIGHTNESS_DAY_NIGHT_THRESHOLD=40 #luminosité moyenne (0-255) en dessous de laquelle c'est considéré comme la nuit
## puis redémarrer le service systemd pour prendre en compte les nouvelles variables d'environnement si le service existe déjà
# sudo systemctl daemon-reload && sudo systemctl restart homecamera
```

Puis installer le service systemd :

```
sudo apt install ffmpeg -y
sudo apt install python3-pip -y
cd /home/pi/homecamera
# création du venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo cp homecamera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homecamera
# sudo systemctl start homecamera #si le now n'est pas dispo
```

### Rétention des logs

Pour s'assurer que les logs du service sont stockés et que l'on ne garde des logs que des 7 derniers jours (car l'OS du Raspberry Pi ne les stocke pas par défaut).

```
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo mkdir -p /etc/systemd/journald.conf.d
sudo nano /etc/systemd/journald.conf.d/homecamera-retention.conf  #y mettre le contenu ci-dessous
sudo systemctl restart systemd-journald
sudo journalctl --flush
sudo journalctl --rotate
```

Contenu du fichier homecamera-retention.conf :

```
[Journal]
Storage=persistent
MaxRetentionSec=7day
SystemMaxUse=200M
```

Pour copier quotidiennement ces logs dans un répertoire plus facile d'accès :

```
sudo mkdir -p /home/pi/homecamera/logs
sudo crontab -e
# 55 23 * * * journalctl -u homecamera --since "00:00" --until "23:59" > /home/pi/homecamera/logs/homecamera-$(date +\%F).log 2>&1 && find /home/pi/homecamera/logs -name "homecamera-*.log" -mtime +7 -delete
```

Pour parcourir les logs stockés du service :

```
# Tous les logs du service homecamera
journalctl -u homecamera

# Suivre en direct (comme tail -f)
journalctl -u homecamera -f

# Les 50 dernières lignes
journalctl -u homecamera -n 50

# Depuis le dernier démarrage du service
journalctl -u homecamera -b
```
