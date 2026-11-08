"""Piggyback brain: laptop server between the Chrome extension, the LLM, and the ESP32 head.

Run: uvicorn brain:app --port 8000
"""

import asyncio
import glob
import json
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import serial
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

import store
import sales
import tiger
from sales import load_env_file
from sales import router as sales_router
from vision import vision

load_env_file()  # settings from the git-ignored .env, before the config below reads them; real env vars win

# ---------------------------------------------------------------- config

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")  # gemini | claude | mock
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low")  # low | medium | high (some models: minimal); lower = faster
# Tried when GEMINI_MODEL is rate-limited or times out. Quotas are per model, so this one usually has room.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
EXECUTOR_MODEL = os.getenv("EXECUTOR_MODEL", "claude-sonnet-5-5")
ADVISOR_MODEL = os.getenv("ADVISOR_MODEL", "claude-opus-5-5")
USE_ADVISOR = os.getenv("USE_ADVISOR", "1") == "1"
# On-demand budget review: Gemini (same sign-in as the checkout chat) unless a Claude key is set.
REVIEW_PROVIDER = os.getenv("REVIEW_PROVIDER") or ("claude" if os.getenv("ANTHROPIC_API_KEY") else "gemini")
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-opus-5-5")  # when REVIEW_PROVIDER=claude
SERIAL_PORT = os.getenv("SERIAL_PORT")  # None = auto-detect
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
VOICE = os.getenv("VOICE")  # None = system default voice
SPEECH_RATE = int(os.getenv("SPEECH_RATE", "215"))  # words per minute for `say` (macOS default is about 180)
END_PAUSE = float(os.getenv("END_PAUSE", "0.8"))  # seconds of quiet that end a spoken answer
TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "60"))
# Judging table: the extension never places a real order, and the profile goes back to the seeded demo
# for each visitor (the dashboard's "Next visitor" button, or by itself after DEMO_RESET_MINUTES idle).
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"
DEMO_RESET_MINUTES = float(os.getenv("DEMO_RESET_MINUTES", "10"))
LISTEN = os.getenv("LISTEN", "1") == "1"  # hear spoken answers on the laptop mic
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")
SPEAKER = os.getenv("SPEAKER", "macbook")  # Piggyback's voice: the first audio output whose name contains this
MIC_DEVICE = os.getenv("MIC_DEVICE", "macbook")  # the laptop's own mic; picked by name so iPhone/AirPods mics can't take over
MAX_LISTEN_SECONDS = float(os.getenv("MAX_LISTEN_SECONDS", "8"))
NO_SPEECH_SECONDS = float(os.getenv("NO_SPEECH_SECONDS", "6"))
VISION = os.getenv("VISION", "1") == "1"  # webcam in the head: tracking + eye contact
SEND_FRAMES = os.getenv("SEND_FRAMES", "1") == "1"  # attach a webcam frame to Gemini calls
CONFIRM_SECONDS = float(os.getenv("CONFIRM_SECONDS", "3"))  # eye contact needed to confirm a release
CONFIRM_WAIT = float(os.getenv("CONFIRM_WAIT", "10"))
TRACK_GAIN = float(os.getenv("TRACK_GAIN", "0.15"))
# Where the head rests when nobody's buying anything ("look" units, pan,tilt): turned away, minding its own
# business. Something going in the bag starts a slow turn toward the user; at checkout it's facing them.
IDLE_LOOK = [float(v) for v in os.getenv("IDLE_LOOK", "0.6,0").split(",")]
TURN_SECONDS = float(os.getenv("TURN_SECONDS", "1.5"))  # how long a turn toward or away from the user takes
TRACK_INVERT_PAN = os.getenv("TRACK_INVERT_PAN", "0") == "1"
GAZE_INVERT_X = os.getenv("GAZE_INVERT_X", "0") == "1"
# Pricier purchases mean a longer argument: "close" uses over half of what's left, "over" exceeds it.
QUESTIONS_BY_TIER = {"easy": 2, "close": 3, "over": 3}
LOOK_AWAY_GRACE = float(os.getenv("LOOK_AWAY_GRACE", "0.6"))  # seconds of looking away that breaks an answer

HERE = Path(__file__).parent
DECISIONS_FILE = Path(os.getenv("DECISIONS_FILE", HERE / "decisions.jsonl"))  # tests point this elsewhere

EXPRESSIONS = ["idle", "alert", "suspicious", "skeptical", "satisfied", "disappointed", "thinking"]
# How long tracking waits after an expression so its head turns can play (matches the steps in pig_eyes.ino).
EXPR_SECONDS = {"suspicious": 2.7, "skeptical": 2.2, "disappointed": 4.6, "thinking": 3.0}
VERDICTS = ["release", "timer", "escalate"]
SERIAL_GLOBS = ["/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*"]

