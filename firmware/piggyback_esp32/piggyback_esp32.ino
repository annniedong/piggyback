// Piggyback ESP32 firmware: neck servos + two OLED eyes, driven by JSON lines over USB serial.
//
// Commands (one JSON object per line, 115200 baud):
//   {"expr": "suspicious"}   {"look": [pan, tilt]} (each -1..1)   {"ping": 1}
//   {"gaze": [x, y]} (-1..1) moves the pupils to look at the user (no reply, sent ~10x/s)
//   {"stare": p} (0..1) stare-down eyes with lids closing as p grows; any "expr" ends it (no reply)
//   {"gesture": "shake" | "nod" | "lookback" | "center"} head motion only, eyes unchanged
//   {"budget": [left, total], "note": "-$45 socks"} shows the budget on the third OLED ("note" optional)
// Replies: {"ready":true} at boot, then {"ok":true} | {"err":"..."} | {"pong":1}
// Also prints each head-motion step as it starts, so you can see which move is which:
//   {"move":"skeptical","step":"3/5","doing":"glance back at you","pan":90,"tilt":90,"ms":375}
//   {"move":"skeptical","doing":"done"}   ("slowed":true = the servo speed cap stretched that step)
//
// Libraries: ArduinoJson (v7), Adafruit SSD1306, Adafruit GFX, ESP32Servo.

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ---------------------------------------------------------------- pins (confirm for your board)

const int PAN_PIN = 18;
const int TILT_PIN = 19;
const int LEFT_SDA = 21, LEFT_SCL = 22;    // I2C bus 0
const int RIGHT_SDA = 25, RIGHT_SCL = 26;  // I2C bus 1
const uint8_t OLED_ADDR = 0x3C;
// Budget screen: a third SSD1306 sharing an I2C bus with one eye, so it needs the other address
// (0x3D: move the resistor/jumper on its back labelled "I2C address"). Found on either bus at boot.
const uint8_t BUDGET_ADDR = 0x3D;

// ---------------------------------------------------------------- limits and presets (calibrate on the head)

struct Limits { int panMin, panMax, tiltMin, tiltMax; };
const Limits LIMITS = {0, 180, 60, 120};  // narrow panMin/panMax if the head hits the frame
const int HOME_PAN = 90, HOME_TILT = 90;
// Trim: degrees added to every servo write so "home" really faces straight ahead, fixing a horn that
// sits a few degrees off (its teeth only let you reposition it in big jumps). Tune live in Serial
// Monitor with "trim pan 3" / "trim tilt -2", then paste the printed values here.
const int PAN_TRIM = 0, TILT_TRIM = 0;
int panTrim = PAN_TRIM, tiltTrim = TILT_TRIM;

// The servo's top speed. Every move is slowed down so it never asks for more than this,
// so a fast gesture just plays a bit slower instead of the servo lagging behind.
const float MAX_DEG_PER_SEC = 100;

enum Eyes { EYES_IDLE, EYES_ALERT, EYES_SUSPICIOUS, EYES_SKEPTICAL, EYES_SATISFIED, EYES_DISAPPOINTED, EYES_THINKING, EYES_STARE };

// A motion is a list of steps. Each step moves to an offset (degrees) from where the user is
// (the last "look" target; the home pose until tracking starts), over `ms` milliseconds.
// A step to the same offset as the one before is a hold. dTilt > 0 = head down.
// `what` is printed over serial as each step starts, so you can see which move is which.
struct Step { int dPan, dTilt, ms; const char* what; };
struct Motion { const char* name; const Step* steps; int n; bool loops; };
#define MOTION(name, a, loops) {name, a, sizeof(a) / sizeof(a[0]), loops}

// Expressions
const Step IDLE_STEPS[] = {  // loops while nobody is being tracked
  {10, 0, 2500, "drift one way"},
  {-10, 0, 5000, "drift the other way"},
  {0, 0, 2500, "drift back to center"},
};
const Step ALERT_STEPS[] = {
  {0, -10, 300, "snap to face you, chin up"},
};
const Step SUSP_STEPS[] = {
  {8, 5, 800, "lean in, slightly off to one side"},
};
const Step SKEPT_STEPS[] = {
  {25, 3, 700, "look away"},
  {25, 3, 500, "hold, looking away"},
  {0, 0, 350, "glance back at you"},
  {0, 0, 400, "hold your eye"},
  {20, 3, 700, "turn away again"},
};
const Step SATIS_STEPS[] = {
  {0, 12, 350, "nod down"},
  {0, -4, 350, "nod up"},
  {0, 8, 300, "small nod down"},
  {0, 0, 300, "back to facing you"},
};
const Step DISAP_STEPS[] = {
  {-15, 10, 700, "shake one way, head dropping"},
  {15, 15, 900, "shake the other way, lower"},
  {-12, 18, 900, "shake back, lower"},
  {10, 20, 900, "shake the other way, head down"},
  {12, 20, 800, "settle, turned away and down"},
};
const Step THINK_STEPS[] = {  // loops while waiting for the AI
  {15, -8, 1000, "look up and aside"},
  {8, -10, 1500, "sway, still looking up"},
  {18, -6, 1500, "sway back"},
};

