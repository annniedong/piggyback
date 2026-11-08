"""Check the webcam, mic, and speaker before running the brain.

  python check_devices.py          # camera, mic, then speaker
  python check_devices.py camera
  python check_devices.py mic
  python check_devices.py speaker             # list audio outputs and say a test line on the picked one
  python check_devices.py camera --snapshot   # also save what each camera sees to /tmp
  python check_devices.py gemini              # check GEMINI_API_KEY, the model name, and response time

The first run triggers macOS permission popups for camera and microphone: click Allow.
"""

import sys
import time

import brain
from vision import CAMERA_ROTATE, is_head_camera, list_cameras, pick_camera, upright, vision


def probe_cameras():
    """Which camera indices open, and do they give real pictures or black frames?"""
    import cv2

    names = list_cameras()
    for i in range(max(len(names), 1)):
        name = names[i] if i < len(names) else "?"
        cap = cv2.VideoCapture(i)
        if not cap.isOpened():
            print(f"  camera {i} ({name}): not available")
            continue
        frames = [cap.read()[1] for _ in range(15)]  # first frames can be dark while it warms up
        cap.release()
        frame = frames[-1]
        if frame is None:
            print(f"  camera {i} ({name}): opens but returns no frames")
            continue
        print(f"  camera {i} ({name}): {frame.shape[1]}x{frame.shape[0]}, brightness {frame.mean():.0f}/255"
              + ("  <- black frames" if frame.mean() < 10 else ""))
        if "--snapshot" in sys.argv:
            path = f"/tmp/piggyback_camera{i}.jpg"
            pig_camera = is_head_camera(name)
            cv2.imwrite(path, upright(frame) if pig_camera else frame)
            turned = f", turned {CAMERA_ROTATE}° counterclockwise" if pig_camera and CAMERA_ROTATE else ""
            print(f"    saved {path}{turned} (open it to see what this camera sees)")


def check_camera(seconds=12):
    import cv2

    index, name = pick_camera()
    print(f"Opening {name} (index {index}); click Allow if macOS asks...")
    deadline = time.time() + 20
    while time.time() < deadline:  # the first request fails until the permission popup is answered
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened()
        cap.release()
        if ok:
            break
        time.sleep(1)
    else:
        print("Camera didn't open. Allow it in System Settings > Privacy & Security > Camera "
              "for the app running this terminal, or pick one with CAMERA_NAME or CAMERA_INDEX.")
        return
    print("Cameras found:")
    probe_cameras()
    vision.start()
    print("Look at the camera for a few seconds, then look away.")
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.5)
        s = vision.state
        if s["error"]:
            print("ERROR:", s["error"])
            return
        print(f"{time.time() - t0:4.1f}s fps={s['fps']:4.1f} light={s['brightness']:5.1f} face={s['present']!s:5} looking={s['looking']!s:5} "
              f"yaw={s['yaw']:+.2f} pitch={s['pitch']:.2f} eyes={s['eye_turn']:.2f}", flush=True)


def check_mic():
    import sounddevice as sd

    print("Inputs:", ", ".join(d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0))
    print("Using mic:", brain.pick_mic()[1])
    print("Loading speech model...")
    brain.whisper_model()
    print("Say a sentence now (it stops when you pause).")
    text, _ = brain.listen()
    print("Heard:", repr(text) if text else "(nothing: check the input device and microphone permission)")


def check_speaker():
    """Which output Piggyback's voice goes to (SPEAKER, default "jbl"), and a test line on it."""
    import subprocess

    outputs = brain.audio_outputs()
    speaker, name = brain.pick_speaker()
    print("Audio outputs:")
    for i, n in outputs:
        print(f"  {'->' if i == speaker else '  '} {n}")
    if not speaker:
        print(f"No output named like {brain.SPEAKER!r}: using the system default. Is the speaker on and connected? "
              "Or set SPEAKER to part of a name above.")
    print(f"Saying a test line on: {name}")
    subprocess.run(["say", "-r", str(brain.SPEECH_RATE)] + (["-a", speaker] if speaker else [])
                   + ["Hi, I'm Piggyback. Put it back."])


def check_gemini():
    import asyncio
    import os

    vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")
    if vertex:
        # Signed in with `gcloud auth application-default login` instead of an API key.
        print(f"Using Vertex AI: project={os.getenv('GOOGLE_CLOUD_PROJECT')} "
              f"location={os.getenv('GOOGLE_CLOUD_LOCATION')}")
        if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
            print("Set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION (e.g. global) too.")
            return
    elif not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY isn't set. Get a key at https://aistudio.google.com/apikey, then:\n"
              "  export GEMINI_API_KEY=your-key\n"
              "(If your account blocks API keys, use Vertex AI: see 'Gemini sign-in' in CLAUDE.md.)")
        return
    from google import genai

    client = genai.Client()
    if not vertex:  # Vertex lists models differently; there we just try the configured one below
        try:
            flash = sorted(m.name.removeprefix("models/") for m in client.models.list() if "flash" in m.name)
        except Exception as e:
            print("Couldn't list models (bad key or no internet?):", repr(e))
            return
        print("Flash models this key can use:", ", ".join(flash) or "(none)")
        if brain.GEMINI_MODEL not in flash:
            print(f"GEMINI_MODEL={brain.GEMINI_MODEL} isn't in that list. Pick one above, e.g.\n"
                  f"  export GEMINI_MODEL={flash[-1] if flash else 'gemini-...-flash'}")
            return
    session = {"item": "Wireless headphones", "price": 150, "category": "electronics", "tier": "close",
               "total": 3, "questions": 0, "transcript": [], "strikes": 0, "verified": True}

    async def run():  # one event loop, so the calls share the brain's Gemini client like they do in a real run
        for _ in range(3):  # the first call includes connection setup; later ones are the real per-turn cost
            t0 = time.time()
            try:
                text = await brain.ask_gemini("Purchase: Wireless headphones for $150. Ask question 1 of 3.")
            except Exception as e:
                print(f"Gemini call with {brain.GEMINI_MODEL} failed:", repr(e))
                return
            print(f"{brain.GEMINI_MODEL} (thinking={brain.GEMINI_THINKING}) replied in {time.time() - t0:.1f}s: {text}")
        print("Parsed reply:", await brain.ask_llm(session, final=False))

    asyncio.run(run())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    which = args[0] if args else "both"
    if which == "gemini":
        check_gemini()
    if which in ("camera", "both"):
        check_camera()
    if which in ("mic", "both"):
        check_mic()
    if which in ("speaker", "both"):
        check_speaker()
