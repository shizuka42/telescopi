"""Motion detection: background subtraction analyzer + dedicated reader thread."""
import asyncio
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from telescopi import state
from telescopi.camera import CameraService
from telescopi.config import (
    BACKGROUND_ALPHA,
    BRIGHTNESS_DAY_NIGHT_THRESHOLD,
    DETECTION_FPS,
    EXPOSURE_JUMP,
    MOTION_CONSECUTIVE_FRAMES,
    PIXEL_DIFF_THRESHOLD,
    THRESHOLD_DAY,
    THRESHOLD_NIGHT,
    WATCHDOG_TIMEOUT_SECONDS,
    hard_exit,
    logger,
)


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
        while True:
            started = time.monotonic()
            try:
                gray = self._camera.grab_gray()  # always read: doubles as camera health heartbeat
                self.last_frame_at = time.monotonic()
                errors = 0
                if not state.is_active or self.busy or time.monotonic() < self.resume_at:
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


def watchdog(det: MotionDetector) -> None:
    """Watchdog thread that ensures the camera is providing frames regularly."""
    while True:
        time.sleep(10)
        if time.monotonic() - det.last_frame_at > WATCHDOG_TIMEOUT_SECONDS:
            hard_exit(f"No camera frame for {WATCHDOG_TIMEOUT_SECONDS}s")
