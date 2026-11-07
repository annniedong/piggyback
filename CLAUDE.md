# Piggyback

A robot piggy bank that judges your online purchases. Built for MakeCU 2026 (Columbia's hardware hackathon, Nov 7–8), Creativity track.

When you click "Place order" on a shopping site, a Chrome extension pauses checkout. Piggyback turns its head toward you, changes its eyes, and asks two short questions out loud. Then it either releases the purchase, imposes a cooling-off timer, or escalates to an accountability buddy. Catchphrase: "Put it back."

Pitch idea: digital money is invisible, so Piggyback gives your spending a physical, expressive presence. The AI can argue, but it can't overrule the budget.

## Commit rules

- Do NOT add "Co-Authored-By: Claude" or any Claude session links to commits or PRs. Commits are authored by the team only.

## Architecture

```
Chrome extension ──WebSocket──► brain.py (laptop, FastAPI :8000)
                                   │  Gemini Flash: checkout questions + verdicts
                                   │  Claude: on-demand budget review (GET /review)
                                   │  speaks via macOS `say`
                                   └──USB serial (JSON lines)──► ESP32 firmware
                                                                   ├─PWM──► 2× neck servos (pan/tilt)
                                                                   ├─I2C bus 0─► left SSD1306 OLED eye
                                                                   └─I2C bus 1─► right SSD1306 OLED eye
```

No Raspberry Pi. The laptop talks to the ESP32 over a USB cable, so there's no Wi-Fi or network setup. The ESP32 has two hardware I2C buses, so both OLEDs can keep the default 0x3C address.

## Files

- `brain.py` (laptop): conversation logic, LLM calls, hard budget rules, TTS, logging, and commands to the ESP32.
- `firmware/piggyback_esp32/piggyback_esp32.ino` (ESP32, Arduino): turns expression names into servo motion and eye images.
- `test_client.py` (laptop): pretends to be the extension for testing the brain.

## Message formats

Extension → brain (WebSocket `ws://localhost:8000/ws`):

```json
{"type": "checkout", "item": "Smart toaster", "price": 400, "category": "kitchen"}
{"type": "answer", "text": "I need it for work"}
{"type": "decision", "item": "Smart toaster", "outcome": "completed" | "abandoned", "seconds_on_page": 42}
```

Brain → extension:

```json
{"type": "question", "line": "...", "expression": "suspicious"}
{"type": "verdict", "verdict": "release" | "timer" | "escalate", "seconds": 60, "line": "...", "rule": null}
```

Budget review (HTTP, extension button → `GET http://localhost:8000/review`, add `?speak_it=true` to have Piggyback say the summary out loud):

```json
{"monthly_budget": 600, "spent": 369, "remaining": 231, "by_category": {"food": 244, "fun": 85},
 "checkouts_completed": 3, "checkouts_abandoned": 2,
 "summary": "...", "tips": ["...", "...", "..."], "expression": "skeptical", "error": null}
```

The numbers are computed in code from `budget.json` and `decisions.jsonl`; Claude only writes `summary`, `tips`, and `expression`. It gives general budgeting guidance only (no investment, tax, credit, or product advice). If Claude fails, `summary` is a plain computed sentence, `tips` is empty, and `error` says why. Takes a few seconds, so show a loading state.

Brain → ESP32 (USB serial, 115200 baud, one JSON object per line ending in `\n`):

```json
{"expr": "suspicious"}
{"look": [pan, tilt]}
{"ping": 1}
```

ESP32 → brain (one JSON object per line):

```json
{"ready": true}
{"ok": true}
{"err": "unknown expr"}
{"pong": 1}
```

- Expressions: `idle, alert, suspicious, skeptical, satisfied, disappointed, thinking`. Unknown names fall back to `idle` and reply with `err`.
- `look` values are −1…1, with `[0, 0]` as center. The ESP32 maps them onto its own `LIMITS`, so the brain never needs servo angles.
- The ESP32 replies right away and moves in the background. A new command interrupts the current motion.
- The brain never waits on replies; it only logs them.

## Brain behavior (brain.py)

1. On `checkout`: start a session, send `alert`, then `thinking` while waiting for the LLM.
2. The LLM returns JSON `{"line", "expression", "verdict"}`. The server moves the head, sends the line to the extension, and speaks it.
3. After 2 questions (`QUESTIONS_PER_PURCHASE`), it asks for a final verdict.
4. `apply_rules()` can only make a verdict stricter: over budget → escalate; release using more than half the remaining budget → timer; invalid verdict → timer.
5. `decision` messages are logged to `decisions.jsonl`; completed purchases are added to `budget.json` (seeded with a fake month of spending, $600 budget).

Fallbacks: ESP32 not plugged in or serial write fails → commands are printed and the brain retries opening the port; bad LLM JSON → default line; Claude advisor tool fails → retries without it.

Opening the serial port resets the ESP32, so the brain waits for `{"ready": true}` (or about 2 s) before sending the first command.

## Config (environment variables)

- `LLM_PROVIDER` (checkout conversation): `gemini` (default, the plan), `claude`, or `mock` (canned lines, no API key; for testing and as a demo backup)
- `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.8-flash`; check what the key supports)
- `ANTHROPIC_API_KEY`, `EXECUTOR_MODEL` (default `claude-sonnet-5-5`), `ADVISOR_MODEL` (default `claude-opus-5-5`), `USE_ADVISOR` (default `1`; Claude advisor tool, beta `advisor-tool-2026-03-01`, used only for the final verdict so questions stay fast). Claude requests also enable server-side refusal fallbacks (`fallbacks: "default"`).
- `REVIEW_MODEL` (default `claude-opus-5-5`): model for the budget review; needs `ANTHROPIC_API_KEY`
- `TIMER_SECONDS` (default `60`): cooling-off length sent with a `timer` verdict
- `SERIAL_PORT` (default: auto-detect the first `/dev/cu.usbserial*`, `/dev/cu.SLAB_USBtoUART*`, or `/dev/cu.wchusbserial*`)
- `SERIAL_BAUD` (default `115200`)
- `VOICE` (macOS `say` voice)

