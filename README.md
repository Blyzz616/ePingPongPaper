# 🏓 PingPong Scorer

> A wireless, battery-powered ping-pong scoring system built around a Raspberry Pi 3, a TV display, and two ESP32-C6 score buttons.

---

## How It Works

Two large recordable buttons — one green, one blue — connect wirelessly over Wi-Fi directly to a Raspberry Pi 3. The Pi runs a local MQTT broker, hosts the game logic, and serves a real-time scoreboard web page to a TV via HDMI. Every button press is published as an MQTT event; the scorer updates the TV instantly over WebSocket with no perceptible delay.

The Pi acts as its own Wi-Fi access point, so the whole system is completely self-contained — no router, no internet connection required at the venue.

```
[Green Button]──┐
   ESP32-C6     │  Wi-Fi / MQTT        ┌──────────────────────────────┐
                ├────────────────────▶│     Raspberry Pi 3           │
[Blue Button]───┘                      │  hostapd AP + Mosquitto MQTT │
   ESP32-C6                            │  Flask/SocketIO scorer       │──▶ TV (HDMI)
                                       │  Firefox kiosk               │
                                       └──────────────────────────────┘
```

---

## Hardware

### Raspberry Pi 3 Model B / B+

The brains of the system. Runs Raspberry Pi OS (Debian Trixie), hosts a Mosquitto MQTT broker, runs the Flask/SocketIO scoring server, and drives a TV in kiosk mode via HDMI.

| Spec | Detail |
|---|---|
| CPU | 1.2 GHz quad-core ARM Cortex-A53 |
| RAM | 1 GB |
| Wi-Fi | 802.11 b/g/n (2.4 GHz) — used as the AP |
| Display | HDMI → TV or monitor |
| Power | Micro USB, 5V |

---

### Seeed Studio XIAO ESP32-C6 *(×2 — one per button)*

<img src="https://files.seeedstudio.com/wiki/SeeedStudio-XIAO-ESP32C6/img/xiaoc6.jpg" alt="Seeed Studio XIAO ESP32-C6" width="360"/>

An ultra-compact Wi-Fi module soldered inside each score button. Connects to the Pi's access point on boot, then publishes press events over MQTT. Also sends a heartbeat every 5 seconds and a battery voltage reading every 30 seconds.

| Spec | Detail |
|---|---|
| MCU | ESP32-C6 (RISC-V, 160 MHz) |
| Wi-Fi | 802.11 b/g/n (2.4 GHz) |
| Size | 21 × 17.5 mm |
| GPIO | 11 digital I/O |
| Power | 3.7V LiPo via onboard charger circuit |