struct Preset { const char* name; Eyes eyes; Motion motion; const char* eyesLook; };
const Preset PRESETS[] = {
  {"idle",         EYES_IDLE,         MOTION("idle", IDLE_STEPS, true), "open, normal pupils"},
  {"alert",        EYES_ALERT,        MOTION("alert", ALERT_STEPS, false), "wide open, small pupils"},
  {"suspicious",   EYES_SUSPICIOUS,   MOTION("suspicious", SUSP_STEPS, false), "narrowed into slits"},
  {"skeptical",    EYES_SKEPTICAL,    MOTION("skeptical", SKEPT_STEPS, false), "one squinting, one wide with a raised brow"},
  {"satisfied",    EYES_SATISFIED,    MOTION("satisfied", SATIS_STEPS, false), "happy arcs, like ^ ^"},
  {"disappointed", EYES_DISAPPOINTED, MOTION("disappointed", DISAP_STEPS, false), "half-closed, looking down"},
  {"thinking",     EYES_THINKING,     MOTION("thinking", THINK_STEPS, true), "pupils circling near the top"},
};
const int NUM_PRESETS = sizeof(PRESETS) / sizeof(PRESETS[0]);

// Gestures: head motion only, eyes unchanged. {"gesture": "shake"}
const Step SHAKE_STEPS[] = {
  {-18, 0, 400, "shake one way"},
  {18, 0, 500, "shake the other way"},
  {-18, 0, 500, "shake one way"},
  {18, 0, 500, "shake the other way"},
  {0, 0, 400, "back to facing you"},
};
const Step NOD_STEPS[] = {
  {0, 15, 400, "nod down"},
  {0, -5, 400, "nod up"},
  {0, 10, 350, "small nod down"},
  {0, 0, 350, "back to facing you"},
};
const Step LOOKBACK_STEPS[] = {
  {35, 0, 700, "turn away"},
  {35, 0, 600, "hold, turned away"},
  {0, 0, 500, "look back at you"},
  {0, 0, 300, "hold your eye"},
};
const Step CENTER_STEPS[] = {
  {0, 0, 500, "face you (straight ahead before tracking)"},
};
struct Gesture { const char* name; Motion motion; };
const Gesture GESTURES[] = {
  {"shake",    MOTION("shake", SHAKE_STEPS, false)},
  {"nod",      MOTION("nod", NOD_STEPS, false)},
  {"lookback", MOTION("lookback", LOOKBACK_STEPS, false)},
  {"center",   MOTION("center", CENTER_STEPS, false)},
};
const int NUM_GESTURES = sizeof(GESTURES) / sizeof(GESTURES[0]);

// ---------------------------------------------------------------- hardware

TwoWire bus1 = TwoWire(1);
Adafruit_SSD1306 leftEye(128, 64, &Wire, -1);
Adafruit_SSD1306 rightEye(128, 64, &bus1, -1);
bool leftOk = false, rightOk = false;
Adafruit_SSD1306* budgetScreen = nullptr;  // set in setup() if the third OLED answers
Servo panServo, tiltServo;

// ---------------------------------------------------------------- motion (smoothstep, non-blocking)

float curPan = HOME_PAN, curTilt = HOME_TILT;
float fromPan, fromTilt, toPan = HOME_PAN, toTilt = HOME_TILT;
unsigned long moveStart = 0, moveMs = 0;

// Where the user is: the last "look" target. Expression and gesture steps are offsets from here.
int userPan = HOME_PAN, userTilt = HOME_TILT;
unsigned long lastLookMs = 0;

