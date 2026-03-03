/*
 * =============================================================================
 *  Ping-Pong Scorer – BLUE Button Firmware
 *  Hardware: Seeed Studio XIAO ESP32-C6
 * =============================================================================
 *
 *  Identical logic to the GREEN button; only the MQTT client ID,
 *  topics, and serial labels differ.
 *
 *  BEHAVIOUR
 *  ---------
 *  Boot:
 *    1. Connect to the Pi's Wi-Fi access point.
 *    2. Connect to the MQTT broker on the Pi (192.168.4.1:1883).
 *    3. Publish "connected" to status/blue.
 *
 *  Button events (published to "button/blue"):
 *    short   – single tap
 *    double  – two taps within DOUBLE_MS
 *    long    – held >= LONG_MS
 *
 *  WIRING
 *  ------
 *  Button between GPIO D1 and GND. Internal pull-up; LOW = pressed.
 *
 *  DEPENDENCIES  (Arduino Library Manager)
 *  ────────────────────────────────────────
 *  - PubSubClient  by Nick O'Leary
 *  - WiFi (built-in)
 *
 *  BOARD PACKAGE
 *  ─────────────
 *  Seeed XIAO ESP32-C6 package URL:
 *  https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
 * =============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>

// ── Wi-Fi credentials (Pi's AP) ───────────────────────────────────────────────
const char* WIFI_SSID     = "PingPongScorer";
const char* WIFI_PASSWORD = "pingpong123";

// ── MQTT broker (Pi's AP gateway IP) ─────────────────────────────────────────
const char* MQTT_SERVER   = "192.168.4.1";
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "button_blue";
const char* TOPIC_BUTTON  = "button/blue";
const char* TOPIC_STATUS  = "status/blue";

// ── Button pin ────────────────────────────────────────────────────────────────
const int   BUTTON_PIN    = D1;

// ── Timing ───────────────────────────────────────────────────────────────────
const unsigned long DEBOUNCE_MS = 40;
const unsigned long DOUBLE_MS   = 350;
const unsigned long LONG_MS     = 700;

// ── State ─────────────────────────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

enum BtnState { IDLE, PRESSED, WAIT_DOUBLE };
BtnState      btnState      = IDLE;
unsigned long pressStart    = 0;
unsigned long releaseTime   = 0;
bool          longFired     = false;

unsigned long lastReconnect = 0;
const unsigned long RECONNECT_INTERVAL = 3000;

// ─────────────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[BLUE] Ping-Pong button booting…");

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  connectWiFi();
  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  connectMQTT();
}

// ── Wi-Fi ─────────────────────────────────────────────────────────────────────

void connectWiFi() {
  Serial.printf("[BLUE] Connecting to AP '%s'…", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long t = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
    if (millis() - t > 20000) {
      Serial.println("\n[BLUE] Wi-Fi timeout, restarting…");
      ESP.restart();
    }
  }
  Serial.printf("\n[BLUE] Wi-Fi connected. IP: %s\n",
                WiFi.localIP().toString().c_str());
}

// ── MQTT ──────────────────────────────────────────────────────────────────────

void connectMQTT() {
  while (!mqttClient.connected()) {
    Serial.print("[BLUE] Connecting to MQTT…");
    if (mqttClient.connect(MQTT_CLIENT)) {
      Serial.println(" connected.");
      mqttClient.publish(TOPIC_STATUS, "connected", true);
      mqttClient.subscribe(TOPIC_STATUS);
    } else {
      Serial.printf(" failed (rc=%d), retry in 3s\n", mqttClient.state());
      delay(3000);
    }
  }
}

void ensureMQTT() {
  if (!mqttClient.connected()) {
    unsigned long now = millis();
    if (now - lastReconnect > RECONNECT_INTERVAL) {
      lastReconnect = now;
      Serial.println("[BLUE] MQTT lost, reconnecting…");
      if (WiFi.status() != WL_CONNECTED) connectWiFi();
      if (mqttClient.connect(MQTT_CLIENT)) {
        mqttClient.publish(TOPIC_STATUS, "connected", true);
        mqttClient.subscribe(TOPIC_STATUS);
      }
    }
  }
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // Reserved for future use (e.g., haptic feedback commands from Pi)
}

// ── Publish ───────────────────────────────────────────────────────────────────

void publishEvent(const char* event) {
  Serial.printf("[BLUE] → %s\n", event);
  if (mqttClient.connected()) {
    mqttClient.publish(TOPIC_BUTTON, event);
  } else {
    Serial.println("[BLUE] MQTT not connected; event dropped.");
  }
}

// ── Button FSM ────────────────────────────────────────────────────────────────
/*
 * Non-blocking press detector. See green button comments for full description.
 *
 *  IDLE       → LOW detected            → PRESSED
 *  PRESSED    → held >= LONG_MS         → publish "long"
 *  PRESSED    → release (no long)       → WAIT_DOUBLE
 *  WAIT_DOUBLE → second LOW in window   → publish "double" → IDLE
 *  WAIT_DOUBLE → window expires         → publish "short"  → IDLE
 */

void handleButton() {
  bool pressed = (digitalRead(BUTTON_PIN) == LOW);
  unsigned long now = millis();

  switch (btnState) {

    case IDLE:
      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          pressStart = millis();
          longFired  = false;
          btnState   = PRESSED;
        }
      }
      break;

    case PRESSED:
      if (!longFired && (now - pressStart >= LONG_MS)) {
        publishEvent("long");
        longFired = true;
      }
      if (!pressed) {
        if (longFired) {
          btnState = IDLE;
        } else {
          releaseTime = now;
          btnState    = WAIT_DOUBLE;
        }
      }
      break;

    case WAIT_DOUBLE:
      if (pressed) {
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          publishEvent("double");
          while (digitalRead(BUTTON_PIN) == LOW) {
            mqttClient.loop();
            delay(10);
          }
          btnState = IDLE;
        }
      } else if (now - releaseTime >= DOUBLE_MS) {
        publishEvent("short");
        btnState = IDLE;
      }
      break;
  }
}

// ── Main loop ─────────────────────────────────────────────────────────────────

void loop() {
  ensureMQTT();
  mqttClient.loop();
  handleButton();
}
