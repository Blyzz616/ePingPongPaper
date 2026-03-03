# Ping-Pong Scorer – Setup Guide

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

# AP tools
sudo apt install -y hostapd dnsmasq

# MQTT broker
sudo apt install -y mosquitto mosquitto-clients

# ImageMagick (for BMP generation)
sudo apt install -y imagemagick

# Python deps
pip3 install paho-mqtt
```

---

## 2 – Pi as Wi-Fi Access Point

### /etc/hostapd/hostapd.conf
```
interface=wlan0
driver=nl80211
ssid=PingPong
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=<supersecurepassword>
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
```

### /etc/dnsmasq.conf  (add these lines)
```
interface=wlan0
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
```

### /etc/dhcpcd.conf  (add at end)
```
interface wlan0
    static ip_address=192.168.4.1/24
    nohook wpa_supplicant
```

### Enable & start
```bash
sudo systemctl unmask hostapd
sudo systemctl enable hostapd dnsmasq
sudo systemctl start  hostapd dnsmasq
```

---

## 3 – Mosquitto broker on Pi

### /etc/mosquitto/mosquitto.conf
```
listener 1883 0.0.0.0
allow_anonymous true
```

```bash
sudo systemctl enable mosquitto
sudo systemctl start  mosquitto
```

---

## 4 – IT8951 display driver

Clone and build the IT8951 utility so it lives at `/IT8951/IT8951`:

```bash
git clone https://github.com/waveshare/IT8951-ePaper /IT8951/src
cd /IT8951/src
make
cp epd /IT8951/IT8951
```
*(Adjust path in `pingpong.py` → `EPAPER_CMD` if your binary lives elsewhere.)*

---

## 5 – Run the scorer

```bash
# Normal (MQTT) mode
python3 pingpong.py

# Simulation mode (no MQTT needed – type button events in terminal)
python3 pingpong.py --sim
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
4. Install **PubSubClient** via Library Manager.
5. Open `button_green.ino` or `button_blue.ino`, set your SSID/password at top, upload.

---

## 7 – Button wiring (both ESP32-C6 units)

```
XIAO ESP32-C6 D1 ──┤ button ├── GND
```
Internal pull-up is enabled in firmware. No resistor needed.

---

## 8 – Hand-drawn artwork override

Place any custom BMP (800×600, 1-bit monochrome) in:

```
/tmp/pingpong_imgs/override/<key>.bmp
```

The key for a score screen is printed in the log when that screen is generated.
Example: `score_G3_B7_svgreen_s2_gm2_gl1_bl0.bmp`

---

## 9 – Log files

Each session writes to `logs/<epoch>.txt` in the working directory.
The format follows the specification exactly, including serve headers,
change-of-serve markers, and double-press undo records.

---

## 10 – Autostart on boot

```bash
# /etc/systemd/system/pingpong.service
[Unit]
Description=Ping-Pong Scorer
After=network.target mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/pingpong.py
WorkingDirectory=/home/pi
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable pingpong
sudo systemctl start  pingpong
```
