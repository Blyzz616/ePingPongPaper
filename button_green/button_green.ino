/*
 * =============================================================================
 *  Ping-Pong Scorer — GREEN Button Firmware  v5.0 (stable OTA + static IP)
 *  Hardware: Seeed Studio XIAO ESP32-C6
 * =============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoOTA.h>

// ── Credentials ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "pingpong";
const char* WIFI_PASSWORD = "";  // Be sure to update your password here

const char* MQTT_SERVER   = "10.11.12.1";
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "button_green";

const char* TOPIC_BUTTON  = "button/green";
const char* TOPIC_STATUS  = "status/green";
const char* TOPIC_HB      = "heartbeat/green";
const char* TOPIC_BAT     = "battery/green";

// ── Static IP ( /29 network ) ────────────────────────────────────────────────
IPAddress local_IP(10, 11, 12, 2);
IPAddress gateway(10, 11, 12, 1);
IPAddress subnet(255, 255, 255, 248);

// ── Pins ──────────────────────────────────────────────────────────────────────
const int BUTTON_PIN  = D1;
const int SPEAKER_PIN = D2;
const int BAT_ADC_PIN = A0;

// ── Feature flags ─────────────────────────────────────────────────────────────
#define BAT_ADC_ENABLED true

// ── Timing ────────────────────────────────────────────────────────────────────
const unsigned long DEBOUNCE_MS       = 40;
const unsigned long TAP_GAP_MS        = 500;
const unsigned long MQTT_RECONNECT_MS = 3000;
const unsigned long HEARTBEAT_MS      = 5000;
const unsigned long BATTERY_MS        = 30000;

// ── State ─────────────────────────────────────────────────────────────────────
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long lastMqttAttempt = 0;
unsigned long lastHeartbeat   = 0;
unsigned long lastBattery     = 0;

enum BtnState { IDLE, PRESSED, WAIT_NEXT };
BtnState btnState = IDLE;

int tapCount = 0;
unsigned long lastReleaseMs = 0;

bool otaStarted = false;

// =============================================================================
// BEEP
// =============================================================================
void beep(int freq = 2000, int durationMs = 80) {
  tone(SPEAKER_PIN, freq, durationMs);
}

// =============================================================================
// BATTERY
// =============================================================================
int readBatteryPercent() {
#if BAT_ADC_ENABLED
  long sum = 0;
  for (int i = 0; i < 8; i++) {
    sum += analogReadMilliVolts(BAT_ADC_PIN);
    delay(2);
  }
  int mv = (sum / 8) * 2;
  int pct = (int)((mv - 3000) * 100.0 / (4200 - 3000));
  return constrain(pct, 0, 100);
#else
  return -1;
#endif
}

// =============================================================================
// WIFI (STATIC IP)
// =============================================================================
void initWiFi() {
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);

  if (!WiFi.config(local_IP, gateway, subnet)) {
    Serial.println("[WiFi] static IP config failed");
  }

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.println("[WiFi] connecting...");
}

// =============================================================================
// OTA (SAFE START)
// =============================================================================
void startOTA() {
  ArduinoOTA.setHostname(MQTT_CLIENT);
  ArduinoOTA.setPassword(""); // Be sure to update your OTA password here.

  ArduinoOTA.onStart([]() {
    Serial.println("[OTA] start");
  });

  ArduinoOTA.onEnd([]() {
    Serial.println("[OTA] end");
  });

  ArduinoOTA.onError([](ota_error_t error) {
    Serial.printf("[OTA] error %u\n", error);
  });

  ArduinoOTA.begin();
  otaStarted = true;

  Serial.println("[OTA] enabled");
}

// =============================================================================
// MQTT
// =============================================================================
void mqttCallback(char* topic, byte* payload, unsigned int len) {}

void ensureMQTT() {
  if (mqtt.connected()) return;
  if (WiFi.status() != WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RECONNECT_MS) return;

  lastMqttAttempt = now;

  if (mqtt.connect(MQTT_CLIENT)) {
    mqtt.publish(TOPIC_STATUS, "connected", true);
    Serial.println("[MQTT] connected");
  }
}

void publishEvent(const char* ev) {
  Serial.printf("[BTN] %s\n", ev);
  if (mqtt.connected()) mqtt.publish(TOPIC_BUTTON, ev);
}

// =============================================================================
// PERIODIC TASKS
// =============================================================================
void sendHeartbeat() {
  if (mqtt.connected()) mqtt.publish(TOPIC_HB, "ok");
}

void sendBattery() {
  if (!mqtt.connected()) return;

  char buf[8];
  itoa(readBatteryPercent(), buf, 10);
  mqtt.publish(TOPIC_BAT, buf);
}

// =============================================================================
// BUTTON FSM
// =============================================================================
void dispatchTaps() {
  if      (tapCount == 1) publishEvent("short");
  else if (tapCount == 2) publishEvent("double");
  else if (tapCount == 3) publishEvent("double");
  else if (tapCount >= 4) publishEvent("reset");
  tapCount = 0;
}

void handleButton() {
  bool pressed = (digitalRead(BUTTON_PIN) == LOW);
  unsigned long now = millis();

  switch (btnState) {

    case IDLE:
      if (tapCount > 0 && (now - lastReleaseMs) > TAP_GAP_MS) {
        dispatchTaps();
      }

      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          beep(500, 80);
          btnState = PRESSED;
        }
      }
      break;

    case PRESSED:
      if (!pressed) {
        tapCount++;
        lastReleaseMs = millis();
        btnState = WAIT_NEXT;
      }
      break;

    case WAIT_NEXT:
      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          beep(500, 80);
          btnState = PRESSED;
        }
      } else if (now - lastReleaseMs > TAP_GAP_MS) {
        dispatchTaps();
        btnState = IDLE;
      }
      break;
  }
}

// =============================================================================
// SETUP / LOOP
// =============================================================================
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(SPEAKER_PIN, OUTPUT);

  delay(500);
  beep(500, 100);

  initWiFi();

  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
}

void loop() {
  mqtt.loop();
  handleButton();

  ensureMQTT();

  // OTA starts only AFTER WiFi is stable
  if (!otaStarted &&
      WiFi.status() == WL_CONNECTED &&
      millis() > 10000) {
    startOTA();
  }

  if (otaStarted) {
    ArduinoOTA.handle();
  }

  unsigned long now = millis();

  if (now - lastHeartbeat > HEARTBEAT_MS) {
    lastHeartbeat = now;
    sendHeartbeat();
  }

  if (now - lastBattery > BATTERY_MS) {
    lastBattery = now;
    sendBattery();
  }
}
