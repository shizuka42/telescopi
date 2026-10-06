"""Mutable process-wide state, shared by reference across modules (set once in main/post_init)."""

is_active = True       # motion detection enabled by default
is_recording = False    # a clip (motion or manual) is being written
camera = None           # CameraService
recorder = None         # Recorder
detector = None         # MotionDetector
outbox = None           # Outbox
recording_lock = None   # asyncio.Lock: only one clip at a time (one circular output)
