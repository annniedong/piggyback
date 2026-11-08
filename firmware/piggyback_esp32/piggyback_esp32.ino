// Piggyback ESP32 firmware: neck servos + two OLED eyes, driven by JSON lines over USB serial.
//
// Commands (one JSON object per line, 115200 baud):
//   {"expr": "suspicious"}   {"look": [pan, tilt]} (each -1..1, optional "s": seconds, default 0.3)   {"ping": 1}
// "look" sets where the pig faces. Expressions are small nods and tilts AROUND that direction, so the
// pig keeps facing the user while it reacts instead of turning away.
//   {"gaze": [x, y]} (-1..1) moves the pupils to look at the user (no reply, sent ~10x/s)
//   {"stare": p} (0..1) stare-down eyes with lids closing as p grows; any "expr" ends it (no reply)
// Replies: {"ready":true} at boot, then {"ok":true} | {"err":"..."} | {"pong":1}
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

// ---------------------------------------------------------------- limits and presets (calibrate on the head)

struct Limits { int panMin, panMax, tiltMin, tiltMax; };
const Limits LIMITS = {45, 135, 60, 120};
const int HOME_PAN = 90, HOME_TILT = 90;

enum Eyes { EYES_IDLE, EYES_ALERT, EYES_SUSPICIOUS, EYES_SKEPTICAL, EYES_SATISFIED, EYES_DISAPPOINTED, EYES_THINKING, EYES_STARE };

// pan / tilt are degrees added to the facing direction (from "look"), not absolute angles.
struct Preset { const char* name; int pan; int tilt; Eyes eyes; float seconds; };
const Preset PRESETS[] = {
  {"idle",          0,   0, EYES_IDLE,         1.0},
  {"alert",         0,  -8, EYES_ALERT,        0.4},
  {"suspicious",    5,   4, EYES_SUSPICIOUS,   0.8},
  {"skeptical",    -5,   6, EYES_SKEPTICAL,    0.8},
  {"satisfied",     0,  -4, EYES_SATISFIED,    0.6},
  {"disappointed",  0,  15, EYES_DISAPPOINTED, 1.2},
  {"thinking",      8,  -6, EYES_THINKING,     1.0},
};
const int NUM_PRESETS = sizeof(PRESETS) / sizeof(PRESETS[0]);

// ---------------------------------------------------------------- hardware

TwoWire bus1 = TwoWire(1);
Adafruit_SSD1306 leftEye(128, 64, &Wire, -1);
Adafruit_SSD1306 rightEye(128, 64, &bus1, -1);
bool leftOk = false, rightOk = false;
Servo panServo, tiltServo;

// ---------------------------------------------------------------- motion (smoothstep, non-blocking)

float curPan = HOME_PAN, curTilt = HOME_TILT;
int facePan = HOME_PAN, faceTilt = HOME_TILT;  // where the pig faces (last "look"); expressions are offsets from it
float fromPan, fromTilt, toPan = HOME_PAN, toTilt = HOME_TILT;
unsigned long moveStart = 0, moveMs = 0;

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

void moveTo(int pan, int tilt, float seconds) {
  // Start from wherever the head is now, so a new command interrupts cleanly.
  fromPan = curPan; fromTilt = curTilt;
  toPan = clampi(pan, LIMITS.panMin, LIMITS.panMax);
  toTilt = clampi(tilt, LIMITS.tiltMin, LIMITS.tiltMax);
  moveStart = millis();
  moveMs = (unsigned long)(seconds * 1000);
}

float secondsLeft() {
  unsigned long gone = millis() - moveStart;
  return gone < moveMs ? (moveMs - gone) / 1000.0 : 0;
}

void stepMotion() {
  float t = moveMs ? (float)(millis() - moveStart) / moveMs : 1.0;
  if (t > 1) t = 1;
  float s = t * t * (3 - 2 * t);
  curPan = fromPan + (toPan - fromPan) * s;
  curTilt = fromTilt + (toTilt - fromTilt) * s;
  panServo.write((int)curPan);
  tiltServo.write((int)curTilt);
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

// ---------------------------------------------------------------- commands

void reply(const char* json) { Serial.println(json); }

void applyExpr(const char* name) {
  for (int i = 0; i < NUM_PRESETS; i++) {
    if (strcmp(PRESETS[i].name, name) == 0) {
      // Don't speed up a slow turn that's still going: finish it at least as slowly.
      moveTo(facePan + PRESETS[i].pan, faceTilt + PRESETS[i].tilt, max(PRESETS[i].seconds, secondsLeft()));
      showEyes(PRESETS[i].eyes);
      reply("{\"ok\":true}");
      return;
    }
  }
  moveTo(facePan, faceTilt, max(PRESETS[0].seconds, secondsLeft()));
  showEyes(PRESETS[0].eyes);
  reply("{\"err\":\"unknown expr\"}");
}

void applyLook(float x, float y, float seconds) {
  x = constrain(x, -1.0, 1.0);
  y = constrain(y, -1.0, 1.0);
  int pan = HOME_PAN + (int)(x * (x >= 0 ? LIMITS.panMax - HOME_PAN : HOME_PAN - LIMITS.panMin));
  int tilt = HOME_TILT + (int)(y * (y >= 0 ? LIMITS.tiltMax - HOME_TILT : HOME_TILT - LIMITS.tiltMin));
  facePan = clampi(pan, LIMITS.panMin, LIMITS.panMax);
  faceTilt = clampi(tilt, LIMITS.tiltMin, LIMITS.tiltMax);
  moveTo(facePan, faceTilt, seconds);
  reply("{\"ok\":true}");
}

void handleLine(const char* line) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) {
    reply("{\"err\":\"bad json\"}");
    return;
  }
  if (doc["expr"].is<const char*>()) {
    applyExpr(doc["expr"]);
  } else if (doc["look"].is<JsonArray>() && doc["look"].size() == 2) {
    applyLook(doc["look"][0].as<float>(), doc["look"][1].as<float>(), doc["s"] | 0.3f);
  } else if (doc["gaze"].is<JsonArray>() && doc["gaze"].size() == 2) {
    gazeX = constrain(doc["gaze"][0].as<float>(), -1.0, 1.0);
    gazeY = constrain(doc["gaze"][1].as<float>(), -1.0, 1.0);
    eyesDirty = true;  // no reply: this is sent many times a second
  } else if (!doc["stare"].isNull()) {
    stareP = constrain(doc["stare"].as<float>(), 0.0, 1.0);
    curEyes = EYES_STARE;
    eyesDirty = true;  // no reply: sent ~10x/s during a stare
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
    if (c == '\n' || c == '\r') {
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

  panServo.setPeriodHertz(50);
  tiltServo.setPeriodHertz(50);
  panServo.attach(PAN_PIN, 500, 2400);
  tiltServo.attach(TILT_PIN, 500, 2400);
  panServo.write(HOME_PAN);
  tiltServo.write(HOME_TILT);

  showEyes(EYES_IDLE);
  reply("{\"ready\":true}");
}

void loop() {
  readSerial();
  stepMotion();
  stepEyes();
  delay(5);
}
