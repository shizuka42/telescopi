# Installation de TeleScoPi

Ce guide explique comment installer TeleScoPi sur un Raspberry Pi. Il couvre la préparation du matériel, l'installation du système d'exploitation, la configuration du réseau et l'installation des dépendances nécessaires pour faire fonctionner le projet.

## Pré-requis

Avant de commencer l'installation, assurez-vous de disposer du matériel et des logiciels suivants :

- Un Raspberry Pi (3B ou plus récent)
- Un module caméra compatible avec le Raspberry Pi (Camera Module v1/v2/v3 ou HQ Camera)
- Une carte microSD (16 Go minimum, 32 Go conseillé, classe 10 / A1)
- Une alimentation micro-USB 5V/2.5A pour le Raspberry Pi
- Un ordinateur (Windows/Mac/Linux) avec un lecteur de carte SD, pour préparer la carte
- Une connexion Internet pour le Raspberry Pi et l'ordinateur
- Un compte Telegram pour configurer le bot et recevoir les notifications

## Installation du système d'exploitation

Pour ce projet, nous recommandons d'utiliser Raspberry Pi OS Lite (64-bit). Vous pouvez flasher la carte microSD avec Raspberry Pi Imager en suivant les étapes ci-dessous :

1. Téléchargez et installez Raspberry Pi Imager depuis le site officiel : [raspberrypi.com/software](https://www.raspberrypi.com/software)
2. Lancez l'application Raspberry Pi Imager et insérez votre carte microSD dans le lecteur de carte de votre ordinateur.
3. Cliquez sur "Choisir l'appareil" et sélectionnez "Raspberry Pi 3".
4. Cliquez sur "Choisir le système d'exploitation" et sélectionnez "Raspberry Pi OS (other)" → "Raspberry Pi OS Lite (64-bit)".
5. Cliquez sur "Choisir le stockage" et sélectionnez votre carte microSD.
6. Configurez le nom d'hôte, le réseau Wi-Fi et l'utilisateur selon vos préférences. Dans la suite de cette documentation, le nom d'hôte utilisé sera `homepi` et l'utilisateur admin sera `pi`.
7. Activez la connexion via SSH.
8. Cliquez sur "Écrire" pour flasher la carte microSD avec le système d'exploitation.
9. Une fois l'écriture terminée, retirez proprement la carte microSD de votre ordinateur.

## Initialisation du projet

Après avoir flashé et inséré la carte microSD dans le Raspberry Pi, procédez comme suit pour initialiser le projet :

1. Insérez la carte microSD dans le Raspberry Pi et connectez l'alimentation.
2. Connectez-vous au Raspberry Pi via SSH sur votre réseau local : `ssh pi@homepi.local`.
3. Faire les mises à jour du système en exécutant les commandes suivantes :
   ```bash
   sudo apt update
   sudo apt upgrade -y
   sudo apt full-upgrade -y
   ```
4. Redémarrez le Raspberry Pi pour appliquer les mises à jour : `sudo reboot`
5. Après le redémarrage, reconnectez-vous au Raspberry Pi via SSH et vérifiez que la caméra est détectée correctement : `rpicam-hello --list-cameras` (doit afficher `imx219` pour la Camera Module v2). Si rien n'apparaît, vérifier le branchement du câble ruban (Pi éteint).
6. Créez un répertoire pour le projet TeleScoPi sur le Raspberry Pi : `mkdir -p ~/telescopi`

### Configuration du réseau Wi-Fi

Une connexion internet est nécessaire pour que le projet puisse communiquer avec Telegram et envoyer des notifications. Il faut donc configurer le Raspberry Pi pour qu'il soit connecté à internet, soit via Ethernet, soit via Wi-Fi et qu'il conserve ses authentifications (à configurer lors de l'installation de l'OS Raspberry Pi).

**Si votre réseau wifi local est configuré avec une restriction par liste blanche d'adresse MAC, il faut que le raspberry pi utilise toujours la même adresse.** Si votre réseau local n'a pas de restriction par liste blanche d'adresse MAC, cette étape peut être ignorée.
Pour cela il faut d'abord connaitre son adresse MAC, donc il va falloir désactiver cette limitation sur votre réseau lors de cette initialisation, pour que le raspberry puisse se connecter une première fois, avant de pouvoir la réactiver ensuite.

1. Désactiver temporairement la restriction par liste blanche d'adresse MAC sur votre réseau Wi-Fi.
2. Laissez le Raspberry Pi se connecter à votre réseau Wi-Fi.
3. Connectez-vous au Raspberry Pi via SSH sur votre réseau local : `ssh pi@homepi.local`.
4. Vérifiez l'adresse MAC du Raspberry Pi en exécutant la commande suivante : `ip -br link show wlan0`.
5. Configurez le Raspberry Pi pour utiliser toujours la même adresse MAC : `sudo nano /etc/NetworkManager/conf.d/100-disable-wifi-mac-randomization.conf`
   Ajoutez les lignes suivantes dans le fichier :
   ```ini
   [connection]
    wifi.cloned-mac-address=preserve

    [device]
    wifi.scan-rand-mac-address=no
   ```
6. Appliquez ces modifications : `sudo nmcli general reload conf` puis `sudo systemctl restart NetworkManager`
7. Ajoutez l'adresse MAC du Raspberry Pi à la liste blanche de votre réseau Wi-Fi pour garantir qu'il pourra toujours se connecter.
8. Réactivez la restriction par liste blanche d'adresse MAC sur votre réseau Wi-Fi.

### Configuration sur Telegram

Si la création de bot sur Telegram évolue, veuillez vous référer à la documentation officielle pour obtenir les instructions les plus récentes : https://core.telegram.org/bots#how-do-i-create-a-bot 

1. Créez un bot sur Telegram en interrogeant le BotFather : `@BotFather` et suivez ses instructions pour créer un nouveau bot (commande `/newbot`).
2. Notez le token d'accès fourni par le BotFather après la création du bot. Vous en aurez besoin pour configurer le bot sur le Raspberry Pi.
3. Interrogez le bot `@userinfobot` pour obtenir votre identifiant utilisateur Telegram. Vous en aurez besoin pour configurer le bot sur le Raspberry Pi.

## Installation de TeleScoPi

1. Transférez les fichiers du projet TeleScoPi sur le Raspberry Pi, par exemple en utilisant `scp -r /local/path/to/TeleScoPi pi@homepi.local:/home/pi/telescopi/`
2. Connectez-vous au Raspberry Pi via SSH : `ssh pi@homepi.local`.
3. Accédez au répertoire du projet sur le Raspberry Pi : `cd ~/telescopi`
4. Corrigez le script d'installation si besoin : `dos2unix scripts/telescopi_setup.sh`
5. Donnez les droits d'exécution au script : `chmod +x scripts/telescopi_setup.sh`
6. Exécutez le script d'installation de TeleScoPi et suivez ses instructions : `scripts/telescopi_setup.sh`.
7. Observez les logs du service TeleScoPi pour vérifier que tout démarre correctement : `journalctl -u telescopi -f`.
8. Accédez à l'interface Telegram du bot pour interagir avec TeleScoPi et vérifier que les notifications et commandes fonctionnent correctement.

Si vous le souhaitez, vous pouvez personnaliser le bot Telegram à votre convenance (avatar, menu, description, etc.) en suivant les instructions fournies par le BotFather sur Telegram.
