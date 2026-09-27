/*
  Smart Heating Controller — firmware for the current Arduino Uno (no Wi-Fi)

  The Uno is driven over USB by bridge/serial_bridge.py running on a computer
  that has internet. It still understands the original PREPARE / NORMAL /
  PREHEAT commands, so the old weather_controller.py keeps working.

  Serial commands (9600 baud, one per line):
    SET <angle> <min> <max>   move the dial servo, never past min/max
    ANGLE <angle>             servo test within the stored limits
    LCD1 <text> / LCD2 <text> set a line of the 16x2 display
    PREPARE | NORMAL | PREHEAT   original mode commands
    STATUS                    reply with the current angle

  Wiring: LCD RS D12, E D11, D4 D7, D5 D6, D6 D5, D7 D4, RW GND.
          SG90 brown GND, red 5V, orange D9. D2 is not used.
*/
#include <LiquidCrystal.h>
#include <Servo.h>

const int SERVO_PIN = 9;
LiquidCrystal lcd(12, 11, 7, 6, 5, 4);
Servo dial;

int currentAngle = -1;
int minAngle = 60, maxAngle = 120;
String line1 = "SMART HEATING", line2 = "SYSTEM READY";

void showLcd() {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
}

void moveServo(int angle) {
  angle = constrain(angle, minAngle, maxAngle);
  if (angle != currentAngle) {
    dial.attach(SERVO_PIN);
    if (currentAngle < 0) {
      dial.write(angle);
      delay(600);
    } else {
      int step = angle > currentAngle ? 1 : -1;
      for (int a = currentAngle; a != angle; a += step) {
        dial.write(a);
        delay(15);
      }
      dial.write(angle);
      delay(300);
    }
    dial.detach();
    currentAngle = angle;
  }
  Serial.print("OK ANGLE ");
  Serial.println(currentAngle);
}

void setup() {
  Serial.begin(9600);
  lcd.begin(16, 2);
  showLcd();
  Serial.println("SMART HEATING CONTROLLER READY");
}

void loop() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd.startsWith("SET ")) {
    int a, lo, hi;
    if (sscanf(cmd.c_str() + 4, "%d %d %d", &a, &lo, &hi) == 3 && lo < hi && lo >= 0 && hi <= 180) {
      minAngle = lo;
      maxAngle = hi;
      moveServo(a);
    } else {
      Serial.println("ERR SET");
    }
  } else if (cmd.startsWith("ANGLE ")) {
    moveServo(cmd.substring(6).toInt());
  } else if (cmd.startsWith("LCD1 ")) {
    line1 = cmd.substring(5);
    showLcd();
    Serial.println("OK LCD");
  } else if (cmd.startsWith("LCD2 ")) {
    line2 = cmd.substring(5);
    showLcd();
    Serial.println("OK LCD");
  } else if (cmd == "PREPARE" || cmd == "NORMAL" || cmd == "PREHEAT") {
    line2 = "MODE: " + cmd;
    showLcd();
    Serial.print("MODE: ");
    Serial.println(cmd);
  } else if (cmd == "STATUS") {
    Serial.print("OK ANGLE ");
    Serial.println(currentAngle);
  } else {
    Serial.println("ERR UNKNOWN");
  }
}
