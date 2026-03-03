/*
 * =============================================================================
 *  Ping-Pong Scorer – GREEN Button Firmware
 *  Hardware: Seeed Studio XIAO ESP32-C6
 * =============================================================================
 *
 *  BEHAVIOUR
 *  ---------
 *  Boot:
 *    1. Connect to the Pi's Wi-Fi access point (SSID / password below).
 *    2. Connect to the MQTT broker running on the Pi (192.168.4.1:1883).
 *    3. Publish "connected" to status/green so the Pi knows we're ready.
 *    4. Subscribe to status/green for any acknowledgement (optional heartbeat).
 *
 *  Button events (published to "button/green"):
 *    short   – press < LONG_MS and no second press within DOUBLE_MS
 *    double  – two presses within DOUBLE_MS
 *    long    – press held >= LONG_MS
 *
 *  WIRING
 *  ------
 *  Button between GPIO D1 (pin 1) and GND.
 *  Internal pull-up enabled; LOW = pressed.
 *
 *  DEPENDENCIES  (install via Arduino Library Manager)
 *  ────────────────────────────────────────────────────
 *  - PubSubClient  by Nick O'Leary  (MQTT)
 *  - WiFi          (built-in ESP32 core)
 *
 *  BOARD PACKAGE
 *  ─────────────
 *  Seeed XIAO ESP32-C6 via:
 *  https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
 * =============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>

// ── Wi-Fi credentials (the Pi's AP) ──────────────────────────────────────────
const char* WIFI_SSID     = "PingPongScorer";   // Pi AP SSID – change if needed
const char* WIFI_PASSWORD = "pingpong123";       // Pi AP password

// ── MQTT broker (the Pi) ─────────────────────────────────────────────────────
const char* MQTT_SERVER   = "192.168.4.1";       // Pi AP gateway IP
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "button_green";
const char* TOPIC_BUTTON  = "button/green";      // we publish here
const char* TOPIC_STATUS  = "status/green";      // we publish "connected" here

// ── Button pin ────────────────────────────────────────────────────────────────
const int   BUTTON_PIN    = D1;   // GPIO on XIAO ESP32-C6; adjust if needed

// ── Press timing (milliseconds) ──────────────────────────────────────────────
const unsigned long DEBOUNCE_MS = 40;    // ignore bounces shorter than this
const unsigned long DOUBLE_MS   = 350;   // max gap between two presses for double
const unsigned long LONG_MS     = 700;   // hold duration for long press

// ── Internal state ────────────────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

// Button FSM
enum BtnState {
  IDLE,           // waiting for first press
  PRESSED,        // button is currently held
  WAIT_DOUBLE,    // released; waiting to see if a second press follows
};

BtnState     btnState       = IDLE;
unsigned long pressStart    = 0;   // millis() when button went LOW
unsigned long releaseTime   = 0;   // millis() when button went HIGH
bool         longFired      = false;  // prevent re-fire while held

// MQTT reconnect back-off
unsigned long lastReconnect  = 0;
const unsigned long RECONNECT_INTERVAL = 3000;

// ─────────────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[GREEN] Ping-Pong button booting…");

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  connectWiFi();
  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  connectMQTT();
}

// ── Wi-Fi ─────────────────────────────────────────────────────────────────────

void connectWiFi() {
  Serial.printf("[GREEN] Connecting to AP '%s'…", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long t = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
    if (millis() - t > 20000) {
      // Timeout – restart and try again
      Serial.println("\n[GREEN] Wi-Fi timeout, restarting…");
      ESP.restart();
    }
  }
  Serial.printf("\n[GREEN] Wi-Fi connected. IP: %s\n",
                WiFi.localIP().toString().c_str());
}

// ── MQTT ──────────────────────────────────────────────────────────────────────

void connectMQTT() {
  while (!mqttClient.connected()) {
    Serial.print("[GREEN] Connecting to MQTT…");
    if (mqttClient.connect(MQTT_CLIENT)) {
      Serial.println(" connected.");
      // Announce presence to the Pi
      mqttClient.publish(TOPIC_STATUS, "connected", true);  // retain=true
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
      Serial.println("[GREEN] MQTT lost, reconnecting…");
      if (WiFi.status() != WL_CONNECTED) connectWiFi();
      if (mqttClient.connect(MQTT_CLIENT)) {
        mqttClient.publish(TOPIC_STATUS, "connected", true);
        mqttClient.subscribe(TOPIC_STATUS);
      }
    }
  }
}

// Incoming MQTT messages (optional: Pi could send commands back)
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // Currently unused on the button side
}

// ── Publish helper ────────────────────────────────────────────────────────────

void publishEvent(const char* event) {
  Serial.printf("[GREEN] → %s\n", event);
  if (mqttClient.connected()) {
    mqttClient.publish(TOPIC_BUTTON, event);
  } else {
    Serial.println("[GREEN] MQTT not connected; event dropped.");
  }
}

// ── Button state machine ──────────────────────────────────────────────────────
/*
 * We use a non-blocking FSM (no delay() calls) so the MQTT loop stays healthy.
 *
 *  IDLE:
 *    LOW detected → record pressStart, go to PRESSED
 *
 *  PRESSED:
 *    Still LOW + held >= LONG_MS → fire "long", set longFired=true
 *    HIGH detected (release):
 *      if longFired → reset to IDLE  (already fired)
 *      else         → record releaseTime, go to WAIT_DOUBLE
 *
 *  WAIT_DOUBLE:
 *    LOW detected within DOUBLE_MS → fire "double", go to IDLE
 *    DOUBLE_MS elapsed without press → fire "short", go to IDLE
 */

void handleButton() {
  bool pressed = (digitalRead(BUTTON_PIN) == LOW);
  unsigned long now = millis();

  switch (btnState) {

    case IDLE:
      if (pressed) {
        delay(DEBOUNCE_MS);   // simple debounce at leading edge
        if (digitalRead(BUTTON_PIN) == LOW) {
          pressStart  = millis();
          longFired   = false;
          btnState    = PRESSED;
        }
      }
      break;

    case PRESSED:
      if (!longFired && (now - pressStart >= LONG_MS)) {
        publishEvent("long");
        longFired = true;
        // Stay in PRESSED until release to swallow the button-up event
      }
      if (!pressed) {
        if (longFired) {
          btnState = IDLE;   // already handled
        } else {
          releaseTime = now;
          btnState    = WAIT_DOUBLE;
        }
      }
      break;

    case WAIT_DOUBLE:
      if (pressed) {
        // Second press within window → double
        delay(DEBOUNCE_MS);
        if (digitalRead(BUTTON_PIN) == LOW) {
          publishEvent("double");
          // Wait for release before going IDLE
          while (digitalRead(BUTTON_PIN) == LOW) {
            mqttClient.loop();
            delay(10);
          }
          btnState = IDLE;
        }
      } else if (now - releaseTime >= DOUBLE_MS) {
        // Window expired → single short press
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