SYSTEM_PROMPT = """You are Piggyback, a robot piggy bank with a moving head and OLED eyes who sits on
the user's desk and checks in on their online purchases before checkout. Your catchphrase is
"Put it back." Save it for timers; don't say it every line.

Personality: warm, playful, a little cheeky, like a friend who's good with money and on their side.
Curious, not accusatory: ask, don't lecture. Tease the purchase lightly, never the person, and never
be sarcastic, harsh, or guilt-trippy. When they make a good point, say so.

Your job is to stop impulse buys, not necessities. Tell needs from wants:
- A need is something they rely on that broke or ran out, health or safety, or something work or school
  requires, and it can't reasonably wait. A need deserves a release even when it hurts the budget; you
  can still nudge them toward a cheaper option.
- A want is an upgrade, a duplicate, a treat, a gift, or "it's on sale". Wants that strain the budget wait.
- Believe a reason that is specific and stays consistent. A vague reason, or a story that changes between
  answers, is a want until proven otherwise.

Every reply is ONE spoken line: under 18 words, plain words, no emoji, no lists, no stage directions.
It is read aloud by text-to-speech, so it must sound natural spoken.

Only use facts you're given: the purchase, the budget, their spending history, their savings goals, past
attempts to buy this, what you noticed on the page before checkout, what they've said, and a webcam photo
if one is attached. Never invent details about their room, desk, or belongings that you
weren't shown.

Reply with JSON only: {"line": str, "expression": str, "verdict": str, "need": str}
- expression is one of: idle, alert, suspicious, skeptical, satisfied, disappointed, thinking.
- When asked a question turn, ask ONE short, friendly question about the purchase and set verdict to "none".
  Good questions let them prove a need: what happened to the old one, what they use it for, whether it
  can wait, whether a cheaper option would do. Don't ask about other people's finances.
  If they're saving for a goal, ask how the purchase fits with it. If they've tried to buy this
  before, or said something different last time, mention it kindly.
  What you noticed on the page (an ad or sponsored listing brought them, an ad that followed them around
  the web, add-ons from "frequently bought together", a sale or countdown, a big cart, lots of shopping
  today) are classic impulse pushes: bring them up and ask if they'd still buy it without the push.
  You can explain the trick in a few words (sellers pay for every click; retargeting ads follow you after
  you look at something). They're reasons to question, not reasons to refuse; a real need still wins.
- The user answers out loud while looking you in the eyes, and the prompt says how their eye contact went.
  Holding your gaze while arguing shows conviction; looking away is worth a light tease. Still judge
  mainly on their reasons, not on eye contact alone.
- When asked for the final verdict, set verdict to one of:
  "release" (a reasonable purchase or a proven need: the default when the reasons are sensible),
  "timer" (probably an impulse: a friendly "sleep on it", not a punishment),
  "escalate" (clearly a bad idea or way over budget: loop in their accountability buddy).
  The line announces the verdict in character. Use "satisfied" for release, "skeptical" or
  "disappointed" for timer, "disappointed" for escalate.
- need is your current read: "need" (they've shown a real necessity), "want", or "unclear".
- If your final need is "need", the verdict is "release": a timer on something urgent only hurts them.
  If the price stings, say so and point them to a cheaper option in the line, but still release.
  Timer and escalate are for wants and unclear cases."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "line": {"type": "string"},
        "expression": {"type": "string", "enum": EXPRESSIONS},
        "verdict": {"type": "string", "enum": ["none"] + VERDICTS},
        "need": {"type": "string", "enum": ["need", "want", "unclear"]},
    },
    "required": ["line", "expression", "verdict", "need"],
    "additionalProperties": False,
}

NUDGE_LINE = "Take your time. I'm listening."
ACKS = ["Hmm.", "Okay.", "Mm-hm.", "Right.", "Interesting."]  # said right after an answer is heard

DEFAULT_QUESTION = {"line": "Hold on. Do you actually need this, or do you just want it?", "expression": "suspicious", "verdict": "none"}
DEFAULT_VERDICT = {"line": "I can't decide, so you wait. Put it back for a minute.", "expression": "skeptical", "verdict": "timer"}

RULE_LINES = {
    "over_budget": ("escalate", "disappointed", "That's more than your whole remaining budget, so I'm looping in your buddy on this one."),
    "half_budget": ("timer", "skeptical", "That's over half of what's left this month. Let's give it a minute first."),
    "invalid_verdict": ("timer", "thinking", "I can't decide on this one. Give it a minute and see if you still want it."),
    "no_eye_contact": ("timer", "skeptical", "I didn't get that stare, so let's give it a minute."),
    "looked_away": ("timer", "skeptical", "You looked away twice, so let's pause this one for a minute."),
    "over_budget_argued": ("timer", "thinking", "It's over budget, but you made a real case to my face. "
                                                "Sleep on it instead of me calling your buddy."),
}


def log(*args):
    print(datetime.now().strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------- budget rules

def budget_tier(price: float, remaining: float) -> str:
    if price > remaining:
        return "over"
    return "close" if price > remaining / 2 else "easy"


def apply_rules(verdict, price: float, remaining: float, clean: bool = False, failed: bool = False,
                need: bool = False):
    """Hard rules can only make a verdict stricter. Returns (verdict, rule or None).

    clean: the user answered every question with unbroken, camera-verified eye contact.
    failed: they looked away mid-answer twice, which ends the argument.
    need: the AI judged it a genuine necessity.
    A clean argument can save a close-to-budget release, or soften over-budget from escalate to timer.
    An over-budget purchase is only released for a clean argument that proved a necessity."""
    tier = budget_tier(price, remaining)
    if failed:
        if verdict == "escalate" or tier == "over":
            return "escalate", "looked_away"
        return "timer", "looked_away"
    if tier == "over":
        if clean and need and verdict == "release":
            return "release", "necessity"
        if clean and verdict in ("release", "timer"):
            return "timer", "over_budget_argued"
        return "escalate", "over_budget"
    if verdict not in VERDICTS:
        return "timer", "invalid_verdict"
    if verdict == "release" and tier == "close" and not clean:
        return "timer", "half_budget"
    return verdict, None


# ---------------------------------------------------------------- ESP32 link

# Shared with the tracking loop: expressions pause head tracking, recording freezes the head.
tracking = {"paused_until": 0.0, "recording": False, "look": [0.0, 0.0], "gaze": [0.0, 0.0], "session": None,
            "user_look": [0.0, 0.0],  # head direction that last had the user's face centered
            "engaged_until": 0.0}     # facing the user until then (or while a checkout session is open)


class Head:
    """USB serial link to the ESP32. If it's unplugged, commands are printed instead."""

    def __init__(self):
        self.ser = None
        self.lock = threading.Lock()
        self.last_attempt = 0.0

    def _find_port(self):
        if SERIAL_PORT:
            return SERIAL_PORT
        for pattern in SERIAL_GLOBS:
            ports = sorted(glob.glob(pattern))
            if ports:
                return ports[0]
        return None

    def _open(self):
        # Retry at most every 3 s so an unplugged board doesn't slow every command.
        if time.time() - self.last_attempt < 3:
            return
        self.last_attempt = time.time()
        port = self._find_port()
        if not port:
            return
        try:
            ser = serial.Serial(port, SERIAL_BAUD, timeout=0.1)
        except serial.SerialException as e:
            log("ESP32 open failed:", e)
            return
        # Opening the port resets the ESP32; wait for {"ready": true} or ~2.5 s.
        deadline = time.time() + 2.5
        while time.time() < deadline:
            line = ser.readline().decode(errors="replace").strip()
            if '"ready"' in line:
                break
        self.ser = ser
        log("ESP32 connected on", port)
        threading.Thread(target=self._read_loop, args=(ser,), daemon=True).start()

    def _read_loop(self, ser):
        while self.ser is ser:
            try:
                line = ser.readline().decode(errors="replace").strip()
            except (serial.SerialException, OSError):
                break
            if line and line != '{"ok":true}':
                log("ESP32 <", line)
        log("ESP32 reader stopped")

    def connected(self) -> bool:
        return self.ser is not None

    def send(self, cmd: dict, quiet: bool = False):
        """quiet=True for the many-per-second tracking commands, so they don't flood the log."""
        with self.lock:
            if self.ser is None:
                self._open()
            if self.ser is None:
                if not quiet:
                    log("ESP32 (offline) >", json.dumps(cmd))
                return
            try:
                self.ser.write((json.dumps(cmd) + "\n").encode())
                if not quiet:
                    log("ESP32 >", json.dumps(cmd))
            except (serial.SerialException, OSError) as e:
                log("ESP32 write failed, will reconnect:", e)
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    async def expr(self, name: str):
        # let the expression's head turns (or a slow turn still going) play before tracking resumes
        tracking["paused_until"] = max(tracking["paused_until"], time.time() + EXPR_SECONDS.get(name, 1.3))
        await asyncio.to_thread(self.send, {"expr": name})


