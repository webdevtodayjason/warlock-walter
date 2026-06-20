#!/usr/bin/env python3
"""
WARLOCK C2 — Walter Base Camp Monitor
 ========================================
Command and monitor Walter drop boxes over MQTT (LTE-M backhaul).

Usage:
  walter-c2.py monitor [mac]     Live monitor: telemetry, responses, interactive commands
  walter-c2.py status [mac]      One-shot status query
  walter-c2.py scan [mac]        One-shot WiFi scan
  walter-c2.py cmd <command>     Send arbitrary command
  walter-c2.py gnss [mac]        Request GNSS fix (takes up to 2 min)

mac = Walter MAC address (default: 48:CA:43:0F:90:38)
"""

import sys
import json
import time
import threading
import argparse
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: pip install paho-mqtt")
    sys.exit(1)

# ============================================================
# CONFIG
# ============================================================

BROKER = "broker.emqx.io"
PORT = 1883
DEFAULT_MAC = "48:CA:43:0F:90:38"

# ============================================================
# MQTT CLIENT
# ============================================================

class WalterC2:
    def __init__(self, mac=DEFAULT_MAC, broker=BROKER, port=PORT):
        self.mac = mac
        self.broker = broker
        self.port = port
        self.device_id = f"walter-{mac.replace(':','')[-6:].upper()}"

        self.topic_cmd = f"warlock/walter/{mac}/commands"
        self.topic_rsp = f"warlock/walter/{mac}/response"
        self.topic_tel = f"warlock/walter/{mac}/telemetry"

        self.connected = False
        self.responses = []
        self.telemetry = []
        self.lock = threading.Lock()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"warlock-base-{int(time.time())}"
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.connected = True
        client.subscribe([(self.topic_rsp, 1), (self.topic_tel, 0)])
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [C2] Connected to {self.broker}")
        print(f"[{ts}] [C2] Listening for device {self.mac}")
        print(f"[{ts}] [C2]   Telemetry: {self.topic_tel}")
        print(f"[{ts}] [C2]   Response:  {self.topic_rsp}")
        print()

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self.connected = False
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [C2] Disconnected from broker")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode('utf-8', errors='replace')
        ts = datetime.now().strftime("%H:%M:%S")

        try:
            data = json.loads(payload)
        except:
            print(f"[{ts}] [RAW] {msg.topic}: {payload}")
            return

        msg_type = data.get('type', 'unknown')

        with self.lock:
            if msg.topic == self.topic_tel:
                self.telemetry.append(data)
            elif msg.topic == self.topic_rsp:
                self.responses.append(data)

        if msg_type == 'telemetry':
            print(f"[{ts}] [TEL]  uptime={data.get('uptime','?')}s "
                  f"wifi={data.get('wifi_ssid','none')} "
                  f"rssi={data.get('wifi_rssi','?')} "
                  f"lte={'UP' if data.get('lte_connected') else 'DOWN'} "
                  f"mqtt={'UP' if data.get('mqtt_connected') else 'DOWN'} "
                  f"sats={data.get('sats','?')} "
                  f"temp={data.get('temp_c','?')}C "
                  f"heap={data.get('free_heap','?')}")
            if data.get('lat') and data.get('lat', 0) != 0:
                print(f"         GPS: {data['lat']:.6f}, {data.get('lon',0):.6f} "
                      f"conf={data.get('confidence','?')}m")

        elif msg_type == 'wifi_scan':
            nets = data.get('networks', [])
            print(f"[{ts}] [SCAN] {len(nets)} networks found:")
            for n in sorted(nets, key=lambda x: x.get('rssi', -999), reverse=True):
                ssid = n.get('ssid', '<hidden>')
                bssid = n.get('bssid', '??')
                rssi = n.get('rssi', '?')
                ch = n.get('channel', '?')
                auth = n.get('auth', '?')
                bar = self._signal_bar(rssi if isinstance(rssi, (int, float)) else -100)
                print(f"         {bar} {rssi:>4} dBm  ch{str(ch):>2}  {auth:<10}  {ssid:<28} {bssid}")
            print()

        elif msg_type == 'status':
            print(f"[{ts}] [STAT] {json.dumps(data, indent=2)}")
            print()

        elif msg_type == 'gnss_fix':
            lat = data.get('lat', 0)
            lon = data.get('lon', 0)
            sats = data.get('sats', '?')
            conf = data.get('confidence', '?')
            if lat == 0 and lon == 0:
                print(f"[{ts}] [GNSS] NO FIX — sats={sats}, conf={conf} (likely indoor/weak sky)")
            else:
                print(f"[{ts}] [GNSS] FIX: {lat:.6f}, {lon:.6f}  sats={sats}  conf={conf}m")
                print(f"         https://maps.google.com/?q={lat},{lon}")
            print()

        else:
            print(f"[{ts}] [{msg_type.upper()}] {json.dumps(data, indent=2)}")
            print()

    @staticmethod
    def _signal_bar(rssi):
        if rssi >= -50: return "█████ EXCELLENT"
        if rssi >= -60: return "████▒ GOOD     "
        if rssi >= -70: return "███▒▒ FAIR     "
        if rssi >= -80: return "██▒▒▒ WEAK     "
        if rssi >= -90: return "█▒▒▒▒ POOR     "
        return "▒▒▒▒▒ NO SIGNAL"

    def connect(self, timeout=10):
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_start()
        start = time.time()
        while not self.connected and time.time() - start < timeout:
            time.sleep(0.2)
        return self.connected

    def send_command(self, command):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [SEND] >>> {command}")
        self.client.publish(self.topic_cmd, command, qos=1)

    def wait_response(self, timeout=30):
        start = time.time()
        initial = len(self.responses)
        while time.time() - start < timeout:
            with self.lock:
                if len(self.responses) > initial:
                    return self.responses[-1]
            time.sleep(0.3)
        return None

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def monitor(self):
        """Interactive monitor mode."""
        print()
        print("╔══════════════════════════════════════════════════════╗")
        print("║       WARLOCK C2 — WALTER BASE CAMP MONITOR         ║")
        print("╠══════════════════════════════════════════════════════╣")
        print(f"║  Broker: {self.broker:<42}     ║")
        print(f"║  Device: {self.mac:<42}     ║")
        print(f"║  Channel: LTE-M (AT&T) + MQTT                        ║")
        print("╠══════════════════════════════════════════════════════╣")
        print("║  Commands: status, wifi scan, gnss fix, lte status,  ║")
        print("║            lte connect, lte disconnect, reboot       ║")
        print("║  Type 'quit' or Ctrl-C to exit.                      ║")
        print("╚══════════════════════════════════════════════════════╝")
        print()

        try:
            while True:
                try:
                    cmd = input("walter> ").strip()
                except EOFError:
                    break
                if not cmd:
                    continue
                if cmd.lower() in ('quit', 'exit', 'q'):
                    break
                self.send_command(cmd)
        except KeyboardInterrupt:
            print("\n[C2] Shutting down...")

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Warlock C2 — Walter Base Camp Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('mode', nargs='?', default='monitor',
                       help='monitor | status | scan | cmd | gnss')
    parser.add_argument('command', nargs='?', default=None,
                       help='Command to send (for cmd mode) or MAC address')
    parser.add_argument('--mac', default=DEFAULT_MAC,
                       help=f'Walter MAC (default: {DEFAULT_MAC})')
    parser.add_argument('--broker', default=BROKER,
                       help=f'MQTT broker (default: {BROKER})')
    args = parser.parse_args()

    # Parse args flexibly — second positional might be MAC or command
    mac = args.mac
    command = args.command

    # If mode is a known command and command looks like MAC, swap
    if args.mode and ':' in args.mode:
        mac = args.mode
        mode = 'monitor'
    elif command and ':' in command:
        mac = command
        command = None

    mode = args.mode if args.mode not in (mac,) else 'monitor'

    c2 = WalterC2(mac=mac, broker=args.broker)

    if not c2.connect():
        print("[ERROR] Could not connect to MQTT broker")
        sys.exit(1)

    if mode == 'monitor':
        c2.monitor()

    elif mode == 'status':
        c2.send_command("status")
        rsp = c2.wait_response(30)
        if not rsp:
            print("[TIMEOUT] No response from Walter")

    elif mode == 'scan':
        c2.send_command("wifi scan")
        rsp = c2.wait_response(30)
        if not rsp:
            print("[TIMEOUT] No response from Walter")

    elif mode == 'gnss':
        print("[C2] Requesting GNSS fix (LTE will time-slice off, up to 2 min)...")
        c2.send_command("gnss fix")
        rsp = c2.wait_response(180)
        if not rsp:
            print("[TIMEOUT] No GNSS response — Walter may be indoors or LTE re-linking")

    elif mode == 'cmd':
        if not command:
            print("Usage: walter-c2.py cmd <command>")
            sys.exit(1)
        c2.send_command(command)
        rsp = c2.wait_response(30)
        if not rsp:
            print("[TIMEOUT] No response from Walter")
    else:
        # Treat unknown mode as a raw command
        c2.send_command(mode)
        rsp = c2.wait_response(30)

    # Brief pause to let final messages arrive
    time.sleep(2)
    c2.disconnect()

if __name__ == '__main__':
    main()
