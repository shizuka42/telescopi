#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/implementation.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/implementation.md)

# Informations sur l'implémentation de TeleScopi

- Une seule pipeline Picamera2 reste ouverte en permanence :
  - flux `main` (1280x720 par défaut) encodé en H.264 par le matériel du Pi, écrit dans un **tampon circulaire** de quelques secondes ;
  - flux `lores` (320x180) analysé environ 10 fois par seconde (soustraction de fond, filtrage du bruit, changement de luminosité ignoré).
- Au déclenchement, le tampon est vidé dans un fichier et l'enregistrement continue : la vidéo contient les `PRE_ROLL_SECONDS` (5 s) **avant** le mouvement puis `MOTION_VIDEO_DURATION` (30 s) après. Une photo annotée (cadre rouge) est envoyée immédiatement.
- `/photo` capture une image du flux en cours : instantané, même pendant un enregistrement. Sa résolution est celle du flux vidéo (`VIDEO_WIDTH` x `VIDEO_HEIGHT`).
- `/video` enregistre `MANUAL_VIDEO_DURATION` secondes (plus le pré-enregistrement). Un seul clip à la fois : une demande pendant un enregistrement attend la fin du précédent.
- Toutes les notifications passent par une **outbox** persistante (`CAPTURES_DIR/outbox`) : 3 essais immédiats par destinataire, puis l'élément reste sur disque (il survit aux redémarrages) et est rejoué toutes les 10 minutes, par destinataire, jusqu'à livraison. Un élément est supprimé après 10 jours ou si le disque a moins de 30 % d'espace libre (plus anciens d'abord). Un message rejoué en retard indique l'heure réelle de l'événement. Les réponses interactives aux commandes (ex. "Taking photo...") ne sont pas rejouées.
- Un watchdog quitte le processus si la caméra ne fournit plus d'images pendant 60 s, pour que systemd le redémarre. Le ping quotidien indique aussi l'état de la caméra.
