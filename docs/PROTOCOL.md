# Warlock-Walter C2 Protocol

## MQTT Architecture

### Broker

The C2 operator runs an MQTT broker (mosquitto, EMQX, etc.) reachable from the cellular network. Walter connects as an MQTT client and uses three topics per device.

### Topic Structure

All topics are prefixed with `warlock/walter/{MAC}/` where `{MAC}` is the ESP32-S3 WiFi STA MAC in `XX:XX:XX:XX:XX:XX` format.

| Topic | Direction | QoS | Purpose |
|-------|-----------|-----|---------|
| `.../telemetry` | Device → C2 | 0 | Periodic status heartbeat |
| `.../commands` | C2 → Device | 1 | Operator commands |
| `.../response` | Device → C2 | 1 | Command results |

### Message Format

All messages are JSON. Every message includes a `type` field.

## Telemetry

Published automatically every 60 seconds (configurable via `TELEMETRY_INTERVAL_MS`).

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

Fields may be null/absent when the corresponding subsystem is inactive.

## Commands

Published to `.../commands` as plain-text JSON or raw command strings.

### Command Reference

| Command | Arguments | Example |
|---------|-----------|---------|
| `status` | none | `status` |
| `wifi scan` | none | `wifi scan` |
| `wifi connect` | `<ssid> <password>` | `wifi connect CorpWiFi SecretPass123` |
| `gnss fix` | none | `gnss fix` |
| `lte status` | none | `lte status` |
| `lte connect` | none | `lte connect` |
| `lte disconnect` | none | `lte disconnect` |
| `reboot` | none | `reboot` |
| `help` | none | `help` |

### Response Format

Responses are published to `.../response` as JSON:

```json
{
  "type": "status",
  "device": "walter-AABBCC",
  "uptime_s": 3600,
  "wifi_connected": true,
  "wifi_ssid": "jb-wifi7",
  "wifi_ip": "192.168.1.50",
  "lte_connected": true,
  "mqtt_connected": true,
  "gnss_valid": true,
  "temp_c": 42.3,
  "free_heap": 234567,
  "lat": 35.123456,
  "lon": -80.654321
}
```

### Error Response

```json
{
  "type": "error",
  "msg": "unknown command: explode"
}
```

## Serial Interface

All commands are also available over USB serial at 115200 baud. Commands are newline-terminated. Responses are prefixed with `RESP:`.

```
> status
RESP:{"type":"status","device":"walter-AABBCC",...}

> wifi scan
RESP:{"type":"wifi_scan","networks":[...]}
```

## LTE/GNSS Time-Slicing Protocol

The Sequans GM02SP cannot run LTE and GNSS concurrently. The firmware manages this automatically:

```
State A: LTE Connected
  ├── MQTT active (commands + telemetry)
  ├── GNSS clock sync via NITZ
  ├── Assistance data download (almanac + ephemeris)
  └── Telemetry publishing active

State B: GNSS Fix Acquisition
  ├── LTE disconnected (modem → MINIMUM state)
  ├── GNSS receiver active (cold or hot start)
  ├── Fix acquisition (up to 2 min timeout)
  └── No MQTT during this window

Transition A→B: On gnss fix command or scheduled GNSS cycle
Transition B→A: After fix acquired or timeout
```

During State B, the device is unreachable via MQTT. Commands sent during this window will be queued by the broker and delivered when LTE reconnects.

## Security Considerations

### Current (Development)

- MQTT is unencrypted (port 1883)
- No authentication on MQTT connection
- WiFi credentials stored in plaintext flash
- No message authentication/integrity

### Operational (Roadmap)

- **MQTTS** (port 8883) with TLS certificates
- **HMAC-SHA256** message authentication with shared key
- **NVS encryption** for stored credentials
- **Topic ACLs** on broker to restrict device access
- **Client certificates** for mutual TLS

### OPSEC Notes

- MQTT broker IP is visible in cellular traffic — use a VPS or Tor hidden service
- GNSS position in telemetry is sensitive — scrub in logs
- WiFi scan results reveal operator proximity to targets
- The debug serial output includes AT commands — disable in production builds
