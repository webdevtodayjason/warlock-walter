# Warlock-Walter: Cellular C2 Drop Box

> ESP32-S3 + Sequans GM02SP cellular C2 relay and reconnaissance drop box for authorized red team engagements.
> Built on the [DPTechnics Walter](https://quickspot.io/) platform.

## Overview

**Warlock-Walter** transforms a DPTechnics Walter board (ESP32-S3-WROOM-1-N16R2 + Sequans Monarch 2 GM02SP) into a covert cellular command-and-control relay for authorized red team operations. It provides out-of-band backhaul via LTE-M/NB-IoT, local WiFi reconnaissance, GNSS positioning, and a bidirectional command interface over both MQTT (cellular) and USB serial.

## Hardware

### Walter Board

| Component | Part | Capabilities |
|-----------|------|-------------|
| **MCU** | ESP32-S3-WROOM-1-N16R2 | Dual-core LX7, 16MB flash, 2MB PSRAM |
| **Modem** | Sequans GM02SP (Monarch 2) | LTE Cat-M1, LTE Cat-NB1/NB2, GNSS |
| **WiFi** | ESP32-S3 onboard | 802.11 b/g/n (WiFi 4) |
| **BLE** | ESP32-S3 onboard | BLE 5.0 |
| **GNSS** | Sequans integrated | GPS + Galileo, LNA + SAW filter |
| **USB** | Type-C | Power, flashing, serial debug |
| **SIM** | Nano-SIM + integrated SIM | eUICC capable |

### Modem UART Pin Map (ESP32 ↔ Sequans)

| Function | GPIO |
|----------|------|
| Modem RX | 14 |
| Modem TX | 48 |
| RTS | 21 |
| CTS | 47 |
| Reset (active low) | 45 |

### Antennas

Walter uses u.FL connectors for cellular and GNSS antennas. The WiFi/BLE antennas are on-board PCB traces.

- **Cellular:** u.FL → LTE-M/NB-IoT antenna (required for cellular)
- **GNSS:** u.FL → GPS/Galileo antenna (required for positioning)
- **WiFi/BLE:** On-board PCB antenna (no external connection needed)

### Host Connection

Walter connects to the host (cyberdeck) via USB Type-C. The ESP32-S3 exposes a native USB CDC serial interface:

- **USB VID:PID:** `303a:1001` (Espressif USB JTAG/serial debug unit)
- **Linux device:** `/dev/ttyACM*` (cdc_acm driver)
- **Baud:** 115200

The Sequans modem is **not** directly accessible from USB — it sits behind the ESP32 on an internal UART (`Serial2`). All modem communication is proxied through the ESP32 firmware via the WalterModem library.

## Architecture

### Dual-Homed Design

```
                    ┌─────────────────────────────┐
                    │     Walter (ESP32-S3)        │
                    │                              │
  Target WiFi ──────┤ WiFi    ┌──────────────┐     │
  (known SSIDs)     │ ──────► │ Command       │ ◄───┤ USB Serial CLI
                    │         │ Handler       │     │ (local operator)
                    │ LTE-M ──┤               │     │
  Cellular tower ───┤ ◄────── └──────┬───────┘     │
                    │  Sequans GM02SP │             │
                    │  GNSS ──────────┘             │
                    └────────────┬──────────────────┘
                                 │
                          MQTT over LTE-M
                                 │
                    ┌────────────▼──────────────────┐
                    │   C2 MQTT Broker               │
                    │   (operator-controlled)         │
                    │                                │
                    │  warlock/walter/{MAC}/telemetry│
                    │  warlock/walter/{MAC}/commands │
                    │  warlock/walter/{MAC}/response │
                    └────────────────────────────────┘
```

### LTE/GNSS Time-Slicing

The Sequans GM02SP **cannot** operate LTE and GNSS simultaneously. The firmware time-slices:

1. Connect LTE → sync GNSS clock (NITZ) → download assistance data
2. Disconnect LTE → acquire GNSS fix
3. Reconnect LTE → publish telemetry with position

This cycle repeats on demand or at configured intervals.

### MQTT Topic Structure

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `warlock/walter/{MAC}/telemetry` | Device → C2 | Periodic status, position, signal |
| `warlock/walter/{MAC}/commands` | C2 → Device | Remote commands |
| `warlock/walter/{MAC}/response` | Device → C2 | Command responses |

`{MAC}` is the ESP32-S3 WiFi STA MAC address in `XX:XX:XX:XX:XX:XX` format.

### WiFi Known Networks

The firmware embeds a list of known WiFi credentials synced from the host. On boot, Walter scans for these SSIDs and auto-connects if found. This provides high-bandwidth local connectivity when in range, falling back to LTE-M when WiFi is unavailable.

## Firmware Features

### Command Set (Serial + MQTT)

| Command | Description |
|---------|-------------|
| `status` | Full device state (WiFi, LTE, MQTT, GNSS, temp, heap) as JSON |
| `wifi scan` | Scan all nearby APs, return SSID/BSSID/RSSI/channel/auth as JSON |
| `wifi connect <ssid> <pass>` | Join a specific WiFi network |
| `gnss fix` | Acquire GNSS position (time-slices LTE off) |
| `lte status` | Cellular registration state |
| `lte connect` | Force LTE attach |
| `lte disconnect` | Tear down LTE connection |
| `reboot` | Restart the board |
| `help` | List available commands |

### Telemetry Payload (JSON)

Published every 60 seconds when MQTT is connected:

```json
{
  "type": "telemetry",
  "device": "walter-AABBCC",
  "uptime": 3600,
  "wifi_ssid": "jb-wifi7",
  "wifi_ip": "192.168.1.50",
  "wifi_rssi": -55,
  "lte_connected": true,
  "lat": 35.123456,
  "lon": -80.654321,
  "sats": 8,
  "confidence": 12.5,
  "temp_c": 42.3,
  "free_heap": 234567
}
```

### WiFi Scan Response (JSON)

```json
{
  "type": "wifi_scan",
  "networks": [
    {"ssid": "CorpWiFi", "bssid": "AA:BB:CC:DD:EE:FF", "rssi": -65, "channel": 6, "auth": "WPA2"},
    {"ssid": "GuestNet", "bssid": "11:22:33:44:55:66", "rssi": -72, "channel": 11, "auth": "open"}
  ]
}
```

## Build & Flash

### Prerequisites

- **arduino-cli** v1.1+ (or Arduino IDE 2.x)
- **ESP32 Arduino core** (esp32:esp32)
- **Libraries:** WalterModem (QuickSpot), ArduinoJson v7
- **esptool** (for direct flashing if needed)
- Board target: `esp32:esp32:dpt_walter` or `esp32s3box` (if Walter board def unavailable)

### Toolchain Setup

```bash
# Install arduino-cli
wget https://github.com/arduino/arduino-cli/releases/download/v1.1.1/arduino-cli_1.1.1_Linux_ARM64.tar.gz
tar xzf arduino-cli_*.tar.gz && sudo mv arduino-cli /usr/local/bin/

# Configure for ESP32
arduino-cli config init
arduino-cli config set board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32

# Install libraries
arduino-cli lib install ArduinoJson
git clone https://github.com/QuickSpot/walter-arduino.git ~/Arduino/libraries/WalterModem
```

### Compile

```bash
arduino-cli compile \
  --board esp32:esp32:dpt_walter \
  --build-property "build.extra_flags=-DBOARD_HAS_PSRAM" \
  ~/Arduino/walter_c2/walter_c2.ino
```

### Flash

```bash
# Via arduino-cli
arduino-cli upload -p /dev/ttyACM1 --board esp32:esp32:dpt_walter ~/Arduino/walter_c2/

# Or via esptool directly
esptool.py --port /dev/ttyACM1 write_flash 0x0 walter_c2.ino.bin
```

### Arduino IDE Settings (if using IDE)

| Setting | Value |
|---------|-------|
| Board | DPTechnics Walter |
| CPU Frequency | 240MHz |
| Core Debug Level | Debug |
| USB DFU On Boot | Disabled |
| Erase All Flash Before Upload | Enabled |
| Flash Mode | QIO 80MHz |
| Flash Size | 16MB (128Mb) |
| Partition Scheme | 16M Flash (2MB APP/12.5MB FATFS) |
| PSRAM | QSPI PSRAM |
| Upload Mode | UART0 / Hardware CDC |
| Upload Speed | 921600 |
| USB Mode | Hardware CDC and JTAG |

## Configuration

Edit the following `#define` values at the top of `walter_c2.ino`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MQTT_HOST` | `broker.emqx.io` | C2 MQTT broker address |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `CELLULAR_APN` | `""` (auto) | Carrier APN (empty = auto-detect) |
| `RADIO_TECH` | `WALTER_MODEM_RAT_LTEM` | LTE-M or NB-IoT |
| `TELEMETRY_INTERVAL_MS` | `60000` | Telemetry publish interval |
| `GNSS_FIX_TIMEOUT_MS` | `120000` | GNSS fix timeout |

Add/edit entries in the `knownNetworks[]` array to sync WiFi credentials from the host.

## Cellular / SIM Requirements

Walter's Sequans GM02SP requires **LTE-M (Cat-M1)** or **NB-IoT (Cat-NB1/NB2)** provisioning. Standard phone SIMs will **not** work — you need an IoT/M2M SIM.

### Recommended SIM Providers

| Provider | Notes |
|----------|-------|
| **Hologram.io** | eUICC, multi-carrier (AT&T + T-Mobile in US), $0.50/MB, developer API |
| **Soracom** | Air SIM, global coverage, built-in relay services |
| **Twilio Super SIM** | Multi-carrier, API-driven |

### Current SIM Status

The board shipped with an unknown SIM. LTE-M registration fails (`+CEREG: 0`). 6× Hologram Hyper eUICC IoT SIMs have been ordered. Once activated, update `CELLULAR_APN` if needed (Hologram typically auto-provisions).

## Operational Use Cases (Authorized Engagements)

### 1. Out-of-Band C2 Drop Box

Physically place Walter on the target network perimeter. WiFi connects to target infrastructure (if credentials known), LTE-M provides covert backhaul. Commands flow via MQTT — invisible to target network monitoring.

### 2. WiFi Reconnaissance

Deploy Walter in proximity to the target. `wifi scan` enumerates all nearby APs with signal strength, enabling positioning mapping and target identification.

### 3. GNSS Logging

Walter's GNSS provides precise positioning. Useful for wardriving/mapping operations, geotagging findings, and tracking device movement.

### 4. Cellular Relay

When the host cyberdeck has no WiFi, Walter provides LTE-M uplink for out-of-band operator communication.

## File Structure

```
warlock-walter/
├── README.md                  # This file
├── walter_c2/
│   └── walter_c2.ino          # Main firmware
├── docs/
│   ├── HARDWARE.md            # Hardware details, pin maps, antenna specs
│   ├── PROTOCOL.md            # MQTT protocol, message formats, crypto roadmap
│   └── OPERATIONS.md          # Deployment guides for engagement scenarios
├── tools/
│   ├── walter-monitor.py      # Host-side MQTT monitor/CLI for C2
│   └── wifi-sync.sh           # Sync known networks from NetworkManager
└── LICENSE                    # TBD
```

## Roadmap

- [ ] **MQTT over TLS (MQTTS)** — encrypt C2 channel
- [ ] **Shared-key HMAC auth** — authenticate MQTT messages
- [ ] **WiFi evil twin / rogue AP** — active WiFi attacks
- [ ] **WiFi deauth** — targeted deauthentication
- [ ] **BLE beacon scanning** — device enumeration
- [ ] **Deep sleep mode** — field endurance (weeks/months)
- [ ] **OTA firmware updates** — remote patching via LTE
- [ ] **Data staging** — local buffering when LTE unavailable
- [ ] **Host-side C2 dashboard** — Python TUI for multi-Walter fleet

## Security Notes

- WiFi credentials are stored in plaintext in firmware flash. For production, use NVS encryption or a secrets manager.
- MQTT is currently unencrypted (port 1883). Use MQTTS (8883) with TLS for operational security.
- The Sequans modem supports TLS — the WalterModem library has `socketConfigSecure()` for encrypted sockets.
- GNSS position data is sensitive — scrub from logs when not needed.

## Acknowledgments

- **DPTechnics BV** (Belgium) — Walter hardware design and open-source libraries
- **Espressif Systems** — ESP32-S3 platform
- **Sequans Communications** — Monarch 2 GM02SP modem
- **QuickSpot** — Arduino/ESP-IDF/MicroPython libraries and documentation

## License

Firmware: Based on QuickSpot/walter-arduino (DPTechnics 5-clause license).
Warlock extensions: TBD (operator's choice).

---

*For authorized red team, security research, and defensive use only.*