def engaged() -> bool:
    return bool(tracking["session"]) or time.time() < tracking["engaged_until"]


async def turn_to_user(hold: float = 0):
    """Start the slow turn toward where the user was last seen; tracking fine-tunes it once the camera
    (in the head) sees them. The head keeps facing them for `hold` seconds, or while a checkout is open."""
    tracking["engaged_until"] = max(tracking["engaged_until"], time.time() + hold)
    if tracking["look"] == tracking["user_look"]:
        return
    tracking["look"] = list(tracking["user_look"])
    tracking["paused_until"] = time.time() + TURN_SECONDS
    await asyncio.to_thread(head.send, {"look": tracking["look"], "s": TURN_SECONDS})


head = Head()


# ---------------------------------------------------------------- speech

speech_proc = None
_speaker = {"checked": 0.0, "id": None, "name": "system default"}


def audio_outputs() -> list[tuple[str, str]]:
    """(id, name) of every audio output, from `say -a ?` (lines like "   94 MacBook Pro Speakers")."""
    import subprocess

    try:
        out = subprocess.run(["say", "-a", "?"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [tuple(line.strip().split(" ", 1)) for line in out.splitlines() if line.strip()[:1].isdigit()]


def pick_speaker() -> tuple[str | None, str]:
    """(id, name) of the first output whose name contains SPEAKER, else the system default (id None).
    Re-checked every 30 s, so a speaker that connects or reconnects (new id) is picked up."""
    if time.time() - _speaker["checked"] > 30:
        found = next(((i, n) for i, n in audio_outputs() if SPEAKER and SPEAKER.lower() in n.lower()),
                     (None, "system default"))
        if found[1] != _speaker["name"] or not _speaker["checked"]:
            log(f"Speaker: {found[1]}" + ("" if found[0] else f" (no output named like {SPEAKER!r})"))
        _speaker.update(checked=time.time(), id=found[0], name=found[1])
    return _speaker["id"], _speaker["name"]


async def speak(line: str):
    """Speak with macOS `say`, cutting off anything still being said."""
    global speech_proc
    log("🐷 Piggyback says:", line)
    if speech_proc and speech_proc.returncode is None:
        speech_proc.terminate()
    speaker, _ = pick_speaker()
    args = (["say", "-r", str(SPEECH_RATE)] + (["-v", VOICE] if VOICE else [])
            + (["-a", speaker] if speaker else []) + [line])
    try:
        speech_proc = await asyncio.create_subprocess_exec(*args)
    except FileNotFoundError:
        log("SAY (no `say` command):", line)


async def done_speaking():
    if speech_proc and speech_proc.returncode is None:
        await speech_proc.wait()


# ---------------------------------------------------------------- speech input

SAMPLE_RATE = 16000
FRAME = 480  # 30 ms at 16 kHz

_whisper = None


def whisper_model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")  # downloads on first use
    return _whisper


def pick_mic():
    """(index, name): first input whose name contains MIC_DEVICE, else the system default input
    unless it's a phone or virtual device, else the first input that isn't."""
    import sounddevice as sd
    from vision import SKIP_DEVICES

    inputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    for i, name in inputs:
        if MIC_DEVICE.lower() in name.lower():
            return i, name
    usable = [(i, n) for i, n in inputs if not any(word in n.lower() for word in SKIP_DEVICES)]
    default = sd.default.device[0]
    for i, name in usable:
        if i == default:
            return i, name
    return usable[0] if usable else (None, "system default")


def mic_frames():
    import sounddevice as sd

    device, name = pick_mic()
    log(f"Mic: {name}")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME,
                        device=device) as stream:
        while True:
            data, _ = stream.read(FRAME)
            yield data[:, 0].copy()


def record_answer(frames, gate=lambda: True):
    """Collect audio from the first loud frame until a 1 s pause. Returns (audio or None, broke).

    gate() is checked every frame (is the user looking at the pig?). Speech only starts counting while
    it's true, and once they're talking, looking away for LOOK_AWAY_GRACE seconds breaks the answer:
    recording stops and broke=True."""
    import numpy as np
    from collections import deque

    noise, preroll, voiced = [], deque(maxlen=10), []
    started, silence, elapsed, peak = False, 0.0, 0.0, 0.0
    for f in frames:
        elapsed += FRAME / SAMPLE_RATE
        rms = float(np.sqrt(np.mean(f ** 2)))
        if len(noise) < 10:  # first 0.3 s measures the room so a noisy venue doesn't count as speech
            noise.append(rms)
            continue
        loud = rms > max(float(np.median(noise)) * 3, 0.01)
        looking = gate()
        if not started:
            preroll.append(f)
            if loud and looking:
                started, away = True, 0.0
                voiced.extend(preroll)
            elif elapsed > NO_SPEECH_SECONDS:
                return None, False
            continue
        voiced.append(f)
        away = 0.0 if looking else away + FRAME / SAMPLE_RATE
        if away > LOOK_AWAY_GRACE:
            return np.concatenate(voiced), True
        # The room can get louder after the first 0.3 s (fans, the mic's auto gain), so "done talking"
        # also counts anything well below their speaking volume as a pause.
        peak = max(peak, rms)
        talking = loud and rms > peak * 0.2
        silence = 0.0 if talking else silence + FRAME / SAMPLE_RATE
        if silence > END_PAUSE or elapsed > MAX_LISTEN_SECONDS:
            break
    return (np.concatenate(voiced) if voiced else None), False


def transcribe(audio) -> str:
    segments, _ = whisper_model().transcribe(audio, language="en", beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def listen(gate=lambda: True):
    """Returns (transcript, broke). broke=True means they looked away mid-answer."""
    t0 = time.time()
    frames = mic_frames()
    tracking["recording"] = True  # hold the head still so servo noise doesn't reach the mic
    head.send({"hold": 1})  # also stops the ESP32's own turns (thinking sway, a look-back still playing)
    try:
        audio, broke = record_answer(frames, gate)
    finally:
        tracking["recording"] = False
        head.send({"hold": 0})
        frames.close()
    t1 = time.time()
    text = transcribe(audio) if audio is not None else ""
    spoken = len(audio) / SAMPLE_RATE if audio is not None else 0
    log(f"Timing: listened {t1 - t0:.1f}s (speech {spoken:.1f}s), transcribed in {time.time() - t1:.1f}s")
    return text, broke


# ---------------------------------------------------------------- LLM

def parse_reply(text: str, default: dict) -> dict:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        line = str(data["line"]).strip()
        if not line:
            raise ValueError("empty line")
    except (ValueError, KeyError, TypeError) as e:
        log("Bad LLM JSON, using default:", e, repr(text[:200]))
        return dict(default)
    expression = data.get("expression") if data.get("expression") in EXPRESSIONS else default["expression"]
    return {"line": line, "expression": expression, "verdict": data.get("verdict", "none"),
            "need": data.get("need", "unclear")}


FRAME_NOTE = ("\n\nAttached is a webcam photo of the user taken just now from Piggyback's eyes. You may playfully "
              "react to an object clearly visible in it (something they're holding or wearing). Don't comment on "
              "their eyes, face, or expression: the photo may catch a blink, and eye contact is measured separately "
              "and reported above. Never judge honesty from the photo or base the verdict on it.")


_gemini = None
_gemini_no_thinking = set()  # models that rejected the thinking setting


async def ask_gemini(prompt: str, image: bytes | None = None, system: str = SYSTEM_PROMPT,
                     schema: dict = RESPONSE_SCHEMA, fast: bool = False) -> str:
    """fast: one quick line where speed beats depth (add-to-bag remarks): use the lighter model only."""
    global _gemini
    from google import genai
    from google.genai import errors, types

    if _gemini is None:
        # One client for the whole run, so each turn reuses the connection. Uses the
        # GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_* settings, or GEMINI_API_KEY. Never Alisha's page-reading key.
        _gemini = genai.Client()
    contents = prompt
    if image:
        contents = [types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt + FRAME_NOTE]

    def config(thinking: bool):
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            # One short spoken line doesn't need much reasoning, and thinking is most of the wait.
            thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING) if thinking else None,
        )

    models = [GEMINI_MODEL] + ([GEMINI_FALLBACK_MODEL] if GEMINI_FALLBACK_MODEL not in ("", GEMINI_MODEL) else [])
    if fast and GEMINI_FALLBACK_MODEL:
        models = [GEMINI_FALLBACK_MODEL]
    for i, model in enumerate(models):
        for thinking in ((False,) if model in _gemini_no_thinking else (True, False)):
            try:
                resp = await _gemini.aio.models.generate_content(model=model, contents=contents,
                                                                 config=config(thinking))
                return resp.text or ""
            except errors.APIError as e:
                if thinking and e.code == 400:  # this model doesn't support the thinking setting
                    log(f"Gemini {model} rejected thinking_level={GEMINI_THINKING}, retrying without it")
                    _gemini_no_thinking.add(model)
                    continue
                if e.code in (429, 503, 504) and i + 1 < len(models):
                    log(f"Gemini {model} unavailable ({e.code}), retrying with {models[i + 1]}")
                    break
                raise
    return ""


