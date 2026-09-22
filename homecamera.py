import asyncio
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# --- CONFIGURATION ---
# Set these as real environment variables (never hardcode secrets in source).
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Comma-separated Telegram user IDs allowed to control the bot, e.g. "111111,222222"
ALLOWED_USER_IDS = {int(uid) for uid in os.environ["ALLOWED_USER_IDS"].split(",")}

THRESHOLD_DAY = int(os.environ.get("THRESHOLD_DAY", "1000"))  # Higher for full-frame analysis (adjust if needed)
THRESHOLD_NIGHT = int(os.environ.get("THRESHOLD_NIGHT", "200"))  # IR/low-light noise usually needs a higher value
# Mean gray-level (0-255) of the analysis frame; below this the scene is considered "night".
BRIGHTNESS_DAY_NIGHT_THRESHOLD = int(os.environ.get("BRIGHTNESS_DAY_NIGHT_THRESHOLD", "60"))
MOTION_VIDEO_DURATION = 30
DELAY_AFTER_MOTION = 5
VIDEO_BITRATE = 1000000  # 1Mbps for fast upload

# --- Detection speed knobs (they only change how fast a comparison happens, not what THRESHOLD_* mean) ---
# The captured JPEG is decoded at 1/N size (1, 2, 4 or 8): much faster decode/blur/contours on a Pi.
# Contour areas are converted back to full-frame pixels, so THRESHOLD_DAY/NIGHT keep the same meaning.
# Note: the alert snapshot sent to Telegram is also 1/N size (use 2 for a sharper picture).
ANALYSIS_DOWNSCALE = int(os.environ.get("ANALYSIS_DOWNSCALE", "4"))
if ANALYSIS_DOWNSCALE not in (1, 2, 4, 8):
    raise ValueError("ANALYSIS_DOWNSCALE must be 1, 2, 4 or 8")
# Pause between the end of one comparison and the start of the next capture (was a fixed 0.5s).
# 0 = compare as fast as the camera allows (more CPU / heat); raise it if the Pi runs too hot.
MOTION_CHECK_PAUSE = float(os.environ.get("MOTION_CHECK_PAUSE", "0"))

# Frame that triggered the detection, sent right away with the "Motion Detected" alert.
MOTION_SNAPSHOT_DRAW_BOX = os.environ.get("MOTION_SNAPSHOT_DRAW_BOX", "1") == "1"  # red box around the moving area
MOTION_SNAPSHOT_MAX_WIDTH = int(os.environ.get("MOTION_SNAPSHOT_MAX_WIDTH", "1600"))  # px; downscaled for fast upload

# Daily "system OK" ping, "HH:MM" 24h format, always interpreted in Europe/Paris time (handles DST).
_ping_hour, _ping_minute = (int(part) for part in os.environ.get("PING_TIME", "07:30").split(":"))
PING_TIME = dt_time(hour=_ping_hour, minute=_ping_minute, tzinfo=ZoneInfo("Europe/Paris"))

# Process start time, used to reveal restarts between two daily pings.
START_TIME = datetime.now(ZoneInfo("Europe/Paris"))

# Retry of undelivered media kept on disk after a failed send.
PENDING_FILE_PATTERNS = ("motion_*.mp4", "manual_*.mp4", "snap_*.jpg")
PENDING_MIN_AGE_SECONDS = 120  # avoid touching a file still being recorded/sent
PENDING_MAX_AGE_DAYS = 10  # discard undelivered files older than this
PENDING_DISK_FREE_MIN_PERCENT = 30  # discard oldest-first once free disk drops below this
RETRY_INTERVAL_SECONDS = 600

# Retry on transient send failure (e.g. wifi briefly down), shared by photo/video/ping sends.
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_DELAY_SECONDS = 10

# Folder for recorded photos/videos. Default "./captures"; override via CAPTURES_DIR (absolute or relative).
CAPTURES_DIR = Path(os.environ.get("CAPTURES_DIR", "captures"))
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables
is_active = True  # Controls if motion detection is enabled
is_recording = False  # True only while a *motion* video is being recorded (used for user feedback)

