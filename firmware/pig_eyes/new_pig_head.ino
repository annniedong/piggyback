/*
  Pig Interrogator - ESP32 head firmware (single OLED, two eyes)
  ==============================================================
  Hardware:
    OLED (SSD1306 128x64 I2C, 0x3C)  -> Wire : SDA 21, SCL 22
      Both eyes are drawn side by side on this one screen.
    PCA9685 servo driver (0x40)      -> Wire : SDA 21, SCL 22
      ch0 neck pan (MG90S)    ch1 neck tilt (MG90S)
      ch2 left ear (SG90)     ch3 right ear (SG90)
    (Left/right are as seen from the FRONT of the pig.)

  Power:
    OLED VCC and PCA9685 VCC -> ESP32 3.3V
    Servo power: bench supply 5V -> PCA9685 V+ screw terminal ONLY
    470uF capacitor across V+ and GND (stripe to GND)
    All grounds connected together

  Libraries: Adafruit SSD1306, Adafruit GFX, Adafruit PWM Servo Driver
  Board: ESP32 Dev Module

  Connections (both work at the same time):
    USB serial at 115200 baud
    Wi-Fi: the ESP32 joins WIFI_SSID (must be 2.4 GHz) and listens for one
    TCP client on TCP_PORT. Connect to pig.local:3333, or to the IP address
    printed in the Serial Monitor. A new client replaces the old one.
    Leave WIFI_SSID empty ("") to turn Wi-Fi off.

  Commands (one per line, case-insensitive, same over USB and Wi-Fi):
    PING                 -> PONG
    LOOK <pan> <tilt>    neck angles in degrees (clamped to safe limits)
    GAZE <x> <y>         pupils, -100..100 (x+ = viewer's right, y+ = down)
    EYES <expr> [ms]     idle suspicious skeptical shocked happy sad angry sleepy
                         optional ms = hold time before returning to idle (0 = forever)
    EARS <pose>          up neutral down wiggle flick
    BLINK                one blink
    CENTER               everything back to home
    IDLE <on|off>        idle glances and ear flicks after a few quiet seconds
    SERVO <ch> <us>      raw pulse for calibration (bypasses limits!)
    RELAX                stop driving all servos (they go limp)
    STATUS               print current state
    HELP                 list commands
  Every reply starts with OK, ERR, PONG, or READY.

  Brain commands (piggyback/brain.py, one JSON object per line, same ports):
    {"expr": "suspicious"}  idle alert suspicious skeptical satisfied disappointed thinking
                            -> eyes + ears + a short head nudge; holds until the next expr.
                            Unknown names fall back to idle and reply {"err":"unknown expr"}.
    {"look": [pan, tilt]}   -1..1 each, [0,0] = home; mapped onto the servo limits below
    {"gaze": [x, y]}        -1..1 pupils (no reply)
    {"stare": p}            0..1 stare-down eyes, lids close as p grows, no blinking;
                            any expr ends it (no reply)
    {"ping": 1}             -> {"pong":1}
  Replies: {"ready":true} at boot, then {"ok":true} | {"err":"..."} | {"pong":1}.
  While the brain is sending (and for BRAIN_QUIET_MS after), idle glances are off
  and expressions don't time out, so they don't fight the brain's head tracking.
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_PWMServoDriver.h>
#include <WiFi.h>
#include <ESPmDNS.h>

// ======================= CONFIG =======================

// Wi-Fi: use a phone hotspot on 2.4 GHz (iPhone: turn on "Maximize Compatibility").
// Don't push real passwords to a public repo.
#define WIFI_SSID  "your-hotspot-name"
#define WIFI_PASS  "your-hotspot-password"
#define MDNS_NAME  "pig"     // reachable as pig.local
#define TCP_PORT   3333

#define SDA_PIN   21
#define SCL_PIN   22
#define OLED_ADDR 0x3C
#define PCA_ADDR  0x40
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

// Nominal pulse range used to convert degrees to microseconds.
// Keep the safe limits in servos[] narrow until you calibrate with SERVO.
#define US_AT_0    500
#define US_AT_180  2400

#define IDLE_AFTER_MS 4000   // idle behavior starts this long after the last command
#define EXPR_HOLD_MS  6000   // expressions return to "idle" after this long (0 = hold until changed)

// Ear poses in logical degrees. The right ear is mirrored via its invert flag,
// so the same number moves both ears the same way visually.
#define EAR_UP       60
#define EAR_NEUTRAL  90
#define EAR_DOWN    125

// If idle glances make the pupils look the opposite way from the head, flip this.
#define GAZE_PAN_SIGN 1.0f

// Brain (JSON) control
#define BRAIN_QUIET_MS 30000  // idle behavior comes back after this long with no brain commands
#define POSE_MS        1200   // expression head nudges last this long (the brain pauses tracking ~1.3 s)
#define TILT_UP_SIGN   -1.0f  // flip to 1.0f if "alert" tilts the head down instead of up

#define SCREEN_W 128
#define SCREEN_H 64

// ======================= TYPES =======================

enum { S_PAN, S_TILT, S_EAR_L, S_EAR_R, NUM_SERVOS };

struct ServoCh {
  uint8_t ch;
  float minDeg, maxDeg, homeDeg;
  float speed;    // max degrees per second
  float ease;     // easing rate (higher = snappier)
  bool invert;    // true if the servo is mounted mirrored
  float cur, tgt;
};

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

enum EarMode { EAR_HOLD, EAR_WIGGLE, EAR_FLICK };

// ======================= GLOBALS =======================

// Conservative starting limits. Widen them after calibration.
ServoCh servos[NUM_SERVOS] = {
  // ch  min  max  home speed ease invert   cur tgt
  { 0,  60, 120,  90, 120,   7, false,   90, 90 },  // neck pan  (MG90S)
  { 1,  75, 105,  90,  90,   7, false,   90, 90 },  // neck tilt (MG90S)
  { 2,  50, 130,  90, 450,  35, false,   90, 90 },  // left ear  (SG90)
  { 3,  50, 130,  90, 450,  35, true,    90, 90 },  // right ear (SG90)
};

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

// The brain's expression names -> eyes (from EXPRS), ears, head nudge, and a pupil glance.
enum EarPose { EP_NEUTRAL, EP_UP, EP_DOWN, EP_WIGGLE, EP_FLICK };
struct BrainExpr {
  const char* name;
  const char* eyes;
  EarPose ears;
  float tiltUp;          // degrees of head nudge, + = up (see TILT_UP_SIGN)
  float glanceX, glanceY; // pupil glance held for POSE_MS (0,0 = none)
};
const BrainExpr BRAIN_EXPRS[] = {
  {"idle",         "idle",       EP_NEUTRAL, 0,  0,    0},
  {"alert",        "shocked",    EP_UP,      6,  0,    0},
  {"suspicious",   "suspicious", EP_FLICK,  -3,  0,    0},
  {"skeptical",    "skeptical",  EP_FLICK,   0,  0,    0},
  {"satisfied",    "happy",      EP_WIGGLE,  3,  0,    0},
  {"disappointed", "sad",        EP_DOWN,   -8,  0,    0.3},
  {"thinking",     "thinking",   EP_NEUTRAL, 4, -0.6, -0.7},
};
const int NUM_BRAIN_EXPRS = sizeof(BRAIN_EXPRS) / sizeof(BRAIN_EXPRS[0]);

Adafruit_SSD1306 oled(SCREEN_W, SCREEN_H, &Wire, -1, I2C_HZ, I2C_HZ);
Adafruit_PWMServoDriver pwm(PCA_ADDR, Wire);

bool oledOk = false, pcaOk = false;

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

// Ear state
EarMode  earMode = EAR_HOLD;
uint32_t earT0 = 0;
float    earBase = EAR_NEUTRAL;
int      flickEar = S_EAR_L;

// Servo modes
bool rawMode = false;   // SERVO command active: smoothing paused
bool relaxed = false;   // RELAX: servos not driven

// Idle state
bool     idleOn = true;
uint32_t lastCmdMs = 0, nextGlanceMs = 0, nextFlickMs = 0;

// Brain control state
uint32_t lastBrainMs = 0;        // last JSON command (0 = never)
bool     staring = false;
float    stareP = 0;
float    baseTilt = 90;          // tilt the brain (or LOOK) asked for, before any nudge
uint32_t poseUntilMs = 0;        // expression nudge ends here
uint32_t glanceUntilMs = 0;      // incoming gaze is ignored until here

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

// ======================= SERVOS =======================

void writeServo(int i) {
  if (!pcaOk) return;
  ServoCh &s = servos[i];
  float deg = s.invert ? 180.0f - s.cur : s.cur;
  uint16_t us = US_AT_0 + (uint16_t)((deg / 180.0f) * (US_AT_180 - US_AT_0));
  pwm.writeMicroseconds(s.ch, us);
}

void writeAllServos() {
  for (int i = 0; i < NUM_SERVOS; i++) writeServo(i);
}

void setTarget(int i, float deg) {
  servos[i].tgt = clampf(deg, servos[i].minDeg, servos[i].maxDeg);
}

// Leave raw or relaxed mode and resume normal smoothed control
void resumeServos() {
  if (rawMode || relaxed) {
    rawMode = false;
    relaxed = false;
    writeAllServos();
  }
}

void updateServos(float dt) {
  if (rawMode || relaxed) return;
  for (int i = 0; i < NUM_SERVOS; i++) {
    ServoCh &s = servos[i];
    float err = s.tgt - s.cur;
    if (fabsf(err) < 0.1f) {
      if (err != 0) { s.cur = s.tgt; writeServo(i); }
      continue;
    }
    float step = err * (1.0f - expf(-dt * s.ease));      // ease out
    float maxStep = s.speed * dt;                        // speed cap
    step = clampf(step, -maxStep, maxStep);
    if (fabsf(step) < 0.1f) step = (err > 0) ? fminf(0.1f, err) : fmaxf(-0.1f, err);
    s.cur += step;
    writeServo(i);
  }
}

// ======================= EARS =======================

void setEars(float deg) {
  setTarget(S_EAR_L, deg);
  setTarget(S_EAR_R, deg);
}

void updateEars(uint32_t now) {
  if (earMode == EAR_WIGGLE) {
    uint32_t step = (now - earT0) / 110;
    if (step >= 8) { earMode = EAR_HOLD; setEars(earBase); return; }
    float off = (step % 2) ? 25 : -25;
    setTarget(S_EAR_L, earBase + off);
    setTarget(S_EAR_R, earBase - off);
  } else if (earMode == EAR_FLICK) {
    if (now - earT0 < 180) setTarget(flickEar, earBase - 35);
    else { earMode = EAR_HOLD; setEars(earBase); }
  }
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

// Ends an expression's head nudge once POSE_MS has passed
void updatePose(uint32_t now) {
  if (poseUntilMs && (int32_t)(now - poseUntilMs) >= 0) {
    poseUntilMs = 0;
    setTarget(S_TILT, baseTilt);
  }
}

void updateIdle(uint32_t now) {
  if (!idleOn || rawMode || relaxed || brainActive(now)) return;
  if (now - lastCmdMs < IDLE_AFTER_MS) return;

  if ((int32_t)(now - nextGlanceMs) >= 0) {
    if (random(100) < 30) {
      // Look back to center
      setTarget(S_PAN,  servos[S_PAN].homeDeg);
      setTarget(S_TILT, servos[S_TILT].homeDeg);
      gazeTX = 0;
      gazeTY = 0;
    } else {
      // Small random glance; pupils lead in the same direction
      float dp = random(-18, 19);
      float dtl = random(-8, 9);
      setTarget(S_PAN,  servos[S_PAN].homeDeg + dp);
      setTarget(S_TILT, servos[S_TILT].homeDeg + dtl);
      gazeTX = clampf(GAZE_PAN_SIGN * dp / 18.0f * 0.7f, -1, 1);
      gazeTY = random(-3, 4) / 10.0f;
    }
    nextGlanceMs = now + random(1500, 4500);
  }

  if ((int32_t)(now - nextFlickMs) >= 0) {
    if (earMode == EAR_HOLD) {
      earMode = EAR_FLICK;
      earT0 = now;
      flickEar = random(2) ? S_EAR_L : S_EAR_R;
    }
    nextFlickMs = now + random(6000, 14000);
  }
}

// ======================= COMMANDS =======================

int findExpr(const char* name) {
  for (int i = 0; i < NUM_EXPRS; i++) {
    if (!strcasecmp(name, EXPRS[i].name)) return i;
  }
  return -1;
}

void printHelp() {
  out->println("OK commands: PING | LOOK <pan> <tilt> | GAZE <x> <y> | EYES <expr> [ms] |");
  out->println("OK   EARS <up|neutral|down|wiggle|flick> | BLINK | CENTER | IDLE <on|off> |");
  out->println("OK   SERVO <ch> <us> | RELAX | STATUS | HELP");
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

  } else if (!strcasecmp(cmd, "LOOK")) {
    if (!a1 || !a2) { out->println("ERR usage: LOOK <pan> <tilt>"); return; }
    resumeServos();
    setTarget(S_PAN,  atof(a1));
    setTarget(S_TILT, atof(a2));
    baseTilt = servos[S_TILT].tgt;
    poseUntilMs = 0;
    out->printf("OK LOOK %.0f %.0f\n", servos[S_PAN].tgt, servos[S_TILT].tgt);

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

  } else if (!strcasecmp(cmd, "EARS")) {
    if (!a1) { out->println("ERR usage: EARS <up|neutral|down|wiggle|flick>"); return; }
    resumeServos();
    if      (!strcasecmp(a1, "up"))      { earMode = EAR_HOLD; earBase = EAR_UP;      setEars(earBase); }
    else if (!strcasecmp(a1, "neutral")) { earMode = EAR_HOLD; earBase = EAR_NEUTRAL; setEars(earBase); }
    else if (!strcasecmp(a1, "down"))    { earMode = EAR_HOLD; earBase = EAR_DOWN;    setEars(earBase); }
    else if (!strcasecmp(a1, "wiggle"))  { earMode = EAR_WIGGLE; earT0 = now; }
    else if (!strcasecmp(a1, "flick"))   { earMode = EAR_FLICK; earT0 = now; flickEar = random(2) ? S_EAR_L : S_EAR_R; }
    else { out->println("ERR unknown ear pose"); return; }
    out->printf("OK EARS %s\n", a1);

  } else if (!strcasecmp(cmd, "BLINK")) {
    startBlink(now);
    out->println("OK BLINK");

  } else if (!strcasecmp(cmd, "CENTER")) {
    resumeServos();
    for (int i = 0; i < NUM_SERVOS; i++) setTarget(i, servos[i].homeDeg);
    baseTilt = servos[S_TILT].homeDeg;
    poseUntilMs = 0;
    staring = false;
    earMode = EAR_HOLD;
    earBase = EAR_NEUTRAL;
    gazeTX = 0;
    gazeTY = 0;
    out->println("OK CENTER");

  } else if (!strcasecmp(cmd, "IDLE")) {
    if (!a1) { out->println("ERR usage: IDLE <on|off>"); return; }
    idleOn = !strcasecmp(a1, "on");
    out->printf("OK IDLE %s\n", idleOn ? "on" : "off");

  } else if (!strcasecmp(cmd, "SERVO")) {
    if (!a1 || !a2) { out->println("ERR usage: SERVO <ch> <us>"); return; }
    int ch = atoi(a1);
    int us = constrain(atoi(a2), 400, 2600);
    if (ch < 0 || ch > 15) { out->println("ERR channel must be 0-15"); return; }
    rawMode = true;
    if (pcaOk) pwm.writeMicroseconds(ch, us);
    float deg = (us - US_AT_0) * 180.0f / (US_AT_180 - US_AT_0);
    out->printf("OK SERVO ch%d %dus (~%.0f deg before invert). CENTER or LOOK resumes.\n", ch, us, deg);

  } else if (!strcasecmp(cmd, "RELAX")) {
    if (pcaOk) for (int i = 0; i < NUM_SERVOS; i++) pwm.setPWM(servos[i].ch, 0, 4096);
    relaxed = true;
    out->println("OK RELAX (send CENTER or LOOK to resume)");

  } else if (!strcasecmp(cmd, "STATUS")) {
    out->printf("OK STATUS pan=%.0f tilt=%.0f earL=%.0f earR=%.0f eyes=%s idle=%d raw=%d relaxed=%d oled=%d pca=%d\n",
                  servos[S_PAN].cur, servos[S_TILT].cur, servos[S_EAR_L].cur, servos[S_EAR_R].cur,
                  EXPRS[exprIdx].name, idleOn, rawMode, relaxed, oledOk, pcaOk);
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
// brain.py sends one small object per line with a single key, e.g. {"look": [0.2, -0.1]}.
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

// Maps -1..1 onto a servo's limits, with 0 = home.
float axisToDeg(int i, float v) {
  ServoCh &s = servos[i];
  v = clampf(v, -1, 1);
  return v >= 0 ? s.homeDeg + v * (s.maxDeg - s.homeDeg) : s.homeDeg + v * (s.homeDeg - s.minDeg);
}

void setEarPose(EarPose e, uint32_t now) {
  switch (e) {
    case EP_UP:     earMode = EAR_HOLD; earBase = EAR_UP;   setEars(earBase); break;
    case EP_DOWN:   earMode = EAR_HOLD; earBase = EAR_DOWN; setEars(earBase); break;
    case EP_WIGGLE: earBase = EAR_NEUTRAL; earMode = EAR_WIGGLE; earT0 = now; break;
    case EP_FLICK:  earBase = EAR_NEUTRAL; earMode = EAR_FLICK; earT0 = now;
                    flickEar = random(2) ? S_EAR_L : S_EAR_R; break;
    default:        earMode = EAR_HOLD; earBase = EAR_NEUTRAL; setEars(earBase); break;
  }
}

void brainExpr(const char* name, uint32_t now) {
  int b = 0;   // unknown names fall back to idle
  bool known = false;
  for (int i = 0; i < NUM_BRAIN_EXPRS; i++) {
    if (!strcasecmp(name, BRAIN_EXPRS[i].name)) { b = i; known = true; break; }
  }
  const BrainExpr &e = BRAIN_EXPRS[b];

  int idx = findExpr(e.eyes);
  exprIdx = idx < 0 ? 0 : idx;
  exprSetMs = now;
  exprHoldMs = 0;          // the brain decides when to change it
  staring = false;

  resumeServos();
  setEarPose(e.ears, now);
  if (e.tiltUp != 0) {
    setTarget(S_TILT, baseTilt + TILT_UP_SIGN * e.tiltUp);
    poseUntilMs = now + POSE_MS;
  } else {
    setTarget(S_TILT, baseTilt);
    poseUntilMs = 0;
  }
  if (e.glanceX != 0 || e.glanceY != 0) {
    gazeTX = e.glanceX;
    gazeTY = e.glanceY;
    glanceUntilMs = now + POSE_MS;
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

  } else if (!strcmp(key, "look")) {
    if (!readPair(p, a, b)) { out->println("{\"err\":\"bad look\"}"); return; }
    resumeServos();
    setTarget(S_PAN, axisToDeg(S_PAN, a));
    baseTilt = axisToDeg(S_TILT, b);
    if (!poseUntilMs) setTarget(S_TILT, baseTilt);   // a running nudge finishes first
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
    client.printf("READY oled=%d pca=%d\n", oledOk, pcaOk);
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

  Wire.beginTransmission(PCA_ADDR);
  pcaOk = (Wire.endTransmission() == 0);
  if (pcaOk) {
    pwm.begin();
    pwm.setOscillatorFrequency(27000000);
    pwm.setPWMFreq(50);
    writeAllServos();   // go to home positions
  }

  uint32_t now = millis();
  lastCmdMs    = now;
  nextBlinkMs  = now + 1500;
  nextGlanceMs = now + IDLE_AFTER_MS;
  nextFlickMs  = now + 8000;

  wifiEnabled = strlen(WIFI_SSID) > 0;
  if (wifiEnabled) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);          // lower latency for commands
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  Serial.println("{\"ready\":true}");   // brain.py waits for this line after opening the port
  Serial.printf("READY oled=%d pca=%d wifi=%s\n", oledOk, pcaOk,
                wifiEnabled ? "connecting to " WIFI_SSID : "off");
  if (!oledOk) Serial.println("ERR OLED not found at 0x3C (SDA 21, SCL 22)");
  if (!pcaOk)  Serial.println("ERR PCA9685 not found at 0x40 (servos disabled)");
}

void loop() {
  static uint32_t lastServoMs = 0, lastFrameMs = 0;
  uint32_t now = millis();

  pollSerial();
  pollNetwork();

  // Motion at ~50 Hz
  if (now - lastServoMs >= 20) {
    float dt = (now - lastServoMs) / 1000.0f;
    if (dt > 0.1f) dt = 0.1f;
    lastServoMs = now;
    updateEars(now);
    updatePose(now);
    updateIdle(now);
    updateServos(dt);
  }

  // Eyes at up to ~30 fps, only redrawn when something changes
  if (now - lastFrameMs >= 33) {
    float dt = (now - lastFrameMs) / 1000.0f;
    if (dt > 0.1f) dt = 0.1f;
    lastFrameMs = now;
    updateEyes(dt, now);
  }
}