- [Seeed Studio product page](https://www.seeedstudio.com/Seeed-Studio-XIAO-ESP32C6-p-5884.html)

---

### Score Buttons *(×2)*

<img width="887" height="805" alt="image" src="https://github.com/user-attachments/assets/2fbcae6a-5147-4c4e-a297-c42e77e6b4de" alt="Recordable Talking Button" width="300" />

Large 85 mm recordable talking buttons with built-in LEDs — one green, one blue. The original AAA battery compartment houses the ESP32-C6 and LiPo battery. The original button mechanism is wired to the ESP32's GPIO. A passive speaker on D2 beeps on every press for tactile confirmation.

| Spec | Detail |
|---|---|
| Diameter | 85 × 85 mm |
| Height | 35 mm |
| Material | ABS plastic |
| LED | Built-in, colour-matched |
| Speaker | Passive buzzer on GPIO D2 |

- [Amazon.ca listing](https://www.amazon.ca/Recordable-Talking-Button-Recording-Bright/dp/B0FLNVCPL3)

---

### Batteries *(×2 — one per button)*

<img width="1198" height="1030" alt="image" src="https://github.com/user-attachments/assets/6e42a5bc-edb4-4daa-b363-187f18e5bab4" alt="HAWK'S WORK LiPo Battery" width="300"/>

Each button runs from a 3.7V LiPo cell. To recharge, remove the ESP32 from the button housing and charge the battery via USB. The ESP32 monitors supply voltage via ADC and reports approximate battery level to the scoreboard.

| Spec | Detail |
|---|---|
| Voltage | 3.7V |
| Capacity | 550 mAh |
| Connector | XH 2.54 |
| Size | 47 × 20.7 × 7.5 mm |
| Protection IC | Built-in (over-charge / over-discharge) |

- [Amazon.ca listing](https://www.amazon.ca/HAWKS-WORK-Rechargeable-Helicopter-Connector/dp/B0DHX1KVFX/)

---

## Button Wiring

```
XIAO ESP32-C6 D1 ──┤ button mechanism ├── GND   (internal pull-up, LOW = pressed)
XIAO ESP32-C6 D2 ──┤ passive speaker  ├── GND   (beeps on press)
XIAO ESP32-C6 A0 ──┤ voltage divider  ├── GND   (battery monitoring)
                    100kΩ to BAT+, 100kΩ to GND
```

---

## Files

| File | Purpose |
|---|---|
| `pingpong_server.py` | Main Pi scoring server (Flask + SocketIO + MQTT + SQLite) |
| `templates/scoreboard.html` | TV scoreboard web page (served by Flask) |
| `setup.sh` | One-run Pi setup script |
| `button_green/button_green.ino` | Arduino sketch for the green ESP32-C6 |
| `button_blue/button_blue.ino` | Arduino sketch for the blue ESP32-C6 |

---

## Network

| Parameter | Value |
|---|---|
| AP SSID | `pingpong` |
| AP password | Set during `setup.sh` |
| Pi IP | `10.11.12.1` |
| Subnet | `10.11.12.0/29` (6 usable hosts) |
| MQTT broker | `10.11.12.1:1883` |
| Scoreboard URL (on Pi) | `http://localhost:5000` |
| Scoreboard URL (on network) | `http://10.11.12.1:5000` |

---

## Pi Setup

### 1 — Flash Pi OS

Use **Raspberry Pi OS with Desktop** (32-bit or 64-bit, Bookworm or Trixie). Enable SSH during imaging.

### 2 — Run the Setup Script

Copy all project files to the Pi, then:

```bash
sudo bash setup.sh
```

The script will:
- Ask for a Wi-Fi hotspot password (flash this same password into the ESP32s)
- Install hostapd, dnsmasq, Mosquitto, Python venv, Firefox ESR
- Configure `wlan0` as a Wi-Fi AP (`pingpong` / your password) at `10.11.12.1/29`
- Create a `pingpong.service` systemd unit that starts the server on boot
- Configure auto-login and Firefox kiosk mode on the HDMI output

### 3 — Reboot

```bash
sudo reboot
```

After reboot the Pi broadcasts the `pingpong` Wi-Fi network, starts the scoring server, and opens the scoreboard full-screen on the TV automatically.

---

## ESP32 Button Setup

### Arduino IDE

1. **File → Preferences → Additional Board Manager URLs:**
   ```
   https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
   ```
2. **Tools → Board → Boards Manager** → search `Seeed XIAO ESP32C6` → Install
3. **Tools → Board** → select `XIAO_ESP32C6`
4. **Tools → Library Manager** → install **PubSubClient** by Nick O'Leary
5. Open `button_green.ino` or `button_blue.ino` and set:
   ```cpp
   const char* WIFI_PASSWORD = "your_hotspot_password";
   ```
   Everything else (`WIFI_SSID = "pingpong"`, `MQTT_SERVER = "10.11.12.1"`) is pre-configured.
6. **Tools → Port** — if no port appears, install the CH340 or CP2102 USB driver for your OS
7. Upload to the correct button

---

## MQTT Topics

| Topic | Direction | Payload | Description |
|---|---|---|---|
| `button/green` | ESP32 → Pi | `short` / `double` / `reset` | Button press events |
| `button/blue` | ESP32 → Pi | `short` / `double` / `reset` | Button press events |
| `status/green` | ESP32 → Pi | `connected` | Retained, published on every WiFi connect |
| `status/blue` | ESP32 → Pi | `connected` | Retained, published on every WiFi connect |
| `heartbeat/green` | ESP32 → Pi | `ok` | Every 5 seconds |
| `heartbeat/blue` | ESP32 → Pi | `ok` | Every 5 seconds |
| `battery/green` | ESP32 → Pi | `0`–`100` or `-1` | Battery % every 30 seconds |
| `battery/blue` | ESP32 → Pi | `0`–`100` or `-1` | Battery % every 30 seconds |

---

## Controls

| Press | Action |
|---|---|
| Short tap | Score a point / advance menu |
| Double tap | Undo last point |
| Triple tap | Handled as a double-tap (undo) |
| Quad-tap (4× within 500 ms each) | Full reset → jumps straight to rule selection |

**Green button** = top player on screen (far end of table)
**Blue button** = bottom player on screen (near end of table)

---

## Game Flow

```
Quad-press either button at any time → instant reset to rule selection
                │
                ▼
  Choose race-to length      Green = Race to 11     Blue = Race to 21
                │
                ▼
  Choose best of             Green = Best of 3      Blue = Best of 5
                │
                ▼
  Who serves first?          Tap your own button to claim first serve
                │
                ▼
  PLAYING                    Tap your button each time YOU score
                │
           game ends
                │
     ┌──────────┴──────────┐
     │                     │
  Best-of-3             Best-of-5 / extended
  complete?             → match over screen
     │
     ▼
  Extend prompt
  Green = play best of 5
  Blue  = start new match
```

Players swap ends after every game. The games-won tally follows each player automatically.

---

## Serve Rotation

- Each player serves **2 consecutive points**, then serve rotates
- A glowing ball on the table graphic shows which end is currently serving
- The `SERVE ● ●` dots in the score panel show the current serve count (1 or 2)

---

## Scoreboard Features

| Feature | Detail |
|---|---|
| Real-time updates | WebSocket push — no polling, sub-100 ms response |
| Loss of signal | Full-screen overlay after 4 seconds of no server contact |
| Battery indicators | Per-button battery icon + % in each score panel; flashes red below 25% |
| Connection status | Heartbeat-monitored; button marked disconnected after 12 s of silence |
| Simulation mode | Press `S` to show sim panel — test full game flow without physical buttons |
| Standalone mode | Open `scoreboard.html` directly — full JS game engine runs locally, no server needed |

### Keyboard Shortcuts

| Key | Action |
|---|---|
| `g` | Green short press (score) |
| `G` | Green double press (undo) |
| `b` | Blue short press (score) |
| `B` | Blue double press (undo) |
| `r` / `R` | Reset (quad-press equivalent) |
| `c` / `C` | Connect both buttons |
| `S` | Toggle sim panel |
| `F` | Toggle fullscreen |

---

## Crash Recovery

Game state is saved to a SQLite database (`~/pingpong/game_state.db`) after every button press. If the server crashes or the Pi loses power mid-game, it automatically restores the exact score, serve position, and game count on next startup. A deliberate quad-press reset marks the database as clean so recovery does not trigger after an intentional reset.

---

## Logging

| Log | Location | Format |
|---|---|---|
| Human-readable game log | `~/pingpong/logs/game_YYYYMMDD.log` | Plain English with timestamps, rotates daily, 14-day retention |
| System / crash log | `journalctl -u pingpong` | systemd journal |
| Match history | `~/pingpong/game_state.db` → `match_history` table | SQLite — queryable by date, winner, duration, scores |

```bash
# Follow the live game log
tail -f ~/pingpong/logs/game_$(date +%Y%m%d).log

# View recent server output
sudo journalctl -u pingpong -n 50 --no-pager

# Query match history
sqlite3 ~/pingpong/game_state.db \
  "SELECT timestamp, winner, race_to, best_of, duration_s FROM match_history ORDER BY id DESC LIMIT 10;"
```

---

## Systemd Service

```bash
# Check status
sudo systemctl status pingpong

# Follow live logs
sudo journalctl -u pingpong -f

# Restart
sudo systemctl restart pingpong
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| ESP32 can't connect to AP | Confirm SSID `pingpong` and password match. `sudo systemctl status hostapd` |
| No MQTT messages arriving | `sudo systemctl status mosquitto` — must be running on `0.0.0.0:1883` |
| `wlan0` has no IP after reboot | `ip addr show wlan0` — if missing `10.11.12.1`, run `sudo ifup wlan0` |
| Scoreboard not loading | `sudo systemctl status pingpong` — look for Python errors |
| Server crash-loops on start | `sudo journalctl -u pingpong -n 30` — usually a missing Python package |
| Firefox doesn't open on TV | `~/pingpong/launch_kiosk.sh` polls until the server responds — check server first |
| Mouse cursor visible on TV | `sudo raspi-config` → Advanced → Wayland → switch to X11, then reboot |
| Screen goes blank | Add `@xset s off` / `@xset -dpms` / `@xset s noblank` to `~/.config/lxsession/LXDE-pi/autostart` |
| Battery always shows `—` | Voltage divider not wired to A0, or set `BAT_ADC_ENABLED false` in firmware |
| No COM port in Arduino IDE | Install CH340 or CP2102 USB driver. Try holding BOOT while plugging in USB |

---

## Licence

MIT — do whatever you like with it, just don't blame me if your ping-pong rivalries get out of hand or Mike claims victory regardless of the score.
