"""Configuration (env vars), logging setup and process-wide constants.

Set these as real environment variables (never hardcode secrets in source).
"""
import logging
import os
import sys
import threading
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARN")  # must be set before importing picamera2 (CAMERA_BACKEND=picam only)


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.environ["BOT_TOKEN"]
# Comma-separated Telegram user IDs allowed to control the bot, e.g. "111111,222222"
ALLOWED_USER_IDS = sorted({int(uid) for uid in os.environ["ALLOWED_USER_IDS"].split(",") if uid.strip()})

TZ = ZoneInfo("Europe/Paris")

# Video / camera
VIDEO_WIDTH = _env_int("VIDEO_WIDTH", 1280)    # also the resolution of photos (grabbed from the video stream)
VIDEO_HEIGHT = _env_int("VIDEO_HEIGHT", 720)
VIDEO_FPS = _env_int("VIDEO_FPS", 20)
VIDEO_BITRATE = _env_int("VIDEO_BITRATE", 2_000_000)
CAMERA_ROTATE_180 = _env_bool("CAMERA_ROTATE_180", True)  # camera mounted upside down (old --hflip --vflip)

# Camera backend: "picam" = Pi NoIR/HQ camera module via Picamera2 (default), "usb" = USB webcam via OpenCV/V4L2.
CAMERA_BACKEND = os.environ.get("CAMERA_BACKEND", "picam").strip().lower()
if CAMERA_BACKEND not in ("picam", "usb"):
    raise SystemExit(f"Invalid CAMERA_BACKEND={CAMERA_BACKEND!r}: expected 'picam' or 'usb'")
# USB webcam only. WEBCAM_DEVICE accepts a V4L2 path ("/dev/video0") or a numeric index ("0").
# Defaults suit a Logitech C310, which natively does MJPG at 1280x720/30fps (YUYV is much slower).
WEBCAM_DEVICE = os.environ.get("WEBCAM_DEVICE", "0")
WEBCAM_FOURCC = os.environ.get("WEBCAM_FOURCC", "MJPG")

PRE_ROLL_SECONDS = _env_int("PRE_ROLL_SECONDS", 5)         # video kept BEFORE the trigger
MOTION_VIDEO_DURATION = _env_int("MOTION_VIDEO_DURATION", 30)   # seconds recorded AFTER the trigger
MANUAL_VIDEO_DURATION = _env_int("MANUAL_VIDEO_DURATION", 30)
DELAY_AFTER_MOTION = _env_int("DELAY_AFTER_MOTION", 5)     # cool-down once a motion clip is finished

# Motion analysis (runs on the small "lores" stream). Areas are in pixels of that analysis frame.
LORES_WIDTH = 320
LORES_HEIGHT = int(round(LORES_WIDTH * VIDEO_HEIGHT / VIDEO_WIDTH / 2)) * 2  # even, same aspect as main
THRESHOLD_DAY = _env_int("THRESHOLD_DAY", 500)     # min size (px of the 320-wide frame) of the moving blob, daylight
THRESHOLD_NIGHT = _env_int("THRESHOLD_NIGHT", 100)  # same at night (IR / low light noise -> higher)
# Mean gray level (0-255) of the analysis frame below which the scene is considered "night".
BRIGHTNESS_DAY_NIGHT_THRESHOLD = _env_int("BRIGHTNESS_DAY_NIGHT_THRESHOLD", 30)
PIXEL_DIFF_THRESHOLD = _env_int("PIXEL_DIFF_THRESHOLD", 25)  # gray-level change for a pixel to count as "moving"
MOTION_CONSECUTIVE_FRAMES = _env_int("MOTION_CONSECUTIVE_FRAMES", 2)  # filters one-frame glitches
DETECTION_FPS = _env_int("DETECTION_FPS", 10)
BACKGROUND_ALPHA = _env_float("BACKGROUND_ALPHA", 0.03)  # how fast the reference image adapts to slow changes
EXPOSURE_JUMP = _env_float("EXPOSURE_JUMP", 15)  # global brightness jump (auto-exposure, lights) -> reset reference
MOTION_SNAPSHOT_DRAW_BOX = _env_bool("MOTION_SNAPSHOT_DRAW_BOX", True)  # red box around the moving area

# Daily "system OK" ping, "HH:MM" 24h format, always interpreted in Europe/Paris time (handles DST).
_ping_hour, _ping_minute = (int(part) for part in os.environ.get("PING_TIME", "07:30").split(":"))
PING_TIME = dt_time(hour=_ping_hour, minute=_ping_minute, tzinfo=TZ)

START_TIME = datetime.now(TZ)  # process start, reveals restarts between two daily pings

# Telegram delivery: 3 attempts on the spot, then the item stays in the outbox and is replayed later.
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_DELAY_SECONDS = 10
RETRY_INTERVAL_SECONDS = 300
PENDING_MAX_AGE_DAYS = 10            # undelivered items older than this are discarded
PENDING_DISK_FREE_MIN_PERCENT = 30   # oldest-first discard while free disk is below this
DELAYED_NOTE_AFTER_SECONDS = 120     # replayed items older than this get an "original time" note
MEDIA_WRITE_TIMEOUT = 120
MEDIA_READ_TIMEOUT = 60

# Supervision: if no frame comes out of the camera for this long, exit and let systemd restart us.
WATCHDOG_TIMEOUT_SECONDS = 60

# Folder for recorded photos/videos. Default "./captures"; override via CAPTURES_DIR.
CAPTURES_DIR = Path(os.environ.get("CAPTURES_DIR", "captures")).expanduser().resolve()
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telescopi")


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))


def _log_uncaught_thread_exception(args) -> None:
    logger.critical("Uncaught exception in thread %s", args.thread.name if args.thread else "?", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _log_uncaught_exception
threading.excepthook = _log_uncaught_thread_exception


def _silence_c_library_stderr() -> None:
    """USB webcams routinely send slightly non-standard MJPEG frames; libjpeg (used internally by
    OpenCV's decoder) reports this by writing straight to the process's real stderr file
    descriptor ("Corrupt JPEG data: N extraneous bytes..."), which floods the systemd journal and
    can't be filtered through Python's logging (it never goes through it). The warnings are benign
    - the frame still decodes - so we redirect the OS-level stderr fd to /dev/null. Our own logs
    and uncaught exceptions are unaffected: they were switched to stdout just above.
    """
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)


if CAMERA_BACKEND == "usb":
    _silence_c_library_stderr()


def hard_exit(reason: str) -> None:
    """Exit the program immediately with a critical log message."""
    logger.critical("%s - exiting so that systemd restarts the service", reason)
    logging.shutdown()
    os._exit(1)