_claude = None


def claude_client():
    global _claude
    import anthropic

    if _claude is None:
        _claude = anthropic.AsyncAnthropic()
    return _claude


async def ask_claude(prompt: str, use_advisor: bool) -> str:
    betas = ["server-side-fallback-2026-07-01"]
    kwargs = {}
    if use_advisor:
        betas.append("advisor-tool-2026-03-01")
        kwargs["tools"] = [{"type": "advisor_20260301", "name": "advisor", "model": ADVISOR_MODEL, "max_uses": 1}]
    resp = await claude_client().beta.messages.create(
        model=EXECUTOR_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        fallbacks="default",
        betas=betas,
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        log("Claude refused:", resp.stop_details)
        return ""
    texts = [b.text for b in resp.content if b.type == "text"]
    return texts[-1] if texts else ""


MOCK_QUESTIONS = [
    {"line": "Do you already own something that does this?", "expression": "suspicious", "verdict": "none"},
    {"line": "Would you still want it if it arrived next month?", "expression": "skeptical", "verdict": "none"},
    {"line": "What would you give up this month to pay for it?", "expression": "skeptical", "verdict": "none"},
]


def ask_mock(final: bool, question_number: int = 0) -> str:
    """Canned lines for testing without an API key. Doesn't read the answers, unlike the real AI."""
    if final:
        return json.dumps(random.choice([
            {"line": "Fine. It's reasonable. Go ahead, but I'm watching.", "expression": "satisfied", "verdict": "release"},
            {"line": "Sounds like an impulse. Sit with it for a minute.", "expression": "skeptical", "verdict": "timer"},
        ]))
    return json.dumps(MOCK_QUESTIONS[question_number % len(MOCK_QUESTIONS)])


async def ask_llm(session: dict, final: bool) -> dict:
    profile = store.load()
    remaining = store.remaining(profile)
    recent = ", ".join(f'{p["item"]} ${p["price"]}' for p in profile["purchases"][-5:])
    history = store.context_for(profile, session["item"], session["price"])
    seen = sales.seen_on(session.get("url"))
    transcript = "\n".join(f"{who}: {text}" for who, text in session["transcript"]) or "(nothing yet)"
    turn = ("Give your FINAL VERDICT now." if final else
            f"Ask question {session['questions'] + 1} of {session['total']}.")
    tier_note = {
        "easy": "This fits the budget comfortably. Keep it light, and release it if their reasons are sensible.",
        "close": "This uses more than half of what's left this month. Ask them to make their case and gently "
                 "push on vague reasons. A release only stands if they hold eye contact through every answer.",
        "over": "This is MORE than the whole remaining budget. Ask them to make a real case. Only a proven "
                "necessity that can't wait earns a release; a good argument for a want earns a timer; otherwise escalate.",
    }[session["tier"]]
    if not session["verified"]:
        eyes = "Eye contact can't be checked right now."
    elif session["strikes"]:
        eyes = "They looked away mid-answer once and had to redo it."
    else:
        eyes = "They've held eye contact through every answer so far."
    prompt = (
        f"Purchase: {session['item']} for ${session['price']} (category: {session['category']}).\n"
        f"Monthly budget ${profile['monthly_budget']}, ${remaining:.0f} left. Recent purchases: {recent}.\n"
        + (f"{history}\n" if history else "")
        + (f"What you noticed on the page before checkout:\n{seen}\n" if seen else "") +
        f"{tier_note}\n{eyes}\n"
        f"Conversation so far:\n{transcript}\n\n{turn}"
    )
    default = DEFAULT_VERDICT if final else DEFAULT_QUESTION
    try:
        if LLM_PROVIDER == "mock":
            text = ask_mock(final, session["questions"])
        elif LLM_PROVIDER == "claude":
            use_advisor = USE_ADVISOR and final  # advisor only for the verdict, to keep questions fast
            try:
                text = await ask_claude(prompt, use_advisor)
            except Exception as e:
                if not use_advisor:
                    raise
                log("Advisor call failed, retrying without it:", e)
                text = await ask_claude(prompt, False)
        else:
            image = vision.jpeg() if SEND_FRAMES and vision.state["present"] else None
            text = await ask_gemini(prompt, image)
    except Exception as e:
        log(f"LLM error ({LLM_PROVIDER}):", repr(e))
        text = ""
    return parse_reply(text, default)


# ---------------------------------------------------------------- budget review (Claude)

REVIEW_PROMPT = """You are Piggyback, a robot piggy bank, giving the user a quick review of this month's
spending. Dry, funny, on their side. Base everything only on the data given; never invent numbers.
checkouts_completed / checkouts_abandoned count this calendar month; checkouts_today counts only today.
Only say "today" about numbers from checkouts_today.
Give general budgeting guidance (spending patterns, categories to cut, habits like waiting before
buying). Do not give investment, tax, credit, or debt advice, and do not recommend financial products.
If they have savings goals, say how this month's spending affects them. Give them credit for things
they let go, and call out items still in the waiting room or arguments they lost.

Reply with JSON:
- summary: what Piggyback says out loud, under 45 words, plain spoken language, no emoji or lists.
- tips: exactly 3 short, specific budgeting tips, one sentence each, tied to their actual spending.
- expression: one of idle, alert, suspicious, skeptical, satisfied, disappointed, thinking."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
        "expression": {"type": "string", "enum": EXPRESSIONS},
    },
    "required": ["summary", "tips", "expression"],
    "additionalProperties": False,
}


def spending_stats() -> dict:
    """Numbers are computed here, not by the LLM, so they're always right."""
    profile = store.load()
    by_category = {}
    for p in profile["purchases"]:
        by_category[p["category"]] = by_category.get(p["category"], 0) + p["price"]
    decisions = []
    if DECISIONS_FILE.exists():
        decisions = [json.loads(l) for l in DECISIONS_FILE.read_text().splitlines() if l.strip()]
    today = datetime.now().date().isoformat()
    this_month = [d for d in decisions if str(d.get("time", "")).startswith(today[:7])]
    today_only = [d for d in this_month if str(d.get("time", "")).startswith(today)]
    return {
        "monthly_budget": profile["monthly_budget"],
        "spent": store.spent(profile),
        "remaining": store.remaining(profile),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "purchases": profile["purchases"],
        "goals": profile["goals"],
        "saved_by_letting_go": store.saved_by_letting_go(profile),
        "waiting_room": [{k: w[k] for k in ("item", "price", "tries", "status")} for w in profile["waiting"]
                         if w["status"] in ("waiting", "still_want")],
        "recent_arguments": [{k: a.get(k) for k in ("item", "price", "verdict", "need")}
                             for a in profile["arguments"][-10:]],
        # Checkout counts are this calendar month and today, so the AI can't present all-time totals as "today".
        "today": today,
        "checkouts_completed": sum(d.get("outcome") == "completed" for d in this_month),
        "checkouts_abandoned": sum(d.get("outcome") == "abandoned" for d in this_month),
        "checkouts_today": {"completed": sum(d.get("outcome") == "completed" for d in today_only),
                            "abandoned": sum(d.get("outcome") == "abandoned" for d in today_only)},
        "recent_decisions": [{k: d.get(k) for k in ("time", "item", "outcome", "verdict")} for d in this_month[-10:]],
    }


async def budget_review() -> dict:
    stats = spending_stats()
    data = "This month's data:\n" + json.dumps(stats, indent=1)
    if REVIEW_PROVIDER == "gemini":
        result = json.loads(await ask_gemini(data, system=REVIEW_PROMPT, schema=REVIEW_SCHEMA))
        if not str(result.get("summary", "")).strip():
            raise ValueError("Gemini returned no summary")
        if result.get("expression") not in EXPRESSIONS:
            result["expression"] = "skeptical"
        result["tips"] = [str(t) for t in result.get("tips", [])][:3]
        return result
    resp = await claude_client().beta.messages.create(
        model=REVIEW_MODEL,
        max_tokens=16000,
        system=REVIEW_PROMPT,
        messages=[{"role": "user", "content": data}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        fallbacks="default",
        betas=["server-side-fallback-2026-07-01"],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude refused: {resp.stop_details}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------- server

app = FastAPI()
# The extension, the fake store page, and the dashboard call these from the browser.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# Fake store for the demo, at http://localhost:8000/store/ (the extension's content script runs there).
app.mount("/store", StaticFiles(directory=HERE / "store", html=True), name="store")
# Page reading for the extension: POST /sale and POST /cart (see sales.py).
app.include_router(sales_router)


class ShoppingAlert(BaseModel):
    type: Literal["shopping_visit", "ad_referral", "sponsored", "retargeted", "upsell", "cart",
                  "over_budget", "cart_count", "shopping_visits"]
    website: HttpUrl
    value: float | None = None  # visits today, the cart total, or days since a retargeted visit
    count: int | None = None    # cart item count
    detail: str | None = Field(None, max_length=120)  # which ad, "product"/"store", or the upsell section


@app.post("/alert")
def shopping_alert(alert: ShoppingAlert):
    """Browsing signals from the extension. Silent: Piggyback brings them up at checkout instead."""
    log("Browser signal:", alert.model_dump(mode="json"))
    sales.note_alert(alert.type, str(alert.website), alert.value, alert.count, alert.detail)
    return {"ok": True}


class AddedToBag(BaseModel):
    website: HttpUrl
    item: str = Field("something", max_length=120)
    price: float | None = Field(None, ge=0)


REMARK_PROMPT = """You are Piggyback, a robot piggy bank: warm, playful, a little cheeky. The user just put
something in their shopping bag. Say ONE quick spoken line, under 12 words, reacting to it, and hint that
you'll talk about it at checkout. Don't ask a question they need to answer yet. No lecture, no emoji.
If an ad brought them to this site, that's the joke: they got distracted by an ad (like "Ooh, this was
from an ad, wasn't it? You got distracted."); up to 14 words then. Otherwise you can poke at a sale or
countdown that's rushing them.
Reply with JSON: line, expression (one of idle, alert, suspicious, skeptical, satisfied, disappointed, thinking)."""

REMARK_SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}, "expression": {"type": "string", "enum": EXPRESSIONS}},
    "required": ["line", "expression"],
    "additionalProperties": False,
}

