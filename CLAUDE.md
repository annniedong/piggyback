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
                                               │  speaks via macOS `say`, hears answers on the laptop's mic (faster-whisper)
                                         │  vision.py: webcam in the head → face position + eye contact (MediaPipe)
                                         │  store.py + dashboard.html: profile, goals, waiting room (GET /)
                                         │  tiger.py: history in Tiger Data (Postgres + TimescaleDB, cloud)
                                         └──USB serial (JSON lines)──► ESP32 firmware
                                                                         ├─PWM──► pan servo (head turns left/right)
                                                                         └─I2C (SDA 21, SCL 22)─► one SSD1306 OLED, both eyes
```

All HTTP/WebSocket endpoints (`/ws`, `/alert`, `/review`, `/health`, plus the `/store` demo page) live on the single `app` in `brain.py`. Don't start a second server: only one process can hold the ESP32's serial port, and every endpoint needs it.

No Raspberry Pi. The laptop talks to the ESP32 over a USB cable, so there's no Wi-Fi or network setup, and the Logitech webcam plugs into the laptop by USB. One 128×64 OLED shows both eyes.

## Files

- `brain.py` (laptop): conversation logic, LLM calls, hard budget rules, TTS, logging, and commands to the ESP32.
- `firmware/pig_eyes/pig_eyes.ino` (ESP32, Arduino): draws both eyes on one OLED and turns the head (one pan servo) from the brain's commands. `firmware/piggyback_esp32/` is the older all-in-one sketch, kept as a reference.
- `vision.py` (laptop): reads the webcam in the pig's head; tracks the face and whether the user is looking at the eyes. Downloads `models/face_landmarker.task` on first run.
- `store.py` (laptop, or Cloud Run later): the user's profile: budget, purchases, savings goals, the waiting room, and past arguments. `STORE=json` keeps it in `profile.json` (migrated automatically from the old `budget.json`); `STORE=firestore` keeps it in Firestore document `profiles/<PROFILE_ID>` (needs `pip install google-cloud-firestore`; not tested yet). Same functions either way.
- `tiger.py` (laptop): history in Tiger Data (Postgres + TimescaleDB). Hypertables `gaze` (one row per 0.1 s during a checkout: present, looking, answering, yaw/pitch; compressed after a day) and `events` (checkout, verdict, decision, purchase, added, let_go). Continuous aggregates `eye_contact_by_minute` and `money_by_day` (real-time, local days) feed `GET /trends`. Writes are batched once a second from a background thread, so the pig never waits on the database; without `TIGER_DB_URL` or if it's unreachable, nothing is recorded and everything else works. On the first connect to an empty database it copies the profile's purchases and arguments in. `profile.json` stays the source of truth.
- `dashboard.html`: the profile page at `http://localhost:8000/`: money left, money saved by letting things go, arguments won, savings goals, the waiting room, recent arguments, monthly budget, and "Stare history" from `/trends` (eye contact per argument, spent vs. saved per day, riskiest hour; hidden without Tiger Data). Refreshes every 4 s, so it fills in live during the demo. Works at phone width.
- `seed_demo.py` (laptop): fills the demo with three weeks of history: goals, purchases ($231 left of $600), a waiting room with let-go items, 11 past arguments with quotes, `decisions.jsonl`, and in Tiger Data the matching events plus a gaze time series per argument (eye contact high on releases, low on stopped ones; 11 pm is the riskiest hour). Backs up `profile.json` and `decisions.jsonl` to `*.backup-<time>` first and empties the Tiger tables. Run `python seed_demo.py` before a demo; works while the brain runs.
- `check_devices.py` (laptop): checks the camera, mic, speaker, and Gemini before a run; lists cameras by name with brightness, `--snapshot` saves what each sees to `/tmp`.
- `demo.sh` (laptop): the judging table. `./demo.sh` seeds fresh demo data and runs the brain in demo mode, restarting it if it crashes; `./demo.sh --keep` keeps today's history after a restart. See Demo mode below.
- `tests/test_integration.py` (laptop): integration tests, `python -m unittest discover tests` (~40 s). Each starts a real brain on a spare port (mock AI, typed answers, no camera/mic/ESP32, a silent `say`, its own `PROFILE_FILE`/`DECISIONS_FILE`) and drives it like the extension: budget tiers and rules, waiting room and retries, the bought-anyway shake finishing before idle, disconnects, `/added`, `/alert`, the profile API, the review fallback, and an unreachable database. The Tiger Data tests run in a throwaway schema (dropped after) and are skipped without `TIGER_DB_URL`.
- `test_client.py` (laptop): pretends to be the extension for testing the brain (`--auto` canned answers, `--voice` answer out loud; `BRAIN_URL` for another port).
- `extension/` (Chrome, Manifest V3): holds "Place order" on the demo store and runs the checkout conversation (`content.js`), Budget review popup (`popup.html`), browsing alerts to `/alert` (`background.js`). Load unpacked; see `extension/README.md`.
- `store/` (served by the brain at http://localhost:8000/store/): fake demo store; its Place order button carries `data-piggyback="place-order"` with `data-item`, `data-price`, `data-category`.
- `sales.py` (laptop): `POST /sale`, AI check for sales/coupons/promo codes on a shopping page, and `POST /cart`, AI reading of item count + subtotal from any store's cart page or cart drawer. Uses only `GEMINI_API_KEY_WEB`; local checks without it. Loaded into `brain.py` with `include_router`; also loads the git-ignored `.env`.
- `bot/` (laptop, optional): Playwright browser that watches Amazon/Target/Walmart tabs and posts browsing alerts to `/alert`. Separate process; see `bot/README.md`. Tests: `python -m unittest bot.test_detector`.

## Demo mode (judging table)

`./demo.sh` sets `DEMO_MODE=1`, `TIMER_SECONDS=20`, `DEMO_RESET_MINUTES=10` (unless `.env` says otherwise), seeds the demo, and keeps the brain running. Visitors shop on any real site in the demo Chrome and run into Piggyback at checkout.

- No real orders: every `verdict` carries `"demo": true`, and the extension remembers it (it assumes demo until a verdict says otherwise). On real stores, final order buttons (`PLACE_ORDER` labels: "Place order", "Pay now", "Buy now"...) never go through, not after a release, not inside the "already went ahead" window, not with holding turned off: the visitor sees "Piggyback demo: no real order was placed", and a released one still counts as `completed`, so the budget drops. "Proceed to checkout" buttons still work, so visitors can walk through a checkout. Payment buttons inside a store's iframe (Shop Pay, PayPal) can't be seen, so use a Chrome profile with no saved cards or signed-in store accounts.
- After going ahead on a site, its checkout isn't held again for 3 minutes (30 outside demo mode); adding something to the bag ends that early.
- Next visitor: the dashboard's "↺ Next visitor" button (`POST /demo/reset`, 403 outside demo mode, 409 mid-argument), or by itself after `DEMO_RESET_MINUTES` with no activity. It puts back `seed_demo.py`'s profile and decisions and forgets what the extension noticed on sites. Tiger Data keeps everyone's arguments, so the stare history fills up over the day.

## Message formats

Extension → brain (WebSocket `ws://localhost:8000/ws`):

```json
{"type": "checkout", "item": "Smart toaster", "price": 400, "category": "kitchen", "url": "https://..."}
{"type": "decision", "item": "Smart toaster", "outcome": "completed" | "abandoned", "seconds_on_page": 42}
{"type": "answer", "text": "I need it for work"}
```

- `checkout`: sent when the user clicks Place order (or similar). `price` is a number in dollars. `url` (optional) is the checkout page, used to match what the extension noticed on that site.
- `decision`: `completed` when the user goes ahead with the order, `abandoned` when they put it back.
- `answer`: optional. Spoken answers are heard by the brain itself; typed answers are for testing or if the mic fails. The overlay hides its text box behind a "Can't talk? Type instead" link, since typed answers never count as eye contact.

Add to bag (HTTP `POST http://localhost:8000/added`, from `background.js` when an "Add to cart/bag" button is clicked):

```json
{"website": "https://...", "item": "Wool Twill Pants", "price": 1200}
```

The item goes in the bag right away; Piggyback turns to `alert` and says one quick line out loud (Gemini on `GEMINI_FALLBACK_MODEL`, about 2 s, canned line after 2.5 s), like "Twelve-hundred bucks for pants? We'll chat at checkout." The remark is told what the extension noticed on that store (below); when an ad brought them there, it teases them about it ("Ooh, this was from an ad, wasn't it? You got distracted."), and so do the canned lines. The argument still waits for checkout, where the AI is told what went in the bag. Skipped during an argument and within 5 s of the last remark.

Brain → extension (the overlay, a small card in the bottom-right corner, shows all of these):

```json
{"type": "question", "line": "...", "expression": "suspicious"}
{"type": "listening", "seconds": 6}
{"type": "nudge", "line": "Take your time. I'm listening."}
{"type": "heard", "text": "I need it for my cooking class"}
{"type": "confirm", "seconds": 3, "line": "Look me in the eyes for 3 seconds if you're sure."}
{"type": "looked_away", "strikes": 1, "heard": "For my"}
{"type": "verdict", "verdict": "release" | "timer" | "escalate", "seconds": 60, "line": "...", "rule": null}
```

Browser bot → brain (HTTP `POST http://localhost:8000/alert`, returns `{"ok": true}`):

```json
{"type": "shopping_visit" | "ad_referral" | "sponsored" | "retargeted" | "upsell" | "cart" | "over_budget" | "cart_count" | "shopping_visits",
 "website": "https://www.amazon.com/...", "value": 182.5, "count": 7, "detail": "a Google ad"}
```

Signals are silent: nothing is spoken and the head doesn't move. `sales.py` keeps them per store for 30 minutes (subdomains together: an ad landing on oldnavy.gap.com and the bag on secure-oldnavy.gap.com are both gap.com) (`ad_referral` with `detail` = which ad; `sponsored` = clicked a paid search result; `retargeted` with `detail` = product/store and `value` = days since first visit; `upsell` with `detail` = the section; `cart` with `count` and `value` = total; `shopping_visit` with `value` = pages today), along with any sale `/sale` found. Detection rules are in `extension/adsignals.js`. When a checkout arrives with a `url` on that site, the AI prompt gets a "what you noticed on the page" section, and Piggyback brings it up ("An ad brought you here and the countdown is rushing you"). These are reasons to question, not to refuse; a real need still wins.

Budget review (HTTP, extension button → `GET http://localhost:8000/review`, add `?speak_it=true` to have Piggyback say the summary out loud):

```json
{"monthly_budget": 600, "spent": 369, "remaining": 231, "by_category": {"food": 244, "fun": 85},
 "checkouts_completed": 3, "checkouts_abandoned": 2,
 "summary": "...", "tips": ["...", "...", "..."], "expression": "skeptical", "error": null}
```

The numbers are computed in code from the profile (`store.py`) and `decisions.jsonl`; The AI also sees savings goals, money saved by letting things go, the waiting room, and recent arguments. It runs on Gemini through the same Vertex sign-in as the checkout chat (about 4 s), or on Claude when `ANTHROPIC_API_KEY` is set. The AI only writes `summary`, `tips`, and `expression`. It gives general budgeting guidance only (no investment, tax, credit, or product advice). If the AI fails, `summary` is a plain computed sentence, `tips` is empty, and `error` says why. Takes a few seconds, so show a loading state.

Profile API (HTTP, used by the dashboard; every response is the dashboard summary):

- `GET /profile`: budget numbers, `saved_by_letting_go`, `arguments_won`/`arguments_total`, `goals`, `waiting`, `arguments` (latest 10), `purchases` (latest 10)
- `PUT /budget` `{"monthly_budget": 600}`
- `POST /goals` `{"name": "Spring break trip", "target": 800, "deadline": "2027-03-01"}` (deadline optional)
- `POST /goals/{id}/save` `{"amount": 50}` (negative to take money out); `DELETE /goals/{id}`
- `GET /trends`: from Tiger Data: `arguments` (latest 12 with `eye_contact` 0…1 while answering, null if typed), `days` (spent, saved, verdict counts), `by_hour`, `gaze_rows`, `eye_contact`, `compression`. `{"enabled": false}` without a database.
- `POST /waiting/{id}/let_go` `{"goal_id": "..."}` (goal optional: its price moves into that goal); `POST /waiting/{id}/still_want`

No sign-in yet: fine on localhost, but add auth before putting this on a public URL.

Brain → ESP32 (USB serial, 115200 baud, one JSON object per line ending in `\n`):

```json
{"expr": "suspicious"}
{"look": [pan, tilt]}
{"look": [pan, tilt], "s": 1.5}
{"hold": 1}
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
- The head only turns left and right (one pan servo); nods and droops are left to the eyes.
- `look` values are −1…1, with `[0, 0]` as center. The ESP32 maps pan onto its own `PAN_MIN`/`PAN_MAX`, so the brain never needs servo angles; tilt is ignored. `look` sets the direction the pig faces; optional `s` is how long the move takes (default 0.3 s; the brain uses `TURN_SECONDS` for slow turns toward or away from the user). A `look` that arrives during a one-shot expression waits until it finishes.
- Expressions are short left-right turns around the facing direction, and each ends facing that way again: `alert` double take, `suspicious` side-eye, `skeptical` looks away and glances back, `satisfied` happy wiggle, `disappointed` slow "no" shake, `thinking` glances aside and sways (repeats until the next command), `idle` faces forward and holds still. An expression never speeds up a slow turn that's still going.
- `hold` (1/0) is sent while the brain records an answer: the ESP32 stops any turns, faces the user, and stays still (expressions only change the eyes) until `hold: 0`.
- `gaze` (−1…1) moves the OLED pupils toward the user; `stare` (0…1) shows stare-down eyes whose lids close as the confirmation progresses. Both are sent ~10×/s and get no reply. Any `expr` ends a stare.
- The ESP32 replies right away and moves in the background. A new command interrupts the current motion.
- The brain never waits on replies; it only logs them.

## Brain behavior (brain.py)

1. On `checkout`: start a session, send `alert`, then `thinking` while waiting for the LLM.
2. The LLM returns JSON `{"line", "expression", "verdict"}`. The server moves the head, sends the line to the bot, and speaks it.
3. After speaking a question, it waits for eye contact (up to 3 s, then says "Look me in the eyes and tell me." and waits 6 s more). The whole answer has to be said looking at the pig: speech only starts counting while the user is looking, and looking away for more than `LOOK_AWAY_GRACE` (0.6 s; a blink or glance is fine) mid-answer breaks it.
   - First break: "You looked away. Look at me and say that again." and they redo that answer.
   - Second break: "You looked away twice, so let's pause this one for a minute." The argument ends, the verdict comes right away, and it can't be better than `timer` (rule `looked_away`).
   Without a working camera, answers can't be verified, so close-to-budget and over-budget purchases get the strict rules.
   It sends `listening` when it starts recording (the overlay shows a pulsing "Listening. Take your time"). If nothing is said in `NO_SPEECH_SECONDS`, it nudges once ("Take your time. I'm listening.") and listens again; the second silence counts as "(no answer)". After a spoken answer it says a short "Hmm." / "Okay." so the ~2 s AI wait isn't dead air.
   It listens on the mic: recording starts at the first loud sound, stops after a 0.8 s pause (`END_PAUSE`; anything well below the speaker's own volume counts as a pause, so room noise doesn't keep it recording; max 8 s), and faster-whisper transcribes it. Nothing heard in 6 s, twice, counts as "(no answer)". A typed `answer` also works; whichever arrives first wins.
4. The budget tier sets how long the argument is (`QUESTIONS_BY_TIER`): **easy** (fits comfortably) 2 questions, **close** (more than half of what's left) 3, **over** (more than what's left) 3. The AI is told the tier and how the user's eye contact has gone, and presses harder on close and over.
   Its job is to stop impulse buys, not necessities: with each reply it also rates the purchase `need`, `want`, or `unclear`. A need (something they rely on broke or ran out, health or safety, required for work or school, can't wait) gets a release even if it hurts the budget; upgrades, duplicates, treats, gifts, and vague or changing stories are wants.
5. `apply_rules()` can only make a verdict stricter. A **clean** argument means every answer was spoken with camera-verified, unbroken eye contact (typed answers never count) and nothing went unanswered.
   - easy: the AI's verdict stands.
   - close: a release only stands if the argument was clean; otherwise → timer (`half_budget`).
   - over: escalate (`over_budget`), unless the argument was clean. Clean, and the AI says release and rated it a `need` → release (`necessity`, still needs the 3 s stare). Clean and the AI was convinced otherwise (release or timer) → timer (`over_budget_argued`).
   - two look-aways → timer, or escalate if over budget or the AI said escalate (`looked_away`). Invalid verdict → timer.
   A `release` then has to be earned: Piggyback sends `confirm`, says "Look me in the eyes for 3 seconds if you're sure," and the OLED lids close as you hold its stare. Looking away resets it. No 3-second stare within 10 s → `timer` with rule `no_eye_contact`.
6. `decision` messages are logged to `decisions.jsonl` with the verdict. If the user completed a purchase after a `timer` or `escalate`, Piggyback looks disappointed and says "You went for it anyway. I'll keep that in mind." Completed purchases are added to the profile (seeded with a fake month of spending, a $600 budget, and one savings goal), and mark that item as bought in the waiting room.
7. Memory (`store.py`): each verdict is saved as an argument (item, what the user said, need/want, verdict). A `timer` or `escalate` puts the item in the waiting room; checking out the same item again counts as another try. The AI is told the user's savings goals (and what share of the remaining goal this purchase is), which attempt this is, and what they said last time, so it can say "Third time this week" or "Last time you said they were a gift." On the dashboard the user can let a waiting item go (optionally moving its price into a goal) or say they still want it.

Facing the user: when nobody's buying, the head rests turned away (`IDLE_LOOK`), minding its own business. Something going in the bag starts a slow turn toward where the user was last seen (before the remark is even written), and it keeps facing them for 15 s, through the whole checkout argument, and 8 s after the decision; then it slowly turns away again.

Tracking (10×/s, while facing the user): the OLED pupils follow the user's face (`gaze`), and the head turns to keep the face centered in the camera (`look`). Since the camera is in the head, centered face = pig facing the user. Tracking pauses after each expression so its turns play (`EXPR_SECONDS`, matched to the steps in `pig_eyes.ino`), and the head holds still while recording (`hold`) so servo noise doesn't reach the mic and the pig faces you while you answer. The head only follows left and right; the pupils follow up and down. When a face appears after 5 s of nobody, Piggyback wakes up (`alert`).

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
- `REVIEW_PROVIDER` (default `gemini`, or `claude` when `ANTHROPIC_API_KEY` is set): who writes the budget review. `REVIEW_MODEL` (default `claude-opus-5-5`) is the Claude model; Gemini uses `GEMINI_MODEL`.
- `SCRAPE_AI` (default `auto`): when the web-scraping checks (`/sale`, `/cart` in `sales.py`) call the LLM. `auto` = free local check first, LLM only when it isn't sure (lone "Sale" word, struck prices with no stated offer, cart missing a number or two local readers disagreeing); `always`; `never`. The checkout chat always uses the LLM.
- `TIMER_SECONDS` (default `60`): cooling-off length sent with a `timer` verdict
- `SERIAL_PORT` (default: auto-detect the first `/dev/cu.usbserial*`, `/dev/cu.SLAB_USBtoUART*`, or `/dev/cu.wchusbserial*`)
- `SERIAL_BAUD` (default `115200`)

- `VOICE` (macOS `say` voice), `SPEECH_RATE` (default `215` words per minute; macOS default is about 180)
- `END_PAUSE` (default `0.8`): seconds of quiet that end a spoken answer
- `VISION` (default `1`; `0` turns off the camera), `CAMERA_NAME` (default `logitech`; the first camera whose name contains this), `CAMERA_INDEX` (unset; a number skips name matching), `CAMERA_ROTATE` (default `0`; degrees counterclockwise, 0/90/180/270, to turn the picture upright when the webcam is mounted on its side: the pig's head needs `90`, set in `.env`; only applied to the camera matching `CAMERA_NAME` (or a `CAMERA_URL` stream), so a laptop camera standing in stays upright; the Logitech shows up as "USB Camera VID:1133 PID:2085", so `.env` also sets `CAMERA_NAME=VID:1133`), `SEND_FRAMES` (default `1`; `0` stops sending frames to Gemini). `CAMERA_URL` (unset; not used for the demo): read the camera as a network video stream instead (e.g. from a Pi running ustreamer); it keeps only the newest frame and reconnects if the stream drops, but only if the stream was up when the brain started. Nothing in the repo sets up a stream yet.

- `CONFIRM_SECONDS` (default `3`), `CONFIRM_WAIT` (default `10`): stare needed to confirm a release, and how long to wait for it
- `LOOK_AWAY_GRACE` (default `0.6`): seconds of looking away mid-answer that break it
- `IDLE_LOOK` (default `0.6,0`; pan,tilt in `look` units): where the head rests when idle. Point it away from the user (toward the laptop, say); `0,0` to keep facing forward. `TURN_SECONDS` (default `1.5`): how long the turn toward or away from the user takes; raise it if the servos lag.
- Tracking calibration: `TRACK_GAIN` (default `0.15`), `TRACK_INVERT_PAN`, `GAZE_INVERT_X` (set to `1` if the head or pupils move away from the user)
- Eye-contact thresholds: `LOOK_MAX_YAW` (`0.15`), `LOOK_MIN_PITCH` (`0.25`), `LOOK_MAX_PITCH` (`0.85`), `LOOK_MAX_EYE_TURN` (`0.45`)
- `TIGER_DB_URL` (unset = off): Tiger Cloud connection string (`postgres://…?sslmode=require`), in `.env` only. Service: 0.5 CPU, development, us-east-1, no replica or pooler; allow the venue's IP (or all IPs) in its allow list. `TZ_NAME` (default `America/New_York`): local time for days and hours; changing it means dropping `money_by_day` so it's rebuilt.
- `DEMO_MODE` (default `0`; `demo.sh` sets `1`), `DEMO_RESET_MINUTES` (default `10`): see Demo mode
- `DECISIONS_FILE` (default `decisions.jsonl`; tests point it elsewhere)
- `STORE` (default `json`; `firestore` for Cloud Run), `PROFILE_ID` (default `default`; the Firestore document), `PROFILE_FILE` (default `profile.json`; tests point it elsewhere)
- `MIC_DEVICE` (default `macbook`, the laptop's built-in mic; the first input whose name contains this. `logitech` uses the webcam's mic instead)
- `SPEAKER` (default `macbook`, the laptop's built-in speakers, so AirPods or BlackHole can't take the voice; Piggyback's voice goes to the first audio output whose name contains this, via `say -a`; re-checked every 30 s so a speaker that reconnects is picked up; falls back to the system default)
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

Camera calibration: with the brain running, open `http://localhost:8000/vision.jpg` (refresh to update) and `http://localhost:8000/vision`. Sit where the user sits: the marker should turn green only when you look at the OLED eyes. Adjust the `LOOK_*` values from the numbers shown. Then check the head turns toward you, not away; if not, set `TRACK_INVERT_PAN`. Allow camera access for your terminal the first time.

Devices are picked by name, so a nearby iPhone (Continuity Camera) or AirPods can't take over: the camera whose name contains "logitech", the laptop's own mic ("macbook"), and the laptop's own speakers ("macbook"; `python check_devices.py speaker` lists outputs and says a test line on the one picked). If none matches (e.g. testing on a laptop), it uses the first camera and the default mic that aren't a phone or virtual device (iPhone, iPad, Desk View, BlackHole, OBS, Zoom, Teams). `/health` and the startup log show which camera, mic, and speaker were picked. Run `python check_devices.py` from your own Terminal app first: macOS asks for camera and microphone permission there, and commands run by Claude Code can't trigger the popup. Turning off Continuity Camera on nearby iPhones (Settings → General → AirPlay & Continuity) also helps. The Whisper model (~150 MB) downloads on the first run, so start the brain once before the venue Wi-Fi matters.

## ESP32 firmware

- `firmware/pig_eyes/pig_eyes.ino` (Lorraine; head turns adapted from Hana's PR #3): the eyes and the head. One SSD1306 128×64 OLED at 0x3C on SDA 21 / SCL 22 shows both eyes side by side, with blinking, brows, pupils that follow `gaze`, and stare-down lids. Speaks the brain's JSON commands over USB. Also takes typed commands in Serial Monitor for testing (`HELP`, `EYES suspicious`, `GAZE 50 0`, `BLINK`, `MOVE skeptical`, `PAN 120`, `HOME`, `TRIM 4`, `STATUS`). Libraries: Adafruit SSD1306, Adafruit GFX, ESP32Servo.
- Head: one MG90S pan servo on GPIO 18; the head only turns left and right. `PAN_HOME` (20°, Hana's calibration) is the servo angle where the mounted head faces straight ahead, and `look` [0, 0]; `PAN_MIN`/`PAN_MAX` are 45° each way from it, clamped to the servo's 0–180° (so 0–65°: only 20° of travel on the low side); `PAN` in Serial Monitor goes anywhere in 0–180° for finding `PAN_HOME` by hand; `MAX_DEG_PER_SEC` (100) caps its speed, so a turn that would need more just takes longer. Each brain expression has a list of steps (`*_STEPS`: degrees from the facing direction, milliseconds), mirrored when the pig faces right of center so turns go toward the middle. Turns use smoothstep easing, stepped inside `loop()` without `delay()`, so new commands interrupt them. `TILT_PIN` is -1 (not driven); set it to 19 to hold a mounted tilt servo still. When tuning steps, update `EXPR_SECONDS` in `brain.py` to match.
- Calibrating "straight ahead": the servo has no position sensor, so at boot it goes to `PAN_HOME` (20°) plus a saved trim. If the head needs a different home after remounting, find it with `PAN`, set `PAN_HOME` in `pig_eyes.ino`, and upload. Type `HOME` in Serial Monitor; if the head isn't facing straight forward, first remount the horn as close to straight as its teeth allow, then type `TRIM 3`, `TRIM -2`, … until it is (+ turns toward servo 180°, − toward 0°). The trim is saved in the ESP32's flash (survives reboots and uploads, and belongs to that board), shifts every head position including `look` [0, 0], and is printed at boot (`READY ... pan_trim=3`) and in `STATUS`. `TRIM 0` clears it.
- `firmware/piggyback_esp32/` is the older all-in-one sketch (two OLEDs, pan/tilt servos), kept as a reference.
- Wi-Fi is off. The brain only uses USB. To test the eyes over Wi-Fi without a cable, copy `firmware/pig_eyes/secrets.example.h` to `secrets.h` (git-ignored, so a hotspot password never reaches this public repo) and fill in a 2.4 GHz hotspot; the eyes then also listen on `pig.local:3333`. Leave it off for the demo: anyone on that network could send commands.
- Arduino IDE or `arduino-cli` with the ESP32 board package (board: ESP32 Dev Module). The sketch folder and `.ino` name must match.
- At boot it prints `{"ready":true}`; the brain waits for that line after opening the port.

## Hardware

- ESP32 dev board, MG90S neck servo for pan (the head only turns left and right; head must be light), 1× SSD1306 0.96" 128×64 OLED (both eyes), Logitech webcam (USB to the laptop; answers are heard on the laptop's own mic), Piggyback's voice from the laptop's speakers (no external speaker), Facmogu bench supply at 5V for servos, barrel-jack screw adapter, 470 µF capacitor across the servo rail. PCA9685 is a spare.
- Webcam sits on top of the head, just above the OLED eyes, mounted on its side (`CAMERA_ROTATE=90` turns its picture upright), so looking at the eyes = looking at the camera. Take it out of its clip/stand to save weight: MG90S servos are small, so keep the camera close to the pan axis and leave slack in the USB cable so it doesn't drag the head. The head still holds still while recording, so servo noise doesn't reach the mic if you switch back to the webcam's mic (`MIC_DEVICE=logitech`). With the laptop mic, keep the laptop within about an arm's length of the user; its speakers are right by its mic, but the brain never listens while Piggyback is speaking.
- Power rules: servos run from the bench supply, never from the ESP32's 5V/VIN pin; the ESP32 is powered by the laptop's USB; all grounds shared (bench supply ground ↔ ESP32 GND). The OLED runs from the ESP32's 3.3V pin.

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
| B2 | Servos move | Lorraine, Hana | In Serial Monitor, send `{"expr":"alert"}` and each of the other 6 expressions, then `{"look":[1,0]}`, `{"look":[-1,0]}`, `{"hold":1}`, `{"hold":0}` | Head turns smoothly for each, ends facing the same way, stays within `PAN_MIN`/`PAN_MAX`, holds still while held, ESP32 doesn't brown out or reset |
| B3 | Both eyes draw | Lorraine, Hana | Same commands | The OLED shows the right eyes for each expression |
| B4 | Head calibrated | Lorraine, Hana | Same commands with the head shell mounted | `HOME` faces straight forward (set `TRIM` if not); poses read clearly from where the user sits; step lists, `PAN_MIN`/`PAN_MAX`, and pins committed |
| B5 | Pupils and stare | Lorraine, Hana | Send `{"gaze":[1,0]}`, `{"gaze":[-1,0.5]}`, then `{"stare":0}` … `{"stare":1}` | Pupils move to each side; stare lids close smoothly; an `expr` command returns to normal eyes |
| C1 | Bot reads checkout | Alisha | Open checkout on the fake store (and one real store) with the brain off | Bot prints the right item, price, and category, and reconnects when the brain starts |
| C2 | Bot ↔ brain | Alisha + Annie | Fake store → brain on the laptop (ESP32 unplugged) | Reaching checkout starts the questions; buying sends `completed`, leaving sends `abandoned`; both land in `decisions.jsonl`; buying after a `timer` triggers "You went for it anyway" |
| M4 | Full end-to-end | All | Fake store → brain → ESP32 over USB | Head and pupils follow the user, answers only count with eye contact, a release needs the 3 s stare, verdicts are spoken, 3 runs in a row |
| M5 | Failure drills | All | During a run: unplug the ESP32's USB; set a bad API key; deny or unplug the mic; cover or unplug the camera | Brain keeps going (prints commands / uses default line / accepts typed answers / skips eye contact) and reconnects when replugged |
| M6 | Latency check | Annie | Time from reaching checkout to first spoken word, and from end of an answer to the next line | Under ~3 s; if not, switch model, shorten prompt, or start `thinking` sooner |
| M7 | Demo ready | All | Full scripted demo: one release, one timer, one escalate, then a budget review | 3 clean rehearsals; backup video recorded |

Stretch milestones (only after M4): ElevenLabs voice.

## Next up

- Personality: warm and playful, not harsh (tuned Nov 7); lines under 18 words.
- Possible MLH prizes: Best Use of Gemini API; Best Use of ElevenLabs (swap `speak()` for an ElevenLabs voice).
- Before judging: rehearse, record a backup video.
