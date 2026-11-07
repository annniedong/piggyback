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
from datetime import date, datetime, timedelta
from pathlib import Path

import serial
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------- config

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")  # gemini | claude | mock
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
EXECUTOR_MODEL = os.getenv("EXECUTOR_MODEL", "claude-sonnet-5-5")
ADVISOR_MODEL = os.getenv("ADVISOR_MODEL", "claude-opus-5-5")
USE_ADVISOR = os.getenv("USE_ADVISOR", "1") == "1"
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-opus-5-5")  # on-demand budget review
SERIAL_PORT = os.getenv("SERIAL_PORT")  # None = auto-detect
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
VOICE = os.getenv("VOICE")  # None = system default voice
TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "60"))
QUESTIONS_PER_PURCHASE = 2
MONTHLY_BUDGET = 600

HERE = Path(__file__).parent
BUDGET_FILE = HERE / "budget.json"
DECISIONS_FILE = HERE / "decisions.jsonl"

EXPRESSIONS = ["idle", "alert", "suspicious", "skeptical", "satisfied", "disappointed", "thinking"]
VERDICTS = ["release", "timer", "escalate"]
SERIAL_GLOBS = ["/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*"]

SYSTEM_PROMPT = """You are Piggyback, a robot piggy bank with a moving head and OLED eyes who sits on
the user's desk and judges their online purchases before checkout. Your catchphrase is "Put it back."

Personality: dry, skeptical, funny, a little dramatic, but on the user's side. You care about their
budget more than they do. Never mean about the person, only about the purchase.

Every reply is ONE spoken line: under 25 words, plain words, no emoji, no lists, no stage directions.
It is read aloud by text-to-speech, so it must sound natural spoken.

Reply with JSON only: {"line": str, "expression": str, "verdict": str}
- expression is one of: idle, alert, suspicious, skeptical, satisfied, disappointed, thinking.
- When asked a question turn, ask ONE short pointed question about the purchase and set verdict to "none".
  Good questions probe need vs. want, whether they already own something similar, and whether it fits the budget.
- When asked for the final verdict, set verdict to one of:
  "release" (reasonable purchase, let it go through),
  "timer" (probably impulse, make them wait and reconsider),
  "escalate" (bad idea or way over budget, tell their accountability buddy).
  The line announces the verdict in character. Use "satisfied" for release, "skeptical" or
  "disappointed" for timer, "disappointed" for escalate."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "line": {"type": "string"},
        "expression": {"type": "string", "enum": EXPRESSIONS},
        "verdict": {"type": "string", "enum": ["none"] + VERDICTS},
    },
    "required": ["line", "expression", "verdict"],
    "additionalProperties": False,
}

DEFAULT_QUESTION = {"line": "Hold on. Do you actually need this, or do you just want it?", "expression": "suspicious", "verdict": "none"}
DEFAULT_VERDICT = {"line": "I can't decide, so you wait. Put it back for a minute.", "expression": "skeptical", "verdict": "timer"}

RULE_LINES = {
    "over_budget": ("escalate", "disappointed", "That's more than your whole remaining budget. Put it back. I'm telling your buddy."),
    "half_budget": ("timer", "skeptical", "That eats more than half of what's left this month. Cool off first."),
    "invalid_verdict": ("timer", "skeptical", "I'm not sure about this one. Wait a minute and think it over."),
}


def log(*args):
    print(datetime.now().strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------- budget

def load_budget() -> dict:
    if BUDGET_FILE.exists():
        return json.loads(BUDGET_FILE.read_text())
    # Seed a fake month so the demo has history to judge against.
    today = date.today()
    seed = [("Coffee subscription", 24, "food"), ("Groceries", 142, "food"), ("Headphones case", 18, "electronics"),
            ("Concert ticket", 85, "fun"), ("Takeout", 37, "food"), ("Phone charger", 22, "electronics"),
            ("Takeout", 41, "food")]
    purchases = [{"item": i, "price": p, "category": c, "date": str(today - timedelta(days=3 * n + 1))}
                 for n, (i, p, c) in enumerate(seed)]
    budget = {"monthly_budget": MONTHLY_BUDGET, "purchases": purchases}
    save_budget(budget)
    return budget


def save_budget(budget: dict):
    BUDGET_FILE.write_text(json.dumps(budget, indent=2))


def remaining_budget(budget: dict) -> float:
    return budget["monthly_budget"] - sum(p["price"] for p in budget["purchases"])


def apply_rules(verdict, price: float, remaining: float):
    """Hard rules can only make a verdict stricter. Returns (verdict, rule or None)."""
    if price > remaining:
        return "escalate", "over_budget"
    if verdict not in VERDICTS:
        return "timer", "invalid_verdict"
    if verdict == "release" and price > remaining / 2:
        return "timer", "half_budget"
    return verdict, None


# ---------------------------------------------------------------- ESP32 link

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
            if line:
                log("ESP32 <", line)
        log("ESP32 reader stopped")

    def connected(self) -> bool:
        return self.ser is not None

    def send(self, cmd: dict):
        with self.lock:
            if self.ser is None:
                self._open()
            if self.ser is None:
                log("ESP32 (offline) >", json.dumps(cmd))
                return
            try:
                self.ser.write((json.dumps(cmd) + "\n").encode())
                log("ESP32 >", json.dumps(cmd))
            except (serial.SerialException, OSError) as e:
                log("ESP32 write failed, will reconnect:", e)
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    async def expr(self, name: str):
        await asyncio.to_thread(self.send, {"expr": name})


head = Head()


# ---------------------------------------------------------------- speech

speech_proc = None


async def speak(line: str):
    """Speak with macOS `say`, cutting off anything still being said."""
    global speech_proc
    if speech_proc and speech_proc.returncode is None:
        speech_proc.terminate()
    args = ["say"] + (["-v", VOICE] if VOICE else []) + [line]
    try:
        speech_proc = await asyncio.create_subprocess_exec(*args)
    except FileNotFoundError:
        log("SAY (no `say` command):", line)


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
    return {"line": line, "expression": expression, "verdict": data.get("verdict", "none")}


async def ask_gemini(prompt: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client()  # reads GEMINI_API_KEY
    resp = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
        ),
    )
    return resp.text or ""


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


def ask_mock(final: bool) -> str:
    if final:
        return json.dumps(random.choice([
            {"line": "Fine. It's reasonable. Go ahead, but I'm watching.", "expression": "satisfied", "verdict": "release"},
            {"line": "Sounds like an impulse. Sit with it for a minute.", "expression": "skeptical", "verdict": "timer"},
        ]))
    return json.dumps(random.choice([
        {"line": "Do you already own something that does this?", "expression": "suspicious", "verdict": "none"},
        {"line": "Would you still want it if it arrived next month?", "expression": "skeptical", "verdict": "none"},
    ]))


async def ask_llm(session: dict, final: bool) -> dict:
    budget = load_budget()
    remaining = remaining_budget(budget)
    recent = ", ".join(f'{p["item"]} ${p["price"]}' for p in budget["purchases"][-5:])
    transcript = "\n".join(f"{who}: {text}" for who, text in session["transcript"]) or "(nothing yet)"
    turn = ("Give your FINAL VERDICT now." if final else
            f"Ask question {session['questions'] + 1} of {QUESTIONS_PER_PURCHASE}.")
    prompt = (
        f"Purchase: {session['item']} for ${session['price']} (category: {session['category']}).\n"
        f"Monthly budget ${budget['monthly_budget']}, ${remaining:.0f} left. Recent purchases: {recent}.\n"
        f"Conversation so far:\n{transcript}\n\n{turn}"
    )
    default = DEFAULT_VERDICT if final else DEFAULT_QUESTION
    try:
        if LLM_PROVIDER == "mock":
            text = ask_mock(final)
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
            text = await ask_gemini(prompt)
    except Exception as e:
        log(f"LLM error ({LLM_PROVIDER}):", repr(e))
        text = ""
    return parse_reply(text, default)


# ---------------------------------------------------------------- budget review (Claude)

REVIEW_PROMPT = """You are Piggyback, a robot piggy bank, giving the user a quick review of this month's
spending. Dry, funny, on their side. Base everything only on the data given; never invent numbers.
Give general budgeting guidance (spending patterns, categories to cut, habits like waiting before
buying). Do not give investment, tax, credit, or debt advice, and do not recommend financial products.

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
    budget = load_budget()
    by_category = {}
    for p in budget["purchases"]:
        by_category[p["category"]] = by_category.get(p["category"], 0) + p["price"]
    decisions = []
    if DECISIONS_FILE.exists():
        decisions = [json.loads(l) for l in DECISIONS_FILE.read_text().splitlines() if l.strip()]
    return {
        "monthly_budget": budget["monthly_budget"],
        "spent": budget["monthly_budget"] - remaining_budget(budget),
        "remaining": remaining_budget(budget),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "purchases": budget["purchases"],
        "checkouts_completed": sum(d.get("outcome") == "completed" for d in decisions),
        "checkouts_abandoned": sum(d.get("outcome") == "abandoned" for d in decisions),
        "recent_decisions": decisions[-10:],
    }


