#!/usr/bin/env python3
"""Entry point: builds the camera/outbox services and starts the Telegram bot.

Architecture
------------
* Two interchangeable camera backends, selected via the CAMERA_BACKEND env var:
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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

from telescopi import bot, state
from telescopi.camera import build_camera_service
from telescopi.config import ALLOWED_USER_IDS, BOT_TOKEN, CAMERA_BACKEND, CAPTURES_DIR, logger
from telescopi.outbox import Outbox


def main() -> None:
    logger.info("Camera backend: %s", CAMERA_BACKEND)
    state.camera = build_camera_service()
    state.camera.start()  # fails fast (and systemd restarts us) if the camera is unavailable
    state.recorder = state.camera.create_recorder()
    state.outbox = Outbox(CAPTURES_DIR / "outbox")

    allowed_users_filter = filters.User(user_id=ALLOWED_USER_IDS)
    telegram_app = (
        Application.builder().token(BOT_TOKEN).post_init(bot.post_init).post_shutdown(bot.post_shutdown).build()
    )

    telegram_app.add_handler(CommandHandler("help", bot.cmd_help, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("stop_motion", bot.cmd_stop_motion, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("start_motion", bot.cmd_start_motion, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("photo", bot.cmd_photo, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("video", bot.cmd_video, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("status", bot.cmd_status, filters=allowed_users_filter))
    telegram_app.add_handler(CommandHandler("start", bot.cmd_status, filters=allowed_users_filter))
    telegram_app.add_handler(CallbackQueryHandler(bot.handle_callbacks))
    telegram_app.add_error_handler(bot.error_handler)

    logger.info("Security Bot is Online (%s camera, buttons enabled).", CAMERA_BACKEND)
    # bootstrap_retries=-1: retry indefinitely if wifi is down when the process starts.
    telegram_app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
