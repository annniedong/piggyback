"""Webcam in the pig's head: where the user's face is, and whether they're looking Piggyback in the eyes.

Runs in a background thread. brain.py reads `vision.state` and `vision.jpeg()`.
Frames stay in memory; nothing is saved to disk.
"""

import os
import threading
import time
from pathlib import Path

CAMERA_NAME = os.getenv("CAMERA_NAME", "logitech")  # picked by name so a nearby iPhone can't take over
CAMERA_INDEX = os.getenv("CAMERA_INDEX")  # set a number to skip name matching
# Wireless camera: a video stream URL (e.g. the Logitech on a Pi running ustreamer,
# http://pig-cam.local:8080/stream). When set, CAMERA_NAME and CAMERA_INDEX are ignored.
CAMERA_URL = os.getenv("CAMERA_URL")
# Webcam MJPEG often has odd APP segments; FFmpeg then prints "unable to decode APP fields"
# for every frame even though it decodes fine. -8 = quiet. Must be set before cv2 loads.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
# Never fall back to these: Continuity Camera phones and virtual cameras show the wrong picture.
SKIP_DEVICES = ("iphone", "ipad", "desk view", "continuity", "blackhole", "virtual", "obs", "zoom", "teams")
MODEL_PATH = Path(__file__).parent / "models" / "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/latest/face_landmarker.task")

# "Looking at the pig" thresholds. Tune with GET /vision while sitting where the user sits.
MAX_YAW = float(os.getenv("LOOK_MAX_YAW", "0.15"))        # nose offset / eye width; 0 = facing camera
PITCH_RANGE = (float(os.getenv("LOOK_MIN_PITCH", "0.25")), float(os.getenv("LOOK_MAX_PITCH", "0.85")))
MAX_EYE_TURN = float(os.getenv("LOOK_MAX_EYE_TURN", "0.45"))  # MediaPipe eyeLook* blendshapes, 0..1

# Landmark indices in MediaPipe's face mesh.
NOSE_TIP, LEFT_EYE_OUTER, RIGHT_EYE_OUTER = 1, 33, 263
EYE_LOOK = ["eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft", "eyeLookDownLeft",
            "eyeLookInRight", "eyeLookOutRight", "eyeLookUpRight", "eyeLookDownRight"]


def list_cameras() -> list:
    """Camera names in OpenCV's index order (OpenCV on macOS sorts AVFoundation devices by uniqueID)."""
    try:
        import AVFoundation as AV
    except ImportError:
        return []
    devices = (list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo))
               + list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeMuxed)))
    return [d.localizedName() for d in sorted(devices, key=lambda d: d.uniqueID())]


def pick_camera():
    """(index, name): CAMERA_INDEX if set, else the first name containing CAMERA_NAME,
    else the first camera that isn't a phone or virtual camera."""
    names = list_cameras()
    if CAMERA_INDEX is not None:
        i = int(CAMERA_INDEX)
        return i, names[i] if i < len(names) else f"camera {i}"
    for i, name in enumerate(names):
        if CAMERA_NAME.lower() in name.lower():
            return i, name
    for i, name in enumerate(names):
        if not any(word in name.lower() for word in SKIP_DEVICES):
            return i, name
    return 0, names[0] if names else "camera 0"


class StreamCapture:
    """Reads a network video stream (MJPEG over HTTP, RTSP, ...) like cv2.VideoCapture.

    A background thread keeps only the newest frame, so a slow face tracker never falls
    behind a buffered stream, and it reconnects if Wi-Fi drops."""

    def __init__(self, url: str):
        import cv2

        self.url = url
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(url)
        self._frame = None
        self._new = threading.Event()
        self._lock = threading.Lock()
        if self._cap.isOpened():
            threading.Thread(target=self._grab, daemon=True).start()

    def isOpened(self) -> bool:
        return self._cap.isOpened()

    def _grab(self):
        failed_since = None
        while True:
            ok, frame = self._cap.read()
            if ok:
                failed_since = None
                with self._lock:
                    self._frame = frame
                self._new.set()
                continue
            now = time.monotonic()
            failed_since = failed_since or now
            if now - failed_since > 2:  # stream lost: reopen it
                print("Camera stream lost, reconnecting:", self.url, flush=True)
                self._cap.release()
                time.sleep(1)
                self._cap = self._cv2.VideoCapture(self.url)
                failed_since = None
            else:
                time.sleep(0.05)

    def read(self):
        """The newest frame not yet returned; waits up to 1 s for one."""
        if not self._new.wait(1.0):
            return False, None
        with self._lock:
            self._new.clear()
            return True, self._frame

    def release(self):
        self._cap.release()