# The camera can only be opened by ONE rpicam-* process at a time. Every call that touches the
# camera (motion frame grab, motion video, manual photo, manual video) must hold this lock.
# Created in post_init so it is bound to the running event loop. asyncio.Lock is FIFO, so a manual
# command queued behind the motion loop gets the camera next instead of being starved.
camera_lock = None

# Set after a manual capture so the motion loop drops its now-stale reference frame
# (otherwise a long manual video would be followed by a false motion alert).
reference_stale = False


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


async def _send_media(bot, chat_id, file_path, file_type, caption):
    if file_type == "video":
        await bot.send_video(
            chat_id,
            file_path,
            caption=caption,
            supports_streaming=True,
            duration=MOTION_VIDEO_DURATION,
            reply_markup=get_main_keyboard(),
        )
    else:
        await bot.send_photo(
            chat_id,
            file_path,
            caption=caption,
            reply_markup=get_main_keyboard(),
        )


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


async def send_and_delete(bot, chat_id, file_path, file_type="video", caption=""):
    """Send media to a single chat; keep the local file if the send failed."""
    logger.info("Uploading %s...", file_path)
    sent = await _call_with_retry(
        lambda: _send_media(bot, chat_id, file_path, file_type, caption), f"Send to {chat_id}"
    )
    if sent:
        logger.info("Successfully sent %s", file_path)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        if file_type == "video":
            raw_file_path = file_path.replace('.mp4', '.h264')
            if os.path.exists(raw_file_path):
                try:
                    os.remove(raw_file_path)
                except OSError:
                    pass
    else:
        logger.warning("Kept %s locally after failed send", file_path)


async def broadcast_and_delete(bot, file_path, file_type="video", caption=""):
    """Send media to every allowed user; keep the local file if every send failed."""
    any_sent = False
    logger.info("Uploading %s...", file_path)
    for user_id in ALLOWED_USER_IDS:
        sent = await _call_with_retry(
            lambda user_id=user_id: _send_media(bot, user_id, file_path, file_type, caption), f"Send to {user_id}"
        )
        if sent:
            any_sent = True

    if any_sent:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        raw_file_path = file_path.replace('.mp4', '.h264')
        if os.path.exists(raw_file_path):
            try:
                os.remove(raw_file_path)
            except OSError:
                pass
    else:
        logger.warning("Kept %s locally after failed send to all users", file_path)


async def broadcast_message(bot, text):
    """Sends a plain text alert to every allowed user, retrying transient failures."""
    for user_id in ALLOWED_USER_IDS:
        await _call_with_retry(lambda user_id=user_id: bot.send_message(user_id, text), f"Alert to {user_id}")


