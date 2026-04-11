/*
 * =============================================================================
 *  Ping-Pong Scorer — GREEN Button Firmware  v3.0
 *  Hardware: Seeed Studio XIAO ESP32-C6
 * =============================================================================
 *
 *  WIRING
 *  ------
 *  Button  : D1 → GND  (internal pull-up, LOW = pressed)
 *  Speaker : D2 → GND  (passive buzzer, beeps on every press)
 *  Battery : BAT+ → 100kΩ → A0 → 100kΩ → GND  (voltage divider)
 *            Set BAT_ADC_ENABLED false if divider not wired.
 *
 *  BUTTON EVENTS  (published to "button/green")
 *  ─────────────────────────────────────────────
 *  short   — 1 tap, gap expired
 *  double  — 2 taps within TAP_GAP_MS (undo)
 *  reset   — 4 taps within TAP_GAP_MS each (full reset → jumps to rule select)
 *  (3 taps treated as double)
 *
 *  PUBLISHED TOPICS
 *  ─────────────────
 *  button/green     — short / double / reset
 *  status/green     — "connected" (retained, on every WiFi reconnect)
 *  heartbeat/green  — "ok" every HEARTBEAT_MS
 *  battery/green    — "85" (percent, 0-100, or "-1" if not wired)
 *
 *  DEPENDENCIES
 *  ─────────────
 *  PubSubClient by Nick O'Leary  (Arduino Library Manager)
 *
 *  BOARD PACKAGE
 *  ─────────────
 *  https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
 *  → Seeed XIAO ESP32C6
 * =============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>

// ── Credentials ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "pingpong";
const char* WIFI_PASSWORD = "";          // ← your AP password
const char* MQTT_SERVER   = "10.11.12.1";
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "button_green";
const char* TOPIC_BUTTON  = "button/green";
const char* TOPIC_STATUS  = "status/green";
const char* TOPIC_HB      = "heartbeat/green";
const char* TOPIC_BAT     = "battery/green";

// ── Pins ──────────────────────────────────────────────────────────────────────
const int BUTTON_PIN  = D1;
const int SPEAKER_PIN = D2;
const int BAT_ADC_PIN = A0;

// ── Feature flags ─────────────────────────────────────────────────────────────
#define BAT_ADC_ENABLED true    // set false if voltage divider not wired

// ── Timing (ms) ───────────────────────────────────────────────────────────────
const unsigned long DEBOUNCE_MS       =   40;
const unsigned long TAP_GAP_MS        =  500;  // max gap between taps in sequence
const unsigned long MQTT_RECONNECT_MS = 3000;
const unsigned long HEARTBEAT_MS      = 5000;
const unsigned long BATTERY_MS        = 30000; // send battery every 30s

// ── MQTT state ────────────────────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);
volatile bool wifiJustConnected = false;
unsigned long lastMqttAttempt   = 0;
unsigned long lastHeartbeat     = 0;
unsigned long lastBattery       = 0;

// ── Button FSM ────────────────────────────────────────────────────────────────
enum BtnState { IDLE, PRESSED, WAIT_NEXT };
BtnState      btnState       = IDLE;
int           tapCount       = 0;
unsigned long lastReleaseMs  = 0;

// =============================================================================
//  BEEP
// =============================================================================

void beep(int freq = 2000, int durationMs = 80) {
  tone(SPEAKER_PIN, freq, durationMs);
}

// =============================================================================
//  BATTERY
// =============================================================================

int readBatteryPercent() {
#if BAT_ADC_ENABLED
  // Voltage divider: BAT+ → 100kΩ → A0 → 100kΩ → GND
  // Measured voltage = battery voltage / 2
  // analogReadMilliVolts returns 0-3300mV for the ADC input
  int mv = analogReadMilliVolts(BAT_ADC_PIN) * 2;
  // LiPo: 3000mV = 0%,  4200mV = 100%
  int pct = (int)((mv - 3000) * 100.0 / (4200 - 3000));
  return constrain(pct, 0, 100);
#else
  return -1;  // not wired
#endif
}

// =============================================================================
//  Wi-Fi
// =============================================================================

void onWifiEvent(WiFiEvent_t event) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      Serial.println("[GREEN] WiFi: associated.");
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      Serial.printf("[GREEN] WiFi: IP %s\n", WiFi.localIP().toString().c_str());
      wifiJustConnected = true;
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      Serial.println("[GREEN] WiFi: disconnected — auto-retry.");
      break;
    default: break;
  }
}

void initWiFi() {
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.onEvent(onWifiEvent);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[GREEN] WiFi: connecting to '%s'…\n", WIFI_SSID);
}

// =============================================================================
//  MQTT
// =============================================================================

void mqttCallback(char* topic, byte* payload, unsigned int len) {}

void announceConnected() {
  mqtt.publish(TOPIC_STATUS, "connected", true);  // retained
  Serial.println("[GREEN] MQTT: announced connected.");
}

void ensureMQTT() {
  if (mqtt.connected()) return;
  if (WiFi.status() != WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RECONNECT_MS) return;
  lastMqttAttempt = now;
  Serial.print("[GREEN] MQTT: connecting…");
  if (mqtt.connect(MQTT_CLIENT)) {
    Serial.println(" OK.");
    announceConnected();
  } else {
    Serial.printf(" failed (rc=%d).\n", mqtt.state());
  }
}

void publishEvent(const char* ev) {
  Serial.printf("[GREEN] → %s\n", ev);
  if (mqtt.connected()) mqtt.publish(TOPIC_BUTTON, ev);
  else Serial.println("[GREEN] MQTT not connected — dropped.");
}

// =============================================================================
//  PERIODIC TASKS
// =============================================================================

void sendHeartbeat() {
  if (!mqtt.connected()) return;
  mqtt.publish(TOPIC_HB, "ok");
}

void sendBattery() {
  if (!mqtt.connected()) return;
  int pct = readBatteryPercent();
  char buf[8];
  itoa(pct, buf, 10);
  mqtt.publish(TOPIC_BAT, buf);
  Serial.printf("[GREEN] Battery: %d%%\n", pct);
}

// =============================================================================
//  BUTTON FSM
// =============================================================================
/*
 *  Tap counting:
 *    1 tap  → "short"
 *    2 taps → "double"  (undo)
 *    3 taps → "double"  (treat as double, discard extra)
 *    4 taps → "reset"   (full reset, jumps to rule selection)
 *
 *  Each tap must begin within TAP_GAP_MS of the previous release.
 *  Beep fires immediately on each button press-down for feedback.
 */

