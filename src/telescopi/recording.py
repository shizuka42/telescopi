"""Motion-triggered recording: snapshot alert + clip capture + finalize/upload."""
import asyncio
import time
from datetime import datetime
from pathlib import Path

from telescopi import state
from telescopi.camera import wrap_h264_to_mp4
from telescopi.config import ALLOWED_USER_IDS, CAPTURES_DIR, DELAY_AFTER_MOTION, MOTION_VIDEO_DURATION, TZ, logger
from telescopi.motion import MotionResult
from telescopi.outbox import FAILED


async def send_motion_alert(bot, bbox, area, threshold, detected_at: datetime) -> None:
    """Snapshot of the triggering moment, sent right away while the clip is still recording."""
    caption = f"🚨 Motion Detected! ({detected_at:%H:%M:%S}, {area}/{threshold} ) Recording..."
    loop = asyncio.get_running_loop()
    path = CAPTURES_DIR / f"alert_{int(detected_at.timestamp() * 1000)}.jpg"
    try:
        jpeg = await loop.run_in_executor(None, state.camera.snapshot_jpeg, bbox)
        await loop.run_in_executor(None, path.write_bytes, jpeg)
        item_id = state.outbox.enqueue("photo", ALLOWED_USER_IDS, file=path, caption=caption, created_at=detected_at.timestamp())
    except Exception:
        logger.exception("Could not build the motion snapshot, sending a text alert instead")
        item_id = state.outbox.enqueue("text", ALLOWED_USER_IDS, text=caption, created_at=detected_at.timestamp())
    await state.outbox.deliver(bot, item_id)


async def finalize_and_send_video(bot, raw: Path, mp4: Path, caption: str, created_at: float, recipients) -> str:
    """Finalizes the recorded video by wrapping it in an MP4 container and sends it to the recipients."""
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, wrap_h264_to_mp4, raw, mp4):
        return FAILED
    item_id = state.outbox.enqueue("video", recipients, file=mp4, caption=caption, created_at=created_at)
    return await state.outbox.deliver(bot, item_id)


async def handle_motion(telegram_app, result: MotionResult) -> None:
    """Called (on the event loop) as soon as the detector thread sees motion."""
    detected_at = datetime.now(TZ)
    try:
        if state.recording_lock.locked():  # a manual video is already being recorded
            logger.info("Motion ignored: a recording is already in progress")
            return
        stamp = int(time.time())
        raw = CAPTURES_DIR / f"motion_{stamp}.h264"
        mp4 = CAPTURES_DIR / f"motion_{stamp}.mp4"
        async with state.recording_lock:
            state.recorder.start(raw)  # instant: the pre-roll already sits in the circular buffer
            state.is_recording = True
            try:
                telegram_app.create_task(send_motion_alert(telegram_app.bot, result.bbox, result.area, result.threshold, detected_at))
                await asyncio.sleep(MOTION_VIDEO_DURATION)
            finally:
                state.recorder.stop()
                state.is_recording = False
        # Encoding + upload happen outside the lock: the camera is immediately free again.
        telegram_app.create_task(finalize_and_send_video(
            telegram_app.bot, raw, mp4, "🚨 Motion Detected!", detected_at.timestamp(), ALLOWED_USER_IDS
        ))
    except Exception:
        logger.exception("Motion record error")
    finally:
        state.detector.release(DELAY_AFTER_MOTION)