// The motion being played (an expression's or a gesture's steps).
Motion seq = {"none", nullptr, 0, false};
int seqI = 0;
bool seqActive = false;
bool seqQuiet = false;  // idle's repeat passes aren't printed, so the monitor doesn't scroll forever
int seqDir = 1;     // -1 mirrors pan offsets so turns go toward the middle, not into a limit
unsigned long bootIdleAt = 0;  // when the boot pause ends and idle drift starts
int curPreset = 0;  // last expression, so idle drift can restart when tracking stops

// A "look" that arrives during a one-shot motion waits until the motion finishes.
bool lookPending = false;
int pendingPan, pendingTilt;

// Testing aids, typed in Serial Monitor: "slow" plays every move at 1/3 speed, "step <move>" pauses after
// each step until you press Enter. The brain never turns these on.
int slowFactor = 1;
bool stepMode = false;      // pause after each step
bool stepWaiting = false;   // paused, waiting for Enter
bool stepGo = false;        // Enter was pressed

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

void moveTo(int pan, int tilt, unsigned long ms) {
  // Start from wherever the head is now, so a new command interrupts cleanly.
  fromPan = curPan; fromTilt = curTilt;
  toPan = clampi(pan, LIMITS.panMin, LIMITS.panMax);
  toTilt = clampi(tilt, LIMITS.tiltMin, LIMITS.tiltMax);
  // Smoothstep peaks at 1.5x the average speed, so stretch the move until that peak fits the servo.
  float dist = fmaxf(fabsf(toPan - fromPan), fabsf(toTilt - fromTilt));
  unsigned long minMs = (unsigned long)(1.5f * dist / MAX_DEG_PER_SEC * 1000);
  moveStart = millis();
  moveMs = (ms > minMs ? ms : minMs) * slowFactor;
}

// Print what the head is doing, e.g. {"move":"skeptical","step":"3/5","doing":"glance back at you","pan":90,"tilt":90,"ms":375}
// The brain logs these as "ESP32 <" lines; Serial Monitor shows them too.
void announce(const char* doing) {
  char line[200];
  snprintf(line, sizeof(line), "{\"move\":\"%s\",\"doing\":\"%s\"}", seq.name, doing);
  Serial.println(line);
}

void startStep() {
  const Step& s = seq.steps[seqI];
  moveTo(userPan + seqDir * s.dPan, userTilt + s.dTilt, s.ms);
  if (seqQuiet) return;
  char line[200];
  // "slowed" means MAX_DEG_PER_SEC stretched the step past its ms because the servo can't go that fast.
  snprintf(line, sizeof(line),
           "{\"move\":\"%s\",\"step\":\"%d/%d\",\"doing\":\"%s\",\"pan\":%d,\"tilt\":%d,\"ms\":%lu%s}",
           seq.name, seqI + 1, seq.n, s.what, (int)toPan, (int)toTilt, moveMs,
           moveMs > (unsigned long)s.ms * slowFactor ? ",\"slowed\":true" : "");
  Serial.println(line);
}

void playMotion(const Motion& m) {
  if (seqActive) announce("interrupted");
  seq = m;
  seqQuiet = false;
  seqI = 0;
  seqActive = true;
  seqDir = userPan > HOME_PAN ? -1 : 1;
  lookPending = false;
  startStep();
}

void writeServos() {
  panServo.write(constrain((int)curPan + panTrim, 0, 180));
  tiltServo.write(constrain((int)curTilt + tiltTrim, 0, 180));
}

void stepMotion() {
  float t = moveMs ? (float)(millis() - moveStart) / moveMs : 1.0;
  if (t > 1) t = 1;
  float s = t * t * (3 - 2 * t);
  curPan = fromPan + (toPan - fromPan) * s;
  curTilt = fromTilt + (toTilt - fromTilt) * s;
  writeServos();

  if (t < 1 || !seqActive) return;
  if (stepMode) {  // hold here until Enter
    if (!stepGo) {
      if (!stepWaiting) {
        stepWaiting = true;
        Serial.println(seqI + 1 < seq.n ? "{\"step\":\"press Enter for the next step\"}"
                                        : "{\"step\":\"that was the last step, press Enter to finish\"}");
      }
      return;
    }
    stepGo = false;
    stepWaiting = false;
    if (seqI + 1 >= seq.n) stepMode = false;  // finished: back to normal
  }
  if (++seqI < seq.n) {
    startStep();
  } else if (seq.loops) {
    // Idle drifts quietly after its first pass; thinking keeps printing (it only runs while the AI thinks).
    if (!seqQuiet && strcmp(seq.name, "idle") == 0) {
      seqQuiet = true;
      announce("drifting quietly from now on (type stop to hold still)");
    } else if (!seqQuiet) {
      announce("starting over (this one repeats until the next command)");
    }
    seqI = 0;
    startStep();
  } else {
    seqActive = false;
    if (lookPending) {  // tracking wanted to move during the motion: catch up now
      lookPending = false;
      userPan = pendingPan;
      userTilt = pendingTilt;
      moveTo(userPan, userTilt, 300);
      announce("done, turning to follow your face");
    } else {
      announce("done");
    }
  }
}

