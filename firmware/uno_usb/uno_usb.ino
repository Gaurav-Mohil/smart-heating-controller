/*
  Smart Heating Controller — firmware for the current Arduino Uno (no Wi-Fi)

  The Uno is driven over USB by bridge/serial_bridge.py running on a computer
  that has internet. When you change the temperature on the website, the bridge
  sends SET and the servo turns the thermostat dial within a few seconds.
  It still understands the original PREPARE / NORMAL / PREHEAT commands.

  Serial commands (9600 baud, one per line):
    SET <angle> <min> <max>   move the dial servo, never past min/max
    ANGLE <angle>             servo test within the stored limits
    TEMP                      reply with the indoor temperature ("OK TEMP 21.4" or "OK TEMP NONE")
    LCD1 <text> / LCD2 <text> set a line of the 16x2 display
    PREPARE | NORMAL | PREHEAT   original mode commands
    STATUS                    reply with the current angle

  Wiring: LCD RS D12, E D11, D4 D7, D5 D6, D6 D5, D7 D4, RW GND.
          SG90 brown GND, red 5V, orange D9. D2 is not used.
          Optional temperature sensor: see SENSOR_TYPE below.
*/
#include <LiquidCrystal.h>
#include <Servo.h>

// ---- Optional indoor temperature sensor ----------------------------------------
// Set SENSOR_TYPE to the sensor you have, then upload again.
//   SENSOR_NONE        no sensor (the website shows "—")
//   SENSOR_DHT11       blue DHT11 module:   S/data -> D3, + -> 5V, - -> GND
//   SENSOR_DHT22       white DHT22 module:  same wiring as DHT11
//     (DHT sensors need the "DHT sensor library" by Adafruit: Sketch -> Include Library -> Manage Libraries)
//   SENSOR_TMP36       TMP36 (flat side facing you: left 5V, middle -> A0, right GND)
//   SENSOR_LM35        LM35  (flat side facing you: left 5V, middle -> A0, right GND)
//   SENSOR_THERMISTOR  10k thermistor module from Arduino/Elegoo kits: S -> A0, + -> 5V, - -> GND
#define SENSOR_NONE 0
#define SENSOR_DHT11 1
#define SENSOR_DHT22 2
#define SENSOR_TMP36 3
#define SENSOR_LM35 4
#define SENSOR_THERMISTOR 5

#define SENSOR_TYPE SENSOR_NONE

const int DHT_PIN = 3;
const int ANALOG_TEMP_PIN = A0;

#if SENSOR_TYPE == SENSOR_DHT11 || SENSOR_TYPE == SENSOR_DHT22
#include <DHT.h>
DHT dht(DHT_PIN, SENSOR_TYPE == SENSOR_DHT11 ? DHT11 : DHT22);
#endif

const int SERVO_PIN = 9;
LiquidCrystal lcd(12, 11, 7, 6, 5, 4);
Servo dial;

int currentAngle = -1;
int minAngle = 60, maxAngle = 120;
String line1 = "SMART HEATING", line2 = "SYSTEM READY";

float analogAverage() {
  long sum = 0;
  for (int i = 0; i < 8; i++) {
    sum += analogRead(ANALOG_TEMP_PIN);
    delay(2);
  }
  return sum / 8.0;
}

// Returns the indoor temperature in °C, or NAN if there is no sensor or it failed.
float readTemperature() {
#if SENSOR_TYPE == SENSOR_DHT11 || SENSOR_TYPE == SENSOR_DHT22
  return dht.readTemperature();
#elif SENSOR_TYPE == SENSOR_TMP36
  float volts = analogAverage() * 5.0 / 1023.0;
  return (volts - 0.5) * 100.0;
#elif SENSOR_TYPE == SENSOR_LM35
  return analogAverage() * 500.0 / 1023.0;
#elif SENSOR_TYPE == SENSOR_THERMISTOR
  // Steinhart-Hart equation for the kit's 10k NTC thermistor.
  // If the reading goes DOWN when you warm the sensor with your fingers, swap its + and - wires.
  float raw = analogAverage();
  if (raw < 1 || raw > 1022) return NAN;
  double logR = log(10000.0 * (1023.0 / raw - 1.0));
  double kelvin = 1.0 / (0.001129148 + (0.000234125 + 0.0000000876741 * logR * logR) * logR);
  return kelvin - 273.15;
#else
  return NAN;
#endif
}

void printLine(int row, String text) {
  // Overwrite in place (padded to 16 characters) instead of clearing, so the display doesn't flicker.
  while (text.length() < 16) text += ' ';
  lcd.setCursor(0, row);
  lcd.print(text.substring(0, 16));
}

void showLcd() {
  printLine(0, line1);
  printLine(1, line2);
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
      for (int a = currentAngle; a != angle; a += step) {   // turn gently so the dial isn't jolted
        dial.write(a);
        delay(15);
      }
      dial.write(angle);
      delay(300);
    }
    dial.detach();   // release: no buzzing, less power; the dial stays put by friction
    currentAngle = angle;
  }
  Serial.print("OK ANGLE ");
  Serial.println(currentAngle);
}

void setup() {
  Serial.begin(9600);
  lcd.begin(16, 2);
#if SENSOR_TYPE == SENSOR_DHT11 || SENSOR_TYPE == SENSOR_DHT22
  dht.begin();
#endif
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
  } else if (cmd == "TEMP") {
    float t = readTemperature();
    Serial.print("OK TEMP ");
    if (isnan(t)) Serial.println("NONE");
    else Serial.println(t, 1);
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
