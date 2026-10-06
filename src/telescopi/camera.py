"""Camera backends: Picamera2 (Pi camera module) and USB webcam (OpenCV/V4L2 + ffmpeg).

Both expose the same CameraService interface so motion detection and photo/video commands stay
backend-agnostic (see telescopi.motion and telescopi.bot).
"""
import collections
import queue
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
# picamera2/libcamera are only imported inside PiCameraService, so this module also runs on a
# plain machine with just a USB webcam and no Pi camera stack installed (CAMERA_BACKEND=usb).

from telescopi.config import (
    CAMERA_BACKEND,
    CAMERA_ROTATE_180,
    LORES_HEIGHT,
    LORES_WIDTH,
    MOTION_SNAPSHOT_DRAW_BOX,
    PRE_ROLL_SECONDS,
    VIDEO_BITRATE,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    WEBCAM_DEVICE,
    WEBCAM_FOURCC,
    logger,
)


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
        # Imported here (not at module level) so this file also runs with CAMERA_BACKEND=usb on a
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


def build_camera_service() -> CameraService:
    """Picks the camera backend to use, based on the CAMERA_BACKEND env var."""
    if CAMERA_BACKEND == "usb":
        return WebcamCameraService()
    return PiCameraService()


def wrap_h264_to_mp4(raw: Path, mp4: Path) -> bool:
    """Remuxes the raw H.264 stream to MP4 (no re-encoding). -framerate keeps the real playback speed.
    raw file is removed after successful remuxing.
    """
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