REMARK_LINES = ["Ooh, into the bag. We'll talk at checkout.", "I saw that. We'll chat at checkout.",
                "Noted. Save some arguments for checkout."]
AD_REMARK_LINES = ["Ooh, this was from an ad, wasn't it? You got distracted.",
                   "An ad brought you here. Distracted much? We'll talk at checkout.",
                   "Saw an ad, now it's in the bag? We'll chat at checkout."]
_last_remark = 0.0


async def quick_remark(added: AddedToBag) -> dict:
    ad = sales.ad_source(str(added.website))
    default = {"line": random.choice(AD_REMARK_LINES if ad else REMARK_LINES), "expression": "suspicious"}
    if LLM_PROVIDER != "gemini":
        return default
    remaining = store.remaining(store.load())
    price = f" for ${added.price:,.2f}" if added.price else ""
    prompt = f"They added: {added.item}{price}. ${remaining:.0f} left in this month's budget."
    seen = sales.seen_on(str(added.website))
    if seen:
        prompt += "\nWhat you noticed on this site:\n" + seen
    try:
        text = await asyncio.wait_for(ask_gemini(prompt, system=REMARK_PROMPT, schema=REMARK_SCHEMA, fast=True), 2.5)
    except Exception as e:
        log("Quick remark fell back to a canned line:", repr(e))
        return default
    reply = parse_reply(text, {**default, "verdict": "none"})
    return {"line": reply["line"], "expression": reply["expression"]}


