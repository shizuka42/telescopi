"""Telegram command handlers, inline-keyboard callbacks, status message and bot lifecycle hooks."""
import asyncio
import threading
import time
from datetime import datetime

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes

from telescopi import state
from telescopi.config import (
    ALLOWED_USER_IDS,
    CAMERA_BACKEND,
    CAPTURES_DIR,
    MANUAL_VIDEO_DURATION,
    PING_TIME,
    PRE_ROLL_SECONDS,
    RETRY_INTERVAL_SECONDS,
    START_TIME,
    TZ,
    hard_exit,
    logger,
)
from telescopi.motion import MotionDetector, watchdog
from telescopi.outbox import DONE, notify_all
from telescopi.recording import finalize_and_send_video, handle_motion
from telescopi.ui import get_main_keyboard


# --- SHARED COMMAND IMPLEMENTATIONS (used by both /commands and inline buttons) --------------
async def cmd_help_impl(bot, chat_id):
    help_text = (
        "🛠 *TeleScoPi Bot Control Panel*\n\n"
        "Use the buttons below or the commands:\n"
        "📸 `/photo` - Take a photo\n"
        f"📹 `/video` - Record {MANUAL_VIDEO_DURATION}s video (plus the {PRE_ROLL_SECONDS}s before)\n"
        "🟢 `/start_motion` - Enable motion detection\n"
        "🔴 `/stop_motion` - Disable motion detection\n"
        "ℹ️ `/status` - System status\n"
        "🔄 `/restart` - Restart the service (e.g. after camera glitches)\n\n"
        "Current Status: " + ("✅ Active Monitoring" if state.is_active else "❌ System Off")
    )
    await bot.send_message(chat_id, help_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_status_impl(bot, chat_id):
    await bot.send_message(chat_id, build_status_message(), reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_photo_impl(bot, chat_id):
    status = await bot.send_message(chat_id, "📸 Taking photo...")
    loop = asyncio.get_running_loop()
    photo_file = CAPTURES_DIR / f"snap_{time.time_ns() // 1_000_000}.jpg"
    try:
        # Grabbed from the running stream: instant, works even while a clip is being recorded.
        jpeg = await loop.run_in_executor(None, state.camera.snapshot_jpeg, None)
        await loop.run_in_executor(None, photo_file.write_bytes, jpeg)
        item_id = state.outbox.enqueue("photo", [chat_id], file=photo_file, caption="📸 Snapshot")
        if await state.outbox.deliver(bot, item_id) == DONE:
            try:
                await status.delete()
            except BadRequest:
                pass
    except Exception as exc:
        logger.exception("Photo error")
        await bot.send_message(chat_id, f"❌ Photo error: {exc}", reply_markup=get_main_keyboard())


async def cmd_video_impl(bot, chat_id):
    if state.recording_lock.locked():
        text = f"📹 Recording in progress, {MANUAL_VIDEO_DURATION}s video will start right after it ends..."
    else:
        text = f"📹 Recording {MANUAL_VIDEO_DURATION}s video..."
    status = await bot.send_message(chat_id, text)

    loop = asyncio.get_running_loop()
    stamp = int(time.time())
    raw = CAPTURES_DIR / f"manual_{stamp}.h264"
    mp4 = CAPTURES_DIR / f"manual_{stamp}.mp4"
    try:
        requested_at = time.time()
        async with state.recording_lock:
            state.recorder.start(raw)
            state.is_recording = True
            try:
                await asyncio.sleep(MANUAL_VIDEO_DURATION)
            finally:
                state.recorder.stop()
                state.is_recording = False
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
        state.is_active = False
        await query.answer("Motion detection disabled")
        await context.bot.send_message(chat_id, "🔴 *Motion detection disabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    elif data == "start_motion":
        state.is_active = True
        await query.answer("Motion detection enabled")
        await context.bot.send_message(chat_id, "🟢 *Motion detection enabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    elif data == "show_help":
        await query.answer()
        await cmd_help_impl(context.bot, chat_id)
    elif data == "show_status":
        await query.answer()
        await cmd_status_impl(context.bot, chat_id)
    else:
        await query.answer()


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help_impl(context.bot, update.effective_chat.id)


async def cmd_stop_motion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state.is_active = False
    await update.message.reply_text("🔴 *Motion detection disabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_start_motion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state.is_active = True
    await update.message.reply_text("🟢 *Motion detection enabled.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_photo_impl(context.bot, update.effective_chat.id)


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_video_impl(context.bot, update.effective_chat.id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_status_impl(context.bot, update.effective_chat.id)


async def cmd_restart_impl(bot, chat_id):
    await bot.send_message(chat_id, "🔄 Restarting service...")
    async with state.recording_lock:  # waits for any in-progress recording/capture to finish first
        pass
    hard_exit(f"Manual restart requested via /restart (chat {chat_id})")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_restart_impl(context.bot, update.effective_chat.id)


def build_status_message(startup: bool = False) -> str:
    """Builds the status text shared by the daily ping, /status and the startup notification."""
    status = "Motion detector On" if state.is_active else "Motion detector Off"
    uptime = datetime.now(TZ) - START_TIME
    days, hours, minutes = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    camera_status = "OK" if state.detector is not None and state.detector.camera_ok() else "⚠️ NO FRAMES"
    if state.detector is None:
        lighting_status = "unknown"
    else:
        lighting = state.detector.lighting_status()
        lighting_status = "🌞 Day" if lighting is True else "🌙 Night" if lighting is False else "unknown (calibrating)"
    header = "🔄 System just started." if startup else "✅ Daily check-in: system online."
    message = (
        f"{header}\n"
        f"Status: {status}\n"
        f"📷 Camera: {camera_status} ({CAMERA_BACKEND})\n"
        f"💡 Lighting detected: {lighting_status}\n"
        f"🕒 Running since: {START_TIME.strftime('%Y-%m-%d %H:%M:%S %Z')} (uptime: {days}d {hours}h {minutes}m)"
    )
    return message


async def daily_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirms the bot is alive; a missed ping signals a prolonged outage."""
    await notify_all(context.bot, build_status_message())


async def retry_pending_uploads(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replays everything still waiting in the outbox; cleans up raw files left by a crash."""
    await state.outbox.retry_all(context.bot)
    for stale in CAPTURES_DIR.glob("*.h264"):
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink()
        except OSError:
            pass


async def post_init(telegram_app: Application) -> None:
    """Post-initializes the bot : sends orphans, inits motion detector, and enables job queues."""
    state.recording_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()

    state.outbox.adopt_orphans(("motion_*.mp4", "manual_*.mp4", "snap_*.jpg", "alert_*.jpg"), ALLOWED_USER_IDS)

    state.detector = MotionDetector(state.camera, loop, lambda result: handle_motion(telegram_app, result))
    state.detector.start()
    threading.Thread(target=watchdog, args=(state.detector,), name="watchdog", daemon=True).start()

    telegram_app.job_queue.run_daily(daily_ping, time=PING_TIME, name="daily_ping")
    telegram_app.job_queue.run_repeating(
        retry_pending_uploads, interval=RETRY_INTERVAL_SECONDS, first=30, name="retry_pending_uploads"
    )

    # Notifies on every (re)start, e.g. after a reboot or a systemd restart following a crash.
    await notify_all(telegram_app.bot, build_status_message(startup=True))


async def post_shutdown(telegram_app: Application) -> None:
    state.camera.stop()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)
