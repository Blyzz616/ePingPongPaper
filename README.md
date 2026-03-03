# Ping-Pong Scorer – Setup Guide
# Raspberry Pi OS "Trixie" (Debian 13) — NetworkManager edition

## Files in this package

| File | Purpose |
|------|---------|
| `pingpong.py` | Main Pi Python program |
| `button_green/button_green.ino` | Arduino sketch for the GREEN ESP32-C6 |
| `button_blue/button_blue.ino` | Arduino sketch for the BLUE ESP32-C6 |

---

## 1 – Raspberry Pi Zero W v1: OS & packages

```bash
# Update
sudo apt update && sudo apt upgrade -y

# hostapd is still needed; dnsmasq is managed automatically
# by NetworkManager's shared-mode hotspot feature
sudo apt install -y hostapd

# MQTT broker
sudo apt install -y mosquitto mosquitto-clients

# ImageMagick (BMP generation)
sudo apt install -y imagemagick

# Python deps
# On Trixie, pip outside a venv requires --break-system-packages (PEP 668)
pip3 install paho-mqtt --break-system-packages
```

> **Alternatively, use a venv (cleaner):**
> ```bash
> python3 -m venv /home/pi/scorer-venv
> source /home/pi/scorer-venv/bin/activate
> pip install paho-mqtt
> ```
> Then point the systemd `ExecStart` at `/home/pi/scorer-venv/bin/python`.

---

## 2 – Wi-Fi Access Point via NetworkManager

Trixie uses **NetworkManager** for all network management.
**Do not edit `/etc/dhcpcd.conf`** — dhcpcd is not present in Trixie.
Use `nmcli` to create a hotspot connection profile instead.

### 2a — Create the hotspot profile

```bash
sudo nmcli con add \
  type wifi \
  ifname wlan0 \
  con-name PingPongHotspot \
  autoconnect yes \
  ssid PingPongScorer \
  mode ap \
  ipv4.method shared \
  ipv4.addresses 192.168.4.1/24 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "pingpong123" \
  802-11-wireless.band bg \
  802-11-wireless.channel 6
```

### 2b — Activate immediately

```bash
sudo nmcli con up PingPongHotspot

# Verify the interface has the static IP
ip addr show wlan0
# Expected: inet 192.168.4.1/24
```

`autoconnect yes` means NetworkManager will bring this hotspot up
automatically on every subsequent boot — no further configuration needed.

### 2c — DHCP for clients (automatic)

When `ipv4.method shared` is set, NetworkManager spawns its own scoped
dnsmasq instance on `wlan0` and writes its config to:

```
/run/NetworkManager/dnsmasq-wlan0.conf
```

You do **not** need to write a `/etc/dnsmasq.conf`.
If you need to customise the DHCP range, drop an override into:

```
/etc/NetworkManager/dnsmasq-shared.d/pingpong.conf
```

Example contents:
```
dhcp-range=192.168.4.10,192.168.4.30,255.255.255.0,24h
dhcp-option=3,192.168.4.1
dhcp-option=6,192.168.4.1
```

Then reload: `sudo nmcli con down PingPongHotspot && sudo nmcli con up PingPongHotspot`

---

## 3 – Mosquitto broker on Pi

Create a config fragment (don't edit the default conf directly):

```bash
sudo tee /etc/mosquitto/conf.d/pingpong.conf << 'EOF'
listener 1883 0.0.0.0
allow_anonymous true
EOF
```

```bash
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

# Quick smoke-test
mosquitto_sub -h 192.168.4.1 -t test &
mosquitto_pub -h 192.168.4.1 -t test -m hello
# Should print "hello"
```

---

## 4 – IT8951 display driver

```bash
sudo apt install -y git build-essential
git clone https://github.com/waveshare/IT8951-ePaper /home/pi/IT8951-src
cd /home/pi/IT8951-src
make
sudo mkdir -p /IT8951
sudo cp epd /IT8951/IT8951
sudo chmod +x /IT8951/IT8951
```

Adjust `EPAPER_CMD` near the top of `pingpong.py` if your binary path differs.

---

## 5 – Run the scorer

```bash
# Normal (MQTT + display) mode
python3 /home/pi/pingpong.py

# Simulation mode — no MQTT or display hardware needed
python3 /home/pi/pingpong.py --sim
```

### Simulation commands

| Input | Action |
|-------|--------|
| `connect` | Simulate both buttons connecting |
| `g` | Green short press |
| `b` | Blue short press |
| `gg` | Green double press (undo) |
| `bb` | Blue double press (undo) |
| `GL` | Green long press (full reset) |
| `BL` | Blue long press (full reset) |

---

## 6 – Arduino IDE setup for ESP32-C6

1. **File → Preferences → Additional Board Manager URLs:**
   ```
   https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
   ```
2. **Tools → Board → Boards Manager** → search `Seeed XIAO ESP32C6` → Install.
3. **Tools → Board** → select `XIAO_ESP32C6`.
4. **Sketch → Include Library → Manage Libraries** → install **PubSubClient** by Nick O'Leary.
5. Open `button_green.ino` or `button_blue.ino`.
6. Confirm the credentials at the top match your setup:
   - `WIFI_SSID`     → `PingPongScorer`
   - `WIFI_PASSWORD` → `pingpong123`
   - `MQTT_SERVER`   → `192.168.4.1`
7. Upload.

---

## 7 – Button wiring (both ESP32-C6 units)

```
XIAO ESP32-C6 pin D1 ──┤ momentary button ├── GND
```

The firmware enables the internal pull-up resistor. No external resistor needed.

---

## 8 – Hand-drawn artwork override

Place any custom BMP (800×600, 1-bit monochrome) in:

```
/tmp/pingpong_imgs/override/<key>.bmp
```

The key for each screen is logged when that screen is first generated.
Example key: `score_G3_B7_svgreen_s2_gm2_gl1_bl0`

---

## 9 – Log files

Each run writes to `logs/<epoch>.txt` in the working directory.
Example: `logs/1772516165.txt`
Format follows the specification exactly (serve headers, blank lines,
"Change of serve" markers, undo records, timestamped events).

---

## 10 – Autostart on boot (systemd)

```bash
sudo tee /etc/systemd/system/pingpong.service << 'EOF'
[Unit]
Description=Ping-Pong Scorer
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/pingpong.py
WorkingDirectory=/home/pi
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pingpong
sudo systemctl start  pingpong
sudo systemctl status pingpong
```

> `After=network-online.target` ensures NetworkManager has activated the
> hotspot and Mosquitto is ready before the Python script starts.
> NetworkManager's `NetworkManager-wait-online.service` satisfies
> `network-online.target` on Trixie.

---

## 11 – Troubleshooting

| Symptom | Check |
|---------|-------|
| ESP32 can't see the AP | `nmcli con show PingPongHotspot` — confirm `GENERAL.STATE: activated` |
| ESP32 connects but MQTT fails | `sudo systemctl status mosquitto` — confirm listening on `0.0.0.0:1883` |
| Pi doesn't show 192.168.4.1 | `ip addr show wlan0` — re-run `sudo nmcli con up PingPongHotspot` |
| Display not updating | Confirm `/IT8951/IT8951` exists and is executable |
| ImageMagick BMP write error | Edit `/etc/ImageMagick-7/policy.xml` — find the `path` pattern `@*` and set `rights="read\|write"` |
| pip install fails without flag | Use `--break-system-packages` or a venv (see section 1) |