@app.post("/added")
async def added_to_bag(added: AddedToBag):
    """Something went into the bag: one quick line out loud now; the argument waits for checkout."""
    global _last_remark
    log("Added to bag:", added.model_dump(mode="json"))
    sales.note_added(str(added.website), added.item, added.price)
    visitor_activity()
    tiger.event("added", item=added.item, price=added.price, website=str(added.website))
    session = tracking["session"]
    if (session and not session["done"]) or time.time() - _last_remark < 5:
        return {"ok": True, "line": None}  # mid-argument, or a double click
    _last_remark = time.time()
    await turn_to_user(hold=15)  # starts turning now, while the remark is still being written
    await head.expr("alert")
    remark = await quick_remark(added)
    await head.expr(remark["expression"])
    await speak(remark["line"])
    return {"ok": True, "line": remark["line"]}


@app.get("/review")
async def review(speak_it: bool = False):
    stats = spending_stats()
    result = {k: stats[k] for k in ("monthly_budget", "spent", "remaining", "by_category",
                                    "checkouts_completed", "checkouts_abandoned")}
    if speak_it:
        await turn_to_user(hold=20)
    await head.expr("thinking")
    try:
        result.update(await budget_review())
        result["error"] = None
    except Exception as e:
        log("Budget review failed:", repr(e))
        top = next(iter(stats["by_category"]), "nothing")
        result.update({
            "summary": f"You've spent ${stats['spent']:.0f} of ${stats['monthly_budget']}, mostly on {top}. "
                       f"${stats['remaining']:.0f} left. Put it back.",
            "tips": [], "expression": "skeptical", "error": str(e),
        })
    if speak_it:
        await head.expr(result["expression"])
        await speak(result["summary"])
    else:
        await head.expr("idle")
    return result


@app.get("/health")
def health():
    return {"ok": True, "llm": LLM_PROVIDER, "listening": LISTEN, "esp32": head.connected(),
            "vision": vision.state["running"], "camera": vision.state["camera"], "speaker": pick_speaker()[1],
            "store": store.STORE, "remaining": store.remaining(store.load()),
            "tiger": {k: tiger.state[k] for k in ("connected", "written", "dropped", "error")}, "demo": DEMO_MODE}


@app.get("/trends")
def trends():
    """History from Tiger Data for the dashboard: eye contact per argument, money per day, checkouts by hour."""
    try:
        data = tiger.trends()
    except Exception as e:
        log("Trends query failed:", repr(e))
        return {"enabled": True, "error": repr(e)}
    return {"enabled": False} if data is None else {"enabled": True, **data}


# ---------------------------------------------------------------- profile API (dashboard)

class Budget(BaseModel):
    monthly_budget: float


class Goal(BaseModel):
    name: str
    target: float
    deadline: str | None = None
    saved: float = 0


class Amount(BaseModel):
    amount: float


class LetGo(BaseModel):
    goal_id: str | None = None


def change_profile(fn, *args):
    """Apply a store change and return the dashboard summary; unknown ids are a 404."""
    try:
        with store.edit() as p:
            fn(p, *args)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}")
    visitor_activity()
    return store.summary(store.load())


# ---------------------------------------------------------------- demo mode (judging table)
demo = {"last_activity": 0.0, "dirty": False}


def visitor_activity():
    demo["last_activity"], demo["dirty"] = time.time(), True


def reset_demo():
    """Back to the seeded demo profile and decisions for the next visitor. Tiger Data keeps everyone's
    history, so the dashboard's stare history fills up over the day."""
    import seed_demo

    fresh = seed_demo.build_profile()
    with store.edit() as p:
        p.clear()
        p.update(fresh)
    DECISIONS_FILE.write_text("".join(json.dumps(d) + "\n" for d in seed_demo.decisions()))
    sales._seen.clear()  # what the last visitor's browsing showed (ads, carts) isn't this visitor's
    demo["dirty"] = False
    log("Demo: reset for the next visitor")


async def demo_auto_reset():
    while True:
        await asyncio.sleep(min(30, DEMO_RESET_MINUTES * 20))  # checks 3x per idle period, at most every 30 s
        if demo["dirty"] and not engaged() and time.time() - demo["last_activity"] > DEMO_RESET_MINUTES * 60:
            reset_demo()


@app.post("/demo/reset")
def demo_reset():
    if not DEMO_MODE:
        raise HTTPException(403, "only in demo mode (DEMO_MODE=1): this replaces the profile")
    if tracking["session"] and not tracking["session"]["done"]:
        raise HTTPException(409, "an argument is going on")
    reset_demo()
    return store.summary(store.load())


@app.get("/")
def dashboard():
    return FileResponse(HERE / "dashboard.html")


@app.get("/profile")
def profile():
    return store.summary(store.load())


@app.put("/budget")
def set_budget(body: Budget):
    if body.monthly_budget <= 0:
        raise HTTPException(400, "budget must be positive")
    return change_profile(lambda p: p.update(monthly_budget=body.monthly_budget))


@app.post("/goals")
def add_goal(body: Goal):
    if not body.name.strip() or body.target <= 0:
        raise HTTPException(400, "a goal needs a name and a positive target")
    return change_profile(store.add_goal, body.name.strip(), body.target, body.deadline or None, body.saved)


@app.post("/goals/{goal_id}/save")
def save_toward_goal(goal_id: str, body: Amount):
    return change_profile(store.save_toward, goal_id, body.amount)


@app.delete("/goals/{goal_id}")
def delete_goal(goal_id: str):
    return change_profile(store.delete_goal, goal_id)


@app.post("/waiting/{waiting_id}/let_go")
def let_go(waiting_id: str, body: LetGo):
    w = next((w for w in store.load()["waiting"] if w["id"] == waiting_id), None)
    if w and w["status"] in ("waiting", "still_want"):
        tiger.event("let_go", item=w["item"], price=w["price"], category=w.get("category"), goal_id=body.goal_id)
    return change_profile(store.let_go, waiting_id, body.goal_id)


@app.post("/waiting/{waiting_id}/still_want")
def still_want(waiting_id: str):
    return change_profile(store.still_want, waiting_id)


