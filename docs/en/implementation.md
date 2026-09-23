#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/implementation.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/implementation.md)

# TeleScopi Implementation Information

- A single Picamera2 pipeline remains open at all times:
  - the `main` stream (1280x720 by default) is encoded in H.264 by the Pi's hardware and written to a **circular buffer** containing the last few seconds;
  - the `lores` stream (320x180) is analyzed approximately 10 times per second (background subtraction, noise filtering, and ignored brightness changes).
- When motion is triggered, the buffer is flushed to a file and recording continues: the video contains the `PRE_ROLL_SECONDS` (5 s) **before** the motion, followed by `MOTION_VIDEO_DURATION` (30 s) afterward. An annotated photo (with a red bounding box) is sent immediately.
- `/photo` captures an image from the current stream: it is instantaneous, even during a recording. Its resolution is the same as the video stream (`VIDEO_WIDTH` x `VIDEO_HEIGHT`).
- `/video` records for `MANUAL_VIDEO_DURATION` seconds (plus the pre-recording). Only one clip can be recorded at a time: a request made during a recording waits for the previous recording to finish.
- All notifications go through a persistent **outbox** (`CAPTURES_DIR/outbox`): 3 immediate attempts are made for each recipient, after which the item remains on disk (surviving restarts) and is retried every 10 minutes, for each recipient, until delivery. An item is deleted after 10 days or if the disk has less than 30% free space, with the oldest items deleted first. A delayed retried message indicates the actual time of the event. Interactive command responses (e.g. "Taking photo...") are not retried.
- A watchdog terminates the process if the camera stops providing images for 60 seconds, allowing systemd to restart it. The daily ping also indicates the camera status.