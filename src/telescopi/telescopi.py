#!/usr/bin/env python3
"""TeleScopi - Telegram security camera for Raspberry Pi (Picamera2 or USB webcam).

Architecture
------------
* Two interchangeable camera backends, controlled by PICAM_ENABLED / USB_CAM_ENABLED (either or
  both can be true). When both are enabled, DEFAULT_CAMERA picks which one is active at startup,
  and the /switch_camera Telegram command switches to the other one at runtime:
    - "picam" (default): ONE Picamera2 pipeline stays open all the time, with a hardware H.264
      encoder feeding a circular buffer, plus a "lores" YUV stream read ~DETECTION_FPS times/s
      for motion analysis.
    - "usb": a USB webcam (e.g. Logitech C310) read via OpenCV/V4L2 in a background thread; a
      software libx264 encoder (ffmpeg) replaces the hardware encoder, fed from an in-memory
      ring buffer of raw frames plus the live frames as they arrive.
* Either way, a rolling buffer always holds the last PRE_ROLL_SECONDS of video. When motion is
  detected the buffer is flushed to a file and recording continues: the clip starts BEFORE the
  trigger, so there is no delay between motion and video.
* Photos are grabbed from the live camera feed (no camera restart, instant).
* Every Telegram delivery (alerts, media, daily ping) goes through a persistent outbox on disk:
  3 attempts on the spot, then kept and replayed later until delivered or expired.
"""
import asyncio
import collections
import sys
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARN")  # must be set before importing picamera2 (only matters if PICAM_ENABLED)

import cv2
import numpy as np
# picamera2/libcamera are only imported inside PiCameraService, so this file also runs on a
# plain machine with just a USB webcam and no Pi camera stack installed (PICAM_ENABLED=false).
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)


# --- CONFIGURATION ---------------------------------------------------------------------------
def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# Set these as real environment variables (never hardcode secrets in source).
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

# Which camera(s) are physically connected. At least one must be enabled.
PICAM_ENABLED = _env_bool("PICAM_ENABLED", True)
USB_CAM_ENABLED = _env_bool("USB_CAM_ENABLED", False)
if not PICAM_ENABLED and not USB_CAM_ENABLED:
    raise SystemExit("At least one of PICAM_ENABLED or USB_CAM_ENABLED must be true")
BOTH_CAMERAS_ENABLED = PICAM_ENABLED and USB_CAM_ENABLED

# Camera used at startup. Only meaningful (and required) when both cameras are enabled; otherwise
# it's forced to whichever single camera is enabled. The bot always comes back up on this camera
# after a restart - /switch_camera only changes the camera for the current run.
if BOTH_CAMERAS_ENABLED:
    DEFAULT_CAMERA = os.environ.get("DEFAULT_CAMERA", "picam").strip().lower()
    if DEFAULT_CAMERA not in ("picam", "usb"):
        raise SystemExit(f"Invalid DEFAULT_CAMERA={DEFAULT_CAMERA!r}: expected 'picam' or 'usb'")
else:
    DEFAULT_CAMERA = "picam" if PICAM_ENABLED else "usb"

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


if USB_CAM_ENABLED:
    _silence_c_library_stderr()

# --- GLOBAL STATE ----------------------------------------------------------------------------
is_active = True      # motion detection enabled by default
is_recording = False  # a clip (motion or manual) is being written
camera = None         # CameraService
recorder = None       # Recorder
detector = None       # MotionDetector
outbox = None         # Outbox
recording_lock = None  # asyncio.Lock: only one clip at a time (one circular output)
camera_switch_lock = None    # asyncio.Lock: only one /switch_camera at a time
current_camera_backend = None  # "picam" or "usb": whichever camera is active right now
event_loop = None             # asyncio event loop, needed to (re)build a MotionDetector on switch


def hard_exit(reason: str) -> None:
    """Exit the program immediately with a critical log message."""
    logger.critical("%s - exiting so that systemd restarts the service", reason)
    logging.shutdown()
    os._exit(1)


# --- CAMERA ----------------------------------------------------------------------------------
class CameraService:
    """Common interface implemented by both camera backends (Pi camera module and USB webcam).

    grab_gray()       -> next analysis frame, grayscale, shape (LORES_HEIGHT, LORES_WIDTH)
    snapshot_jpeg()   -> JPEG bytes of the current frame at full (VIDEO_WIDTH, VIDEO_HEIGHT) resolution
    create_recorder() -> an object exposing start(path)/stop() that writes a raw .h264 elementary
                          stream, pre-roll (PRE_ROLL_SECONDS) included, ready for wrap_h264_to_mp4()
    """

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def grab_gray(self) -> np.ndarray:
        raise NotImplementedError

    def snapshot_jpeg(self, bbox=None) -> bytes:
        raise NotImplementedError

    def create_recorder(self):
        raise NotImplementedError