Run:

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn pyserial google-genai anthropic websockets
export GEMINI_API_KEY=...
uvicorn brain:app --port 8000
```

Test without the extension: `python test_client.py "Smart toaster" 400 kitchen` (add `--auto` for canned answers). `GET /health` shows the LLM provider, whether the ESP32 is connected, and the remaining budget.

Close the Arduino Serial Monitor before starting the brain: only one program can hold the port.

## ESP32 firmware (firmware/piggyback_esp32)

- Arduino IDE or `arduino-cli` with the ESP32 board package. Libraries: ArduinoJson, Adafruit SSD1306, Adafruit GFX, ESP32Servo.
- Suggested pins (confirm for your board): pan servo GPIO 18, tilt servo GPIO 19; left eye on I2C bus 0 (SDA 21, SCL 22); right eye on I2C bus 1 (SDA 25, SCL 26). Write the final pins here once wired.
- Servos are driven straight from ESP32 PWM pins with ESP32Servo. MG90S servos accept the 3.3 V signal. If that's unreliable, fall back to the PCA9685 on I2C bus 0 (address 0x40, it can share the bus with the left eye).
- `LIMITS` clamps angles; `PRESETS` maps expressions to (pan, tilt, eyes, seconds). Motion uses smoothstep easing, stepped inside `loop()` without `delay()` so new commands interrupt it.
- At boot: move slowly to the home pose, show `idle` eyes, then print `{"ready": true}`.
- Eyes are drawn with shapes in `drawEye()` (128×64, one color), not bitmaps; `thinking` animates. Swap in bitmaps later if the team draws them.
- TODO: calibrate `PRESETS` and `LIMITS` once the head is mounted.

## Hardware

- ESP32 dev board, 2× MG90S neck servos (pan/tilt; head must be light), 2× SSD1306 0.96" OLED eyes, Logitech webcam with mic (on the laptop), Facmogu bench supply at 5V for servos, barrel-jack screw adapter, 470 µF capacitor across the servo rail. PCA9685 is a spare.
- Power rules: servos run from the bench supply, never from the ESP32's 5V/VIN pin; the ESP32 is powered by the laptop's USB; all grounds shared (bench supply ground ↔ ESP32 GND). OLEDs run from the ESP32's 3.3V pin.

## Team

- Alisha: Chrome extension + fake store page; blocks checkout, sends messages above, logs decisions; "Budget review" button that calls `/review` and shows the summary and tips.
- Annie: brain server, AI, laptop-to-ESP32 serial connection.
- Lorraine and Hana (builders): pig head shell and neck mount; power wiring, servos, OLEDs, ESP32 firmware.

## Testing milestones

Each milestone has a test anyone can run and a pass check. Don't start the next integration milestone until the previous one passes. Tracks A (brain, Annie), B (ESP32, Lorraine and Hana), and C (extension, Alisha) run in parallel until M4.

| # | Milestone | Owner | Test | Pass when |
|---|-----------|-------|------|-----------|
| M0 | Contract agreed | All | Review the brain→ESP32 and extension→brain formats above | Expression names, `look` units, reply lines, and pins are written here and nobody objects |
| A1 | Brain runs alone | Annie | `uvicorn brain:app --port 8000`, then send a `checkout` over a WebSocket client (e.g. `websocat ws://localhost:8000/ws`) | Gets 2 `question`s, answering both gets a `verdict`; laptop speaks each line; ESP32 commands are printed (nothing plugged in) |
| A2 | Budget rules hold | Annie | Send a checkout over the remaining budget, and one using more than half of it | First → `escalate`, second → at least `timer`, no matter what the LLM says |
| A3 | Brain → bare ESP32 | Annie | Flash a firmware that only parses and replies (no servos or OLEDs), plug it in, run A1 again | Every command gets an `ok` back in the brain's log, in order; unplug and replug recovers without restarting the brain |
| A4 | Budget review works | Annie | With `ANTHROPIC_API_KEY` set, `curl 'localhost:8000/review?speak_it=true'` after a few test checkouts | Numbers match `budget.json`; summary and 3 tips mention real categories; Piggyback says the summary; without a key it still returns numbers and a fallback summary |
| B1 | ESP32 talks serial | Lorraine, Hana | Flash the firmware, open Serial Monitor at 115200, type `{"ping":1}` | Boot prints `{"ready":true}`; ping returns `{"pong":1}`; bad JSON returns `err` without crashing |
| B2 | Servos move | Lorraine, Hana | In Serial Monitor, send `{"expr":"alert"}` and each of the other 6 expressions, then `{"look":[1,0]}`, `{"look":[-1,0]}`, `{"look":[0,1]}` | Head moves smoothly to each pose, stays within `LIMITS`, ESP32 doesn't brown out or reset |
| B3 | Both eyes draw | Lorraine, Hana | Same commands | Both OLEDs show the right eyes for each expression |
| B4 | Head calibrated | Lorraine, Hana | Same commands with the head shell mounted | Poses read clearly from where the user sits; `PRESETS`, `LIMITS`, and pins committed |
| C1 | Extension blocks checkout | Alisha | Click "Place order" on the fake store with the brain off | Checkout is held and the extension shows it can't reach the brain |
| C2 | Extension ↔ brain | Alisha + Annie | Fake store → brain on the laptop (ESP32 unplugged) | Questions appear, typed answers go back, `release` lets checkout continue, `timer` and `escalate` hold it; `decision` lands in `decisions.jsonl` |
| C3 | Review button | Alisha + Annie | Click "Budget review" in the extension | Loading state, then summary and tips appear; an `error` shows a friendly message, not a crash |
| M4 | Full end-to-end | All | Fake store → brain → ESP32 over USB | Head and eyes react, the line is spoken, and the verdict releases or holds the page, 3 runs in a row |
| M5 | Failure drills | All | During a run: unplug the ESP32's USB; set a bad API key | Brain keeps going (prints commands / uses default line) and reconnects when replugged; the page never stays stuck |
| M6 | Latency check | Annie | Time from "Place order" to first spoken word | Under ~3 s; if not, switch model, shorten prompt, or start `thinking` sooner |
| M7 | Demo ready | All | Full scripted demo: one release, one timer, one escalate, then a budget review | 3 clean rehearsals; backup video recorded |

Stretch milestones (only after M4): push-to-talk speech input (Whisper), face tracking with MediaPipe (`look` commands), ElevenLabs voice.

## Next up

- Personality tuning.
- Possible MLH prizes: Best Use of Gemini API; Best Use of ElevenLabs (swap `speak()` for an ElevenLabs voice).
- Before judging: rehearse, record a backup video.
