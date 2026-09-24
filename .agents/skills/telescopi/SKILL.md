---
name: telescopi
description: 'Project context and guardrails for TeleScopi, a Python3 Telegram-controlled security camera for Raspberry Pi OS Lite (motion detection, on-demand photo/video, Picamera2/USB backends). Use for any change to src/telescopi/telescopi.py, camera backends, motion detection tuning, Telegram commands, systemd deployment, or config env vars in this repo.'
---

# TeleScopi Project Context

## What this project is
- Single-file Python3 app ([src/telescopi/telescopi.py](../../../src/telescopi/telescopi.py)), run as a systemd service on Raspberry Pi OS Lite (64-bit), headless.
- Purpose: motion-detected + on-demand photo/video capture, delivered and controlled via a Telegram bot.

## Hardware & OS constraints (guardrails)
- Target OS is Raspberry Pi OS Lite — no desktop/GUI. Never suggest GUI toolkits (Tkinter windows, `cv2.imshow`, etc.) for the running service.
- Limited RAM/CPU (tested on Pi 3B). Avoid heavy dependencies; avoid holding large frame buffers beyond the configured ring buffers.
- Two interchangeable camera backends, selected via `CAMERA_BACKEND` env var: `picam` (Picamera2 + hardware H.264, default) or `usb` (OpenCV/V4L2 capture + ffmpeg libx264). `picamera2`/`libcamera` are only imported inside `PiCameraService` so the file still runs on a plain machine with just a USB webcam.
- Secrets (`BOT_TOKEN`, `ALLOWED_USER_IDS`) come from env vars only, loaded by systemd via `EnvironmentFile=config/.env`. Never hardcode or log secrets.
- Deployment unit is [config/telescopi.service.template](../../../config/telescopi.service.template) (`Restart=always`). Startup changes must stay compatible with this unit (`ExecStart` path, `PYTHONUNBUFFERED=1`, etc.).

## Architecture map
- `CameraService` (base) / `PiCameraService` / `WebcamCameraService` in telescopi.py: one `main` stream (H.264, circular buffer holding `PRE_ROLL_SECONDS`) + one `lores` stream analyzed at `DETECTION_FPS` for motion.
- Motion detection: background subtraction on the lores frame, day/night threshold switch (`THRESHOLD_DAY`/`THRESHOLD_NIGHT` via `BRIGHTNESS_DAY_NIGHT_THRESHOLD`), `MOTION_CONSECUTIVE_FRAMES` filters one-frame glitches, `BACKGROUND_ALPHA`/`EXPOSURE_JUMP` separate slow drift from sudden light changes.
- Telegram commands (`cmd_*` handlers): `/help`, `/stop_motion`, `/start_motion`, `/photo`, `/video`, `/status` — all gated by `allowed_users_filter` (built from `ALLOWED_USER_IDS`).
- Delivery: every Telegram send goes through a persistent disk-backed outbox — 3 immediate attempts, then retried every `RETRY_INTERVAL_SECONDS` until delivered or `PENDING_MAX_AGE_DAYS` expires; oldest items are discarded first if free disk drops below `PENDING_DISK_FREE_MIN_PERCENT`.
- Config: all tunables are env vars read via the `_env_int`/`_env_float`/`_env_bool` helpers at the top of telescopi.py — new settings must follow this pattern, not be hardcoded.

## Docs (consult before non-trivial changes)
- [Installation](../../../docs/en/installation.md)
- [Configuration](../../../docs/en/configuration.md)
- [Implementation](../../../docs/en/implementation.md)
- [Usage](../../../docs/en/utilisation.md)

## Common workflows

### Add a new Telegram command
1. Add `cmd_<name>_impl(bot, chat_id)` with the actual logic, plus a thin `cmd_<name>(update, context)` wrapper (mirror `cmd_photo_impl`/`cmd_photo`).
2. Register it: `telegram_app.add_handler(CommandHandler("<name>", cmd_<name>, filters=allowed_users_filter))`.
3. Update the `/help` text (`cmd_help_impl`) and `docs/en(+fr)/utilisation.md`.

### Tune motion detection
1. Adjust via env vars (`THRESHOLD_DAY`, `THRESHOLD_NIGHT`, `PIXEL_DIFF_THRESHOLD`, `MOTION_CONSECUTIVE_FRAMES`) — don't change code defaults without updating `docs/en(+fr)/configuration.md`.
2. Validate day and night behavior separately (`BRIGHTNESS_DAY_NIGHT_THRESHOLD` boundary).

### Switch or add a camera backend
1. Backends subclass `CameraService`; keep the same public interface (main H.264 buffer + lores analysis stream) so motion detection and `/photo`/`/video` stay backend-agnostic.
2. Guard backend-specific imports (e.g. `picamera2`) inside the subclass, not at module top level, so the other backend keeps working without that dependency installed.

### Deploy or update the systemd service
1. Edit [config/telescopi.service.template](../../../config/telescopi.service.template) if startup behavior changes.
2. Re-run install steps in `docs/en/installation.md`; reload with `systemctl daemon-reload && systemctl restart telescopi`.
3. Never commit a real `config/.env`; secrets stay local on the Pi.
