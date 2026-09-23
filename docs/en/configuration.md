#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/configuration.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/configuration.md)

# TeleScoPi Configuration

All TeleScoPi configuration is handled through environment variables, which are read when the `telescopi.py` process starts from the `~/telescopi/config/.env` file (loaded by the telescopi service).

The two required variables (`BOT_TOKEN` and `ALLOWED_USER_IDS`) are requested and written to this file by the installation script. All the other variables are optional: if they are not present in the file, the default value specified below applies.

To modify a variable, edit `~/telescopi/config/.env` by adding or modifying the desired line using the `VARIABLE_NAME=value` format (for example, `PING_TIME=10:00`), then restart the service with `sudo systemctl daemon-reload && sudo systemctl restart telescopi`.

## Required Variables

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `BOT_TOKEN` | *(none, required)* | Telegram bot authentication token, obtained from https://t.me/BotFather | character string provided by BotFather, e.g. `BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `ALLOWED_USER_IDS` | *(none, required)* | Telegram IDs of the users authorized to control the bot; anyone not on this list is ignored | one or more numeric IDs separated by commas, without spaces, e.g. `ALLOWED_USER_IDS=111111111,222222222` |
| `CAMERA_BACKEND` | `picam` | Camera backend to use: `picam` for Raspberry Pi camera module, `usb` for USB webcam | `picam` or `usb` |

## Optional Environment Variables

### Video and Photo Settings

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `VIDEO_WIDTH` | `1280` | width (px) of the video **and** photos (image captured from the video stream) | positive integer, e.g. `VIDEO_WIDTH=1920` |
| `VIDEO_HEIGHT` | `720` | height (px) of the video and photos | positive integer, e.g. `VIDEO_HEIGHT=1080` |
| `VIDEO_FPS` | `20` | number of frames per second in the recorded video | positive integer, e.g. `VIDEO_FPS=25` |
| `VIDEO_BITRATE` | `2000000` | H.264 encoding bitrate, in bits per second | positive integer, e.g. `VIDEO_BITRATE=4000000` |
| `CAMERA_ROTATE_180` | `1` (enabled) | rotates the image by 180° (camera mounted upside down) | Boolean: `1`/`true`/`yes`/`on` to enable, `0`/`false`/`no`/`off` to disable |

### USB Webcam Settings

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `WEBCAM_DEVICE` | `0` | V4L2 path of the camera, preferably by id (e.g., `/dev/v4l/by-id/abc-video0`) or the numeric index of the camera | ex. `WEBCAM_DEVICE=/dev/v4l/by-id/abc-video0` |
| `WEBCAM_FOURCC` | `MJPG` | video codec ? | `MJPG` ou `YUYV` |

### Motion Detection

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `THRESHOLD_DAY` | `500` | minimum size (in pixels of the analysis image, approximately 320 px wide) of the moving area required to trigger an alert during the day | positive integer; increase it if there are too many false alerts during the day, e.g. `THRESHOLD_DAY=450` |
| `THRESHOLD_NIGHT` | `100` | same as `THRESHOLD_DAY`, but at night (IR noise / low light often requires a higher threshold) | positive integer, e.g. `THRESHOLD_NIGHT=150` |
| `BRIGHTNESS_DAY_NIGHT_THRESHOLD` | `30` | average grayscale level (0-255) of the analysis image below which the scene is considered to be at night | integer between 0 and 255, e.g. `BRIGHTNESS_DAY_NIGHT_THRESHOLD=50` |
| `PIXEL_DIFF_THRESHOLD` | `25` | grayscale difference (0-255) above which a pixel is considered to be "in motion" | integer between 0 and 255, e.g. `PIXEL_DIFF_THRESHOLD=30` |
| `MOTION_CONSECUTIVE_FRAMES` | `2` | number of consecutive frames containing motion required before triggering an alert (filters out isolated false positives) | positive integer, e.g. `MOTION_CONSECUTIVE_FRAMES=3` |
| `DETECTION_FPS` | `10` | frequency (frames per second) at which the analysis stream is examined to detect motion | positive integer, e.g. `DETECTION_FPS=15` |
| `BACKGROUND_ALPHA` | `0.03` | speed at which the reference image adapts to slow changes in the scene | decimal number between 0 and 1, e.g. `BACKGROUND_ALPHA=0.05` |
| `EXPOSURE_JUMP` | `15` | global brightness change (caused by auto-exposure or a change in lighting) above which the reference image is reset | decimal number, e.g. `EXPOSURE_JUMP=20` |
| `MOTION_SNAPSHOT_DRAW_BOX` | `1` (enabled) | draws a red box around the moving area in the alert photo sent via Telegram | Boolean: `1`/`true`/`yes`/`on` to enable, `0`/`false`/`no`/`off` to disable |

### Clip Recording

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `PRE_ROLL_SECONDS` | `5` | seconds of video retained in the buffer **before** a motion alert is triggered | positive integer (seconds), e.g. `PRE_ROLL_SECONDS=10` |
| `MOTION_VIDEO_DURATION` | `30` | duration (seconds) recorded **after** a motion alert is triggered | positive integer (seconds), e.g. `MOTION_VIDEO_DURATION=45` |
| `MANUAL_VIDEO_DURATION` | `30` | duration (seconds) of a recording requested manually via `/video` | positive integer (seconds), e.g. `MANUAL_VIDEO_DURATION=15` |
| `DELAY_AFTER_MOTION` | `5` | detection pause (seconds) after a motion clip has finished, to prevent alerts from being triggered in rapid succession | positive integer (seconds), e.g. `DELAY_AFTER_MOTION=10` |

### Notifications and Storage

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `PING_TIME` | `07:30` | time of the daily "system OK" message sent via Telegram, always interpreted as Paris time (automatically handles daylight saving time) | time in 24-hour `HH:MM` format, e.g. `PING_TIME=08:15` |
| `CAPTURES_DIR` | `captures` (relative to the service's working directory) | directory where captured photos/videos and the outbox containing pending deliveries are stored | absolute or relative directory path, e.g. `CAPTURES_DIR=/home/pi/telescopi_captures` |

### Advanced

| Variable | Default | Purpose | How to set it |
|---|---|---|---|
| `LIBCAMERA_LOG_LEVELS` | `*:WARN` | log verbosity level of the underlying `libcamera`/`picamera2` library (technical variable, to be modified only for hardware diagnostics) | string in `libcamera` format, e.g. `LIBCAMERA_LOG_LEVELS=*:INFO` or `LIBCAMERA_LOG_LEVELS=*:DEBUG` |

## Tips

### Detection Settings

Each activity level close to the threshold is recorded in the logs (`Activity level 872 (threshold 150, day)`): `journalctl -u telescopi -f`.

- if you receive too many false alerts: increase `THRESHOLD_DAY`/`THRESHOLD_NIGHT` above the levels observed when there is no actual motion, or set `MOTION_CONSECUTIVE_FRAMES=3`;
- if, on the other hand, some movements are not detected (a person in the distance): lower the threshold;
- if false alerts occur only at nightfall: adjust `BRIGHTNESS_DAY_NIGHT_THRESHOLD` to match the brightness level at which the camera switches to infrared mode.