void dispatchTaps() {
  if      (tapCount == 1) publishEvent("short");
  else if (tapCount == 2) publishEvent("double");
  else if (tapCount == 3) publishEvent("double");  // 3-tap = double
  else if (tapCount >= 4) publishEvent("reset");
  tapCount = 0;
}

void handleButton() {
  bool pressed = (digitalRead(BUTTON_PIN) == LOW);
  unsigned long now = millis();

  switch (btnState) {

    case IDLE:
      // Expire tap window while idle
      if (tapCount > 0 && (now - lastReleaseMs) >= TAP_GAP_MS) {
        dispatchTaps();
      }
      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          beep();
          btnState = PRESSED;
        }
      }
      break;

    case PRESSED:
      if (!pressed) {
        tapCount++;
        lastReleaseMs = millis();
        if (tapCount >= 4) {
          publishEvent("reset");
          tapCount = 0;
          btnState = IDLE;
        } else {
          btnState = WAIT_NEXT;
        }
      }
      break;

    case WAIT_NEXT:
      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          beep();
          btnState = PRESSED;
        }
      } else if (now - lastReleaseMs >= TAP_GAP_MS) {
        dispatchTaps();
        btnState = IDLE;
      }
      break;
  }
}

// =============================================================================
//  Setup / Loop
// =============================================================================

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[GREEN] Ping-Pong button v3.0 booting…");

  pinMode(BUTTON_PIN,  INPUT_PULLUP);
  pinMode(SPEAKER_PIN, OUTPUT);

  // startup beep
  delay(500);
  beep(1500, 150);

  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);

  initWiFi();
}

void loop() {
  if (wifiJustConnected) {
    wifiJustConnected = false;
    lastMqttAttempt   = 0;
  }

  ensureMQTT();
  mqtt.loop();
  handleButton();

  unsigned long now = millis();

  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    sendHeartbeat();
  }

  if (now - lastBattery >= BATTERY_MS) {
    lastBattery = now;
    sendBattery();
  }
}