def measure(landmarks, blendshapes) -> dict:
    """Turn one face's landmarks into head-turn and eye-turn numbers. Pure function, easy to test."""
    nose, left, right = landmarks[NOSE_TIP], landmarks[LEFT_EYE_OUTER], landmarks[RIGHT_EYE_OUTER]
    eye_w = max(abs(right.x - left.x), 1e-6)
    mid_x, mid_y = (left.x + right.x) / 2, (left.y + right.y) / 2
    yaw = (nose.x - mid_x) / eye_w      # turning left/right moves the nose off the eye midpoint
    pitch = (nose.y - mid_y) / eye_w    # nodding changes how far the nose sits below the eyes
    scores = {b.category_name: b.score for b in blendshapes}
    eye_turn = max(scores.get(name, 0.0) for name in EYE_LOOK)
    blink = min(scores.get("eyeBlinkLeft", 0.0), scores.get("eyeBlinkRight", 0.0))
    looking = (abs(yaw) < MAX_YAW and PITCH_RANGE[0] < pitch < PITCH_RANGE[1]
               and eye_turn < MAX_EYE_TURN and blink < 0.6)
    return {"face_x": mid_x, "face_y": mid_y, "yaw": yaw, "pitch": pitch,
            "eye_turn": eye_turn, "looking_raw": looking}


class Vision:
    def __init__(self):
        self.state = {"running": False, "present": False, "looking": False, "looking_for": 0.0,
                      "face_x": 0.5, "face_y": 0.5, "yaw": 0.0, "pitch": 0.0, "eye_turn": 0.0,
                      "last_seen": 0.0, "fps": 0.0, "brightness": 0.0, "camera": None, "error": None}
        self._frame = None
        self._lock = threading.Lock()
        self._score = 0.0
        self._looking_since = None

    def start(self):
        """Call from the main thread: macOS only shows the camera permission prompt there."""
        try:
            import cv2

            if CAMERA_URL:
                self.state["camera"] = CAMERA_URL
                print(f"Camera: stream {CAMERA_URL}", flush=True)
                cap = StreamCapture(CAMERA_URL)
                if not cap.isOpened():
                    raise RuntimeError(f"camera stream {CAMERA_URL} didn't open: check the URL in a browser, "
                                       "and that the laptop and the camera are on the same Wi-Fi")
                threading.Thread(target=self._run, args=(cap,), daemon=True).start()
                return
            index, name = pick_camera()
            self.state["camera"] = name
            print(f"Camera: {name} (index {index})", flush=True)
            cap = cv2.VideoCapture(index)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            if not cap.isOpened():
                raise RuntimeError(f"{name} didn't open: allow camera access for this app "
                                   "(System Settings > Privacy & Security > Camera), or set CAMERA_INDEX")
        except Exception as e:
            self.state["error"] = repr(e)
            print("Vision disabled:", repr(e), flush=True)
            return
        threading.Thread(target=self._run, args=(cap,), daemon=True).start()

    def _run(self, cap):
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            if not MODEL_PATH.exists():
                import urllib.request
                MODEL_PATH.parent.mkdir(exist_ok=True)
                urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
                running_mode=vision.RunningMode.VIDEO, num_faces=1, output_face_blendshapes=True))
        except Exception as e:
            self.state["error"] = repr(e)
            print("Vision disabled:", repr(e), flush=True)
            return

        self.state["running"] = True
        last = time.monotonic()
        n = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            n += 1
            if n % 15 == 0:  # near 0 means black frames (lens covered, wrong camera, or no permission)
                self.state["brightness"] = round(float(frame.mean()), 1)
            now = time.monotonic()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int(now * 1000))
            with self._lock:
                self._frame = frame
            self._update(result, now, now - last)
            last = now

    def _update(self, result, now, dt):
        s = self.state
        s["fps"] = round(0.9 * s["fps"] + 0.1 / max(dt, 1e-3), 1)
        raw = False
        if result.face_landmarks:
            m = measure(result.face_landmarks[0], result.face_blendshapes[0] if result.face_blendshapes else [])
            s.update({k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()})
            s["present"], s["last_seen"] = True, time.time()
            raw = m["looking_raw"]
        elif time.time() - s["last_seen"] > 1.0:
            s["present"] = False
        # Smooth so a blink or one bad frame doesn't break eye contact.
        self._score += (1.0 if raw else -1.0) * dt / 0.25
        self._score = min(max(self._score, 0.0), 1.0)
        looking = self._score > (0.3 if s["looking"] else 0.7)
        if looking and not s["looking"]:
            self._looking_since = now
        s["looking"] = looking
        s["looking_for"] = round(now - self._looking_since, 2) if looking else 0.0

    def jpeg(self, width=512, overlay=False):
        """Latest frame as JPEG bytes (downscaled), or None if the camera isn't running."""
        import cv2

        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            return None
        if overlay:
            s = self.state
            h, w = frame.shape[:2]
            color = (0, 200, 0) if s["looking"] else (0, 0, 255)
            if s["present"]:
                cv2.circle(frame, (int(s["face_x"] * w), int(s["face_y"] * h)), 12, color, 3)
            cv2.putText(frame, f"looking={s['looking']} yaw={s['yaw']:.2f} pitch={s['pitch']:.2f} "
                        f"eyes={s['eye_turn']:.2f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        scale = width / frame.shape[1]
        frame = cv2.resize(frame, (width, int(frame.shape[0] * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None


vision = Vision()
