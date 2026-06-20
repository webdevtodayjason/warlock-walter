# Warlock-Walter Hardware

## Walter Board Specs

### MCU: ESP32-S3-WROOM-1-N16R2

- Xtensa dual-core 32-bit LX7, up to 240MHz
- 16MB Quad-SPI flash
- 2MB Quad-SPI PSRAM
- WiFi 4 (802.11 b/g/n), 150Mbps, on-board PCB antenna
- BLE 5.0, 2Mbps, on-board PCB antenna
- 24 GPIO available (UART, SPI, I2C, CAN, I2S, SD/MMC, ADC, DAC, PWM)
- USB Type-C for power + flash/debug
- 22 test points for production programming

### Modem: Sequans GM02SP (Monarch 2)

- LTE Cat-M1 (LTE-M)
- LTE Cat-NB1 (NB-IoT rel. 13)
- LTE Cat-NB2 (NB-IoT rel. 14), upgradeable to rel. 15/17
- 3GPP LTE Release 14 (upgradeable to Release 17)
- Ultra-low deep-sleep: eDRX and PSM modes
- Adaptive power: +23/+20/+14 dBm
- Integrated LNA + SAW filter for GNSS
- GNSS: GPS + Galileo constellations
- Assisted GNSS (A-GNSS) support
- Integrated SIM + Nano-SIM slot
- u.FL connectors for GNSS and cellular antennas

### Power

- 5.0V via USB Type-C
- 3.0-5.5V via Vin pin
- **Cannot use both simultaneously**
- Extremely low quiescent current design
- Industrial temp range: -40°C to +85°C

### Form Factor

- 55mm × 24.8mm
- 2.54mm pin headers (breadboard-friendly)
- Pin- and footprint-compatible with EOL Pycom GPy

## USB Connection

When connected to a host via USB Type-C, Walter appears as:

```
Bus 001 Device 031: ID 303a:1001 Espressif USB JTAG/serial debug unit
```

Linux driver: `cdc_acm`
Device node: `/dev/ttyACM*`
Serial console baud: 115200

The USB connection provides:
1. Power (5V)
2. Serial console for debug/CLI
3. Programming/flashing interface

### Important: Modem Not Directly Accessible

The Sequans GM02SP is connected to the ESP32-S3 via internal UART, **not** USB. The modem will never appear as a USB modem device. All modem AT commands must be issued through the ESP32 firmware via the WalterModem library.

## Pin Map (ESP32 ↔ Sequans Modem)

| Signal | ESP32 GPIO | Direction |
|--------|-----------|-----------|
| Modem RX | 14 | ESP32 input |
| Modem TX | 48 | ESP32 output |
| RTS | 21 | ESP32 output |
| CTS | 47 | ESP32 input |
| Reset (active low) | 45 | ESP32 output |

## Antenna Connections

| Radio | Connector | External Antenna Required? |
|-------|-----------|--------------------------|
| Cellular (LTE-M/NB-IoT) | u.FL | **YES** — board has no PCB cellular antenna |
| GNSS (GPS/Galileo) | u.FL | **YES** — board has no PCB GNSS antenna |
| WiFi | PCB antenna | No — on-board trace antenna |
| BLE | PCB antenna | No — on-board trace antenna |

**Warning:** Operating the cellular modem without an antenna connected can damage the PA (power amplifier). Always connect a cellular antenna before activating LTE.

## SIM Card

Walter accepts two SIM types:
1. **Nano-SIM** — physical slot
2. **Integrated SIM** — on-chip eSIM (carrier-provisioned)

The board supports eUICC (embedded Universal Integrated Circuit Card), enabling remote SIM provisioning across multiple carriers.

### SIM Requirements for LTE-M/NB-IoT

Standard consumer phone SIMs **will not work**. You need an IoT/M2M SIM provisioned for:
- LTE Cat-M1 (LTE-M), or
- LTE Cat-NB1/NB2 (NB-IoT)

Recommended providers: Hologram.io, Soracom, Twilio Super SIM.

## Host Platform: ClockworkPi uConsole CM5 Cyberdeck

| Component | Details |
|-----------|---------|
| Board | ClockworkPi uConsole with CM5 |
| Hacker Board | HackerGadgets AIO V2 (SDR/LoRa/GPS/RTC/USB hub) |
| Storage | NVMe SSD |
| WiFi (managed) | BCM43455 onboard (wlan0) |
| WiFi (monitor) | MT7921 USB (wlan1) |
| GPS (deck) | u-blox on AIO V2 (/dev/ttyAMA0) |
| Cellular | Walter (Sequans GM02SP via USB) |
| SDR | RTL2838 DVB-T dongle |
| Audio | Samson Go Mic Video (USB webcam+mic) |

Note: The original uConsole 4G module was physically removed when the AIO V2 board was installed. Walter is the **only** cellular radio on this deck.