@app.get("/vision")
def vision_state():
    """Live face / eye-contact numbers, for tuning the LOOK_* thresholds."""
    return vision.state


@app.get("/vision.jpg")
def vision_frame():
    """Latest camera frame with the face marker drawn on (green = looking). Not saved anywhere."""
    jpg = vision.jpeg(width=640, overlay=True)
    return Response(jpg, media_type="image/jpeg") if jpg else Response(status_code=503)


@app.on_event("startup")
async def warm_up():
    if LISTEN:
        # Load (and on first run download) the Whisper model now, not on the first answer.
        asyncio.create_task(asyncio.to_thread(whisper_model))
    if VISION:
        vision.start()
    tiger.start(store.load())
    if DEMO_MODE:
        log(f"Demo mode: no real orders; the profile resets for each visitor (or after {DEMO_RESET_MINUTES:g} idle minutes)")
        asyncio.create_task(demo_auto_reset())
    asyncio.create_task(track_user())  # also turns the head away when idle, camera or not


def eye_contact_available() -> bool:
    return VISION and vision.state["running"]


async def track_user():
    """10x a second: point the pupils at the user and turn the head to keep their face centered.

    The camera is in the head, so centering the face in the frame means the pig is facing them."""
    absent_since = time.time()
    while True:
        await asyncio.sleep(0.1)
        now = time.time()
        if not engaged() and tracking["look"] != IDLE_LOOK and now > tracking["paused_until"]:
            # Nobody's buying anything: slowly turn away and mind its own business.
            tracking["look"] = list(IDLE_LOOK)
            tracking["paused_until"] = now + TURN_SECONDS
            await asyncio.to_thread(head.send, {"look": tracking["look"], "s": TURN_SECONDS})  # logged: rare
        s = vision.state
        if not s["running"]:
            continue
        session = tracking["session"]
        if session and not session["done"]:
            tiger.gaze(session["id"], s, tracking["recording"])  # eye contact through the argument, for history
        if s["present"]:
            ex, ey = s["face_x"] - 0.5, s["face_y"] - 0.5  # face offset from image center, -0.5..0.5
            gaze = [round(ex * 2 * (-1 if GAZE_INVERT_X else 1), 2), round(ey * 2, 2)]
            if max(abs(a - b) for a, b in zip(gaze, tracking["gaze"])) > 0.05:
                tracking["gaze"] = gaze
                await asyncio.to_thread(head.send, {"gaze": gaze}, True)
            if now > tracking["paused_until"] and not tracking["recording"] and engaged():
                if abs(ex) < 0.1:
                    tracking["user_look"] = list(tracking["look"])  # facing them: remember where they sit
                # The head only turns left and right; the pupils follow the face up and down.
                look = list(tracking["look"])
                if abs(ex) > 0.06:
                    look[0] += TRACK_GAIN * ex * (-1 if TRACK_INVERT_PAN else 1)
                look = [round(min(max(v, -1.0), 1.0), 3) for v in look]
                if look != tracking["look"]:
                    tracking["look"] = look
                    await asyncio.to_thread(head.send, {"look": look}, True)
            # Someone sat down after nobody was there: wake up.
            if absent_since and now - absent_since > 5 and not tracking["session"]:
                log("Face appeared, waking up")
                await head.expr("alert")
                await asyncio.sleep(1.2)
                await head.expr("idle")
            absent_since = None
        elif absent_since is None:
            absent_since = now
            tracking["gaze"] = [0.0, 0.0]
            await asyncio.to_thread(head.send, {"gaze": [0.0, 0.0]}, True)


