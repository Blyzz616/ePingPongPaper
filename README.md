# ePingPongPaper

> A wireless, battery-powered ping-pong scoring system built around a 6″ e-paper display, two ESP32-C6 score buttons, and a Raspberry Pi Zero W v1 running Raspberry Pi OS Trixie.

---

## How It Works

Two large recordable buttons — one green, one blue — connect wirelessly over Wi-Fi to a Raspberry Pi Zero W. The Pi runs a local MQTT broker and hosts the scoring logic. Every button press is published as an MQTT event, and the scorer updates a 6″ e-paper display showing the current score, serve side, and games won.

The Pi acts as its own Wi-Fi access point, so the whole system is self-contained — no router or internet connection required.

```
[Green Button]──┐
   ESP32-C6     │  Wi-Fi / MQTT        ┌─────────────────────┐
                ├────────────────────▶│ Raspberry Pi Zero W │──▶ 6″ e-paper
[Blue Button]───┘                      │  (MQTT broker +     │
   ESP32-C6                            │   scoring logic)    │
                                       └─────────────────────┘
```

---

## Hardware

### Raspberry Pi Zero W v1

<img width="225" height="225" alt="image" src="https://github.com/user-attachments/assets/ade2fbc6-d5a9-443c-a962-209a76bea813"  alt="Raspberry Pi Zero W" width="480" />


The brains of the system. Runs Raspberry Pi OS Trixie (Debian 13), hosts a Mosquitto MQTT broker, and drives the e-paper display via SPI.

| Spec | Detail |
|---|---|
| CPU | 1 GHz single-core ARM1176JZF-S |
| RAM | 512 MB |
| Wi-Fi | 802.11 b/g/n (2.4 GHz) |
| GPIO | 40-pin header |
| Power | Micro USB, 5V |

---

### Seeed Studio XIAO ESP32-C6 *(×2 — one per button)*

<img src="https://files.seeedstudio.com/wiki/SeeedStudio-XIAO-ESP32C6/img/xiaoc6.jpg" alt="Seeed Studio XIAO ESP32-C6" width="360"/>

An ultra-compact Wi-Fi + Bluetooth 5 module soldered inside each score button. Connects to the Pi's access point on boot, then publishes short/double/long press events over MQTT.

| Spec | Detail |
|---|---|
| MCU | ESP32-C6 (RISC-V, 160 MHz) |
| Wi-Fi | 802.11 b/g/n (2.4 GHz) |
| Size | 21 × 17.5 mm |
| GPIO | 11 digital I/O |
| Power | 3.3V (via battery boost circuit) |

