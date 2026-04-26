# Network Configuration

## Subnet Layout

```
10.11.12.0/29

.0 — Network address
.1 — Pi (gateway, MQTT broker, DHCP server)
.2 — Green button (static IP)
.3 — Blue button (static IP)
.4 — DHCP pool (reserved for PC debugging/programming)
.5 — DHCP pool
.6 — DHCP pool
.7 — Broadcast
```

---

## Device Configuration

### Raspberry Pi 3

- **Static IP:** `10.11.12.1`
- **Role:** Wi-Fi AP, MQTT broker, scoring server, DHCP server
- **Hostname:** `pingpong` (via `nmcli` or equivalent)
- **DHCP Pool:** `10.11.12.4–10.11.12.6` (for debugging clients)

### Green Button (ESP32-C6)

- **Static IP:** `10.11.12.2` (configured in firmware)
- **Hostname:** `button_green.local`
- **MQTT Client ID:** `button_green`
- **OTA Enabled:** Yes, password `pingpong`

### Blue Button (ESP32-C6)

- **Static IP:** `10.11.12.3` (configured in firmware)
- **Hostname:** `button_blue.local`
- **MQTT Client ID:** `button_blue`
- **OTA Enabled:** Yes, password `pingpong`

### Debugging PC (Optional)

- **IP:** DHCP from Pi (`.4`, `.5`, or `.6`)
- **Use:** Connect to Wi-Fi AP `pingpong` to reprogram/debug buttons or Pi
- **Reach Pi at:** `ssh pi@10.11.12.1` or `ssh pi@pingpong.local`

---

## Debugging

### Check Button IPs

On the Pi:

```bash
# List clients connected to the AP
sudo dnsmasq-leases  # may not exist on all systems
# Or check journalctl
journalctl -u dnsmasq -n 20

# Verify by IP
ping 10.11.12.2   # Green
ping 10.11.12.3   # Blue

# Or by hostname
ping button_green.local
ping button_blue.local
```

### Check MQTT Messages

From any machine on the network:

```bash
mosquitto_sub -h 10.11.12.1 -t "#" -v
```

### Update Button via OTA from PC

1. Connect PC to `pingpong` Wi-Fi
2. Open button code in Arduino IDE
3. **Tools → Port** → select `button_green at 10.11.12.2` (or blue at .3)
4. **Upload** and enter OTA password `pingpong`

### Manual USB Programming

If OTA fails:

1. **Remove button** from housing
2. **Connect ESP32-C6 via USB** to PC
3. **Tools → Board:** `XIAO_ESP32C6`
4. **Tools → Port:** Select COM port
5. **Upload**

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Buttons show no IP in DHCP logs | Firmware has `WiFi.config()` static IP set correctly? Reload firmware. |
| Button not reachable at `.2` or `.3` | `ping 10.11.12.2` / `ping 10.11.12.3`. If no reply, check WiFi connection on button. |
| Can't see OTA ports in Arduino IDE | Button must be powered on and connected to Wi-Fi. Check firewall allows UDP 3232. |
| PC can't reach Pi from DHCP | `ping 10.11.12.1` or `ping pingpong.local`. Both should work. |
| MQTT messages not arriving | `sudo systemctl status mosquitto`. Confirm listening on `0.0.0.0:1883`. |