// ---------------------------------------------------------------- eyes

Eyes curEyes = EYES_IDLE;
unsigned long lastEyeFrame = 0;
float gazeX = 0, gazeY = 0;  // -1..1, where the pupils point
float stareP = 0;            // 0..1 stare-down progress
bool eyesDirty = false;      // redraw on the next frame

// Draw one eye. `isLeft` mirrors slants so the two eyes are symmetric.
void drawEye(Adafruit_SSD1306& d, Eyes e, bool isLeft) {
  d.clearDisplay();
  int cx = 64, cy = 32;
  int dir = isLeft ? 1 : -1;  // used for inward/outward slants
  int px = (int)(gazeX * 14), py = (int)(gazeY * 8);  // pupil offset toward the user
  switch (e) {
    case EYES_IDLE:
      d.fillCircle(cx, cy, 26, SSD1306_WHITE);
      d.fillCircle(cx + px, cy + py, 10, SSD1306_BLACK);
      break;
    case EYES_ALERT:
      d.fillCircle(cx, cy, 30, SSD1306_WHITE);
      d.fillCircle(cx + px, cy + py, 6, SSD1306_BLACK);
      break;
    case EYES_SUSPICIOUS:
      d.fillRoundRect(cx - 34, cy - 8, 68, 16, 8, SSD1306_WHITE);
      d.fillCircle(cx + 14 + px / 2, cy, 7, SSD1306_BLACK);
      break;
    case EYES_SKEPTICAL:
      if (isLeft) {  // one squinting eye, one wide eye
        d.fillRoundRect(cx - 30, cy - 6, 60, 12, 6, SSD1306_WHITE);
        d.fillCircle(cx + px, cy, 5, SSD1306_BLACK);
      } else {
        d.fillCircle(cx, cy + 4, 24, SSD1306_WHITE);
        d.fillCircle(cx + px, cy + 4 + py, 9, SSD1306_BLACK);
        d.fillRect(cx - 30, cy - 30, 60, 10, SSD1306_WHITE);  // raised brow
      }
      break;
    case EYES_SATISFIED:  // happy ^ ^ arcs
      d.fillCircle(cx, cy + 12, 28, SSD1306_WHITE);
      d.fillCircle(cx, cy + 22, 26, SSD1306_BLACK);
      break;
    case EYES_DISAPPOINTED:
      d.fillCircle(cx, cy + 6, 24, SSD1306_WHITE);
      d.fillCircle(cx + px, cy + 16, 9, SSD1306_BLACK);
      // droopy lid slanting down toward the outside
      d.fillTriangle(cx - 40, cy - 30, cx + 40, cy - 30, cx - 40 * dir, cy + 4, SSD1306_BLACK);
      break;
    case EYES_THINKING: {
      // pupil drifts around the top of the eye
      float a = millis() / 600.0;
      d.fillCircle(cx, cy, 26, SSD1306_WHITE);
      d.fillCircle(cx + (int)(12 * cos(a)), cy - 10 + (int)(4 * sin(a)), 9, SSD1306_BLACK);
      break;
    }
    case EYES_STARE: {
      // big locked-on eye; lids close from top and bottom as the stare progresses
      int lid = (int)(stareP * 22);
      d.fillCircle(cx, cy, 30, SSD1306_WHITE);
      d.fillCircle(cx + px, cy + py, 8, SSD1306_BLACK);
      d.fillRect(0, 0, 128, 2 + lid, SSD1306_BLACK);
      d.fillRect(0, 62 - lid, 128, 2 + lid, SSD1306_BLACK);
      break;
    }
  }
  d.display();
}

void showEyes(Eyes e) {
  curEyes = e;
  if (leftOk) drawEye(leftEye, e, true);
  if (rightOk) drawEye(rightEye, e, false);
}

void stepEyes() {
  // Redraw at most ~15 fps: thinking animates, and gaze/stare updates mark the eyes dirty.
  if ((curEyes == EYES_THINKING || eyesDirty) && millis() - lastEyeFrame > 66) {
    lastEyeFrame = millis();
    eyesDirty = false;
    showEyes(curEyes);
  }
}

