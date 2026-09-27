/*
  Smart Heating Controller — firmware for Arduino UNO R4 WiFi

  Connects to home Wi-Fi, checks in with the website every 10 seconds, and turns
  an old mechanical thermostat dial with an SG90 servo. It keeps a copy of the
  weekly schedule (also saved to EEPROM), so heating carries on if the internet,
  the Wi-Fi or the website goes down.

  Wiring (unchanged from the working prototype):
    LCD RS -> D12, E -> D11, D4 -> D7, D5 -> D6, D6 -> D5, D7 -> D4, RW -> GND
    SG90:  brown -> GND, red -> 5V (see README about power), orange -> D9
    D2 is left alone (already used).

  Libraries: WiFiS3 (bundled with the UNO R4 board package), LiquidCrystal, Servo, EEPROM.
*/
#include <WiFiS3.h>
#include <LiquidCrystal.h>
#include <Servo.h>
#include <EEPROM.h>
#include "arduino_secrets.h"

#define FW_VERSION "r4wifi-1.0"
const int SERVO_PIN = 9;
const unsigned long SYNC_EVERY_MS = 10000;
const unsigned long OFFLINE_AFTER_MS = 120000;   // switch to the stored schedule after 2 min without the server
const unsigned long TEST_HOLD_MS = 60000;        // keep a servo-test angle for a minute

LiquidCrystal lcd(12, 11, 7, 6, 5, 4);
Servo dial;

#if SECRET_USE_TLS
WiFiSSLClient net;
#else
WiFiClient net;
#endif

// ---- state received from the server ------------------------------------------
int targetAngle = 90;
int currentAngle = -1;           // unknown until the first move
int minAngle = 60, maxAngle = 120;
String mode = "auto";
String decision = "NORMAL";
String lcd1 = "SMART HEATING", lcd2 = "STARTING...";
String sched = "";               // "mask:minute:temp:angle,..."
long ackId = -1;                 // servo test to acknowledge on the next check-in
unsigned long testHoldUntil = 0;

// clock, estimated from the last successful check-in
int syncMinute = -1, syncDow = 0;
unsigned long syncMillis = 0, lastOkMillis = 0, lastSyncAttempt = 0;
bool haveServer = false;

// ---- EEPROM copy of the schedule ---------------------------------------------------
const int EE_MAGIC = 0x5C;
void saveSchedule() {
  int n = min((int)sched.length(), 500);
  EEPROM.write(0, EE_MAGIC);
  EEPROM.write(1, n & 0xFF);
  EEPROM.write(2, (n >> 8) & 0xFF);
  for (int i = 0; i < n; i++) EEPROM.write(3 + i, sched[i]);
  EEPROM.write(510, minAngle);
  EEPROM.write(511, maxAngle);
}
void loadSchedule() {
  if (EEPROM.read(0) != EE_MAGIC) return;
  int n = EEPROM.read(1) | (EEPROM.read(2) << 8);
  if (n <= 0 || n > 500) return;
  sched = "";
  for (int i = 0; i < n; i++) sched += (char)EEPROM.read(3 + i);
  int lo = EEPROM.read(510), hi = EEPROM.read(511);
  if (lo < hi && hi <= 180) { minAngle = lo; maxAngle = hi; }
}

// ---- servo ------------------------------------------------------------------------
void moveServo(int angle) {
  angle = constrain(angle, minAngle, maxAngle);
  if (angle == currentAngle) return;
  dial.attach(SERVO_PIN);
  if (currentAngle < 0) {
    dial.write(angle);
    delay(600);
  } else {
    int step = angle > currentAngle ? 1 : -1;
    for (int a = currentAngle; a != angle; a += step) {   // move gently so the dial isn't jolted
      dial.write(a);
      delay(15);
    }
    dial.write(angle);
    delay(300);
  }
  dial.detach();   // stop holding: saves power and avoids buzzing; the dial stays put by friction
  currentAngle = angle;
  Serial.print("MOVED ");
  Serial.println(angle);
}

// ---- LCD ---------------------------------------------------------------------------
void showLcd(const String &a, const String &b) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(a.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(b.substring(0, 16));
}

// ---- offline schedule ------------------------------------------------------------------
int scheduledAngle(int dow, int minute) {
  for (int back = 0; back < 8; back++) {
    int d = (dow - back + 7) % 7;
    int bestStart = -1, bestAngle = -1;
    int from = 0;
    while (from < (int)sched.length()) {
      int to = sched.indexOf(',', from);
      if (to < 0) to = sched.length();
      String item = sched.substring(from, to);
      from = to + 1;
      int c1 = item.indexOf(':'), c2 = item.indexOf(':', c1 + 1), c3 = item.indexOf(':', c2 + 1);
      if (c1 < 0 || c2 < 0 || c3 < 0) continue;
      int mask = item.substring(0, c1).toInt();
      int start = item.substring(c1 + 1, c2).toInt();
      int angle = item.substring(c3 + 1).toInt();
      if (!(mask & (1 << d))) continue;
      if (back == 0 && start > minute) continue;
      if (start > bestStart) { bestStart = start; bestAngle = angle; }
    }
    if (bestAngle >= 0) return bestAngle;
  }
  return -1;
}

