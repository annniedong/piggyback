/*
  Pig Interrogator - ESP32 eyes + head firmware (single OLED, two eyes, pan servo)
  ===============================================================================
  The head only turns left and right (one pan servo). Expressions are short turns
  around the direction the pig faces, so nods and droops are left to the eyes.

  Hardware:
    OLED (SSD1306 128x64 I2C, 0x3C)  -> Wire : SDA 21, SCL 22
      Both eyes are drawn side by side on this one screen.
    OLED VCC -> ESP32 3.3V, GND -> GND
    Pan servo (MG90S) signal -> GPIO 18; power from the 5 V bench supply, grounds shared

  Libraries: Adafruit SSD1306, Adafruit GFX, ESP32Servo
  Board: ESP32 Dev Module

  Connections (both work at the same time):
    USB serial at 115200 baud
    Wi-Fi (optional, off by default): only if secrets.h sets WIFI_SSID / WIFI_PASS.
    Then the ESP32 joins that network (must be 2.4 GHz) and listens for one TCP
    client on TCP_PORT. Connect to pig.local:3333, or to the IP address printed in
    the Serial Monitor. A new client replaces the old one. brain.py only uses USB.

  Commands (one per line, case-insensitive, same over USB and Wi-Fi):
    PING                 -> PONG
    GAZE <x> <y>         pupils, -100..100 (x+ = viewer's right, y+ = down)
    EYES <expr> [ms]     idle suspicious skeptical shocked happy sad angry sleepy thinking
                         optional ms = hold time before returning to idle (0 = forever)
    BLINK                one blink
    CENTER               pupils back to center, idle eyes
    IDLE <on|off>        idle pupil glances after a few quiet seconds
    MOVE <expr>          play a brain expression's head turns (idle alert suspicious ...), eyes unchanged
    PAN <deg>            face this servo angle (PAN_MIN..PAN_MAX); expressions turn around it
    STATUS               print current state
    HELP                 list commands
  Every reply starts with OK, ERR, PONG, or READY.

  Brain commands (piggyback/brain.py, one JSON object per line, same ports):
    {"expr": "suspicious"}  idle alert suspicious skeptical satisfied disappointed thinking
                            -> eyes (plus a short pupil glance for some) and head turns; eyes hold
                            until the next expr. Unknown names fall back to idle and reply {"err":"unknown expr"}.
    {"look": [pan, tilt], "s": 1.5}
                            -1..1, where the pig faces; tilt is ignored. Optional s = seconds the turn
                            takes (default 0.3). During a one-shot expression it waits until that ends.
    {"hold": 1} / {"hold": 0}
                            while the brain records an answer: stop any turns, face the user, stay still
    {"gaze": [x, y]}        -1..1 pupils (no reply)
    {"stare": p}            0..1 stare-down eyes, lids close as p grows, no blinking;
                            any expr ends it (no reply)
    {"ping": 1}             -> {"pong":1}
  Replies: {"ready":true} at boot, then {"ok":true} | {"err":"..."} | {"pong":1}.
  While the brain is sending (and for BRAIN_QUIET_MS after), idle glances are off
  and expressions don't time out.
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ESP32Servo.h>

// ======================= CONFIG =======================

// Wi-Fi is off unless secrets.h (next to this file, git-ignored so a hotspot password never
// reaches the public repo) defines WIFI_SSID and WIFI_PASS. Copy secrets.example.h to secrets.h.
// Use a phone hotspot on 2.4 GHz (iPhone: turn on "Maximize Compatibility").
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID  ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS  ""
#endif
#define MDNS_NAME  "pig"     // reachable as pig.local
#define TCP_PORT   3333

#define SDA_PIN   21
#define SCL_PIN   22
#define OLED_ADDR 0x3C
#define I2C_HZ    400000UL   // try 800000UL if eye animation feels choppy

// Eye layout on the 128x64 screen
#define EYE_R   17   // eye radius in pixels
#define EYE_CY  53   // vertical center of both eyes (41 was the original; +12 px is about 2 mm lower)
#define EYE_LX  32   // horizontal center of the left eye (viewer's left)
#define EYE_RX  96   // horizontal center of the right eye
#define PUPIL_SCALE 1.25f  // pupil size multiplier for every expression (1.0 = original)
#define COL_BG    SSD1306_BLACK   // screen background and lids
#define COL_EYE   SSD1306_WHITE   // eyeballs and the glint in the pupils
#define COL_PUPIL SSD1306_BLACK   // pupils
#define COL_BROW  SSD1306_WHITE   // eyebrows

#define IDLE_AFTER_MS 4000   // idle glances start this long after the last command
#define EXPR_HOLD_MS  6000   // expressions return to "idle" after this long (0 = hold until changed)

// Brain (JSON) control
#define BRAIN_QUIET_MS 30000  // idle behavior comes back after this long with no brain commands
#define GLANCE_MS      1200   // expression pupil glances last this long

// Head: one pan servo (left and right only). Calibrate the limits once the head is mounted.
#define PAN_PIN    18
#define PAN_MIN    45     // servo degrees; narrow these if the head hits the frame
#define PAN_MAX    135
#define PAN_HOME   90     // straight ahead; "look" [0, 0]
#define TILT_PIN   -1     // set to 19 to hold a mounted tilt servo still at TILT_HOME; -1 = not driven
#define TILT_HOME  90
#define MAX_DEG_PER_SEC 100.0f  // servo top speed with the head on; faster turns just take longer

#define SCREEN_W 128
#define SCREEN_H 64

// ======================= TYPES =======================

// Expression values are in "design units" for a 22 px eye and are scaled
// automatically to EYE_R, so the table works for any eye size.
// lid:      upper lid closure 0..1
// tilt:     + inner corner lower (angry/suspicious), - outer corner lower (sad)
// low:      lower lid 0..1 (happy squint)
// pupil:    pupil radius
// browY:    brow height (smaller = raised; 18 = level with the top of the eye)
// browTilt: + inner end lower (angry), - inner end higher (worried)
struct EyeP { float lid, tilt, low, pupil, browY, browTilt; };
struct Expr { const char* name; EyeP l, r; };

// Head turns: each step turns to dPan degrees from the facing direction over ms milliseconds
// (a step to the same offset as the one before is a hold). Pan offsets are mirrored when the
// pig faces right of center, so turns go toward the middle instead of into a limit.
struct Step { int8_t dPan; uint16_t ms; };
struct Move { const Step* steps; uint8_t n; bool loops; };
#define MOVE(a, loops) {a, sizeof(a) / sizeof(a[0]), loops}

// ======================= GLOBALS =======================

const Expr EXPRS[] = {
  //               lid   tilt  low   pup brY  brT       lid   tilt  low   pup brY  brT
  {"idle",       {0.05, 0.0,  0.0,   9,  4, -0.1},   {0.05, 0.0,  0.0,   9,  4, -0.1}},
  {"suspicious", {0.45, 0.5,  0.2,   8, 12,  0.6},   {0.45, 0.5,  0.2,   8, 12,  0.6}},
  {"skeptical",  {0.50, 0.3,  0.1,   8, 13,  0.6},   {0.05, 0.0,  0.0,   9,  2, -0.4}},
  {"shocked",    {0.00, 0.0,  0.0,   5,  1, -0.2},   {0.00, 0.0,  0.0,   5,  1, -0.2}},
  {"happy",      {0.05, 0.0,  0.45, 10,  5, -0.2},   {0.05, 0.0,  0.45, 10,  5, -0.2}},
  {"sad",        {0.35,-0.6,  0.0,  10,  8, -0.7},   {0.35,-0.6,  0.0,  10,  8, -0.7}},
  {"angry",      {0.40, 0.8,  0.1,   8, 13,  0.9},   {0.40, 0.8,  0.1,   8, 13,  0.9}},
  {"sleepy",     {0.65, 0.0,  0.1,   9, 10,  0.0},   {0.65, 0.0,  0.1,   9, 10,  0.0}},
  {"thinking",   {0.15, 0.0,  0.0,   8,  6, -0.3},   {0.15, 0.0,  0.0,   8,  2, -0.5}},
};
const int NUM_EXPRS = sizeof(EXPRS) / sizeof(EXPRS[0]);

// Stare-down eyes for {"stare": p}; the upper lid is set from p each frame.
const EyeP STARE_EYE = {0.10, 0.5, 0.15, 7, 12, 0.5};

// Every one-shot move ends facing the same way it started. Keep EXPR_SECONDS in brain.py in step with
// these lengths (it pauses face tracking while a move plays).
const Step IDLE_STEPS[]  = {{0, 400}};                                            // face forward, hold still
const Step ALERT_STEPS[] = {{-8, 200}, {0, 250}};                                 // double take
const Step SUSP_STEPS[]  = {{8, 800}, {8, 1200}, {0, 500}};                       // side-eye, then back
const Step SKEPT_STEPS[] = {{25, 700}, {25, 500}, {0, 400}, {0, 400}};            // look away, glance back
const Step SATIS_STEPS[] = {{6, 150}, {-6, 200}, {6, 200}, {-6, 200}, {0, 150}};  // happy wiggle
const Step DISAP_STEPS[] = {{-12, 700}, {12, 900}, {-8, 1000}, {5, 1000}, {0, 800}};  // slow "no", slowing down
const Step THINK_STEPS[] = {{15, 1000}, {10, 1500}, {18, 1500}};                  // glance aside, sway (loops)

// The brain's expression names -> eyes (from EXPRS), a pupil glance, and head turns.
struct BrainExpr {
  const char* name;
  const char* eyes;
  float glanceX, glanceY; // pupil glance held for GLANCE_MS (0,0 = none)
  Move move;
};
const BrainExpr BRAIN_EXPRS[] = {
  {"idle",         "idle",        0,    0,   MOVE(IDLE_STEPS, false)},
  {"alert",        "shocked",     0,    0,   MOVE(ALERT_STEPS, false)},
  {"suspicious",   "suspicious",  0,    0,   MOVE(SUSP_STEPS, false)},
  {"skeptical",    "skeptical",   0,    0,   MOVE(SKEPT_STEPS, false)},
  {"satisfied",    "happy",       0,    0,   MOVE(SATIS_STEPS, false)},
  {"disappointed", "sad",         0,    0.3, MOVE(DISAP_STEPS, false)},
  {"thinking",     "thinking",   -0.6, -0.7, MOVE(THINK_STEPS, true)},
};
const int NUM_BRAIN_EXPRS = sizeof(BRAIN_EXPRS) / sizeof(BRAIN_EXPRS[0]);

Adafruit_SSD1306 oled(SCREEN_W, SCREEN_H, &Wire, -1, I2C_HZ, I2C_HZ);
bool oledOk = false;

// Eye state
int      exprIdx = 0;
uint32_t exprSetMs = 0, exprHoldMs = EXPR_HOLD_MS;
EyeP     curL = EXPRS[0].l, curR = EXPRS[0].r;
float    gazeX = 0, gazeY = 0, gazeTX = 0, gazeTY = 0;
bool     forceRedraw = true;
float    lastBlinkDrawn = 0;

// Blink state
bool     blinking = false;
uint32_t blinkStart = 0, nextBlinkMs = 0;
int      pendingBlinks = 0;

// Idle state
bool     idleOn = true;
uint32_t lastCmdMs = 0, nextGlanceMs = 0;

// Brain control state
uint32_t lastBrainMs = 0;        // last JSON command (0 = never)
bool     staring = false;
float    stareP = 0;
uint32_t glanceUntilMs = 0;      // incoming gaze is ignored until here

// Head state
Servo    panServo, tiltServo;
float    panCur = PAN_HOME, panFrom = PAN_HOME, panTo = PAN_HOME;
uint32_t panStartMs = 0, panMs = 0;
int      panWritten = -1;
int      facePan = PAN_HOME;         // where the pig faces: the last "look" (the user, or its resting spot)
const Move* curMove = NULL;          // expression turns being played (NULL = none)
uint8_t  moveI = 0;
int8_t   moveDir = 1;                // -1 mirrors the turns toward the middle
bool     lookPending = false;        // a "look" during a one-shot move or a hold waits until it ends
int      pendingPan = PAN_HOME;
uint32_t pendingMs = 0;
bool     holding = false;            // the brain is recording an answer: no head motion

// Command input buffers (one per source) and where replies go
#define LINE_MAX 96
char    serialBuf[LINE_MAX];
uint8_t serialLen = 0;
char    netBuf[LINE_MAX];
uint8_t netLen = 0;
Print*  out = &Serial;

// Wi-Fi
WiFiServer server(TCP_PORT);
WiFiClient client;
bool wifiEnabled = false, wifiStarted = false, wifiWasConnected = false;

// ======================= HELPERS =======================

static inline float clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

void fillQuad(int x0, int y0, int x1, int y1, int x2, int y2, int x3, int y3, uint16_t color) {
  oled.fillTriangle(x0, y0, x1, y1, x2, y2, color);
  oled.fillTriangle(x0, y0, x2, y2, x3, y3, color);
}

// ======================= EYES =======================

void startBlink(uint32_t at) {
  blinking = true;
  blinkStart = at;
}

float blinkAmount(uint32_t now) {
  if (!blinking) return 0;
  int32_t t = (int32_t)(now - blinkStart);
  if (t < 0)   return 0;                         // scheduled in the future
  if (t < 70)  return t / 70.0f;                 // closing
  if (t < 120) return 1.0f;                      // closed
  if (t < 220) return 1.0f - (t - 120) / 100.0f; // opening
  blinking = false;
  if (pendingBlinks > 0) { pendingBlinks--; startBlink(now + 60); }
  return 0;
}

// Draws one eye centered at cx. innerIsRight is true for the LEFT eye:
// its inner corner (toward the snout) is on its right side.
void drawEye(int cx, const EyeP &p, bool innerIsRight, float blink) {
  const int cy = EYE_CY, R = EYE_R;
  const float S = EYE_R / 22.0f;   // scale from design units to this eye size

  // White eyeball on a black screen. Lids are drawn in the background color,
  // so they hide whatever part of the eye they cover.
  oled.fillCircle(cx, cy, R, COL_EYE);

  // Pupil and glint
  float pupil = p.pupil * S * PUPIL_SCALE;
  int pr = (int)(pupil + 0.5f);
  if (pr < 2) pr = 2;
  float reach = R - pupil - 2;
  int px = cx + (int)(gazeX * reach);
  int py = cy + (int)(gazeY * reach * 0.7f);
  if (py + pr > SCREEN_H - 1) py = SCREEN_H - 1 - pr;   // keep the pupil on screen
  oled.fillCircle(px, py, pr, COL_PUPIL);
  oled.fillCircle(px - pr / 3, py - pr / 3, pr >= 6 ? 2 : 1, COL_EYE);

  int xIn  = innerIsRight ? cx + R + 2 : cx - R - 2;
  int xOut = innerIsRight ? cx - R - 2 : cx + R + 2;

  // Upper lid (background-colored shape coming down from the top, tilted per expression)
  float closure = fmaxf(p.lid, blink);
  float tiltPx = p.tilt * 10.0f * S * (1.0f - blink);
  float base = (cy - R - 1) + closure * (2 * R + 2);
  fillQuad(xOut, 0, xIn, 0, xIn, (int)(base + tiltPx), xOut, (int)(base - tiltPx), COL_BG);

  // Lower lid (curved, for a happy squint)
  if (p.low > 0.02f) {
    int yl = (cy + R + 1) - (int)(p.low * (2 * R + 2));
    int lr = R + 6;
    oled.fillCircle(cx, yl + lr, lr, COL_BG);
  }

  // Eyebrow (thick white slanted bar), dips slightly during a blink
  float by = (cy - R) + (p.browY - 18.0f) * S + blink * 2.0f;
  float bt = p.browTilt * 5.0f * S;
  int hw = (int)(20 * S);
  int th = 3;
  int bxIn  = innerIsRight ? cx + hw : cx - hw;
  int bxOut = innerIsRight ? cx - hw : cx + hw;
  int yIn = (int)(by + bt), yOut = (int)(by - bt);
  fillQuad(bxOut, yOut, bxIn, yIn, bxIn, yIn + th, bxOut, yOut + th, COL_BROW);
}

void drawFace(float blink) {
  oled.fillScreen(COL_BG);
  drawEye(EYE_LX, curL, true,  blink);
  drawEye(EYE_RX, curR, false, blink);
  oled.display();
}

float smoothField(float &cur, float tgt, float k) {
  float next = cur + (tgt - cur) * k;
  float delta = fabsf(next - cur);
  cur = next;
  return delta;
}

float smoothEye(EyeP &cur, const EyeP &tgt, float k) {
  float change = 0;
  change += smoothField(cur.lid,      tgt.lid,      k);
  change += smoothField(cur.tilt,     tgt.tilt,     k);
  change += smoothField(cur.low,      tgt.low,      k);
  change += smoothField(cur.pupil,    tgt.pupil,    k) * 0.1f;
  change += smoothField(cur.browY,    tgt.browY,    k) * 0.1f;
  change += smoothField(cur.browTilt, tgt.browTilt, k);
  return change;
}

void updateEyes(float dt, uint32_t now) {
  // Return to idle once the expression's hold time runs out
  if (exprIdx != 0 && exprHoldMs > 0 && now - exprSetMs >= exprHoldMs) exprIdx = 0;

  // Ease toward the target expression (or the stare-down, whose lids close with stareP)
  float k = 1.0f - expf(-dt * 12.0f);
  float change;
  if (staring) {
    EyeP s = STARE_EYE;
    s.lid = STARE_EYE.lid + stareP * 0.75f;
    change = smoothEye(curL, s, k) + smoothEye(curR, s, k);
  } else {
    change = smoothEye(curL, EXPRS[exprIdx].l, k) + smoothEye(curR, EXPRS[exprIdx].r, k);
  }

  // Ease pupils toward the target gaze
  float kg = 1.0f - expf(-dt * 15.0f);
  change += smoothField(gazeX, gazeTX, kg) * 10.0f;
  change += smoothField(gazeY, gazeTY, kg) * 10.0f;

  // Automatic blinking, occasionally a double blink (never during a stare-down)
  if (staring) {
    nextBlinkMs = now + 1500;
  } else if ((int32_t)(now - nextBlinkMs) >= 0) {
    startBlink(now);
    if (random(100) < 20) pendingBlinks = 1;
    nextBlinkMs = now + random(2000, 6000);
  }
  float b = blinkAmount(now);

  // Only push frames when something visibly changed
  if (forceRedraw || change > 0.02f || b != lastBlinkDrawn) {
    if (oledOk) drawFace(b);
    lastBlinkDrawn = b;
    forceRedraw = false;
  }
}

// ======================= IDLE =======================

bool brainActive(uint32_t now) {
  return lastBrainMs != 0 && now - lastBrainMs < BRAIN_QUIET_MS;
}

// Ends an expression's pupil glance once GLANCE_MS has passed
void updateGlance(uint32_t now) {
  if (glanceUntilMs && (int32_t)(now - glanceUntilMs) >= 0) {
    glanceUntilMs = 0;
    gazeTX = 0;
    gazeTY = 0;
  }
}

// Idle: the pupils glance around on their own when nobody is driving the eyes
void updateIdle(uint32_t now) {
  if (!idleOn || brainActive(now)) return;
  if (now - lastCmdMs < IDLE_AFTER_MS) return;

  if ((int32_t)(now - nextGlanceMs) >= 0) {
    if (random(100) < 30) {
      gazeTX = 0;
      gazeTY = 0;
    } else {
      gazeTX = random(-7, 8) / 10.0f;
      gazeTY = random(-3, 4) / 10.0f;
    }
    nextGlanceMs = now + random(1500, 4500);
  }
}

// ======================= HEAD =======================

// Starts a smoothstep turn from wherever the head is now, so a new command interrupts cleanly.
void turnTo(int pan, uint32_t ms) {
  panFrom = panCur;
  panTo = clampf(pan, PAN_MIN, PAN_MAX);
  // Smoothstep peaks at 1.5x the average speed, so stretch the turn until that peak fits the servo.
  uint32_t minMs = (uint32_t)(1.5f * fabsf(panTo - panFrom) / MAX_DEG_PER_SEC * 1000);
  panMs = ms > minMs ? ms : minMs;
  panStartMs = millis();
}

uint32_t turnMsLeft(uint32_t now) {
  int32_t gone = (int32_t)(now - panStartMs);
  if (gone < 0) gone = 0;
  return (uint32_t)gone < panMs ? panMs - gone : 0;
}

void startStep(uint32_t minMs) {
  const Step &s = curMove->steps[moveI];
  turnTo(facePan + moveDir * s.dPan, s.ms > minMs ? s.ms : minMs);
}

void followPendingLook() {
  lookPending = false;
  facePan = pendingPan;
  turnTo(facePan, pendingMs);
}

void playMove(const Move &m, uint32_t now) {
  if (lookPending) { lookPending = false; facePan = pendingPan; }   // turn around the newest position
  curMove = &m;
  moveI = 0;
  moveDir = facePan > PAN_HOME ? -1 : 1;
  startStep(turnMsLeft(now));   // never speed up a slow turn that's still going
}

void applyLook(float x, uint32_t ms) {
  x = clampf(x, -1, 1);
  int pan = PAN_HOME + (int)(x * (x >= 0 ? PAN_MAX - PAN_HOME : PAN_HOME - PAN_MIN));
  if (holding || (curMove && !curMove->loops)) {   // let the move (or the answer) finish, then follow
    lookPending = true;
    pendingPan = pan;
    pendingMs = ms;
    return;
  }
  curMove = NULL;   // a look ends the thinking sway
  facePan = pan;
  turnTo(pan, ms);
}

void setHold(bool on) {
  holding = on;
  if (on) {
    curMove = NULL;           // stop any move and face the user, then stay still
    turnTo(facePan, 300);
  } else if (lookPending) {
    followPendingLook();
  }
}

void updateHead(uint32_t now) {
  int32_t gone = (int32_t)(now - panStartMs);
  float t = panMs ? (float)(gone > 0 ? gone : 0) / panMs : 1.0f;
  if (t > 1) t = 1;
  float s = t * t * (3 - 2 * t);
  panCur = panFrom + (panTo - panFrom) * s;
  int deg = (int)lroundf(panCur);
  if (deg != panWritten) { panServo.write(deg); panWritten = deg; }

  if (t < 1 || !curMove) return;
  if (++moveI < curMove->n) { startStep(0); return; }
  if (curMove->loops) { moveI = 0; startStep(0); return; }
  curMove = NULL;
  if (lookPending && !holding) followPendingLook();   // tracking wanted to move during the move: catch up
}

// ======================= COMMANDS =======================

int findBrainExpr(const char* name) {
  for (int i = 0; i < NUM_BRAIN_EXPRS; i++) {
    if (!strcasecmp(name, BRAIN_EXPRS[i].name)) return i;
  }
  return -1;
}

int findExpr(const char* name) {
  for (int i = 0; i < NUM_EXPRS; i++) {
    if (!strcasecmp(name, EXPRS[i].name)) return i;
  }
  return -1;
}

void printHelp() {
  out->println("OK commands: PING | GAZE <x> <y> | EYES <expr> [ms] | BLINK | CENTER |");
  out->println("OK   IDLE <on|off> | MOVE <expr> | PAN <deg> | STATUS | HELP");
  out->print("OK expressions:");
  for (int i = 0; i < NUM_EXPRS; i++) { out->print(' '); out->print(EXPRS[i].name); }
  out->println();
}

void handleCommand(char* line) {
  char* cmd = strtok(line, " \t");
  if (!cmd) return;
  char* a1 = strtok(NULL, " \t");
  char* a2 = strtok(NULL, " \t");
  uint32_t now = millis();
  lastCmdMs = now;

  if (!strcasecmp(cmd, "PING")) {
    out->println("PONG");

  } else if (!strcasecmp(cmd, "GAZE")) {
    if (!a1 || !a2) { out->println("ERR usage: GAZE <x> <y> (-100..100)"); return; }
    gazeTX = clampf(atof(a1) / 100.0f, -1, 1);
    gazeTY = clampf(atof(a2) / 100.0f, -1, 1);
    out->println("OK GAZE");

  } else if (!strcasecmp(cmd, "EYES")) {
    int idx = a1 ? findExpr(a1) : -1;
    if (idx < 0) { out->println("ERR unknown expression (try HELP)"); return; }
    exprIdx = idx;
    exprSetMs = now;
    staring = false;
    exprHoldMs = a2 ? (uint32_t)atol(a2) : EXPR_HOLD_MS;
    out->printf("OK EYES %s hold=%lums\n", EXPRS[idx].name, (unsigned long)exprHoldMs);

  } else if (!strcasecmp(cmd, "BLINK")) {
    startBlink(now);
    out->println("OK BLINK");

  } else if (!strcasecmp(cmd, "CENTER")) {
    exprIdx = 0;
    staring = false;
    glanceUntilMs = 0;
    gazeTX = 0;
    gazeTY = 0;
    out->println("OK CENTER");

  } else if (!strcasecmp(cmd, "IDLE")) {
    if (!a1) { out->println("ERR usage: IDLE <on|off>"); return; }
    idleOn = !strcasecmp(a1, "on");
    out->printf("OK IDLE %s\n", idleOn ? "on" : "off");

  } else if (!strcasecmp(cmd, "MOVE")) {
    int b = a1 ? findBrainExpr(a1) : -1;
    if (b < 0) { out->println("ERR usage: MOVE <idle|alert|suspicious|skeptical|satisfied|disappointed|thinking>"); return; }
    holding = false;
    playMove(BRAIN_EXPRS[b].move, now);
    out->printf("OK MOVE %s\n", BRAIN_EXPRS[b].name);

  } else if (!strcasecmp(cmd, "PAN")) {
    if (!a1) { out->printf("ERR usage: PAN <deg> (%d..%d)\n", PAN_MIN, PAN_MAX); return; }
    holding = false;
    lookPending = false;
    curMove = NULL;
    facePan = (int)clampf(atof(a1), PAN_MIN, PAN_MAX);
    turnTo(facePan, 500);
    out->printf("OK PAN %d\n", facePan);

  } else if (!strcasecmp(cmd, "STATUS")) {
    out->printf("OK STATUS eyes=%s gaze=%.2f,%.2f staring=%d idle=%d oled=%d\n",
                EXPRS[exprIdx].name, gazeTX, gazeTY, staring, idleOn, oledOk);
    out->printf("OK HEAD pan=%d facing=%d moving=%d holding=%d\n",
                panWritten, facePan, curMove != NULL, holding);
    out->printf("OK WIFI %s ip=%s client=%d\n",
                WiFi.status() == WL_CONNECTED ? "connected" : (wifiEnabled ? "connecting" : "off"),
                WiFi.localIP().toString().c_str(), (int)(client && client.connected()));

  } else if (!strcasecmp(cmd, "HELP")) {
    printHelp();

  } else {
    out->println("ERR unknown command (try HELP)");
  }
}

// ======================= BRAIN (JSON) COMMANDS =======================
// brain.py sends one small object per line with a single key, e.g. {"gaze": [0.2, -0.1]}.
// This tiny parser handles exactly those shapes, so no JSON library is needed.

static const char* skipWs(const char* p) {
  while (*p == ' ' || *p == '\t') p++;
  return p;
}

// Copies a quoted string at p into dst; returns the pointer after the closing quote, or NULL.
static const char* readStr(const char* p, char* dst, int cap) {
  p = skipWs(p);
  if (*p != '"') return NULL;
  p++;
  int n = 0;
  while (*p && *p != '"') { if (n < cap - 1) dst[n++] = *p; p++; }
  dst[n] = 0;
  return *p == '"' ? p + 1 : NULL;
}

// Reads a number at p; returns the pointer after it, or NULL.
static const char* readNum(const char* p, float &v) {
  char* end;
  v = strtof(skipWs(p), &end);
  return end == skipWs(p) ? NULL : end;
}

// Reads [a, b] at p; returns true on success.
static bool readPair(const char* p, float &a, float &b) {
  p = skipWs(p);
  if (*p != '[') return false;
  p = readNum(p + 1, a);
  if (!p) return false;
  p = skipWs(p);
  if (*p != ',') return false;
  return readNum(p + 1, b) != NULL;
}

void brainExpr(const char* name, uint32_t now) {
  int b = findBrainExpr(name);
  bool known = b >= 0;
  if (!known) b = 0;   // unknown names fall back to idle
  const BrainExpr &e = BRAIN_EXPRS[b];
  if (!holding) playMove(e.move, now);   // while an answer is recorded, only the eyes change

  int idx = findExpr(e.eyes);
  exprIdx = idx < 0 ? 0 : idx;
  exprSetMs = now;
  exprHoldMs = 0;          // the brain decides when to change it
  staring = false;

  if (e.glanceX != 0 || e.glanceY != 0) {
    gazeTX = e.glanceX;
    gazeTY = e.glanceY;
    glanceUntilMs = now + GLANCE_MS;
  } else {
    glanceUntilMs = 0;
  }
  if (b == 1) { startBlink(now); pendingBlinks = 1; }   // alert: quick double blink

  out->println(known ? "{\"ok\":true}" : "{\"err\":\"unknown expr\"}");
}

void handleJson(const char* line) {
  uint32_t now = millis();
  lastCmdMs = now;
  lastBrainMs = now;

  char key[12];
  const char* p = readStr(skipWs(line) + 1, key, sizeof(key));
  if (p) p = skipWs(p);
  if (!p || *p != ':') { out->println("{\"err\":\"bad json\"}"); return; }
  p++;

  float a, b;
  if (!strcmp(key, "expr")) {
    char name[20];
    if (!readStr(p, name, sizeof(name))) { out->println("{\"err\":\"bad expr\"}"); return; }
    brainExpr(name, now);

  } else if (!strcmp(key, "look")) {          // [pan, tilt], optional "s"; tilt is ignored (pan-only head)
    if (!readPair(p, a, b)) { out->println("{\"err\":\"bad look\"}"); return; }
    float secs = 0.3f;
    const char* sp = strstr(p, "\"s\"");
    if (sp && (sp = strchr(sp, ':'))) readNum(sp + 1, secs);
    applyLook(a, (uint32_t)(clampf(secs, 0, 10) * 1000));
    out->println("{\"ok\":true}");

  } else if (!strcmp(key, "hold")) {          // 1/true while the brain records an answer
    p = skipWs(p);
    setHold(!strncmp(p, "true", 4) || (readNum(p, a) && a != 0));
    out->println("{\"ok\":true}");

  } else if (!strcmp(key, "gaze")) {          // sent ~10x/s, no reply
    if (!readPair(p, a, b)) return;
    if (glanceUntilMs && (int32_t)(now - glanceUntilMs) < 0) return;
    glanceUntilMs = 0;
    gazeTX = clampf(a, -1, 1);
    gazeTY = clampf(b, -1, 1);

  } else if (!strcmp(key, "stare")) {         // sent ~10x/s, no reply
    if (!readNum(p, a)) return;
    staring = true;
    stareP = clampf(a, 0, 1);

  } else if (!strcmp(key, "ping")) {
    out->println("{\"pong\":1}");

  } else {
    out->println("{\"err\":\"unknown command\"}");
  }
}

// Collects characters into a line and runs it, replying to src
void feedChar(char c, char* buf, uint8_t &len, Print &src) {
  if (c == '\r') return;
  if (c == '\n') {
    buf[len] = 0;
    if (len) {
      out = &src;
      char* p = buf;
      while (*p == ' ' || *p == '\t') p++;
      if (*p == '{') handleJson(p); else handleCommand(buf);
      out = &Serial;
    }
    len = 0;
  } else if (len < LINE_MAX - 1) {
    buf[len++] = c;
  }
}

void pollSerial() {
  while (Serial.available()) feedChar(Serial.read(), serialBuf, serialLen, Serial);
}

void pollNetwork() {
  if (!wifiEnabled) return;

  bool connected = (WiFi.status() == WL_CONNECTED);
  if (connected && !wifiWasConnected) {
    if (!wifiStarted) {
      server.begin();
      server.setNoDelay(true);
      wifiStarted = true;
    }
    MDNS.end();
    if (MDNS.begin(MDNS_NAME)) MDNS.addService("pig", "tcp", TCP_PORT);
    Serial.printf("OK WIFI connected ip=%s name=%s.local port=%d\n",
                  WiFi.localIP().toString().c_str(), MDNS_NAME, TCP_PORT);
  } else if (!connected && wifiWasConnected) {
    Serial.println("ERR WIFI disconnected, retrying");
  }
  wifiWasConnected = connected;
  if (!connected) return;

  // Accept a new client (it replaces any existing one)
  WiFiClient incoming = server.available();
  if (incoming) {
    if (client && client.connected()) client.stop();
    client = incoming;
    client.setNoDelay(true);
    netLen = 0;
    client.println("{\"ready\":true}");
    client.printf("READY oled=%d\n", oledOk);
    Serial.println("OK WIFI client connected");
  }

  if (client && client.connected()) {
    while (client.available()) feedChar(client.read(), netBuf, netLen, client);
  }
}

// ======================= SETUP / LOOP =======================

void setup() {
  Serial.begin(115200);
  delay(200);
  randomSeed(esp_random());

  Wire.begin(SDA_PIN, SCL_PIN, I2C_HZ);

  // periphBegin = false: the bus is already started on the right pins
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR, true, false);

  panServo.setPeriodHertz(50);
  panServo.attach(PAN_PIN, 500, 2400);
  panServo.write(PAN_HOME);
  panWritten = PAN_HOME;
  if (TILT_PIN >= 0) {
    tiltServo.setPeriodHertz(50);
    tiltServo.attach(TILT_PIN, 500, 2400);
    tiltServo.write(TILT_HOME);
  }

  uint32_t now = millis();
  lastCmdMs    = now;
  nextBlinkMs  = now + 1500;
  nextGlanceMs = now + IDLE_AFTER_MS;

  wifiEnabled = strlen(WIFI_SSID) > 0;
  if (wifiEnabled) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);          // lower latency for commands
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  Serial.println("{\"ready\":true}");   // brain.py waits for this line after opening the port
  Serial.printf("READY oled=%d wifi=%s\n", oledOk,
                wifiEnabled ? "connecting to " WIFI_SSID : "off");
  if (!oledOk) Serial.println("ERR OLED not found at 0x3C (SDA 21, SCL 22)");
}

void loop() {
  static uint32_t lastTickMs = 0, lastFrameMs = 0;
  uint32_t now = millis();

  pollSerial();
  pollNetwork();

  // Glances, idle, and head at ~50 Hz (the servo's own update rate)
  if (now - lastTickMs >= 20) {
    lastTickMs = now;
    updateGlance(now);
    updateIdle(now);
    updateHead(millis());   // millis(), not now: a command read above may have just started a turn
  }

  // Eyes at up to ~30 fps, only redrawn when something changes
  if (now - lastFrameMs >= 33) {
    float dt = (now - lastFrameMs) / 1000.0f;
    if (dt > 0.1f) dt = 0.1f;
    lastFrameMs = now;
    updateEyes(dt, now);
  }
}