def encode_motion_snapshot(frame, contour):
    """Returns the frame that triggered detection as JPEG bytes (optionally boxed, downscaled)."""
    height, width = frame.shape[:2]
    if MOTION_SNAPSHOT_DRAW_BOX and contour is not None:
        # The analysis frame and this frame have the same size, so contour coordinates match.
        x, y, box_w, box_h = cv2.boundingRect(contour)
        cv2.rectangle(frame, (x, y), (x + box_w, y + box_h), (0, 0, 255), max(2, width // 400))
    if width > MOTION_SNAPSHOT_MAX_WIDTH:
        new_height = int(height * MOTION_SNAPSHOT_MAX_WIDTH / width)
        frame = cv2.resize(frame, (MOTION_SNAPSHOT_MAX_WIDTH, new_height), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buffer.tobytes()


async def send_motion_alert(bot, frame, contour, detected_at):
    """Sends the triggering frame to every allowed user; falls back to a text alert on failure."""
    caption = f"🚨 Motion Detected! ({detected_at:%H:%M:%S}) Recording..."
    loop = asyncio.get_running_loop()
    try:
        jpeg = await loop.run_in_executor(None, encode_motion_snapshot, frame, contour)
    except Exception:
        logger.exception("Could not encode the motion snapshot, sending a text alert instead")
        await broadcast_message(bot, caption)
        return
    for user_id in ALLOWED_USER_IDS:
        await _call_with_retry(
            lambda user_id=user_id: bot.send_photo(user_id, jpeg, caption=caption),
            f"Motion snapshot to {user_id}",
        )


def record_video_util(filename, duration):
    """Records video using rpicam-vid."""
    raw_file = filename.replace('.mp4', '.h264')
    cmd = [
        "rpicam-vid",
        "--timeout", str(duration * 1000),
        "--output", raw_file,
        "--framerate", "20",
        "--bitrate", str(VIDEO_BITRATE),
        "--nopreview",
        "--vflip", "1", 
        "--hflip", "1",
        "--inline"
    ]
    # raw video
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # wrap raw video to mp4
    cmd_wrap = [
        "ffmpeg",
        "-i", raw_file,
        "-c:v", "copy",
        "-f", "mp4",
        filename
    ]
    subprocess.run(cmd_wrap, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

_IMREAD_FLAGS = {
    1: cv2.IMREAD_COLOR,
    2: cv2.IMREAD_REDUCED_COLOR_2,
    4: cv2.IMREAD_REDUCED_COLOR_4,
    8: cv2.IMREAD_REDUCED_COLOR_8,
}


def get_rpicam_frame():
    """Captures a single frame using rpicam-still for motion analysis (decoded at 1/ANALYSIS_DOWNSCALE size)."""
    tmp_frame = str(CAPTURES_DIR / f"motion_check_{int(time.time()*100)}.jpg")
    cmd = [
        "rpicam-still",
        "--output", tmp_frame,
        "--quality", "50",
        "--immediate",
        "--nopreview",
        "--vflip", "1",
        "--hflip", "1",
        "--timeout", "1"
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Reduced decoding uses JPEG DCT scaling: far cheaper than decoding full-res then resizing.
        frame = cv2.imread(tmp_frame, _IMREAD_FLAGS[ANALYSIS_DOWNSCALE])
        if os.path.exists(tmp_frame):
            os.remove(tmp_frame)
        return frame
    except (subprocess.CalledProcessError, OSError):
        return None


def take_photo_util(filename):
    """Captures a single full-res photo using rpicam-still."""
    subprocess.run([
        "rpicam-still",
        "--output", filename,
        "--nopreview",
        "--immediate",
        "--vflip", "1",
        "--hflip", "1",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def motion_detection_loop(application: Application) -> None:
    """Runs forever as a background asyncio task; blocking work is offloaded via run_in_executor."""
    global is_active, is_recording, reference_stale
    loop = asyncio.get_running_loop()
    reference_frame = None
    is_day = None
    cycle_stats = []
    logger.info("Monitoring system initialized...")

    while True:
        if not is_active:
            reference_frame = None
            await asyncio.sleep(2)
            continue

        # Waits here if a manual photo/video currently owns the camera.
        async with camera_lock:
            capture_start = time.monotonic()
            frame = await loop.run_in_executor(None, get_rpicam_frame)
            capture_s = time.monotonic() - capture_start
        if frame is None:
            await asyncio.sleep(2)
            continue

        if reference_stale:
            reference_stale = False
            reference_frame = None

        analysis_start = time.monotonic()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Kernel/dilation scaled with the frame size (21x21 and 2 iterations at full resolution).
        blur_size = max(3, (21 // ANALYSIS_DOWNSCALE) | 1)
        gray = cv2.GaussianBlur(gray, (blur_size, blur_size), 0)

        brightness = gray.mean()
        was_day = is_day
        is_day = brightness >= BRIGHTNESS_DAY_NIGHT_THRESHOLD
        threshold = THRESHOLD_DAY if is_day else THRESHOLD_NIGHT
        if was_day is not None and was_day != is_day:
            logger.info("Lighting switched to %s (brightness=%.1f)", "day" if is_day else "night", brightness)

        if reference_frame is None:
            reference_frame = gray
            logger.info("Reference frame established. Watching for movement...")
            continue

        frame_delta = cv2.absdiff(reference_frame, gray)
        thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=max(1, 2 // ANALYSIS_DOWNSCALE))
        contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        max_area = 0
        motion_found = False
        trigger_contour = None
        for c in contours:
            # Converted to full-frame pixels so THRESHOLD_DAY/NIGHT don't depend on ANALYSIS_DOWNSCALE.
            area = cv2.contourArea(c) * ANALYSIS_DOWNSCALE ** 2
            if area > max_area:
                max_area = area
            if area > threshold:
                motion_found = True
                trigger_contour = c
                break

        # Every 30 comparisons, report where the time goes (helps tune the speed knobs).
        cycle_stats.append((capture_s, time.monotonic() - analysis_start))
        if len(cycle_stats) >= 30:
            avg_capture = sum(s[0] for s in cycle_stats) / len(cycle_stats)
            avg_analysis = sum(s[1] for s in cycle_stats) / len(cycle_stats)
            logger.info(
                "Detection cycle: capture %.2fs + analysis %.2fs + pause %.2fs = one comparison every %.2fs",
                avg_capture, avg_analysis, MOTION_CHECK_PAUSE, avg_capture + avg_analysis + MOTION_CHECK_PAUSE,
            )
            cycle_stats.clear()

        if max_area > 100:
            logger.info("Activity level %.0f (threshold %s, %s)", max_area, threshold, "day" if is_day else "night")

        if motion_found:
            logger.info("*** MOTION TRIGGERED *** area=%.0f", max_area)
            # Fire-and-forget: encoding + sending must not delay video capture start.
            application.create_task(send_motion_alert(
                application.bot, frame, trigger_contour, datetime.now(ZoneInfo("Europe/Paris"))
            ))

            video_file = str(CAPTURES_DIR / f"motion_{int(time.time())}.mp4")
            try:
                # The camera is released as soon as recording ends; the upload happens outside the lock.
                async with camera_lock:
                    is_recording = True
                    try:
                        await loop.run_in_executor(None, record_video_util, video_file, MOTION_VIDEO_DURATION)
                    finally:
                        is_recording = False
                await broadcast_and_delete(application.bot, video_file, "video", "🚨 Motion Detected!")
            except Exception:
                logger.exception("Motion record error")

            await asyncio.sleep(DELAY_AFTER_MOTION)
            reference_frame = None
        else:
            cv2.accumulateWeighted(gray, reference_frame.astype("float"), 0.2)
            reference_frame = cv2.convertScaleAbs(reference_frame)
            await asyncio.sleep(MOTION_CHECK_PAUSE)


# --- SHARED COMMAND IMPLEMENTATIONS (used by both /commands and inline buttons) ---

async def cmd_help_impl(bot, chat_id):
    help_text = (
        "🛠 *Security Bot Control Panel*\n\n"
        "Use the buttons below or the commands:\n"
        "📸 `/photo` - Take a photo\n"
        "📹 `/video` - Record 30s video\n"
        "🟢 `/start_motion` - Enable motion detection\n"
        "🔴 `/stop_motion` - Disable motion detection\n\n"
        "Current Status: " + ("✅ Active Monitoring" if is_active else "❌ System Off")
    )
    await bot.send_message(chat_id, help_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_photo_impl(bot, chat_id):
    global reference_stale
    if is_recording:
        text = "📸 Motion recording in progress, photo will be taken right after it ends..."
    else:
        text = "📸 Taking photo..."
    status = await bot.send_message(chat_id, text)

    loop = asyncio.get_running_loop()
    photo_file = str(CAPTURES_DIR / f"snap_{int(time.time())}.jpg")
    try:
        # Waits for the camera to be free (motion frame grab or motion video in progress).
        async with camera_lock:
            await loop.run_in_executor(None, take_photo_util, photo_file)
        await send_and_delete(bot, chat_id, photo_file, "photo", "📸 Snapshot")
        try:
            await status.delete()
        except BadRequest:
            pass
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Photo error: {exc}", reply_markup=get_main_keyboard())
    finally:
        reference_stale = True


async def cmd_video_impl(bot, chat_id):
    global reference_stale
    if is_recording:
        text = "📹 Motion recording in progress, 30s video will start right after it ends..."
    else:
        text = "📹 Recording 30s video..."
    status = await bot.send_message(chat_id, text)

    loop = asyncio.get_running_loop()
    video_file = str(CAPTURES_DIR / f"manual_{int(time.time())}.mp4")
    try:
        async with camera_lock:
            await loop.run_in_executor(None, record_video_util, video_file, 30)
        try:
            await status.edit_text("📤 Uploading...")
        except BadRequest:
            pass
        await send_and_delete(bot, chat_id, video_file, "video", "📹 Manual Recording")
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Video error: {exc}", reply_markup=get_main_keyboard())
    finally:
        reference_stale = True


# --- TELEGRAM HANDLERS ---

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
        await query.message.delete()
    elif data == "start_motion":
        is_active = True
        await query.answer("Motion detection enabled")
        await context.bot.send_message(chat_id, "🟢 *Motion detection enabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")
        await query.message.delete()
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
    uptime = datetime.now(ZoneInfo("Europe/Paris")) - START_TIME
    days, hours, minutes = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    uptime_str = f"{days}d {hours}h {minutes}m"
    message = (
        f"✅ Daily check-in: system online. Status: {status}\n"
        f"🕒 Running since: {START_TIME.strftime('%Y-%m-%d %H:%M:%S %Z')} (uptime: {uptime_str})"
    )
    for user_id in ALLOWED_USER_IDS:
        await _call_with_retry(
            lambda user_id=user_id: context.bot.send_message(user_id, message), f"Daily ping to {user_id}"
        )


async def post_init(application: Application) -> None:
    global camera_lock
    camera_lock = asyncio.Lock()
    application.create_task(motion_detection_loop(application))
    application.job_queue.run_daily(daily_ping, time=PING_TIME, name="daily_ping")
    application.job_queue.run_repeating(
        retry_pending_uploads, interval=RETRY_INTERVAL_SECONDS, name="retry_pending_uploads"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


def _file_type_for(path: Path) -> str:
    return "video" if path.suffix.lower() == ".mp4" else "photo"


def _list_pending_files() -> list:
    """Undelivered media old enough to no longer be actively recorded/sent."""
    now = time.time()
    pending = []
    for pattern in PENDING_FILE_PATTERNS:
        for path in CAPTURES_DIR.glob(pattern):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age >= PENDING_MIN_AGE_SECONDS:
                pending.append(path)
    return pending


def _disk_free_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return (usage.free / usage.total) * 100


async def retry_pending_uploads(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resends media kept on disk after a failed send; discards it if too old or disk is low."""
    for path in _list_pending_files():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        await broadcast_and_delete(context.bot, str(path), _file_type_for(path), "Retried delivery")

        if path.exists():
            free_percent = _disk_free_percent(path.parent)
            if age_days >= PENDING_MAX_AGE_DAYS or free_percent < PENDING_DISK_FREE_MIN_PERCENT:
                logger.warning(
                    "Discarding %s (age=%.1fd, disk_free=%.1f%%) after failed retries",
                    path, age_days, free_percent,
                )
                try:
                    path.unlink()
                except OSError:
                    pass


def main() -> None:
    allowed_users_filter = filters.User(user_id=list(ALLOWED_USER_IDS))

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("help", cmd_help, filters=allowed_users_filter))
    application.add_handler(CommandHandler("stop_motion", cmd_stop_motion, filters=allowed_users_filter))
    application.add_handler(CommandHandler("start_motion", cmd_start_motion, filters=allowed_users_filter))
    application.add_handler(CommandHandler("photo", cmd_photo, filters=allowed_users_filter))
    application.add_handler(CommandHandler("video", cmd_video, filters=allowed_users_filter))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_error_handler(error_handler)

    logger.info("Security Bot is Online (Buttons Enabled).")
    # bootstrap_retries=-1: retry indefinitely if wifi is down when the process starts.
    application.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