async def wait_for_eye_contact(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if vision.state["looking"]:
            return True
        await asyncio.sleep(0.1)
    return False


async def stare_to_confirm() -> bool:
    """User holds eye contact for CONFIRM_SECONDS; the OLED lids close as progress. Looking away resets."""
    await done_speaking()
    start = time.time()
    deadline = start + CONFIRM_WAIT
    while time.time() < deadline:
        # Only eye contact held after the prompt counts.
        held = min(vision.state["looking_for"], time.time() - start)
        progress = min(held / CONFIRM_SECONDS, 1.0)
        await asyncio.to_thread(head.send, {"stare": round(progress, 2)}, True)
        if progress >= 1.0:
            return True
        await asyncio.sleep(0.1)
    return False


async def say_and_show(ws: WebSocket, reply: dict, msg: dict):
    await head.expr(reply["expression"])
    await ws.send_json(msg)
    await done_speaking()  # let a short "Hmm." finish instead of cutting it off
    await speak(reply["line"])


async def next_turn(ws: WebSocket, session: dict, pending: "asyncio.Task | None" = None):
    final = session["questions"] >= session["total"] or session["failed"]
    await head.expr("thinking")
    t0 = time.time()
    reply = await pending if pending else await ask_llm(session, final)
    log(f"Timing: AI replied in {time.time() - t0:.1f}s")

    if not final:
        session["questions"] += 1
        session["transcript"].append(("Piggyback", reply["line"]))
        await say_and_show(ws, reply, {"type": "question", "line": reply["line"], "expression": reply["expression"]})
        if LISTEN:
            asyncio.create_task(listen_for_answer(ws, session, session["questions"]))
        return

    remaining = store.remaining(store.load())
    clean = session["verified"] and session["answered_all"] and not session["failed"]
    verdict, rule = apply_rules(reply["verdict"], session["price"], remaining, clean, session["failed"],
                                reply.get("need") == "need")
    if rule and verdict != reply["verdict"]:
        # The LLM's line would contradict the stricter verdict, so use the rule's line.
        _, reply["expression"], reply["line"] = RULE_LINES[rule]
    if verdict == "release" and eye_contact_available() and vision.state["present"]:
        # Earn the release: look Piggyback in the eyes for a few seconds.
        prompt_line = f"Look me in the eyes for {CONFIRM_SECONDS:.0f} seconds if you're sure."
        await ws.send_json({"type": "confirm", "seconds": CONFIRM_SECONDS, "line": prompt_line})
        await speak(prompt_line)
        if not await stare_to_confirm():
            verdict, rule = "timer", "no_eye_contact"
            _, reply["expression"], reply["line"] = RULE_LINES[rule]
    log(f"Verdict: {verdict} (LLM said {reply['verdict']}, need={reply.get('need')}, rule {rule})")
    said = [text for who, text in session["transcript"] if who == "User" and not text.startswith(("(", "["))]
    with store.edit() as p:
        store.note_verdict(p, session["item"], session["price"], session["category"], verdict, rule,
                           reply.get("need", "unclear"), said)
    session["done"] = True
    session["verdict"] = verdict
    tiger.event("verdict", session["id"], session["item"], session["price"], session["category"], verdict,
                rule=rule, need=reply.get("need", "unclear"), llm_verdict=reply["verdict"], tier=session["tier"],
                strikes=session["strikes"], clean=clean)
    await say_and_show(ws, reply, {
        "type": "verdict", "verdict": verdict,
        "seconds": TIMER_SECONDS if verdict == "timer" else 0,
        "line": reply["line"], "rule": rule, "demo": DEMO_MODE,
    })


async def handle_answer(ws: WebSocket, session: dict, turn: int, text: str, verified: bool = False):
    """Spoken and typed answers both land here; whichever comes first for a turn wins.

    verified: given out loud with camera-checked eye contact. Typed answers never are."""
    async with session["lock"]:
        if session["done"] or session["questions"] != turn or session["answered"] >= turn:
            return
        session["answered"] = turn
        if not verified:
            session["verified"] = False
        if not text or text == "(no answer)":
            session["answered_all"] = False
        session["transcript"].append(("User", text))
        await next_turn(ws, session)


async def listen_for_answer(ws: WebSocket, session: dict, turn: int):
    try:
        await done_speaking()  # don't record Piggyback's own voice
        if session["done"] or session["answered"] >= turn:
            return
        verified = eye_contact_available() and vision.state["present"]
        nudged = False
        while True:
            gate = lambda: True
            t_wait = time.time()
            if verified:
                # The whole answer has to be said looking Piggyback in the eyes.
                if not await wait_for_eye_contact(3):
                    await head.expr("suspicious")
                    await speak("Look me in the eyes and tell me.")
                    await done_speaking()
                    await wait_for_eye_contact(6)
                gate = lambda: vision.state["looking"]
            if session["done"] or session["answered"] >= turn:
                return
            if verified:
                log(f"Timing: waited {time.time() - t_wait:.1f}s for eye contact")
            await ws.send_json({"type": "listening", "seconds": NO_SPEECH_SECONDS})
            text, broke = await asyncio.to_thread(listen, gate)
            log("Heard:", repr(text), "(looked away)" if broke else "")
            if not text and not broke and not nudged:
                # They may just be thinking: one gentle nudge, then silence counts as no answer.
                nudged = True
                await ws.send_json({"type": "nudge", "line": NUDGE_LINE})
                await head.expr("thinking")
                await speak(NUDGE_LINE)
                await done_speaking()
                continue
            if not broke:
                break
            session["strikes"] += 1
            await ws.send_json({"type": "looked_away", "strikes": session["strikes"], "heard": text})
            if session["strikes"] == 1:
                await head.expr("suspicious")
                await speak("You looked away. Look at me and say that again.")
                await done_speaking()
                continue
            # Second strike: the argument is over and the verdict comes now.
            session["failed"] = True
            await handle_answer(ws, session, turn, "[looked away mid-answer twice; argument forfeited]")
            return
        await ws.send_json({"type": "heard", "text": text})
        if text:
            await speak(random.choice(ACKS))  # covers the AI's ~2 s so it doesn't feel like dead air
        await handle_answer(ws, session, turn, text or "(no answer)", verified=verified)
    except Exception as e:
        log("Listening failed, waiting for a typed answer instead:", repr(e))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    log("Bot connected")
    session = None
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            log("Bot >", msg)

            if kind in ("checkout", "answer", "decision"):
                visitor_activity()
            if kind == "checkout":
                if session:
                    session["done"] = True  # stop any listener from the previous purchase
                price = float(msg.get("price", 0))
                with store.edit() as p:
                    store.note_checkout(p, str(msg.get("item", "something")), price)
                    tier = budget_tier(price, store.remaining(p))
                session = tracking["session"] = {
                    "id": store.new_id(),
                    "item": str(msg.get("item", "something")),
                    "price": price,
                    "category": str(msg.get("category", "other")),
                    "url": str(msg.get("url") or ""),  # the checkout page, to match what the extension noticed
                    "tier": tier, "total": QUESTIONS_BY_TIER[tier],
                    "questions": 0, "answered": 0, "transcript": [], "done": False,
                    "verdict": None, "lock": asyncio.Lock(),
                    # eye-contact record for the argument
                    "strikes": 0, "failed": False, "verified": True, "answered_all": True,
                }
                log(f"Budget tier: {tier} ({session['total']} questions)")
                tiger.event("checkout", session["id"], session["item"], price, session["category"], tier=tier)
                await turn_to_user()  # usually already facing them from the add-to-bag remark
                await head.expr("alert")
                # Start the AI now so it runs while the alert pose lands (saves ~0.6 s).
                first = asyncio.create_task(ask_llm(session, final=False))
                await asyncio.sleep(0.6)
                async with session["lock"]:
                    await next_turn(ws, session, first)

            elif kind == "answer":
                # Typed answers still work (test_client.py, or if the mic fails).
                if not session or session["done"]:
                    log("Answer with no open session, ignoring")
                    continue
                await handle_answer(ws, session, session["questions"], str(msg.get("text", "")))

            elif kind == "decision":
                record = {**msg, "time": datetime.now().isoformat(timespec="seconds")}
                if session:
                    record["verdict"] = session["verdict"]
                with DECISIONS_FILE.open("a") as f:
                    f.write(json.dumps(record) + "\n")
                item = str(msg.get("item") or (session["item"] if session else "something"))
                price = float(msg.get("price", session["price"] if session else 0))
                category = session["category"] if session else "other"
                sid, verdict = (session["id"], session["verdict"]) if session else (None, None)
                tiger.event("decision", sid, item, price, category, verdict, outcome=msg.get("outcome"),
                            seconds_on_page=msg.get("seconds_on_page"))
                if msg.get("outcome") == "completed":
                    tiger.event("purchase", sid, item, price, category, verdict)
                    with store.edit() as p:
                        store.note_purchase(p, item, price, category)
                bought_anyway = session and msg.get("outcome") == "completed" and session["verdict"] in ("timer", "escalate")
                if session:
                    session["done"] = True
                session = tracking["session"] = None
                tracking["engaged_until"] = time.time() + 8  # keep facing them for the last word, then look away
                if bought_anyway:
                    # The bot can't block checkout, so Piggyback at least lets you know.
                    await done_speaking()  # let the verdict finish first
                    await head.expr("disappointed")
                    await speak("You went for it anyway. I'll keep that in mind.")
                else:
                    await head.expr("idle")

            else:
                log("Unknown message type:", kind)
    except WebSocketDisconnect:
        log("Bot disconnected")
        if session:
            session["done"] = True
        if tracking["session"] is session:  # don't end a checkout that started on another connection
            tracking["session"] = None
        # The extension closes ~0.3 s after a decision: let "You went for it anyway" and its head shake finish.
        await done_speaking()
        await asyncio.sleep(min(max(tracking["paused_until"] - time.time(), 0), 6))
        if tracking["session"] is None:
            await head.expr("idle")