- [Seeed Studio product page](https://www.seeedstudio.com/Seeed-Studio-XIAO-ESP32C6-p-5884.html)

---

### Score Buttons *(×2)*

<img src="https://m.media-amazon.com/images/I/61z+7Q9R6ML._AC_SL1200_.jpg" alt="Recordable Talking Button" width="300"/>

Large 85 mm recordable talking buttons with built-in LEDs — one green, one blue. The original AAA battery compartment has been repurposed to house the ESP32-C6 and LiPo battery. The original button mechanism is wired to the ESP32's GPIO.

| Spec | Detail |
|---|---|
| Diameter | 85 × 85 mm |
| Height | 35 mm |
| Material | ABS plastic |
| LED | Built-in, colour-matched |

- [Amazon.ca listing](https://www.amazon.ca/Recordable-Talking-Button-Recording-Bright/dp/B0FLNVCPL3)

---

### Replacement Batteries *(×2 — one per button)*

<img src="https://m.media-amazon.com/images/I/61lz2qJOSEL._AC_SL1200_.jpg" alt="HAWK'S WORK LiPo Battery" width="300"/>

Each button runs from a 3.7V LiPo cell connected to the ESP32-C6's battery input via a small boost/charge circuit. These drop-in cells fit neatly in the modified button housing.

| Spec | Detail |
|---|---|
| Voltage | 3.7V |
| Capacity | 550 mAh |
| Connector | XH 2.54 |
| Size | 47 × 20.7 × 7.5 mm |
| Weight | 12.5 g |
| Protection IC | Built-in (over-charge / over-discharge) |

- [Amazon.ca listing](https://www.amazon.ca/HAWKS-WORK-Rechargeable-Helicopter-Connector/dp/B0DHX1KVFX/)

---

### 6″ E-Paper Display

<img src="https://www.waveshare.com/w/upload/thumb/2/2d/6inch-e-Paper-HAT-1.jpg/600px-6inch-e-Paper-HAT-1.jpg" alt="Waveshare 6 inch e-paper HAT" width="480"/>

Waveshare 6″ HD e-paper HAT driven by an IT8951 controller over SPI. Connects directly to the Pi Zero's GPIO header. Retains its image with zero power consumption when not updating.

| Spec | Detail |
|---|---|
| Resolution | 800 × 600 |
| Display size | 6 inches |
| Controller | IT8951 |
| Interface | SPI |
| Refresh (GC16) | ~4 s — full quality, used for menus |
| Refresh (A2) | ~0.3 s — fast binary, used for in-game scoring |
| Colours | Black & white (A2 mode) / 16-level grayscale (GC16) |

---

## Files

| File | Purpose |
|---|---|
| `pingpong.py` | Main Pi scoring program |
| `main.c` | IT8951 display binary wrapper (supports fast A2 mode) |
| `button_green/button_green.ino` | Arduino sketch for the green ESP32-C6 |
| `button_blue/button_blue.ino` | Arduino sketch for the blue ESP32-C6 |
| `images/` | All BMP display assets |
| `imagesassets.lst` | Full asset inventory with dimensions and coordinates |

---

## Display Assets

All artwork lives in `/home/jim/images/` on the Pi. The scoring system uses two refresh strategies:

- **GC16 (~4 s)** — full panel refresh for menus, game start, and match-over screens
- **A2 (~0.3 s)** — partial update of only the changed element (one digit, or the serve arrow) for every scored point

| Asset | Description |
|---|---|
| `gamelen.bmp` | Rule selection — choose race-to length |
| `gl11.bmp` / `gl21.bmp` | Race-to-11 / race-to-21 chosen |
| `serveask.bmp` | "Who serves first?" prompt |
| `gl11bo3.bmp` … `gl21bo5.bmp` | In-game base images (one per rule combo) |
| `serve.bmp` | Serve bar overlay (237×82 px) |
| `serveleft.bmp` / `serveright.bmp` | Serve-side arrows (282×150 px) |
| `serveblank.bmp` | Arrow eraser — same size as arrows |
| `0.bmp` … `41.bmp` | Point score digits (330×215 px) |
| `g0.bmp` … `g2.bmp` | Games-won digits (72×106 px) |
| `gameover.bmp` | Match-over screen, best-of-5 |
| `gameover3.bmp` | Match-over screen, best-of-3 |

---

## Setup

### 1 — OS & Packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y mosquitto mosquitto-clients imagemagick git build-essential
pip3 install paho-mqtt --break-system-packages
```

### 2 — Wi-Fi Access Point (NetworkManager)

Trixie uses NetworkManager — do **not** edit `dhcpcd.conf`.

```bash
sudo nmcli con add \
  type wifi ifname wlan0 con-name PingPongHotspot \
  autoconnect yes ssid PingPong mode ap \
  ipv4.method shared ipv4.addresses 192.168.4.1/29 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "supersecurepassword" \
  802-11-wireless.band bg 802-11-wireless.channel 6

sudo nmcli con up PingPongHotspot
```

### 3 — Mosquitto

```bash
sudo tee /etc/mosquitto/conf.d/pingpong.conf << 'EOF'
listener 1883 0.0.0.0
allow_anonymous true
EOF

sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
```

### 4 — IT8951 Display Driver

```bash
git clone https://github.com/waveshare/IT8951 /IT8951-src
cd /IT8951-src
```

Apply two edits (see `BUILD_INSTRUCTIONS.txt`), then:

```bash
sudo make clean && sudo make
sudo cp IT8951 /IT8951/IT8951
```

### 5 — Run the Scorer

```bash
# Live mode
python3 /home/jim/pingpong.py

# Simulation mode (no hardware needed)
python3 /home/jim/pingpong.py --sim
```

### 6 — Autostart on Boot

```bash
sudo tee /etc/systemd/system/pingpong.service << 'EOF'
[Unit]
Description=Ping-Pong Scorer
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/jim/pingpong.py
WorkingDirectory=/home/jim
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pingpong
sudo systemctl start pingpong
```

### 7 — Arduino IDE (ESP32-C6 Buttons)

1. **File → Preferences → Additional Board Manager URLs:**
   ```
   https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
   ```
2. **Tools → Board → Boards Manager** → search `Seeed XIAO ESP32C6` → Install
3. **Tools → Board** → select `XIAO_ESP32C6`
4. **Library Manager** → install **PubSubClient** by Nick O'Leary
5. Open `button_green.ino` or `button_blue.ino` and confirm credentials:
   - `WIFI_SSID` → `PingPong`
   - `WIFI_PASSWORD` → *(your hotspot password)*
   - `MQTT_SERVER` → `192.168.4.1`
6. Upload

#### Button Wiring

```
XIAO ESP32-C6 pin D1 ──┤ button mechanism ├── GND
```

Internal pull-up is enabled in firmware — no resistor needed.

---

## Controls

| Button | Action |
|---|---|
| Short press | Score a point for that side |
| Double press | Undo last point |
| Long press | Full reset (either button) |

### Simulation Mode Commands

| Input | Action |
|---|---|
| `connect` | Simulate both buttons connecting |
| `g` / `b` | Green / blue short press |
| `gg` / `bb` | Double press (undo) |
| `GL` / `BL` | Long press (full reset) |

---

## Game Rules

- **Race to:** 11 or 21 points (green = 11, blue = 21)
- **Best of:** 3 or 5 games (green = 3, blue = 5)
- **Win by two** rule applies at deuce
- Serve rotates every 2 points; players swap ends after each game
- Best-of-3 matches offer an optional extend-to-best-of-5 at match end

---

## Troubleshooting

| Symptom | Check |
|---|---|
| ESP32 can't see the AP | `nmcli con show PingPongHotspot` — confirm `GENERAL.STATE: activated` |
| MQTT connection refused | `sudo systemctl status mosquitto` — confirm listening on `0.0.0.0:1883` |
| Pi not at 192.168.4.1 | `ip addr show wlan0` — re-run `sudo nmcli con up PingPongHotspot` |
| Display not updating | Confirm `/IT8951/IT8951` exists and is executable (`ls -la /IT8951/`) |
| ImageMagick BMP error | Edit `/etc/ImageMagick-7/policy.xml` — set `rights="read\|write"` for `path` pattern `@*` |
| `pip install` fails | Add `--break-system-packages` or use a venv |

---

## Log Files

Each run writes a timestamped log to `/home/jim/logs/<epoch>.txt`.

Format includes serve headers, score events, undo records, change-of-serve markers, and full match summaries.

---

## Licence

MIT — do whatever you like with it, just don't blame me if your ping-pong rivalries get out of hand.