// ---------------------------------------------------------------- budget screen (third OLED)

long budgetLeft = 0, budgetTotal = 0;
bool budgetKnown = false;
char budgetNote[24] = "";
unsigned long budgetNoteUntil = 0;  // the note shows for a few seconds, then the screen goes back to normal

// Print `text` centered horizontally at row y, in text size `size` (each character is 6*size px wide).
void centerText(Adafruit_SSD1306& d, const char* text, int y, int size) {
  d.setTextSize(size);
  int w = strlen(text) * 6 * size;
  d.setCursor(w < 128 ? (128 - w) / 2 : 0, y);
  d.print(text);
}

void drawBudget() {
  if (!budgetScreen) return;
  Adafruit_SSD1306& d = *budgetScreen;
  d.clearDisplay();
  d.setTextColor(SSD1306_WHITE);
  char line[24];
  if (!budgetKnown) {
    centerText(d, "PIGGYBACK", 16, 2);
    centerText(d, "waiting for budget", 44, 1);
    d.display();
    return;
  }
  bool over = budgetLeft < 0;
  centerText(d, over ? "OVER BUDGET BY" : "LEFT THIS MONTH", 0, 1);
  snprintf(line, sizeof(line), "$%ld", over ? -budgetLeft : budgetLeft);
  centerText(d, line, 13, strlen(line) <= 5 ? 3 : 2);  // big number; smaller if it's long
  // Gauge: how much of the month's budget is left.
  float frac = budgetTotal > 0 ? constrain((float)budgetLeft / budgetTotal, 0.0f, 1.0f) : 0;
  d.drawRect(4, 40, 120, 10, SSD1306_WHITE);
  d.fillRect(6, 42, (int)(116 * frac), 6, SSD1306_WHITE);
  if (budgetNote[0] && millis() < budgetNoteUntil) {
    centerText(d, budgetNote, 55, 1);
  } else {
    snprintf(line, sizeof(line), "of $%ld budget", budgetTotal);
    centerText(d, line, 55, 1);
  }
  d.display();
}

void setBudget(long left, long total, const char* note) {
  budgetLeft = left;
  budgetTotal = total;
  budgetKnown = true;
  if (note && note[0]) {
    strncpy(budgetNote, note, sizeof(budgetNote) - 1);
    budgetNote[sizeof(budgetNote) - 1] = '\0';
    budgetNoteUntil = millis() + 6000;
  }
  drawBudget();
  char out[96];
  snprintf(out, sizeof(out), "{\"budget\":\"$%ld left of $%ld\",\"screen\":%s}", left, total,
           budgetScreen ? "true" : "false");
  Serial.println(out);
}

void stepBudget() {
  // Clear the note once its time is up.
  if (budgetNote[0] && millis() >= budgetNoteUntil) {
    budgetNote[0] = '\0';
    drawBudget();
  }
}

bool i2cAnswers(TwoWire& bus, uint8_t addr) {
  bus.beginTransmission(addr);
  return bus.endTransmission() == 0;
}

// ---------------------------------------------------------------- commands

void reply(const char* json) { Serial.println(json); }

void applyExpr(const char* name) {
  for (int i = 0; i < NUM_PRESETS; i++) {
    if (strcmp(PRESETS[i].name, name) == 0) {
      curPreset = i;
      char line[160];
      snprintf(line, sizeof(line), "{\"expression\":\"%s\",\"eyes\":\"%s\"}", PRESETS[i].name, PRESETS[i].eyesLook);
      Serial.println(line);
      playMotion(PRESETS[i].motion);
      showEyes(PRESETS[i].eyes);
      reply("{\"ok\":true}");
      return;
    }
  }
  curPreset = 0;
  playMotion(PRESETS[0].motion);
  showEyes(PRESETS[0].eyes);
  reply("{\"err\":\"unknown expr\"}");
}

void applyGesture(const char* name) {
  for (int i = 0; i < NUM_GESTURES; i++) {
    if (strcmp(GESTURES[i].name, name) == 0) {
      char line[80];
      snprintf(line, sizeof(line), "{\"gesture\":\"%s\"}", GESTURES[i].name);
      Serial.println(line);
      playMotion(GESTURES[i].motion);
      reply("{\"ok\":true}");
      return;
    }
  }
  reply("{\"err\":\"unknown gesture\"}");
}

