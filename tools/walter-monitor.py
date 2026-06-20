#!/usr/bin/env python3
"""
Warlock-Walter C2 Monitor

Subscribes to a Walter device's MQTT topics and provides a CLI
for sending commands and viewing telemetry. Designed to run on
the host cyberdeck or a remote operator workstation.

Usage:
    python3 walter-monitor.py --mac AA:BB:CC:DD:EE:FF
    python3 walter-monitor.py --mac AA:BB:CC:DD:EE:FF --broker 192.168.1.100

Requirements:
    pip install paho-mqtt
"""

import argparse
import json
import sys
import time
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: Install paho-mqtt: pip install paho-mqtt")
    sys.exit(1)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[+] Connected to broker")
        mac = userdata['mac']
        topics = [
            f"warlock/walter/{mac}/telemetry",
            f"warlock/walter/{mac}/response",
        ]
        for t in topics:
            client.subscribe(t)
            print(f"[+] Subscribed to {t}")
    else:
        print(f"[-] Connection failed (code {rc})")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        data = {"raw": msg.payload.decode()}

    ts = datetime.now().strftime("%H:%M:%S")
    topic_short = msg.topic.split('/')[-1]

    if topic_short == "telemetry":
        wifi = data.get("wifi_ssid", "—")
        lte = "✓" if data.get("lte_connected") else "✗"
        lat = data.get("lat", "—")
        lon = data.get("lon", "—")
        temp = data.get("temp_c", "—")
        uptime = data.get("uptime", 0)
        
        print(f"\r[{ts}] TELEMETRY | WiFi:{wifi} LTE:{lte} "
              f"GPS:{lat},{lon} Temp:{temp}°C Up:{uptime}s")
    elif topic_short == "response":
        print(f"\r[{ts}] RESPONSE:")
        print(json.dumps(data, indent=2))
    else:
        print(f"\r[{ts}] {topic_short}: {json.dumps(data, indent=2)}")


def main():
    parser = argparse.ArgumentParser(description="Warlock-Walter C2 Monitor")
    parser.add_argument("--mac", required=True, help="Walter MAC address (XX:XX:XX:XX:XX:XX)")
    parser.add_argument("--broker", default="broker.emqx.io", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    args = parser.parse_args()

    mac = args.mac.upper()
    userdata = {"mac": mac}

    client = mqtt.Client(userdata=userdata)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[*] Connecting to {args.broker}:{args.port}...")
    try:
        client.connect(args.broker, args.port, 60)
    except Exception as e:
        print(f"[-] Connection error: {e}")
        sys.exit(1)

    client.loop_start()

    cmd_topic = f"warlock/walter/{mac}/commands"
    print(f"\n[*] Walter C2 Monitor — {mac}")
    print(f"[*] Command topic: {cmd_topic}")
    print(f"[*] Type commands or 'quit' to exit\n")

    while True:
        try:
            cmd = input("warlock> ").strip()
            if not cmd:
                continue
            if cmd.lower() in ('quit', 'exit', 'q'):
                break
            
            client.publish(cmd_topic, cmd, qos=1)
            print(f"[→] Sent: {cmd}")
        except KeyboardInterrupt:
            print("\n[*] Shutting down...")
            break

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
