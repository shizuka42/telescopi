"""Persistent disk-backed outbox: every Telegram delivery goes through it.

An item is removed once every recipient received it. Otherwise it stays (surviving restarts and
reboots) and is replayed by retry_all() until delivered or expired.
"""
import asyncio
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from telescopi import state
from telescopi.config import (
    ALLOWED_USER_IDS,
    CAPTURES_DIR,
    DELAYED_NOTE_AFTER_SECONDS,
    MEDIA_READ_TIMEOUT,
    MEDIA_WRITE_TIMEOUT,
    PENDING_DISK_FREE_MIN_PERCENT,
    PENDING_MAX_AGE_DAYS,
    SEND_RETRY_ATTEMPTS,
    SEND_RETRY_DELAY_SECONDS,
    TZ,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    logger,
)
from telescopi.ui import get_main_keyboard

DONE, PARTIAL, FAILED, SKIPPED = "done", "partial", "failed", "skipped"


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
    item_id = state.outbox.enqueue("text", ALLOWED_USER_IDS, text=text)
    return await state.outbox.deliver(bot, item_id)
