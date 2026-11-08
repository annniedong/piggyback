# Piggyback

A robot piggy bank that judges your online purchases. Built for MakeCU 2026 (Columbia's hardware hackathon, Nov 7–8), Creativity track.

When you click "Place order" on a shopping site, the Piggyback Chrome extension pauses checkout and tells Piggyback what you're buying. Piggyback turns its head toward you, changes its eyes, and asks two or three short questions out loud. You have to look it in the (OLED) eyes to answer: a webcam in its head knows when you're making eye contact, and its eyes and head follow you. Then it gives a verdict: go ahead (only after you hold its stare for 3 seconds), take a cooling-off timer, or it escalates to an accountability buddy. The extension holds the order until the verdict; you can still buy anyway, and Piggyback notices and says so. Catchphrase: "Put it back."

Pitch idea: digital money is invisible, so Piggyback gives your spending a physical, expressive presence. You can argue with it, but only to its face: holding eye contact through a real argument can save a close call, and only a proven necessity gets an over-budget purchase through.

## Commit rules

- Do NOT add "Co-Authored-By: Claude" or any Claude session links to commits or PRs. Commits are authored by the team only.

## Architecture

```
Chrome extension ──WebSocket /ws────► brain.py (laptop, one FastAPI app :8000)
bot/browser.py ───HTTP POST /alert──►    │  Gemini Flash (Vertex, piggyback-brain): checkout questions + verdicts
extension ────────POST /sale, /cart──►   │  sales.py: page reading (Gemini flash-lite, Alisha's key)
                                         │  Claude: on-demand budget review (GET /review)
                                         │  speaks via macOS `say`, hears answers on the webcam mic (faster-whisper)
                                         │  vision.py: webcam in the head → face position + eye contact (MediaPipe)
                                         │  store.py + dashboard.html: profile, goals, waiting room (GET /)
                                         └──USB serial (JSON lines)──► ESP32 firmware
                                                                         ├─PWM──► 2× neck servos (pan/tilt)
                                                                         ├─I2C bus 0─► left SSD1306 OLED eye
                                                                         └─I2C bus 1─► right SSD1306 OLED eye
```

All HTTP/WebSocket endpoints (`/ws`, `/alert`, `/review`, `/health`, plus the `/store` demo page) live on the single `app` in `brain.py`. Don't start a second server: only one process can hold the ESP32's serial port, and every endpoint needs it.

No Raspberry Pi. The laptop talks to the ESP32 over a USB cable, so there's no Wi-Fi or network setup. The ESP32 has two hardware I2C buses, so both OLEDs can keep the default 0x3C address.

## Files

- `brain.py` (laptop): conversation logic, LLM calls, hard budget rules, TTS, logging, and commands to the ESP32.
- `firmware/piggyback_esp32/piggyback_esp32.ino` (ESP32, Arduino): turns expression names into servo motion and eye images.
- `vision.py` (laptop): reads the webcam in the pig's head; tracks the face and whether the user is looking at the eyes. Downloads `models/face_landmarker.task` on first run.
- `store.py` (laptop, or Cloud Run later): the user's profile: budget, purchases, savings goals, the waiting room, and past arguments. `STORE=json` keeps it in `profile.json` (migrated automatically from the old `budget.json`); `STORE=firestore` keeps it in Firestore document `profiles/<PROFILE_ID>` (needs `pip install google-cloud-firestore`; not tested yet). Same functions either way.
- `dashboard.html`: the profile page at `http://localhost:8000/`: money left, money saved by letting things go, arguments won, savings goals, the waiting room, recent arguments, monthly budget. Refreshes every 4 s, so it fills in live during the demo. Works at phone width.
- `check_devices.py` (laptop): checks the camera, mic, and Gemini before a run; lists cameras by name with brightness, `--snapshot` saves what each sees to `/tmp`.
- `test_client.py` (laptop): pretends to be the extension for testing the brain (`--auto` canned answers, `--voice` answer out loud; `BRAIN_URL` for another port).
- `extension/` (Chrome, Manifest V3): holds "Place order" on the demo store and runs the checkout conversation (`content.js`), Budget review popup (`popup.html`), browsing alerts to `/alert` (`background.js`). Load unpacked; see `extension/README.md`.
- `store/` (served by the brain at http://localhost:8000/store/): fake demo store; its Place order button carries `data-piggyback="place-order"` with `data-item`, `data-price`, `data-category`.
- `sales.py` (laptop): `POST /sale`, AI check for sales/coupons/promo codes on a shopping page, and `POST /cart`, AI reading of item count + subtotal from any store's cart page or cart drawer. Uses only `GEMINI_API_KEY_WEB`; local checks without it. Loaded into `brain.py` with `include_router`; also loads the git-ignored `.env`.
- `bot/` (laptop, optional): Playwright browser that watches Amazon/Target/Walmart tabs and posts browsing alerts to `/alert`. Separate process; see `bot/README.md`. Tests: `python -m unittest bot.test_detector`.

## Message formats

Extension → brain (WebSocket `ws://localhost:8000/ws`):

```json
{"type": "checkout", "item": "Smart toaster", "price": 400, "category": "kitchen", "url": "https://..."}
{"type": "decision", "item": "Smart toaster", "outcome": "completed" | "abandoned", "seconds_on_page": 42}
{"type": "answer", "text": "I need it for work"}
```

- `checkout`: sent when the user clicks Place order (or similar). `price` is a number in dollars. `url` (optional) is the checkout page, used to match what the extension noticed on that site.
- `decision`: `completed` when the user goes ahead with the order, `abandoned` when they put it back.
- `answer`: optional. Spoken answers are heard by the brain itself; typed answers are for testing or if the mic fails.

Brain → extension (the overlay shows `question` and `verdict`; it doesn't show `heard`, `confirm`, or `looked_away` yet):

```json
{"type": "question", "line": "...", "expression": "suspicious"}
{"type": "heard", "text": "I need it for my cooking class"}
{"type": "confirm", "seconds": 3, "line": "Look me in the eyes for 3 seconds if you're sure."}
{"type": "looked_away", "strikes": 1, "heard": "For my"}
{"type": "verdict", "verdict": "release" | "timer" | "escalate", "seconds": 60, "line": "...", "rule": null}
```

Browser bot → brain (HTTP `POST http://localhost:8000/alert`, returns `{"ok": true}`):

```json
{"type": "shopping_visit" | "ad_referral" | "cart" | "over_budget" | "cart_count" | "shopping_visits",
 "website": "https://www.amazon.com/...", "value": 182.5, "count": 7}
```

Signals are silent: nothing is spoken and the head doesn't move. `sales.py` keeps them per site for 30 minutes (`ad_referral`; `cart` with `count` and `value` = total; `shopping_visit` with `value` = pages today), along with any sale `/sale` found. When a checkout arrives with a `url` on that site, the AI prompt gets a "what you noticed on the page" section, and Piggyback brings it up ("An ad brought you here and the countdown is rushing you"). These are reasons to question, not to refuse; a real need still wins.

Budget review (HTTP, extension button → `GET http://localhost:8000/review`, add `?speak_it=true` to have Piggyback say the summary out loud):

```json
{"monthly_budget": 600, "spent": 369, "remaining": 231, "by_category": {"food": 244, "fun": 85},
 "checkouts_completed": 3, "checkouts_abandoned": 2,
 "summary": "...", "tips": ["...", "...", "..."], "expression": "skeptical", "error": null}
```

The numbers are computed in code from the profile (`store.py`) and `decisions.jsonl`; Claude also sees the savings goals and money saved by letting things go. Claude only writes `summary`, `tips`, and `expression`. It gives general budgeting guidance only (no investment, tax, credit, or product advice). If Claude fails, `summary` is a plain computed sentence, `tips` is empty, and `error` says why. Takes a few seconds, so show a loading state.

Profile API (HTTP, used by the dashboard; every response is the dashboard summary):

- `GET /profile`: budget numbers, `saved_by_letting_go`, `arguments_won`/`arguments_total`, `goals`, `waiting`, `arguments` (latest 10), `purchases` (latest 10)
- `PUT /budget` `{"monthly_budget": 600}`
- `POST /goals` `{"name": "Spring break trip", "target": 800, "deadline": "2027-03-01"}` (deadline optional)
- `POST /goals/{id}/save` `{"amount": 50}` (negative to take money out); `DELETE /goals/{id}`
- `POST /waiting/{id}/let_go` `{"goal_id": "..."}` (goal optional: its price moves into that goal); `POST /waiting/{id}/still_want`

No sign-in yet: fine on localhost, but add auth before putting this on a public URL.

Brain → ESP32 (USB serial, 115200 baud, one JSON object per line ending in `\n`):

```json
{"expr": "suspicious"}
{"look": [pan, tilt]}
{"gaze": [x, y]}
{"stare": 0.4}
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
- `gaze` (−1…1) moves the OLED pupils toward the user; `stare` (0…1) shows stare-down eyes whose lids close as the confirmation progresses. Both are sent ~10×/s and get no reply. Any `expr` ends a stare.
- The ESP32 replies right away and moves in the background. A new command interrupts the current motion.
- The brain never waits on replies; it only logs them.

## Brain behavior (brain.py)

1. On `checkout`: start a session, send `alert`, then `thinking` while waiting for the LLM.
2. The LLM returns JSON `{"line", "expression", "verdict"}`. The server moves the head, sends the line to the bot, and speaks it.
3. After speaking a question, it waits for eye contact (up to 3 s, then says "Look me in the eyes and tell me." and waits 6 s more). The whole answer has to be said looking at the pig: speech only starts counting while the user is looking, and looking away for more than `LOOK_AWAY_GRACE` (0.6 s; a blink or glance is fine) mid-answer breaks it.
   - First break: "You looked away. That doesn't count. Look at me and say it again." and they redo that answer.
   - Second break: "You looked away twice. Argument over." The argument ends, the verdict comes right away, and it can't be better than `timer` (rule `looked_away`).
   Without a working camera, answers can't be verified, so close-to-budget and over-budget purchases get the strict rules.
   It listens on the mic: recording starts at the first loud sound, stops after a 1 s pause (anything well below the speaker's own volume counts as a pause, so room noise doesn't keep it recording; max 8 s), and faster-whisper transcribes it. Nothing heard in 6 s counts as "(no answer)". A typed `answer` also works; whichever arrives first wins.
4. The budget tier sets how long the argument is (`QUESTIONS_BY_TIER`): **easy** (fits comfortably) 2 questions, **close** (more than half of what's left) 3, **over** (more than what's left) 3. The AI is told the tier and how the user's eye contact has gone, and presses harder on close and over.
   Its job is to stop impulse buys, not necessities: with each reply it also rates the purchase `need`, `want`, or `unclear`. A need (something they rely on broke or ran out, health or safety, required for work or school, can't wait) gets a release even if it hurts the budget; upgrades, duplicates, treats, gifts, and vague or changing stories are wants.
5. `apply_rules()` can only make a verdict stricter. A **clean** argument means every answer was spoken with camera-verified, unbroken eye contact (typed answers never count) and nothing went unanswered.
   - easy: the AI's verdict stands.
   - close: a release only stands if the argument was clean; otherwise → timer (`half_budget`).
   - over: escalate (`over_budget`), unless the argument was clean. Clean, and the AI says release and rated it a `need` → release (`necessity`, still needs the 3 s stare). Clean and the AI was convinced otherwise (release or timer) → timer (`over_budget_argued`).
   - two look-aways → timer, or escalate if over budget or the AI said escalate (`looked_away`). Invalid verdict → timer.
   A `release` then has to be earned: Piggyback sends `confirm`, says "Look me in the eyes for 3 seconds if you're sure," and the OLED lids close as you hold its stare. Looking away resets it. No 3-second stare within 10 s → `timer` with rule `no_eye_contact`.
6. `decision` messages are logged to `decisions.jsonl` with the verdict. If the user completed a purchase after a `timer` or `escalate`, Piggyback looks disappointed and says "You bought it anyway." Completed purchases are added to the profile (seeded with a fake month of spending, a $600 budget, and one savings goal), and mark that item as bought in the waiting room.
7. Memory (`store.py`): each verdict is saved as an argument (item, what the user said, need/want, verdict). A `timer` or `escalate` puts the item in the waiting room; checking out the same item again counts as another try. The AI is told the user's savings goals (and what share of the remaining goal this purchase is), which attempt this is, and what they said last time, so it can say "Third time this week" or "Last time you said they were a gift." On the dashboard the user can let a waiting item go (optionally moving its price into a goal) or say they still want it.

Tracking (10×/s): the OLED pupils follow the user's face (`gaze`), and the head turns to keep the face centered in the camera (`look`). Since the camera is in the head, centered face = pig facing the user. Tracking pauses for ~1.3 s after each expression so the pose plays, and the head holds still while recording so servo noise doesn't reach the mic. When a face appears after 5 s of nobody, Piggyback wakes up (`alert`).

Gemini sees a downscaled webcam frame with each call (when a face is visible), so it can react to what it sees ("You're already holding one"). The prompt tells it never to judge honesty from faces and not to base the verdict on the photo. Frames are kept in memory only, never saved; tell demo users the camera is on.

Fallbacks: camera fails → no tracking and no eye-contact rule (answers by voice or typing still work); mic fails → logs it and waits for a typed answer; ESP32 not plugged in or serial write fails → commands are printed and the brain retries opening the port; bad LLM JSON → default line; Claude advisor tool fails → retries without it.

Opening the serial port resets the ESP32, so the brain waits for `{"ready": true}` (or about 2 s) before sending the first command.

## Config (environment variables)

- `LLM_PROVIDER` (checkout conversation): `gemini` (default, the plan), `claude`, or `mock` (canned lines, no API key; for testing and as a demo backup)
- Two Gemini sign-ins, one per job, so their usage limits never collide:
  - **Checkout conversation** (`brain.py`): Vertex AI on project `piggyback-brain`, signed in with Application Default Credentials (the team's Google accounts block API keys). Install the gcloud CLI, run `gcloud auth application-default login` and `gcloud auth application-default set-quota-project piggyback-brain` once, then set `GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=piggyback-brain GOOGLE_CLOUD_LOCATION=global` (export them or put them in `.env`). A personal `GEMINI_API_KEY` also works if Vertex isn't set. Check with `python check_devices.py gemini`.
  - **Page reading** (`sales.py`, `/sale` and `/cart`): `GEMINI_API_KEY_WEB`, Alisha's key from her own project. Only this key is used there; without it, page reading uses local checks only.
  - Settings can go in a git-ignored `.env` next to `brain.py` (loaded at startup; real environment variables win). Never put keys in code.
- `GEMINI_MODEL` (default `gemini-3.8-flash`), `GEMINI_THINKING` (default `low`; ~1.5–2 s per reply on 3.8-flash vs 3–5 s on the model default). If a model rejects the thinking setting (3.8-flash rejects `minimal`), that model is retried without it. On a free key, set `GEMINI_MODEL=gemini-3.5-flash GEMINI_THINKING=minimal` in `.env` (3.7/3.8-flash timed out on the free key).
- `GEMINI_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`): the chat retries on this model when `GEMINI_MODEL` returns 429/503/504. The free tier allows 5 requests/min per model and a checkout uses up to 4, so back-to-back rehearsals on a free key hit it.
- `GEMINI_WEB_MODEL` (default `gemini-3.5-flash-lite`, ~1 s): model for the page-reading checks in `sales.py`
- `ANTHROPIC_API_KEY`, `EXECUTOR_MODEL` (default `claude-sonnet-5-5`), `ADVISOR_MODEL` (default `claude-opus-5-5`), `USE_ADVISOR` (default `1`; Claude advisor tool, beta `advisor-tool-2026-03-01`, used only for the final verdict so questions stay fast). Claude requests also enable server-side refusal fallbacks (`fallbacks: "default"`).
- `REVIEW_MODEL` (default `claude-opus-5-5`): model for the budget review; needs `ANTHROPIC_API_KEY`
- `SCRAPE_AI` (default `auto`): when the web-scraping checks (`/sale`, `/cart` in `sales.py`) call the LLM. `auto` = free local check first, LLM only when it isn't sure (lone "Sale" word, struck prices with no stated offer, cart missing a number or two local readers disagreeing); `always`; `never`. The checkout chat always uses the LLM.
- `TIMER_SECONDS` (default `60`): cooling-off length sent with a `timer` verdict
- `SERIAL_PORT` (default: auto-detect the first `/dev/cu.usbserial*`, `/dev/cu.SLAB_USBtoUART*`, or `/dev/cu.wchusbserial*`)
- `SERIAL_BAUD` (default `115200`)
- `VOICE` (macOS `say` voice)
- `VISION` (default `1`; `0` turns off the camera), `CAMERA_NAME` (default `logitech`; the first camera whose name contains this), `CAMERA_INDEX` (unset; a number skips name matching), `SEND_FRAMES` (default `1`; `0` stops sending frames to Gemini)
- `CONFIRM_SECONDS` (default `3`), `CONFIRM_WAIT` (default `10`): stare needed to confirm a release, and how long to wait for it
- `LOOK_AWAY_GRACE` (default `0.6`): seconds of looking away mid-answer that break it
- Tracking calibration: `TRACK_GAIN` (default `0.15`), `TRACK_INVERT_PAN`, `TRACK_INVERT_TILT`, `GAZE_INVERT_X` (set to `1` if the head or pupils move away from the user)
- Eye-contact thresholds: `LOOK_MAX_YAW` (`0.15`), `LOOK_MIN_PITCH` (`0.25`), `LOOK_MAX_PITCH` (`0.85`), `LOOK_MAX_EYE_TURN` (`0.45`)
- `STORE` (default `json`; `firestore` for Cloud Run), `PROFILE_ID` (default `default`; the Firestore document), `PROFILE_FILE` (default `profile.json`; tests point it elsewhere)
- `MIC_DEVICE` (default `logitech`; the first input whose name contains this)
- `LISTEN` (default `1`; `0` turns off the mic, answers must be typed), `WHISPER_MODEL` (default `base.en`), `MAX_LISTEN_SECONDS` (default `8`), `NO_SPEECH_SECONDS` (default `6`)

Run:

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # only needed for bot/
uvicorn brain:app --port 8000   # with the Vertex settings exported or in .env
```

Test without the bot: `LISTEN=0 uvicorn brain:app --port 8000`, then `python test_client.py "Smart toaster" 400 kitchen` (add `--auto` for canned answers). Open `http://localhost:8000/` for the dashboard. `GET /health` shows the LLM provider, the store, whether the ESP32 is connected, and the remaining budget.

Close the Arduino Serial Monitor before starting the brain: only one program can hold the port.

Camera calibration: with the brain running, open `http://localhost:8000/vision.jpg` (refresh to update) and `http://localhost:8000/vision`. Sit where the user sits: the marker should turn green only when you look at the OLED eyes. Adjust the `LOOK_*` values from the numbers shown. Then check the head turns toward you, not away; if not, set `TRACK_INVERT_PAN` / `TRACK_INVERT_TILT`. Allow camera access for your terminal the first time.

Devices are picked by name, so a nearby iPhone (Continuity Camera) or AirPods can't take over: the camera and mic whose names contain "logitech". If none matches (e.g. testing on a laptop), it uses the first camera and the default mic that aren't a phone or virtual device (iPhone, iPad, Desk View, BlackHole, OBS, Zoom, Teams). `/health` and the startup log show which camera and mic were picked. Run `python check_devices.py` from your own Terminal app first: macOS asks for camera and microphone permission there, and commands run by Claude Code can't trigger the popup. Turning off Continuity Camera on nearby iPhones (Settings → General → AirPlay & Continuity) also helps. The Whisper model (~150 MB) downloads on the first run, so start the brain once before the venue Wi-Fi matters.

## ESP32 firmware (firmware/piggyback_esp32)

- Arduino IDE or `arduino-cli` with the ESP32 board package. Libraries: ArduinoJson, Adafruit SSD1306, Adafruit GFX, ESP32Servo.
- Suggested pins (confirm for your board): pan servo GPIO 18, tilt servo GPIO 19; left eye on I2C bus 0 (SDA 21, SCL 22); right eye on I2C bus 1 (SDA 25, SCL 26). Write the final pins here once wired.
- Servos are driven straight from ESP32 PWM pins with ESP32Servo. MG90S servos accept the 3.3 V signal. If that's unreliable, fall back to the PCA9685 on I2C bus 0 (address 0x40, it can share the bus with the left eye).
- `LIMITS` clamps angles; `PRESETS` maps expressions to (pan, tilt, eyes, seconds). Motion uses smoothstep easing, stepped inside `loop()` without `delay()` so new commands interrupt it.
- Both I2C buses run at 400 kHz so full-frame eye redraws keep up with `gaze` updates; eyes redraw at most ~15 fps.
- At boot: move slowly to the home pose, show `idle` eyes, then print `{"ready": true}`.
- Eyes are drawn with shapes in `drawEye()` (128×64, one color), not bitmaps; `thinking` animates. Swap in bitmaps later if the team draws them.
- TODO: calibrate `PRESETS` and `LIMITS` once the head is mounted.

## Hardware

- ESP32 dev board, 2× MG90S neck servos (pan/tilt; head must be light), 2× SSD1306 0.96" OLED eyes, Logitech webcam with mic (on the laptop), Facmogu bench supply at 5V for servos, barrel-jack screw adapter, 470 µF capacitor across the servo rail. PCA9685 is a spare.
- Webcam goes inside the head, between or just above the OLED eyes, so looking at the eyes = looking at the camera. Take it out of its clip/stand to save weight: MG90S servos are small, so keep the camera close to the tilt axis and leave slack in the USB cable so it doesn't drag the head. Its mic is now right next to the servos, which is why the head freezes while recording.
- Power rules: servos run from the bench supply, never from the ESP32's 5V/VIN pin; the ESP32 is powered by the laptop's USB; all grounds shared (bench supply ground ↔ ESP32 GND). OLEDs run from the ESP32's 3.3V pin.

## Team

- Alisha: page-reading bot (+ fake store page to demo on); detects checkout and the confirmation page, extracts item/price/category, sends `checkout` and `decision`.
- Annie: brain server, AI, laptop-to-ESP32 serial connection.
- Lorraine and Hana (builders): pig head shell and neck mount with the webcam built in; power wiring, servos, OLEDs, ESP32 firmware.

## Testing milestones

Each milestone has a test anyone can run and a pass check. Don't start the next integration milestone until the previous one passes. Tracks A (brain, Annie), B (ESP32, Lorraine and Hana), and C (bot, Alisha) run in parallel until M4.

| # | Milestone | Owner | Test | Pass when |
|---|-----------|-------|------|-----------|
| M0 | Contract agreed | All | Review the brain→ESP32 and bot→brain formats above | Expression names, `look` units, reply lines, and pins are written here and nobody objects |
| A1 | Brain runs alone | Annie | `uvicorn brain:app --port 8000`, then send a `checkout` over a WebSocket client (e.g. `websocat ws://localhost:8000/ws`) | Gets 2 `question`s, answering both gets a `verdict`; laptop speaks each line; ESP32 commands are printed (nothing plugged in) |
| A2 | Budget rules hold | Annie | Typed answers (`LISTEN=0`): a checkout over the remaining budget, and one using more than half of it | First → `escalate`, second → at least `timer`, no matter what the LLM says (typed answers are never a clean argument) |
| A3 | Brain → bare ESP32 | Annie | Flash a firmware that only parses and replies (no servos or OLEDs), plug it in, run A1 again | Every command gets an `ok` back in the brain's log, in order; unplug and replug recovers without restarting the brain |
| A4 | Budget review works | Annie | With `ANTHROPIC_API_KEY` set, `curl 'localhost:8000/review?speak_it=true'` after a few test checkouts | Numbers match `/profile`; summary and 3 tips mention real categories; Piggyback says the summary; without a key it still returns numbers and a fallback summary |
| A5 | Spoken answers | Annie | Run the brain with `LISTEN=1`, then `python test_client.py --voice`; answer out loud | Each answer appears as `heard` and is transcribed correctly in a quiet room and with background noise; silence moves on after 6 s |
| A6 | Eye contact | Annie | `/vision.jpg` while sitting at the pig; look at the eyes, then at the screen, your phone, and away. Then argue a close-to-budget purchase (e.g. $150 of $231 left) out loud | Green only when looking at the eyes; a blink doesn't break an answer, a real look away does (first time: redo; second: argument over); a clean close-to-budget argument can end in release; a clean over-budget argument for a want ends in timer; a clean one proving a real need (e.g. broken laptop charger, project due tomorrow) can end in release |
| B1 | ESP32 talks serial | Lorraine, Hana | Flash the firmware, open Serial Monitor at 115200, type `{"ping":1}` | Boot prints `{"ready":true}`; ping returns `{"pong":1}`; bad JSON returns `err` without crashing |
| B2 | Servos move | Lorraine, Hana | In Serial Monitor, send `{"expr":"alert"}` and each of the other 6 expressions, then `{"look":[1,0]}`, `{"look":[-1,0]}`, `{"look":[0,1]}` | Head moves smoothly to each pose, stays within `LIMITS`, ESP32 doesn't brown out or reset |
| B3 | Both eyes draw | Lorraine, Hana | Same commands | Both OLEDs show the right eyes for each expression |
| B4 | Head calibrated | Lorraine, Hana | Same commands with the head shell mounted | Poses read clearly from where the user sits; `PRESETS`, `LIMITS`, and pins committed |
| B5 | Pupils and stare | Lorraine, Hana | Send `{"gaze":[1,0]}`, `{"gaze":[-1,0.5]}`, then `{"stare":0}` … `{"stare":1}` | Pupils move to each side; stare lids close smoothly; an `expr` command returns to normal eyes |
| C1 | Bot reads checkout | Alisha | Open checkout on the fake store (and one real store) with the brain off | Bot prints the right item, price, and category, and reconnects when the brain starts |
| C2 | Bot ↔ brain | Alisha + Annie | Fake store → brain on the laptop (ESP32 unplugged) | Reaching checkout starts the questions; buying sends `completed`, leaving sends `abandoned`; both land in `decisions.jsonl`; buying after a `timer` triggers "You bought it anyway" |
| M4 | Full end-to-end | All | Fake store → brain → ESP32 over USB | Head and pupils follow the user, answers only count with eye contact, a release needs the 3 s stare, verdicts are spoken, 3 runs in a row |
| M5 | Failure drills | All | During a run: unplug the ESP32's USB; set a bad API key; deny or unplug the mic; cover or unplug the camera | Brain keeps going (prints commands / uses default line / accepts typed answers / skips eye contact) and reconnects when replugged |
| M6 | Latency check | Annie | Time from reaching checkout to first spoken word, and from end of an answer to the next line | Under ~3 s; if not, switch model, shorten prompt, or start `thinking` sooner |
| M7 | Demo ready | All | Full scripted demo: one release, one timer, one escalate, then a budget review | 3 clean rehearsals; backup video recorded |

Stretch milestones (only after M4): ElevenLabs voice.

## Next up

- Personality tuning.
- Possible MLH prizes: Best Use of Gemini API; Best Use of ElevenLabs (swap `speak()` for an ElevenLabs voice).
- Before judging: rehearse, record a backup video.
