/*
  Pig Interrogator - ESP32 eyes firmware (single OLED, two eyes)
  ==============================================================
  Eyes only. Neck tilt/pan and ears are handled separately (teammate's servo code).

  Hardware:
    OLED (SSD1306 128x64 I2C, 0x3C)  -> Wire : SDA 21, SCL 22
      Both eyes are drawn side by side on this one screen.
    OLED VCC -> ESP32 3.3V, GND -> GND

  Libraries: Adafruit SSD1306, Adafruit GFX
  Board: ESP32 Dev Module

  Connections (both work at the same time):
    USB serial at 115200 baud
    Wi-Fi: the ESP32 joins WIFI_SSID (must be 2.4 GHz) and listens for one
    TCP client on TCP_PORT. Connect to pig.local:3333, or to the IP address
    printed in the Serial Monitor. A new client replaces the old one.
    Leave WIFI_SSID empty ("") to turn Wi-Fi off.

  Commands (one per line, case-insensitive, same over USB and Wi-Fi):
    PING                 -> PONG
    GAZE <x> <y>         pupils, -100..100 (x+ = viewer's right, y+ = down)
    EYES <expr> [ms]     idle suspicious skeptical shocked happy sad angry sleepy thinking
                         optional ms = hold time before returning to idle (0 = forever)
    BLINK                one blink
    CENTER               pupils back to center, idle eyes
    IDLE <on|off>        idle pupil glances after a few quiet seconds
    STATUS               print current state
    HELP                 list commands
  Every reply starts with OK, ERR, PONG, or READY.

  Brain commands (piggyback/brain.py, one JSON object per line, same ports):
    {"expr": "suspicious"}  idle alert suspicious skeptical satisfied disappointed thinking
                            -> eyes (plus a short pupil glance for some); holds until the next expr.
                            Unknown names fall back to idle and reply {"err":"unknown expr"}.
    {"look": [pan, tilt]}   accepted and ignored here (the head servos are separate); replies ok
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

// ======================= CONFIG =======================

// Wi-Fi: use a phone hotspot on 2.4 GHz (iPhone: turn on "Maximize Compatibility").
// Don't push real passwords to a public repo. Set WIFI_SSID to "" for USB only.
#define WIFI_SSID  "your-hotspot-name"
#define WIFI_PASS  "your-hotspot-password"
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

// The brain's expression names -> eyes (from EXPRS) and a pupil glance.
struct BrainExpr {
  const char* name;
  const char* eyes;
  float glanceX, glanceY; // pupil glance held for GLANCE_MS (0,0 = none)
};
const BrainExpr BRAIN_EXPRS[] = {
  {"idle",         "idle",        0,    0},
  {"alert",        "shocked",     0,    0},
  {"suspicious",   "suspicious",  0,    0},
  {"skeptical",    "skeptical",   0,    0},
  {"satisfied",    "happy",       0,    0},
  {"disappointed", "sad",         0,    0.3},
  {"thinking",     "thinking",   -0.6, -0.7},
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

// ======================= COMMANDS =======================

int findExpr(const char* name) {
  for (int i = 0; i < NUM_EXPRS; i++) {
    if (!strcasecmp(name, EXPRS[i].name)) return i;
  }
  return -1;
}

void printHelp() {
  out->println("OK commands: PING | GAZE <x> <y> | EYES <expr> [ms] | BLINK | CENTER |");
  out->println("OK   IDLE <on|off> | STATUS | HELP");
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

  } else if (!strcasecmp(cmd, "STATUS")) {
    out->printf("OK STATUS eyes=%s gaze=%.2f,%.2f staring=%d idle=%d oled=%d\n",
                EXPRS[exprIdx].name, gazeTX, gazeTY, staring, idleOn, oledOk);
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

  } else if (!strcmp(key, "look")) {          // head servos live in separate firmware
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

  // Glances and idle at ~50 Hz
  if (now - lastTickMs >= 20) {
    lastTickMs = now;
    updateGlance(now);
    updateIdle(now);
  }

  // Eyes at up to ~30 fps, only redrawn when something changes
  if (now - lastFrameMs >= 33) {
    float dt = (now - lastFrameMs) / 1000.0f;
    if (dt > 0.1f) dt = 0.1f;
    lastFrameMs = now;
    updateEyes(dt, now);
  }
}