void applyLook(float x, float y) {
  x = constrain(x, -1.0, 1.0);
  y = constrain(y, -1.0, 1.0);
  int pan = HOME_PAN + (int)(x * (x >= 0 ? LIMITS.panMax - HOME_PAN : HOME_PAN - LIMITS.panMin));
  int tilt = HOME_TILT + (int)(y * (y >= 0 ? LIMITS.tiltMax - HOME_TILT : HOME_TILT - LIMITS.tiltMin));
  lastLookMs = millis();
  if (seqActive && !seq.loops) {
    // Let a shake/nod/lookback finish; follow the user right after.
    lookPending = true;
    pendingPan = pan;
    pendingTilt = tilt;
  } else {
    if (seqActive) announce("stopped, following your face");
    seqActive = false;  // tracking takes over from idle drift or the thinking sway
    userPan = pan;
    userTilt = tilt;
    moveTo(pan, tilt, 300);
  }
  reply("{\"ok\":true}");
}

// ---------------------------------------------------------------- demo: every expression and gesture in turn

const char* DEMO[] = {"alert", "suspicious", "thinking", "skeptical", "satisfied", "disappointed",
                      "shake", "nod", "lookback", "idle"};
const int DEMO_N = sizeof(DEMO) / sizeof(DEMO[0]);
int demoI = -1;  // -1 = not running
unsigned long demoItemStart = 0;

bool isGesture(const char* name) {
  for (int i = 0; i < NUM_GESTURES; i++) if (strcmp(GESTURES[i].name, name) == 0) return true;
  return false;
}

void demoPlay() {
  char line[80];
  snprintf(line, sizeof(line), "{\"demo\":\"======== %d/%d %s ========\"}", demoI + 1, DEMO_N, DEMO[demoI]);
  Serial.println(line);
  if (isGesture(DEMO[demoI])) applyGesture(DEMO[demoI]);
  else applyExpr(DEMO[demoI]);
  demoItemStart = millis();
}

void stepDemo() {
  if (demoI < 0) return;
  // Next item 3 s after a one-shot motion finishes (time to read the log); looping ones (thinking) get 8 s.
  unsigned long elapsed = millis() - demoItemStart;
  static unsigned long doneAt = 0;
  if (seqActive) doneAt = 0;
  else if (!doneAt) doneAt = millis();
  bool finished = seq.loops ? elapsed > 8000UL * slowFactor : (doneAt && millis() - doneAt > 3000);
  if (!finished) return;
  if (++demoI >= DEMO_N) {
    demoI = -1;
    Serial.println("{\"demo\":\"done\"}");
    return;
  }
  demoPlay();
}

void printHelp() {
  Serial.println("{\"help\":\"type a move: idle alert suspicious skeptical satisfied disappointed thinking shake nod lookback center\"}");
  Serial.println("{\"help\":\"demo = all moves in turn | step <move> = one step per Enter | slow / normal = 1/3 speed or back | stop = hold still | budget 150 = test the budget screen\"}");
  Serial.println("{\"help\":\"home = face straight ahead and hold | trim pan 3 / trim tilt -2 = fix a crooked head\"}");
}

void printTrim() {
  char out[128];
  snprintf(out, sizeof(out), "{\"trim\":\"paste into the code: const int PAN_TRIM = %d, TILT_TRIM = %d;\"}", panTrim, tiltTrim);
  Serial.println(out);
}

bool isMove(const char* name) {
  if (isGesture(name)) return true;
  for (int i = 0; i < NUM_PRESETS; i++) if (strcmp(PRESETS[i].name, name) == 0) return true;
  return false;
}

void playByName(const char* name) {
  if (isGesture(name)) applyGesture(name);
  else applyExpr(name);
}

