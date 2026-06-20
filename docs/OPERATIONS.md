# Warlock-Walter Operations Manual

## Deployment Scenarios (Authorized Engagements Only)

### Scenario 1: C2 Drop Box

**Objective:** Establish covert out-of-band access to target network perimeter.

**Pre-deployment:**
1. Flash `walter_c2.ino` firmware
2. Insert activated IoT SIM (LTE-M provisioned)
3. Connect cellular + GNSS antennas to u.FL
4. Configure MQTT broker address in firmware
5. Sync known WiFi credentials (target SSIDs if available)
6. Verify cellular registration: serial → `lte status`
7. Verify MQTT connectivity: serial → `status`

**Deployment:**
1. Power Walter via USB battery or USB port on target hardware
2. Place in target vicinity (under desk, ceiling tile, rack, etc.)
3. If target WiFi creds available: Walter auto-connects to target WiFi
4. LTE-M provides covert backhaul regardless of WiFi status
5. C2 operator connects to MQTT broker and sends commands

**Operational:**
- Monitor telemetry on `warlock/walter/{MAC}/telemetry`
- Send commands on `warlock/walter/{MAC}/commands`
- Read responses on `warlock/walter/{MAC}/response`
- Periodic GNSS fixes confirm device hasn't been moved

**Extraction:**
- Simply unplug. No forensic trace on target network if LTE-only mode was used.
- If WiFi-connected: check DHCP logs, NAC alerts, DNS queries

### Scenario 2: WiFi Reconnaissance Platform

**Objective:** Enumerate wireless infrastructure near target location.

**Deployment:**
1. Carry or place Walter in target vicinity
2. Connect via serial or MQTT

**Commands:**
```
wifi scan     → returns JSON with all nearby APs
               (SSID, BSSID, RSSI, channel, encryption type)
```

**Analysis:**
- Signal strength (RSSI) indicates proximity
- Channel distribution shows congestion
- Open networks indicate easy entry points
- WPA networks with known creds are targets
- Hidden SSIDs (empty string) indicate security-conscious targets

### Scenario 3: Cellular Relay for Field Operations

**Objective:** Provide out-of-band LTE uplink when primary WiFi is unavailable.

**Setup:**
1. Connect Walter to cyberdeck via USB
2. Walter establishes LTE-M connection
3. MQTT tunnel active — cyberdeck can relay through Walter

**Use Cases:**
- Parking lot operations (no target WiFi access)
- Rooftop or outdoor positioning (GNSS + cellular)
- Backup comms when primary network is compromised
- Remote operator check-in when local infrastructure is hostile

### Scenario 4: GNSS Positioning and Logging

**Objective:** Precise geolocation for wardriving, site surveying, or evidence geotagging.

**Commands:**
```
gnss fix      → time-slices LTE off, acquires GPS/Galileo fix
                returns lat, lon, satellite count, confidence
```

**Notes:**
- Cold start can take 2-10 minutes
- Hot start (with valid ephemeris) is 10-30 seconds
- LTE assistance data download speeds up fix acquisition
- GNSS and LTE cannot run simultaneously (Sequans hardware limitation)

## Power and Endurance

### USB-Powered (Continuous)
- Walter draws ~200-500mA during LTE transmission
- Any USB battery pack (5000mAh+) provides 10+ hours
- USB wall adapter for permanent deployment

### Battery-Powered (Roadmap: Deep Sleep)
- Target: weeks to months with PSM/eDRX
- Deep sleep current: ~10µA (ESP32) + modem PSM
- Wake on schedule, transmit, sleep
- Requires firmware modification (deep sleep + RTC timer)

## OPSEC Considerations

### Do
- Use MQTTS (TLS) for C2 traffic
- Use a VPS-hosted broker, not a residential IP
- Scrub GNSS data from logs when not needed
- Disable debug serial output in production builds
- Use separate SIM per engagement (rotate identities)
- Verify cellular coverage at the target location before deployment

### Don't
- Leave WiFi debug output enabled on target network
- Store target WiFi credentials in firmware after engagement
- Use the same MQTT broker across engagements without rotation
- Rely on LTE-M for high-bandwidth exfiltration (~300kbps max)
- Forget that cellular traffic is logged by the carrier

## Troubleshooting

### LTE Won't Register (+CEREG: 0)

1. **SIM not activated:** Contact carrier, confirm LTE-M provisioning
2. **No coverage:** Check LTE-M/NB-IoT coverage maps, try different location
3. **Wrong RAT:** Try switching between LTE-M and NB-IoT in firmware
4. **APN missing:** Set `CELLULAR_APN` to carrier-specific APN
5. **Antenna:** Verify u.FL cellular antenna is connected

### GNSS No Fix

1. **Antenna:** Verify u.FL GNSS antenna connected and has sky view
2. **Cold start:** First fix can take 5-15 minutes — be patient
3. **Assistance data:** Ensure LTE connects first to download almanac/ephemeris
4. **Clock sync:** LTE NITZ must sync the GNSS subsystem clock
5. **Environment:** Buildings, vehicles, and RF interference degrade GNSS

### WiFi Won't Connect

1. **Signal strength:** Check RSSI in `wifi scan` output
2. **Credentials:** Verify SSID and password in `knownNetworks[]`
3. **Auth type:** Some enterprise (802.1X) networks are not supported (WPA-PSK only)
4. **Captive portal:** Open networks with captive portals need manual intervention

### MQTT Won't Connect

1. **LTE first:** MQTT requires LTE data connection — check `lte status`
2. **Broker reachable:** Verify broker IP/port is accessible from cellular
3. **Firewall:** Some carriers block certain ports — try 8883 or 443
4. **APN:** Some APNs are restrictive — check with carrier