def _draw_motion_box(frame: np.ndarray, bbox) -> None:
    """Scales a bbox from analysis-frame coordinates to full resolution and draws it in place (in-place, BGR)."""
    sx, sy = VIDEO_WIDTH / LORES_WIDTH, VIDEO_HEIGHT / LORES_HEIGHT
    x, y, w, h = bbox
    cv2.rectangle(
        frame,
        (int(x * sx), int(y * sy)),
        (int((x + w) * sx), int((y + h) * sy)),
        (0, 0, 255),
        max(2, VIDEO_WIDTH // 400),
    )


class PiCameraService(CameraService):
    """Owns the Picamera2 pipeline: hardware H.264 -> circular buffer, plus a lores stream for analysis."""

    def __init__(self):
        # Imported here (not at module level) so this file also runs with PICAM_ENABLED=false on a
        # machine without the Pi camera stack installed.
        from libcamera import Transform
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import CircularOutput

        self.picam2 = Picamera2()
        transform = Transform(hflip=int(CAMERA_ROTATE_180), vflip=int(CAMERA_ROTATE_180))
        frame_us = int(1_000_000 / VIDEO_FPS)
        config = self.picam2.create_video_configuration(
            main={"size": (VIDEO_WIDTH, VIDEO_HEIGHT), "format": "RGB888"},
            lores={"size": (LORES_WIDTH, LORES_HEIGHT), "format": "YUV420"},
            transform=transform,
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        self.picam2.configure(config)
        # One key frame per second so the pre-roll can start close to PRE_ROLL_SECONDS before the trigger.
        self.encoder = H264Encoder(bitrate=VIDEO_BITRATE, repeat=True, iperiod=VIDEO_FPS)
        # The buffer size is expressed in frames (+1 s of margin: the clip must start on a key frame).
        self.circular = CircularOutput(buffersize=(PRE_ROLL_SECONDS + 1) * VIDEO_FPS)

    def start(self) -> None:
        self.picam2.start_recording(self.encoder, self.circular)
        logger.info(
            "Pi camera started: %dx%d@%dfps main, %dx%d analysis, %ds pre-roll",
            VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, LORES_WIDTH, LORES_HEIGHT, PRE_ROLL_SECONDS,
        )

    def stop(self) -> None:
        try:
            self.picam2.stop_recording()
            self.picam2.close()
        except Exception:
            logger.exception("Error while stopping the camera")

    def grab_gray(self) -> np.ndarray:
        """Next frame of the lores stream as a (LORES_HEIGHT, LORES_WIDTH) grayscale array (Y plane)."""
        frame = self.picam2.capture_array("lores")
        return np.ascontiguousarray(frame[:LORES_HEIGHT, :LORES_WIDTH])

    def snapshot_jpeg(self, bbox=None) -> bytes:
        """JPEG of the next frame of the main stream. bbox = (x, y, w, h) in analysis-frame coordinates."""
        frame = np.ascontiguousarray(self.picam2.capture_array("main"))  # BGR memory order for RGB888
        if bbox is not None and MOTION_SNAPSHOT_DRAW_BOX:
            _draw_motion_box(frame, bbox)
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buffer.tobytes()

    def create_recorder(self):
        return PiRecorder(self.circular)


class PiRecorder:
    """Writes the circular buffer (pre-roll) + live frames to a raw .h264 file (hardware encoder)."""

    def __init__(self, circular):
        self._circular = circular

    def start(self, raw_path: Path) -> None:
        self._circular.fileoutput = str(raw_path)
        self._circular.start()  # flushes the buffered pre-roll first, then keeps writing

    def stop(self) -> None:
        self._circular.stop()


def _parse_webcam_device(value: str):
    """WEBCAM_DEVICE can be a V4L2 path ("/dev/video0") or a numeric index ("0", "1", ...)."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


class WebcamCameraService(CameraService):
    """USB webcam (e.g. Logitech C310) via OpenCV/V4L2.

    There is no hardware encoder here, so a background thread continuously reads frames and:
      * keeps the last PRE_ROLL_SECONDS of raw frames in an in-memory ring buffer (the pre-roll
        equivalent of the Pi's circular H.264 buffer, just uncompressed),
      * fans out every live frame to whichever WebcamRecorder is currently recording (see
        subscribe()/unsubscribe()).
    grab_gray()/snapshot_jpeg() simply read the latest available frame, so photos and analysis
    keep working at any time, including while a clip is being recorded - same behaviour as the Pi.
    """

    def __init__(self):
        self.cap = None
        self._buffer = collections.deque(maxlen=(PRE_ROLL_SECONDS + 1) * VIDEO_FPS)
        self._buffer_lock = threading.Lock()
        self._latest_frame = None
        self._latest_at = 0.0
        self._latest_lock = threading.Lock()
        self._subscribers = []
        self._subscribers_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._capture_thread = None

    def start(self) -> None:
        device = _parse_webcam_device(WEBCAM_DEVICE)
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open USB webcam {WEBCAM_DEVICE!r}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*WEBCAM_FOURCC))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimize latency; ignored by some drivers

        ok, _ = self.cap.read()
        if not ok:
            raise RuntimeError(f"USB webcam {WEBCAM_DEVICE!r} opened but returned no frame")
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or VIDEO_WIDTH
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or VIDEO_HEIGHT
        if (actual_w, actual_h) != (VIDEO_WIDTH, VIDEO_HEIGHT):
            logger.warning(
                "Webcam gave %dx%d instead of the requested %dx%d; frames will be resized",
                actual_w, actual_h, VIDEO_WIDTH, VIDEO_HEIGHT,
            )

        self._stop_event.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, name="webcam-capture", daemon=True)
        self._capture_thread.start()
        logger.info(
            "USB webcam started (%s): %dx%d@%dfps main, %dx%d analysis, %ds pre-roll",
            WEBCAM_DEVICE, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, LORES_WIDTH, LORES_HEIGHT, PRE_ROLL_SECONDS,
        )

    def _capture_loop(self) -> None:
        period = 1.0 / VIDEO_FPS
        while not self._stop_event.is_set():
            started = time.monotonic()
            ok, frame = self.cap.read()
            if not ok:
                logger.warning("USB webcam read failed, retrying...")
                time.sleep(0.2)
                continue
            if frame.shape[1] != VIDEO_WIDTH or frame.shape[0] != VIDEO_HEIGHT:
                frame = cv2.resize(frame, (VIDEO_WIDTH, VIDEO_HEIGHT), interpolation=cv2.INTER_AREA)
            if CAMERA_ROTATE_180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            frame = np.ascontiguousarray(frame)

            with self._latest_lock:
                self._latest_frame = frame
                self._latest_at = time.monotonic()
            with self._buffer_lock:
                self._buffer.append(frame)
            with self._subscribers_lock:
                subs = list(self._subscribers)
            for q in subs:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    logger.warning("Encoder queue full, dropping a frame")

            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

    def stop(self) -> None:
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5)
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                logger.exception("Error while stopping the webcam")

    def _current_frame(self) -> np.ndarray:
        with self._latest_lock:
            frame, age = self._latest_frame, time.monotonic() - self._latest_at
        if frame is None:
            raise RuntimeError("USB webcam: no frame captured yet")
        if age > max(2.0, 5 / VIDEO_FPS):
            raise RuntimeError(f"USB webcam: last frame is {age:.1f}s old")
        return frame

    def grab_gray(self) -> np.ndarray:
        gray = cv2.cvtColor(self._current_frame(), cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (LORES_WIDTH, LORES_HEIGHT), interpolation=cv2.INTER_AREA)

    def snapshot_jpeg(self, bbox=None) -> bytes:
        frame = self._current_frame().copy()
        if bbox is not None and MOTION_SNAPSHOT_DRAW_BOX:
            _draw_motion_box(frame, bbox)
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buffer.tobytes()

    def create_recorder(self):
        return WebcamRecorder(self)

    def pre_roll_frames(self):
        with self._buffer_lock:
            return list(self._buffer)

    def subscribe(self, q: "queue.Queue") -> None:
        with self._subscribers_lock:
            self._subscribers.append(q)

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class WebcamRecorder:
    """Software encoder for the USB webcam: pipes pre-roll + live BGR frames through ffmpeg (libx264)
    into a raw .h264 elementary stream, so the rest of the app (wrap_h264_to_mp4, file naming) is
    identical regardless of the camera backend.
    """

    def __init__(self, camera: WebcamCameraService):
        self._camera = camera
        self._process = None
        self._writer_thread = None
        self._queue = None
        self._stop_event = None

    def start(self, raw_path: Path) -> None:
        self._queue = queue.Queue(maxsize=VIDEO_FPS * 60)
        self._stop_event = threading.Event()
        # Subscribe BEFORE reading the pre-roll buffer so no frame is missed in between the two.
        self._camera.subscribe(self._queue)
        preroll = self._camera.pre_roll_frames()

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
            "-r", str(VIDEO_FPS),
            "-i", "-",
            "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", str(VIDEO_BITRATE),
            "-pix_fmt", "yuv420p",
            "-f", "h264", str(raw_path),
        ]
        self._process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        def _writer():
            try:
                for frame in preroll:
                    self._process.stdin.write(frame.tobytes())
                while not self._stop_event.is_set():
                    try:
                        frame = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    self._process.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                logger.exception("Webcam encoder pipe broken")
            finally:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass

        self._writer_thread = threading.Thread(target=_writer, name="webcam-encoder-writer", daemon=True)
        self._writer_thread.start()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)
        self._camera.unsubscribe(self._queue)
        if self._process is not None:
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
            stderr = b""
            try:
                stderr = self._process.stderr.read()
            except Exception:
                pass
            if self._process.returncode:
                logger.error(
                    "ffmpeg encoder exited with %s: %s",
                    self._process.returncode, stderr.decode(errors="replace")[-500:],
                )
        self._process = None


def build_camera_service(backend: str) -> CameraService:
    """Builds a fresh camera service for the given backend ("picam" or "usb")."""
    if backend == "usb":
        return WebcamCameraService()
    if backend == "picam":
        return PiCameraService()
    raise ValueError(f"Unknown camera backend {backend!r}")


def wrap_h264_to_mp4(raw: Path, mp4: Path) -> bool:
    """Remuxes the raw H.264 stream to MP4 (no re-encoding). -framerate keeps the real playback speed."""
    """raw file is removed after successful remuxing."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(VIDEO_FPS),
        "-i", str(raw),
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(mp4),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        logger.error("ffmpeg failed: %s", exc.stderr.decode(errors="replace")[-500:])
        return False
    except (subprocess.TimeoutExpired, OSError):
        logger.exception("ffmpeg could not run")
        return False
    raw.unlink(missing_ok=True)
    return True


# --- MOTION DETECTION ------------------------------------------------------------------------
@dataclass
class MotionResult:
    area: float
    bbox: tuple  # (x, y, w, h) in analysis-frame coordinates
    threshold: int
    is_day: bool


class MotionAnalyzer:
    """Background subtraction on a small grayscale frame. Pure function of the frames it is given."""

    def __init__(self):
        self.is_day = None
        self.reset()

    def reset(self) -> None:
        self._background = None
        self._hits = 0

    def analyze(self, gray: np.ndarray):
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        brightness = float(blurred.mean())
        was_day = self.is_day
        self.is_day = brightness >= BRIGHTNESS_DAY_NIGHT_THRESHOLD
        threshold = THRESHOLD_DAY if self.is_day else THRESHOLD_NIGHT
        if was_day is not None and was_day != self.is_day:
            logger.info("Lighting switched to %s (brightness=%.1f)", "day" if self.is_day else "night", brightness)

        if self._background is None:
            self._background = blurred.astype(np.float32)
            self._hits = 0
            logger.info("Reference frame established. Watching for movement...")
            return None

        background = cv2.convertScaleAbs(self._background)
        current_exposure_gap = abs(brightness - float(background.mean()))
        if current_exposure_gap > 2*EXPOSURE_JUMP:
            # Large exposure gap => report as motion
            logger.info("Global brightness jump (%.1f -> %.1f), reported as full-frame motion", background.mean(), brightness)
            self._background = blurred.astype(np.float32)
            self._hits = 0
            height, width = gray.shape[:2]
            return MotionResult(float(width * height), (0, 0, width, height), threshold, self.is_day)
        if current_exposure_gap > EXPOSURE_JUMP:
            # Auto-exposure / IR-cut switch / lights: the whole image changed, not a moving object.
            logger.info("Global brightness jump (%.1f -> %.1f), reference reset", background.mean(), brightness)
            self._background = blurred.astype(np.float32)
            self._hits = 0
            return None

        delta = cv2.absdiff(blurred, background)
        mask = cv2.threshold(delta, PIXEL_DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.dilate(mask, None, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best, best_area = None, 0.0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > best_area:
                best, best_area = contour, area

        if best_area >= max(20, threshold // 2):
            logger.info("Activity level %.0f (threshold %s, %s)", best_area, threshold, "day" if self.is_day else "night")

        if best_area >= threshold:
            self._hits += 1
            if self._hits >= MOTION_CONSECUTIVE_FRAMES:
                self._hits = 0
                return MotionResult(best_area, cv2.boundingRect(best), threshold, self.is_day)
        else:
            self._hits = 0
            # Only learn from frames without motion, so a moving object is never absorbed.
            cv2.accumulateWeighted(blurred, self._background, BACKGROUND_ALPHA)
        return None


class MotionDetector(threading.Thread):
    """Dedicated thread: reads the lores stream at DETECTION_FPS and reports motion to the asyncio loop."""

    def __init__(self, cam: CameraService, loop, on_motion):
        super().__init__(name="motion-detector", daemon=True)
        self._camera = cam
        self._loop = loop
        self._on_motion = on_motion  # callable(MotionResult) -> coroutine
        self._analyzer = MotionAnalyzer()
        self.busy = False          # a motion clip is being handled
        self.resume_at = 0.0       # monotonic time before which detection stays paused
        self.last_frame_at = time.monotonic()
        self._stop_event = threading.Event()  # set by stop(), e.g. when switching camera

    def stop(self) -> None:
        """Asks the detection loop to exit; the thread still needs join()ing afterwards."""
        self._stop_event.set()

    def release(self, cooldown: float) -> None:
        self.resume_at = time.monotonic() + cooldown
        self.busy = False

    def camera_ok(self) -> bool:
        return time.monotonic() - self.last_frame_at < 15

    def lighting_status(self):
        """Current day/night reading of the analyzer: True (day), False (night), None (not established yet)."""
        return self._analyzer.is_day

    def run(self) -> None:
        period = 1.0 / DETECTION_FPS
        errors = 0
        logger.info("Monitoring system initialized...")
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                gray = self._camera.grab_gray()  # always read: doubles as camera health heartbeat
                self.last_frame_at = time.monotonic()
                errors = 0
                if not is_active or self.busy or time.monotonic() < self.resume_at:
                    self._analyzer.reset()  # fresh reference when detection resumes
                else:
                    result = self._analyzer.analyze(gray)
                    if result is not None:
                        self._trigger(result)
            except Exception:
                errors += 1
                logger.exception("Detection error (%d)", errors)
                if errors >= 5:
                    hard_exit("Camera keeps failing")
                time.sleep(1)
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
        logger.info("Motion detector thread stopped")

    def _trigger(self, result: MotionResult) -> None:
        logger.info("*** MOTION TRIGGERED *** area=%.0f", result.area)
        self.busy = True
        try:
            asyncio.run_coroutine_threadsafe(self._on_motion(result), self._loop)
        except Exception:
            self.busy = False
            raise


def _watchdog() -> None:
    """Watchdog thread that ensures the camera is providing frames regularly.

    Reads the `detector` global on every tick (rather than taking a fixed reference) so that it
    keeps watching whichever detector is current after a /switch_camera.
    """
    while True:
        time.sleep(10)
        det = detector
        if det is not None and time.monotonic() - det.last_frame_at > WATCHDOG_TIMEOUT_SECONDS:
            hard_exit(f"No camera frame for {WATCHDOG_TIMEOUT_SECONDS}s")


# --- TELEGRAM DELIVERY (persistent outbox) ---------------------------------------------------
def _camera_label(backend: str) -> str:
    return {"picam": "📷 Pi Camera", "usb": "🎥 USB Webcam"}.get(backend, backend)


def _other_backend(backend: str) -> str:
    return "usb" if backend == "picam" else "picam"


def get_main_keyboard():
    """Generates the inline keyboard for bot control."""
    motion_text = "🔴 Stop Motion" if is_active else "🟢 Start Motion"
    motion_callback = "stop_motion" if is_active else "start_motion"
    rows = [
        [
            InlineKeyboardButton("📸 Photo", callback_data="take_photo"),
            InlineKeyboardButton("📹 Video", callback_data="take_video"),
        ],
        [InlineKeyboardButton(motion_text, callback_data=motion_callback)],
    ]
    if BOTH_CAMERAS_ENABLED:
        rows.append([InlineKeyboardButton("🔀 Switch Camera", callback_data="switch_camera")])
    rows.append([
        InlineKeyboardButton("ℹ️ Status", callback_data="show_status"),
        InlineKeyboardButton("❓ Help", callback_data="show_help"),
    ])
    return InlineKeyboardMarkup(rows)


async def _call_with_retry(coro_func, log_label):
    """Runs coro_func up to SEND_RETRY_ATTEMPTS times; returns True on first success."""
    for attempt in range(1, SEND_RETRY_ATTEMPTS + 1):
        try:
            await coro_func()
            return True
        except Exception:
            if attempt == SEND_RETRY_ATTEMPTS:
                logger.exception("%s failed after %d attempts", log_label, attempt)
            else:
                logger.warning("%s failed (attempt %d/%d), retrying...", log_label, attempt, SEND_RETRY_ATTEMPTS)
                await asyncio.sleep(SEND_RETRY_DELAY_SECONDS)
    return False


DONE, PARTIAL, FAILED, SKIPPED = "done", "partial", "failed", "skipped"


class Outbox:
    """Every notification (text, photo, video) is written to disk BEFORE being sent.

    An item is removed once every recipient received it. Otherwise it stays (surviving restarts and
    reboots) and is replayed by retry_all() until delivered or expired.
    """

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._inflight = set()

    def _json_path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.json"

    def _save(self, item: dict) -> None:
        path = self._json_path(item["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(item), encoding="utf-8")
        os.replace(tmp, path)

    def _load_all(self) -> list:
        items = []
        for path in sorted(self.dir.glob("*.json")):  # ids start with a timestamp: oldest first
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.exception("Unreadable outbox entry %s", path)
        return items

    def enqueue(self, kind, recipients, text="", file=None, caption="", created_at=None) -> str:
        item_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        self._save({
            "id": item_id,
            "kind": kind,  # "text" | "photo" | "video"
            "recipients": list(recipients),
            "text": text,
            "file": str(file) if file else None,
            "caption": caption,
            "created_at": created_at or time.time(),
            "attempts": 0,
        })
        return item_id

    def adopt_orphans(self, patterns, recipients) -> None:
        """Media left in CAPTURES_DIR by an older version / a crash, not yet tracked by the outbox."""
        referenced = {item.get("file") for item in self._load_all()}
        for pattern in patterns:
            for path in CAPTURES_DIR.glob(pattern):
                if str(path) in referenced:
                    continue
                kind = "video" if path.suffix.lower() == ".mp4" else "photo"
                logger.info("Adopting undelivered file %s", path)
                self.enqueue(kind, recipients, file=path, caption="Recovered delivery", created_at=path.stat().st_mtime)

    def _discard(self, item: dict) -> None:
        """Discard an outbox item and remove its associated file, if any."""
        self._json_path(item["id"]).unlink(missing_ok=True)
        if item.get("file"):
            Path(item["file"]).unlink(missing_ok=True)

    async def _send_item(self, bot, chat_id: int, item: dict) -> None:
        note = ""
        if time.time() - item["created_at"] > DELAYED_NOTE_AFTER_SECONDS:
            when = datetime.fromtimestamp(item["created_at"], TZ)
            note = f"\n⏱ Event of {when:%d/%m %H:%M:%S} (delayed delivery)"
        kind = item["kind"]
        if kind == "text":
            await bot.send_message(chat_id, item["text"] + note)
        elif kind == "photo":
            with open(item["file"], "rb") as fh:
                await bot.send_photo(
                    chat_id, fh, caption=item["caption"] + note, reply_markup=get_main_keyboard(),
                    write_timeout=MEDIA_WRITE_TIMEOUT, read_timeout=MEDIA_READ_TIMEOUT,
                )
        elif kind == "video":
            with open(item["file"], "rb") as fh:
                await bot.send_video(
                    chat_id, fh, caption=item["caption"] + note, supports_streaming=True,
                    width=VIDEO_WIDTH, height=VIDEO_HEIGHT, reply_markup=get_main_keyboard(),
                    write_timeout=MEDIA_WRITE_TIMEOUT, read_timeout=MEDIA_READ_TIMEOUT,
                )
        else:
            raise ValueError(f"Unknown outbox kind {kind!r}")

    async def deliver(self, bot, item_id: str) -> str:
        """Tries every remaining recipient (3 attempts each). Returns DONE/PARTIAL/FAILED/SKIPPED."""
        if item_id in self._inflight:
            return SKIPPED
        self._inflight.add(item_id)
        try:
            try:
                item = json.loads(self._json_path(item_id).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return SKIPPED
            if item.get("file") and not Path(item["file"]).exists():
                logger.warning("File %s vanished, dropping outbox entry", item["file"])
                self._discard(item)
                return SKIPPED

            total = len(item["recipients"])
            for user_id in list(item["recipients"]):
                sent = await _call_with_retry(
                    lambda user_id=user_id: self._send_item(bot, user_id, item),
                    f"{item['kind']} to {user_id}",
                )
                if sent:
                    item["recipients"].remove(user_id)
                    self._save(item)

            if not item["recipients"]:
                logger.info("Delivered %s (%s)", item["id"], item["kind"])
                self._discard(item)
                return DONE
            item["attempts"] = item.get("attempts", 0) + 1
            self._save(item)
            logger.warning("Kept %s (%s) for later replay, %d recipient(s) pending", item["id"], item["kind"], len(item["recipients"]))
            return PARTIAL if len(item["recipients"]) < total else FAILED
        finally:
            self._inflight.discard(item_id)

    async def retry_all(self, bot) -> None:
        """Replays pending items oldest-first; discards those too old or when the disk is nearly full."""
        network_down = False
        for item in self._load_all():
            if item["id"] in self._inflight:
                continue
            if not network_down:
                if await self.deliver(bot, item["id"]) == FAILED:
                    network_down = True  # nothing went through: stop hammering, retry next cycle
            if not self._json_path(item["id"]).exists():
                continue  # delivered (or dropped)
            age_days = (time.time() - item["created_at"]) / 86400
            usage = shutil.disk_usage(CAPTURES_DIR)
            free_percent = usage.free / usage.total * 100
            if age_days >= PENDING_MAX_AGE_DAYS or free_percent < PENDING_DISK_FREE_MIN_PERCENT:
                logger.warning("Discarding %s (age=%.1fd, disk_free=%.1f%%)", item["id"], age_days, free_percent)
                self._discard(item)


async def notify_all(bot, text: str) -> str:
    item_id = outbox.enqueue("text", ALLOWED_USER_IDS, text=text)
    return await outbox.deliver(bot, item_id)


# --- MOTION HANDLING -------------------------------------------------------------------------
async def send_motion_alert(bot, bbox, area, threshold, detected_at: datetime) -> None:
    """Snapshot of the triggering moment, sent right away while the clip is still recording."""
    caption = f"🚨 Motion Detected! ({detected_at:%H:%M:%S}, {area}/{threshold} ) Recording..."
    loop = asyncio.get_running_loop()
    path = CAPTURES_DIR / f"alert_{int(detected_at.timestamp() * 1000)}.jpg"
    try:
        jpeg = await loop.run_in_executor(None, camera.snapshot_jpeg, bbox)
        await loop.run_in_executor(None, path.write_bytes, jpeg)
        item_id = outbox.enqueue("photo", ALLOWED_USER_IDS, file=path, caption=caption, created_at=detected_at.timestamp())
    except Exception:
        logger.exception("Could not build the motion snapshot, sending a text alert instead")
        item_id = outbox.enqueue("text", ALLOWED_USER_IDS, text=caption, created_at=detected_at.timestamp())
    await outbox.deliver(bot, item_id)


async def finalize_and_send_video(bot, raw: Path, mp4: Path, caption: str, created_at: float, recipients) -> str:
    """Finalizes the recorded video by wrapping it in an MP4 container and sends it to the recipients."""
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, wrap_h264_to_mp4, raw, mp4):
        return FAILED
    item_id = outbox.enqueue("video", recipients, file=mp4, caption=caption, created_at=created_at)
    return await outbox.deliver(bot, item_id)


async def handle_motion(telegram_app: Application, result: MotionResult) -> None:
    """Called (on the event loop) as soon as the detector thread sees motion."""
    global is_recording
    detected_at = datetime.now(TZ)
    try:
        if recording_lock.locked():  # a manual video is already being recorded
            logger.info("Motion ignored: a recording is already in progress")
            return
        stamp = int(time.time())
        raw = CAPTURES_DIR / f"motion_{stamp}.h264"
        mp4 = CAPTURES_DIR / f"motion_{stamp}.mp4"
        async with recording_lock:
            recorder.start(raw)  # instant: the pre-roll already sits in the circular buffer
            is_recording = True
            try:
                telegram_app.create_task(send_motion_alert(telegram_app.bot, result.bbox, result.area, result.threshold, detected_at))
                await asyncio.sleep(MOTION_VIDEO_DURATION)
            finally:
                recorder.stop()
                is_recording = False
        # Encoding + upload happen outside the lock: the camera is immediately free again.
        telegram_app.create_task(finalize_and_send_video(
            telegram_app.bot, raw, mp4, "🚨 Motion Detected!", detected_at.timestamp(), ALLOWED_USER_IDS
        ))
    except Exception:
        logger.exception("Motion record error")
    finally:
        detector.release(DELAY_AFTER_MOTION)


# --- SHARED COMMAND IMPLEMENTATIONS (used by both /commands and inline buttons) --------------
async def cmd_help_impl(bot, chat_id):
    help_text = (
        "🛠 *TeleScoPi Bot Control Panel*\n\n"
        "Use the buttons below or the commands:\n"
        "📸 `/photo` - Take a photo\n"
        f"📹 `/video` - Record {MANUAL_VIDEO_DURATION}s video (plus the {PRE_ROLL_SECONDS}s before)\n"
        "🟢 `/start_motion` - Enable motion detection\n"
        "🔴 `/stop_motion` - Disable motion detection\n"
        + ("🔀 `/switch_camera` - Switch to the other camera\n" if BOTH_CAMERAS_ENABLED else "")
        + "ℹ️ `/status` - System status\n\n"
        "Current Status: " + ("✅ Active Monitoring" if is_active else "❌ System Off")
    )
    await bot.send_message(chat_id, help_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_status_impl(bot, chat_id):
    await bot.send_message(chat_id, build_status_message(), reply_markup=get_main_keyboard())


async def cmd_photo_impl(bot, chat_id):
    status = await bot.send_message(chat_id, "📸 Taking photo...")
    loop = asyncio.get_running_loop()
    photo_file = CAPTURES_DIR / f"snap_{time.time_ns() // 1_000_000}.jpg"
    try:
        # Grabbed from the running stream: instant, works even while a clip is being recorded.
        jpeg = await loop.run_in_executor(None, camera.snapshot_jpeg, None)
        await loop.run_in_executor(None, photo_file.write_bytes, jpeg)
        item_id = outbox.enqueue("photo", [chat_id], file=photo_file, caption="📸 Snapshot")
        if await outbox.deliver(bot, item_id) == DONE:
            try:
                await status.delete()
            except BadRequest:
                pass
    except Exception as exc:
        logger.exception("Photo error")
        await bot.send_message(chat_id, f"❌ Photo error: {exc}", reply_markup=get_main_keyboard())


async def cmd_video_impl(bot, chat_id):
    if recording_lock.locked():
        text = f"📹 Recording in progress, {MANUAL_VIDEO_DURATION}s video will start right after it ends..."
    else:
        text = f"📹 Recording {MANUAL_VIDEO_DURATION}s video..."
    status = await bot.send_message(chat_id, text)

    global is_recording
    loop = asyncio.get_running_loop()
    stamp = int(time.time())
    raw = CAPTURES_DIR / f"manual_{stamp}.h264"
    mp4 = CAPTURES_DIR / f"manual_{stamp}.mp4"
    try:
        requested_at = time.time()
        async with recording_lock:
            recorder.start(raw)
            is_recording = True
            try:
                await asyncio.sleep(MANUAL_VIDEO_DURATION)
            finally:
                recorder.stop()
                is_recording = False
        try:
            await status.edit_text("📤 Uploading...")
        except BadRequest:
            pass
        result = await finalize_and_send_video(bot, raw, mp4, "📹 Manual Recording", requested_at, [chat_id])
        if result == DONE:
            try:
                await status.delete()
            except BadRequest:
                pass
    except Exception as exc:
        logger.exception("Video error")
        await bot.send_message(chat_id, f"❌ Video error: {exc}", reply_markup=get_main_keyboard())


# --- TELEGRAM HANDLERS -----------------------------------------------------------------------
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes button clicks from the inline keyboard."""
    global is_active
    query = update.callback_query
    user_id = update.effective_user.id if update.effective_user else None

    if user_id not in ALLOWED_USER_IDS:
        await query.answer()
        logger.warning("Unauthorized callback from user %s", user_id)
        return

    data = query.data
    chat_id = query.message.chat_id

    if data == "take_photo":
        await query.answer()
        await cmd_photo_impl(context.bot, chat_id)
    elif data == "take_video":
        await query.answer()
        await cmd_video_impl(context.bot, chat_id)
    elif data == "stop_motion":
        is_active = False
        await query.answer("Motion detection disabled")
        await context.bot.send_message(chat_id, "🔴 *Motion detection disabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    elif data == "start_motion":
        is_active = True
        await query.answer("Motion detection enabled")
        await context.bot.send_message(chat_id, "🟢 *Motion detection enabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    elif data == "show_help":
        await query.answer()
        await cmd_help_impl(context.bot, chat_id)
    elif data == "show_status":
        await query.answer()
        await cmd_status_impl(context.bot, chat_id)
    elif data == "switch_camera":
        await query.answer()
        await cmd_switch_camera_impl(context.application, chat_id)
    else:
        await query.answer()


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help_impl(context.bot, update.effective_chat.id)


async def cmd_stop_motion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_active
    is_active = False
    await update.message.reply_text("🔴 *Motion detection disabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_start_motion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_active
    is_active = True
    await update.message.reply_text("🟢 *Motion detection enabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_photo_impl(context.bot, update.effective_chat.id)


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_video_impl(context.bot, update.effective_chat.id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_status_impl(context.bot, update.effective_chat.id)


async def cmd_switch_camera(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_switch_camera_impl(context.application, update.effective_chat.id)


def _switch_camera_sync(old_camera: CameraService, old_detector: MotionDetector, new_backend: str, application: Application):
    """Blocking part of a camera switch: runs off the event loop (see cmd_switch_camera_impl)."""
    old_detector.stop()
    old_detector.join(timeout=5)
    old_camera.stop()

    new_camera = build_camera_service(new_backend)
    new_camera.start()
    new_detector = MotionDetector(new_camera, event_loop, lambda result: handle_motion(application, result))
    new_detector.start()
    return new_camera, new_detector


async def cmd_switch_camera_impl(application: Application, chat_id: int) -> None:
    """Switches to the other camera. Refuses while a recording (manual or motion) is in progress."""
    global camera, recorder, detector, current_camera_backend
    bot = application.bot

    if not BOTH_CAMERAS_ENABLED:
        await bot.send_message(
            chat_id, "Une seule caméra est configurée (PICAM_ENABLED / USB_CAM_ENABLED) : impossible de basculer.",
            reply_markup=get_main_keyboard(),
        )
        return

    def _busy() -> bool:
        # detector.busy is set the instant motion triggers, slightly before recording_lock is
        # actually acquired on the event loop, so checking both closes that small race window.
        return recording_lock.locked() or (detector is not None and detector.busy)

    if _busy():
        await bot.send_message(
            chat_id, "⚠️ Un enregistrement est en cours, réessaie une fois qu'il sera terminé.",
            reply_markup=get_main_keyboard(),
        )
        return

    async with camera_switch_lock:
        if _busy():  # re-check: a recording may have started while we were waiting for this lock
            await bot.send_message(
                chat_id, "⚠️ Un enregistrement est en cours, réessaie une fois qu'il sera terminé.",
                reply_markup=get_main_keyboard(),
            )
            return

        new_backend = _other_backend(current_camera_backend)
        status = await bot.send_message(chat_id, f"🔄 Bascule vers {_camera_label(new_backend)}...")
        loop = asyncio.get_running_loop()
        try:
            new_camera, new_detector = await loop.run_in_executor(
                None, _switch_camera_sync, camera, detector, new_backend, application
            )
        except Exception as exc:
            logger.exception("Camera switch failed")
            try:
                await status.edit_text(f"❌ Échec du changement de caméra : {exc}")
            except BadRequest:
                pass
            await bot.send_message(chat_id, "La caméra précédente reste active.", reply_markup=get_main_keyboard())
            return

        camera, detector = new_camera, new_detector
        recorder = camera.create_recorder()
        current_camera_backend = new_backend
        logger.info("Camera switched to %s", current_camera_backend)
        try:
            await status.edit_text(f"✅ Caméra active : {_camera_label(new_backend)}")
        except BadRequest:
            pass
        await bot.send_message(chat_id, f"📷 Caméra active : {_camera_label(new_backend)}", reply_markup=get_main_keyboard())


def build_status_message(startup: bool = False) -> str:
    """Builds the status text shared by the daily ping, /status and the startup notification."""
    status = "Motion detector On" if is_active else "Motion detector Off"
    uptime = datetime.now(TZ) - START_TIME
    days, hours, minutes = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    camera_status = "OK" if detector is not None and detector.camera_ok() else "⚠️ NO FRAMES"
    if detector is None:
        lighting_status = "unknown"
    else:
        lighting = detector.lighting_status()
        lighting_status = "🌞 Day" if lighting is True else "🌙 Night" if lighting is False else "unknown (calibrating)"
    header = "🔄 System just started." if startup else "✅ Daily check-in: system online."
    message = (
        f"{header}\n"
        f"Status: {status}\n"
        f"📷 Camera: {camera_status} ({_camera_label(current_camera_backend)})\n"
        f"💡 Lighting detected: {lighting_status}\n"
        f"🕒 Running since: {START_TIME.strftime('%Y-%m-%d %H:%M:%S %Z')} (uptime: {days}d {hours}h {minutes}m)"
    )
    return message


async def daily_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirms the bot is alive; a missed ping signals a prolonged outage."""
    await notify_all(context.bot, build_status_message())


async def retry_pending_uploads(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replays everything still waiting in the outbox; cleans up raw files left by a crash."""
    await outbox.retry_all(context.bot)
    for stale in CAPTURES_DIR.glob("*.h264"):
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink()
        except OSError:
            pass


async def post_init(telegram_app: Application) -> None:
    """Post-initializes the bot : sends orphans, inits motion detector, and enables job queues."""
    global recording_lock, camera_switch_lock, detector, event_loop
    recording_lock = asyncio.Lock()
    camera_switch_lock = asyncio.Lock()
    event_loop = asyncio.get_running_loop()

    outbox.adopt_orphans(("motion_*.mp4", "manual_*.mp4", "snap_*.jpg", "alert_*.jpg"), ALLOWED_USER_IDS)

    detector = MotionDetector(camera, event_loop, lambda result: handle_motion(telegram_app, result))
    detector.start()
    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    telegram_app.job_queue.run_daily(daily_ping, time=PING_TIME, name="daily_ping")
    telegram_app.job_queue.run_repeating(
        retry_pending_uploads, interval=RETRY_INTERVAL_SECONDS, first=30, name="retry_pending_uploads"
    )

    # Notifies on every (re)start, e.g. after a reboot or a systemd restart following a crash.
    await notify_all(telegram_app.bot, build_status_message(startup=True))


async def post_shutdown(telegram_app: Application) -> None:
    camera.stop()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


def main() -> None:
    global camera, recorder, outbox, current_camera_backend
    current_camera_backend = DEFAULT_CAMERA
    logger.info(
        "Camera backend at startup: %s (PICAM_ENABLED=%s, USB_CAM_ENABLED=%s)",
        current_camera_backend, PICAM_ENABLED, USB_CAM_ENABLED,
    )
    camera = build_camera_service(current_camera_backend)
    camera.start()  # fails fast (and systemd restarts us) if the camera is unavailable
    recorder = camera.create_recorder()
    outbox = Outbox(CAPTURES_DIR / "outbox")

    allowed_users_filter = filters.User(user_id=ALLOWED_USER_IDS)
    telegram_app = (
        Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    )

    telegram_app.add_handler(CommandHandler("help", cmd_help, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("stop_motion", cmd_stop_motion, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("start_motion", cmd_start_motion, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("photo", cmd_photo, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("video", cmd_video, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("status", cmd_status, filters=allowed_users_filter))
    if BOTH_CAMERAS_ENABLED:
        telegram_app.add_handler(CommandHandler("switch_camera", cmd_switch_camera, filters=allowed_users_filter))
    telegram_app.add_handler(CallbackQueryHandler(handle_callbacks))
    telegram_app.add_error_handler(error_handler)

    logger.info("Security Bot is Online (%s camera, buttons enabled).", current_camera_backend)
    # bootstrap_retries=-1: retry indefinitely if wifi is down when the process starts.
    telegram_app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