// Plain words typed in Serial Monitor: "skeptical", "shake", "demo", "help", "slow", "step skeptical".
void handleWord(const char* word, const char* arg, const char* arg2 = "") {
  if (strcmp(word, "step") == 0) {
    if (!isMove(arg)) {
      reply("{\"err\":\"type step and a move, e.g. step skeptical\"}");
      return;
    }
    stepMode = true;
    stepWaiting = false;
    stepGo = false;
    playByName(arg);
  } else if (strcmp(word, "next") == 0 || strcmp(word, "n") == 0) {
    stepGo = stepWaiting;
  } else if (strcmp(word, "stop") == 0) {
    if (seqActive) announce("stopped");
    seqActive = false;
    curPreset = -1;  // no idle drift until the next move
    moveTo(userPan, userTilt, 500);
    Serial.println("{\"note\":\"holding still, facing forward. Type a move to start again\"}");
  } else if (strcmp(word, "home") == 0) {
    seqActive = false;
    curPreset = -1;  // no idle drift until the next move
    userPan = HOME_PAN;
    userTilt = HOME_TILT;
    moveTo(HOME_PAN, HOME_TILT, 600);
    Serial.println("{\"note\":\"home: facing straight ahead and holding. Crooked? try trim pan 3 or trim tilt -2\"}");
  } else if (strcmp(word, "trim") == 0) {
    // "trim pan 3": the head shifts right away (stepMotion rewrites the servos every loop).
    if ((strcmp(arg, "pan") != 0 && strcmp(arg, "tilt") != 0) || !arg2[0]) {
      reply("{\"err\":\"type trim pan <degrees> or trim tilt <degrees>, e.g. trim pan 3\"}");
      printTrim();
      return;
    }
    int deg = constrain(atoi(arg2), -30, 30);
    if (strcmp(arg, "pan") == 0) panTrim = deg; else tiltTrim = deg;
    seqActive = false;
    curPreset = -1;
    userPan = HOME_PAN;
    userTilt = HOME_TILT;
    moveTo(HOME_PAN, HOME_TILT, 300);
    printTrim();
  } else if (strcmp(word, "slow") == 0) {
    slowFactor = 3;
    Serial.println("{\"note\":\"slow motion on: every move takes 3x longer (type normal to undo)\"}");
  } else if (strcmp(word, "normal") == 0 || strcmp(word, "fast") == 0) {
    slowFactor = 1;
    Serial.println("{\"note\":\"normal speed\"}");
  } else if (strcmp(word, "demo") == 0) {
    demoI = 0;
    demoPlay();
  } else if (strcmp(word, "help") == 0) {
    printHelp();
  } else if (strcmp(word, "budget") == 0) {
    // "budget" redraws it; "budget 150" pretends $150 is left (total stays, or $600) to test the screen.
    if (arg[0]) setBudget(atol(arg), budgetTotal > 0 ? budgetTotal : 600, "test from Serial Monitor");
    else if (budgetKnown) setBudget(budgetLeft, budgetTotal, nullptr);
    else drawBudget();
  } else if (isGesture(word)) {
    applyGesture(word);
  } else {
    for (int i = 0; i < NUM_PRESETS; i++) {
      if (strcmp(PRESETS[i].name, word) == 0) {
        applyExpr(word);
        return;
      }
    }
    reply("{\"err\":\"unknown word, type help\"}");
  }
}

void handleLine(const char* line) {
  if (strstr(line, "budget") == nullptr) bootIdleAt = 0;  // a real command: skip the boot drift
  while (*line == ' ') line++;
  if (*line != '{') {  // not JSON: a word typed by a person
    char word[32], arg[32], arg2[32];
    size_t n = 0, m = 0, k = 0;
    for (; line[n] && line[n] != ' ' && n < sizeof(word) - 1; n++) word[n] = tolower(line[n]);
    word[n] = '\0';
    while (line[n] == ' ') n++;
    for (; line[n + m] && line[n + m] != ' ' && m < sizeof(arg) - 1; m++) arg[m] = tolower(line[n + m]);
    arg[m] = '\0';
    n += m;
    while (line[n] == ' ') n++;
    for (; line[n + k] && line[n + k] != ' ' && k < sizeof(arg2) - 1; k++) arg2[k] = tolower(line[n + k]);
    arg2[k] = '\0';
    bool keepsDemo = strcmp(word, "demo") == 0 || strcmp(word, "slow") == 0 || strcmp(word, "normal") == 0;
    bool keepsStep = strcmp(word, "step") == 0 || strcmp(word, "next") == 0 || strcmp(word, "n") == 0 || keepsDemo;
    if (!keepsDemo) demoI = -1;
    if (!keepsStep) stepMode = stepWaiting = false;
    handleWord(word, arg, arg2);
    return;
  }
  // Any JSON command (from the brain) stops a demo or step mode, except tracking and the budget screen.
  if (strstr(line, "\"gaze\"") == nullptr && strstr(line, "\"stare\"") == nullptr && strstr(line, "\"budget\"") == nullptr) {
    demoI = -1;
    stepMode = stepWaiting = false;
  }
  JsonDocument doc;
  if (deserializeJson(doc, line)) {
    reply("{\"err\":\"bad json\"}");
    return;
  }
  if (doc["expr"].is<const char*>()) {
    applyExpr(doc["expr"]);
  } else if (doc["gesture"].is<const char*>()) {
    applyGesture(doc["gesture"]);
  } else if (doc["look"].is<JsonArray>() && doc["look"].size() == 2) {
    applyLook(doc["look"][0].as<float>(), doc["look"][1].as<float>());
  } else if (doc["gaze"].is<JsonArray>() && doc["gaze"].size() == 2) {
    gazeX = constrain(doc["gaze"][0].as<float>(), -1.0, 1.0);
    gazeY = constrain(doc["gaze"][1].as<float>(), -1.0, 1.0);
    eyesDirty = true;  // no reply: this is sent many times a second
  } else if (!doc["stare"].isNull()) {
    stareP = constrain(doc["stare"].as<float>(), 0.0, 1.0);
    curEyes = EYES_STARE;
    eyesDirty = true;  // no reply: sent ~10x/s during a stare
  } else if (doc["budget"].is<JsonArray>() && doc["budget"].size() == 2) {
    setBudget(lround(doc["budget"][0].as<float>()), lround(doc["budget"][1].as<float>()), doc["note"] | "");
    reply("{\"ok\":true}");
  } else if (!doc["ping"].isNull()) {
    reply("{\"pong\":1}");
  } else {
    reply("{\"err\":\"unknown command\"}");
  }
}