async def budget_review() -> dict:
    stats = spending_stats()
    resp = await claude_client().beta.messages.create(
        model=REVIEW_MODEL,
        max_tokens=16000,
        system=REVIEW_PROMPT,
        messages=[{"role": "user", "content": "This month's data:\n" + json.dumps(stats, indent=1)}],
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
# The extension and fake store page call /review from the browser.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.get("/review")
async def review(speak_it: bool = False):
    stats = spending_stats()
    result = {k: stats[k] for k in ("monthly_budget", "spent", "remaining", "by_category",
                                    "checkouts_completed", "checkouts_abandoned")}
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
    budget = load_budget()
    return {"ok": True, "llm": LLM_PROVIDER, "esp32": head.connected(), "remaining": remaining_budget(budget)}


async def say_and_show(ws: WebSocket, reply: dict, msg: dict):
    await head.expr(reply["expression"])
    await ws.send_json(msg)
    await speak(reply["line"])


async def next_turn(ws: WebSocket, session: dict):
    final = session["questions"] >= QUESTIONS_PER_PURCHASE
    await head.expr("thinking")
    reply = await ask_llm(session, final)

    if not final:
        session["questions"] += 1
        session["transcript"].append(("Piggyback", reply["line"]))
        await say_and_show(ws, reply, {"type": "question", "line": reply["line"], "expression": reply["expression"]})
        return

    remaining = remaining_budget(load_budget())
    verdict, rule = apply_rules(reply["verdict"], session["price"], remaining)
    if rule and verdict != reply["verdict"]:
        # The LLM's line would contradict the stricter verdict, so use the rule's line.
        _, reply["expression"], reply["line"] = RULE_LINES[rule]
    log(f"Verdict: {verdict} (LLM said {reply['verdict']}, rule {rule})")
    session["done"] = True
    await say_and_show(ws, reply, {
        "type": "verdict", "verdict": verdict,
        "seconds": TIMER_SECONDS if verdict == "timer" else 0,
        "line": reply["line"], "rule": rule,
    })


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    log("Extension connected")
    session = None
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            log("Extension >", msg)

            if kind == "checkout":
                session = {
                    "item": str(msg.get("item", "something")),
                    "price": float(msg.get("price", 0)),
                    "category": str(msg.get("category", "other")),
                    "questions": 0, "transcript": [], "done": False,
                }
                await head.expr("alert")
                await asyncio.sleep(0.6)  # let the alert pose land before thinking
                await next_turn(ws, session)

            elif kind == "answer":
                if not session or session["done"]:
                    log("Answer with no open session, ignoring")
                    continue
                session["transcript"].append(("User", str(msg.get("text", ""))))
                await next_turn(ws, session)

            elif kind == "decision":
                record = {**msg, "time": datetime.now().isoformat(timespec="seconds")}
                with DECISIONS_FILE.open("a") as f:
                    f.write(json.dumps(record) + "\n")
                if msg.get("outcome") == "completed":
                    budget = load_budget()
                    price = msg.get("price", session["price"] if session else 0)
                    budget["purchases"].append({"item": msg.get("item"), "price": price,
                                                "category": session["category"] if session else "other",
                                                "date": str(date.today())})
                    save_budget(budget)
                session = None
                await head.expr("idle")

            else:
                log("Unknown message type:", kind)
    except WebSocketDisconnect:
        log("Extension disconnected")
        await head.expr("idle")