void runOffline() {
  if (mode != "auto" || syncMinute < 0 || sched.length() == 0) return;   // manual/away: keep last target
  unsigned long mins = (millis() - syncMillis) / 60000UL;
  long total = syncMinute + (long)mins;
  int dow = (syncDow + total / 1440) % 7;
  int minute = total % 1440;
  int a = scheduledAngle(dow, minute);
  if (a >= 0) targetAngle = a;
}

// ---- talking to the website ---------------------------------------------------------------
void applyLine(const String &line) {
  int eq = line.indexOf('=');
  if (eq < 0) return;
  String k = line.substring(0, eq), v = line.substring(eq + 1);
  if (k == "angle") targetAngle = v.toInt();
  else if (k == "min") minAngle = v.toInt();
  else if (k == "max") maxAngle = v.toInt();
  else if (k == "mode") mode = v;
  else if (k == "decision") decision = v;
  else if (k == "lcd1") lcd1 = v;
  else if (k == "lcd2") lcd2 = v;
  else if (k == "now") syncMinute = v.toInt();
  else if (k == "dow") syncDow = v.toInt();
  else if (k == "sched" && v != sched) { sched = v; saveSchedule(); }
  else if (k == "cmd") {   // "<id>:ANGLE:<angle>"
    int c1 = v.indexOf(':'), c2 = v.indexOf(':', c1 + 1);
    if (c1 > 0 && c2 > c1 && v.substring(c1 + 1, c2) == "ANGLE") {
      moveServo(v.substring(c2 + 1).toInt());
      ackId = v.substring(0, c1).toInt();
      testHoldUntil = millis() + TEST_HOLD_MS;
    }
  }
}

bool syncWithServer() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!net.connect(SECRET_SERVER_HOST, SECRET_SERVER_PORT)) return false;

  String path = "/api/device/sync?fw=" FW_VERSION "&via=wifi&rssi=" + String(WiFi.RSSI());
  if (currentAngle >= 0) path += "&angle=" + String(currentAngle);
  if (ackId >= 0) path += "&ack=" + String(ackId);

  net.print("GET " + path + " HTTP/1.1\r\n");
  net.print("Host: " SECRET_SERVER_HOST "\r\n");
  net.print("X-Device-Token: " SECRET_DEVICE_KEY "\r\n");
  net.print("Connection: close\r\n\r\n");

  unsigned long start = millis();
  bool inBody = false, ok = false;
  String line;
  while ((net.connected() || net.available()) && millis() - start < 8000) {
    if (!net.available()) { delay(5); continue; }
    char c = net.read();
    if (c == '\r') continue;
    if (c != '\n') { line += c; continue; }
    if (!inBody) {
      if (line.startsWith("HTTP/1.1 ") && !line.startsWith("HTTP/1.1 200")) break;
      if (line.length() == 0) inBody = true;
    } else {
      if (line == "ok=1") { ok = true; ackId = -1; }
      applyLine(line);
    }
    line = "";
  }
  if (inBody && line.length()) applyLine(line);
  net.stop();
  if (ok) syncMillis = millis();
  return ok;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  showLcd("CONNECTING WIFI", SECRET_WIFI_SSID);
  WiFi.begin(SECRET_WIFI_SSID, SECRET_WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(250);
}

// ---- USB serial commands (handy for testing on the bench) -------------------------------------
void handleSerial() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.startsWith("ANGLE ")) { moveServo(cmd.substring(6).toInt()); testHoldUntil = millis() + TEST_HOLD_MS; }
  else if (cmd == "PREPARE" || cmd == "PREHEAT" || cmd == "NORMAL") { decision = cmd; showLcd(lcd1, "MODE: " + cmd); }
  else if (cmd == "STATUS") {
    Serial.print("ANGLE "); Serial.print(currentAngle);
    Serial.print(" TARGET "); Serial.print(targetAngle);
    Serial.print(" WIFI "); Serial.print(WiFi.status() == WL_CONNECTED ? "OK" : "DOWN");
    Serial.print(" SERVER "); Serial.println(haveServer ? "OK" : "DOWN");
  }
  Serial.print("MODE: ");
  Serial.println(decision);
}

void setup() {
  Serial.begin(9600);
  lcd.begin(16, 2);
  showLcd("SMART HEATING", "SYSTEM READY");
  loadSchedule();
  Serial.println("SMART HEATING CONTROLLER READY");
  connectWiFi();
}

void loop() {
  handleSerial();

  if (millis() - lastSyncAttempt >= SYNC_EVERY_MS || lastSyncAttempt == 0) {
    lastSyncAttempt = millis();
    connectWiFi();
    if (syncWithServer()) {
      haveServer = true;
      lastOkMillis = millis();
      showLcd(lcd1, lcd2);
    } else if (millis() - lastOkMillis > OFFLINE_AFTER_MS || lastOkMillis == 0) {
      haveServer = false;
      runOffline();
      showLcd("OFFLINE-SCHEDULE", WiFi.status() == WL_CONNECTED ? "SERVER DOWN" : "NO WIFI");
    }
  }

  if (millis() > testHoldUntil) moveServo(targetAngle);
  delay(20);
}