char buf[256];
size_t bufLen = 0;

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' && bufLen == 0 && stepWaiting) {
      stepGo = true;  // bare Enter: next step
    } else if (c == '\n' || c == '\r') {
      if (bufLen > 0) {
        buf[bufLen] = '\0';
        handleLine(buf);
        bufLen = 0;
      }
    } else if (bufLen < sizeof(buf) - 1) {
      buf[bufLen++] = c;
    } else {
      bufLen = 0;  // line too long, drop it
      reply("{\"err\":\"line too long\"}");
    }
  }
}

// ---------------------------------------------------------------- setup / loop

void setup() {
  Serial.begin(115200);

  Wire.begin(LEFT_SDA, LEFT_SCL);
  bus1.begin(RIGHT_SDA, RIGHT_SCL);
  Wire.setClock(400000);  // full-frame redraws need fast I2C to keep tracking smooth
  bus1.setClock(400000);
  leftOk = leftEye.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  rightOk = rightEye.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  if (!leftOk) Serial.println("{\"err\":\"left eye not found\"}");
  if (!rightOk) Serial.println("{\"err\":\"right eye not found\"}");
  TwoWire* budgetBus = i2cAnswers(Wire, BUDGET_ADDR) ? &Wire : i2cAnswers(bus1, BUDGET_ADDR) ? &bus1 : nullptr;
  if (budgetBus) {
    budgetScreen = new Adafruit_SSD1306(128, 64, budgetBus, -1);
    if (!budgetScreen->begin(SSD1306_SWITCHCAPVCC, BUDGET_ADDR)) budgetScreen = nullptr;
  }
  if (budgetScreen) {
    Serial.println(budgetBus == &Wire ? "{\"note\":\"budget screen found on the left eye's bus (0x3D)\"}"
                                      : "{\"note\":\"budget screen found on the right eye's bus (0x3D)\"}");
    drawBudget();
  } else {
    Serial.println("{\"note\":\"no budget screen at 0x3D (fine if it isn't wired yet)\"}");
  }

  panServo.setPeriodHertz(50);
  tiltServo.setPeriodHertz(50);
  panServo.attach(PAN_PIN, 500, 2400);
  tiltServo.attach(TILT_PIN, 500, 2400);
  writeServos();  // home, with trim

  showEyes(EYES_IDLE);
  reply("{\"ready\":true}");
  printHelp();
  printTrim();
  // Hold still at home for 2 s before drifting, so every start looks the same.
  curPreset = -1;
  bootIdleAt = millis() + 2000;
}

void loop() {
  readSerial();
  if (bootIdleAt && millis() >= bootIdleAt) {
    bootIdleAt = 0;
    curPreset = 0;
    playMotion(PRESETS[0].motion);  // idle drift
  }
  // Idle and nobody being tracked for 5 s: go back to drifting.
  if (!seqActive && curPreset == 0 && millis() - lastLookMs > 5000) {
    Serial.println("{\"note\":\"no face for 5 s, drifting again\"}");
    playMotion(PRESETS[0].motion);
  }
  stepMotion();
  stepDemo();
  stepEyes();
  stepBudget();
  delay(5);
}
