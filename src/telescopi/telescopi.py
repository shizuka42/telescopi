#!/usr/bin/env python3
"""TeleScopi - Telegram security camera for Raspberry Pi (Picamera2 version).

Architecture
------------
* ONE Picamera2 pipeline stays open all the time (no more rpicam-* subprocess per frame):
    - "main"  stream : VIDEO_WIDTH x VIDEO_HEIGHT, hardware H.264 encoder -> circular buffer
    - "lores" stream : small YUV frame, read ~DETECTION_FPS times/s for motion analysis
* The circular buffer always holds the last PRE_ROLL_SECONDS of video. When motion is detected
  the buffer is flushed to a file and recording continues: the clip starts BEFORE the trigger,
  so there is no delay between motion and video.
* Photos are grabbed from the running "main" stream (no camera restart, instant).
* Every Telegram delivery (alerts, media, daily ping) goes through a persistent outbox on disk:
  3 attempts on the spot, then kept and replayed later until delivered or expired.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARN")  # must be set before importing picamera2

import cv2
import numpy as np
from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import CircularOutput
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telescopi")

# --- GLOBAL STATE ----------------------------------------------------------------------------
is_active = True      # motion detection enabled by default
is_recording = False  # a clip (motion or manual) is being written
camera = None         # CameraService
recorder = None       # Recorder
detector = None       # MotionDetector
outbox = None         # Outbox
recording_lock = None  # asyncio.Lock: only one clip at a time (one circular output)


def hard_exit(reason: str) -> None:
    """Exit the program immediately with a critical log message."""
    logger.critical("%s - exiting so that systemd restarts the service", reason)
    logging.shutdown()
    os._exit(1)


# --- CAMERA ----------------------------------------------------------------------------------
class CameraService:
    """Owns the Picamera2 pipeline: hardware H.264 -> circular buffer, plus a lores stream for analysis."""

    def __init__(self):
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
            "Camera started: %dx%d@%dfps main, %dx%d analysis, %ds pre-roll",
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
            sx, sy = VIDEO_WIDTH / LORES_WIDTH, VIDEO_HEIGHT / LORES_HEIGHT
            x, y, w, h = bbox
            cv2.rectangle(
                frame,
                (int(x * sx), int(y * sy)),
                (int((x + w) * sx), int((y + h) * sy)),
                (0, 0, 255),
                max(2, VIDEO_WIDTH // 400),
            )
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buffer.tobytes()


class Recorder:
    """Writes the circular buffer (pre-roll) + live frames to a raw .h264 file."""

    def __init__(self, circular: CircularOutput):
        self._circular = circular

    def start(self, raw_path: Path) -> None:
        self._circular.fileoutput = str(raw_path)
        self._circular.start()  # flushes the buffered pre-roll first, then keeps writing

    def stop(self) -> None:
        self._circular.stop()


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
        if abs(brightness - float(background.mean())) > EXPOSURE_JUMP:
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

    def release(self, cooldown: float) -> None:
        self.resume_at = time.monotonic() + cooldown
        self.busy = False

    def camera_ok(self) -> bool:
        return time.monotonic() - self.last_frame_at < 15

    def run(self) -> None:
        period = 1.0 / DETECTION_FPS
        errors = 0
        logger.info("Monitoring system initialized...")
        while True:
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

    def _trigger(self, result: MotionResult) -> None:
        logger.info("*** MOTION TRIGGERED *** area=%.0f", result.area)
        self.busy = True
        try:
            asyncio.run_coroutine_threadsafe(self._on_motion(result), self._loop)
        except Exception:
            self.busy = False
            raise


def _watchdog(det: MotionDetector) -> None:
    """Watchdog thread that ensures the camera is providing frames regularly."""
    while True:
        time.sleep(10)
        if time.monotonic() - det.last_frame_at > WATCHDOG_TIMEOUT_SECONDS:
            hard_exit(f"No camera frame for {WATCHDOG_TIMEOUT_SECONDS}s")


# --- TELEGRAM DELIVERY (persistent outbox) ---------------------------------------------------
def get_main_keyboard():
    """Generates the inline keyboard for bot control."""
    motion_text = "🔴 Stop Motion" if is_active else "🟢 Start Motion"
    motion_callback = "stop_motion" if is_active else "start_motion"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 Photo", callback_data="take_photo"),
            InlineKeyboardButton("📹 Video", callback_data="take_video"),
        ],
        [InlineKeyboardButton(motion_text, callback_data=motion_callback)],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")],
    ])


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
async def send_motion_alert(bot, bbox, detected_at: datetime) -> None:
    """Snapshot of the triggering moment, sent right away while the clip is still recording."""
    caption = f"🚨 Motion Detected! ({detected_at:%H:%M:%S}) Recording..."
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
                telegram_app.create_task(send_motion_alert(telegram_app.bot, result.bbox, detected_at))
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
        "🛠 *Security Bot Control Panel*\n\n"
        "Use the buttons below or the commands:\n"
        "📸 `/photo` - Take a photo\n"
        f"📹 `/video` - Record {MANUAL_VIDEO_DURATION}s video (plus the {PRE_ROLL_SECONDS}s before)\n"
        "🟢 `/start_motion` - Enable motion detection\n"
        "🔴 `/stop_motion` - Disable motion detection\n\n"
        "Current Status: " + ("✅ Active Monitoring" if is_active else "❌ System Off")
    )
    await bot.send_message(chat_id, help_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


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


async def daily_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirms the bot is alive; a missed ping signals a prolonged outage."""
    status = "Active Monitoring" if is_active else "System Off"
    uptime = datetime.now(TZ) - START_TIME
    days, hours, minutes = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    camera_status = "OK" if detector is not None and detector.camera_ok() else "⚠️ NO FRAMES"
    message = (
        f"✅ Daily check-in: system online. Status: {status}\n"
        f"📷 Camera: {camera_status}\n"
        f"🕒 Running since: {START_TIME.strftime('%Y-%m-%d %H:%M:%S %Z')} (uptime: {days}d {hours}h {minutes}m)"
    )
    await notify_all(context.bot, message)


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
    global recording_lock, detector
    recording_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()

    outbox.adopt_orphans(("motion_*.mp4", "manual_*.mp4", "snap_*.jpg", "alert_*.jpg"), ALLOWED_USER_IDS)

    detector = MotionDetector(camera, loop, lambda result: handle_motion(telegram_app, result))
    detector.start()
    threading.Thread(target=_watchdog, args=(detector,), name="watchdog", daemon=True).start()

    telegram_app.job_queue.run_daily(daily_ping, time=PING_TIME, name="daily_ping")
    telegram_app.job_queue.run_repeating(
        retry_pending_uploads, interval=RETRY_INTERVAL_SECONDS, first=30, name="retry_pending_uploads"
    )


async def post_shutdown(telegram_app: Application) -> None:
    camera.stop()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


def main() -> None:
    global camera, recorder, outbox
    camera = CameraService()
    camera.start()  # fails fast (and systemd restarts us) if the camera is unavailable
    recorder = Recorder(camera.circular)
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
    telegram_app.add_handler(CallbackQueryHandler(handle_callbacks))
    telegram_app.add_error_handler(error_handler)

    logger.info("Security Bot is Online (Picamera2, buttons enabled).")
    # bootstrap_retries=-1: retry indefinitely if wifi is down when the process starts.
    telegram_app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
