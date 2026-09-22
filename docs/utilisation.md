# Utilisation de TeleScoPi

Pour utiliser TeleScoPi, assurez-vous d'avoir correctement installé et configuré le service comme décrit dans la [documentation d'installation](./installation.md) et la [documentation de configuration](./configuration.md).

## Commandes sur le bot Telegram

Voici les commandes disponibles sur le bot Telegram pour interagir avec TeleScoPi :

- `/help` : Obtenir la liste des commandes disponibles et le statut actuel du service TeleScoPi.
- `/photo` : Prendre une photo avec la caméra.
- `/video` : Enregistrer une vidéo avec la caméra (durée définie par la configuration du service).
- `start_motion` : Activer la détection de mouvement avec la caméra.
- `/stop_motion` : Désactiver la détection de mouvement avec la caméra.

## Accès aux logs

Les logs du service TeleScoPi sont conservés pendant 7 jours et sont copiés quotidiennement dans le répertoire `~/telescopi/logs` pour un accès plus facile.
Pour parcourir les logs stockés du service (en étant connecté en ssh sur le Raspberry Pi) :

```
# Tous les logs du service telescopi
journalctl -u telescopi

# Suivre en direct (comme tail -f)
journalctl -u telescopi -f

# Les 50 dernières lignes
journalctl -u telescopi -n 50

# Depuis le dernier démarrage du service
journalctl -u telescopi -b
```
