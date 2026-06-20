"""
WARLOCK RED TEAM — BASE CAMP SERVER
=====================================
FastAPI backend with MQTT + Serial bridge for Walter C2 devices.

- Operator login (basic auth + session cookie for WS/fetch compatibility)
- WebSocket push of live telemetry/responses to dashboard
- MQTT bridge subscribes to all warlock/walter/+/+ topics
- Serial bridge reads /dev/ttyACM1 directly (works without LTE)
- REST API for command history, telemetry, scan results
- SQLite for persistence (no external DB needed for MVP)

Deploy on the R750 or anywhere with Python 3.10+.
"""

import os
import json
import time
import asyncio
import sqlite3
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

import serial as pyserial
import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, status, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ============================================================
# CONFIG
# ============================================================

MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.emqx.io")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_ROOT = "warlock/walter/+/+"

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM1")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))

DB_PATH = os.environ.get("BASECAMP_DB", "/home/sem/Arduino/walter_c2/basecamp/basecamp.db")

OPERATOR_USER = os.environ.get("OPERATOR_USER", "admin")
OPERATOR_PASS = os.environ.get("OPERATOR_PASS", "warlock-c2-change-me")

# ============================================================
# SESSION TOKENS (simple in-memory)
# ============================================================

_session_tokens: set[str] = set()

def make_session_token() -> str:
    token = secrets.token_urlsafe(32)
    _session_tokens.add(token)
    return token

def valid_session(token: Optional[str]) -> bool:
    return token is not None and token in _session_tokens

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY,
            device_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            wifi_ssid TEXT,
            wifi_ip TEXT,
            wifi_rssi INTEGER,
            lte_connected INTEGER DEFAULT 0,
            mqtt_connected INTEGER DEFAULT 0,
            operator TEXT,
            band TEXT,
            cell_id TEXT,
            rsrp REAL,
            lat REAL,
            lon REAL,
            sats INTEGER,
            temp_c REAL,
            free_heap INTEGER,
            uptime_s INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT,
            ts TEXT,
            payload TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT,
            ts TEXT,
            event_type TEXT,
            payload TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT,
            ts TEXT,
            command TEXT,
            operator TEXT,
            response TEXT,
            via TEXT DEFAULT 'mqtt'
        )
    """)
    # Migrations: add columns that may not exist in older DBs
    # Check for 'via' column in commands
    c.execute("PRAGMA table_info(commands)")
    cols = [row[1] for row in c.fetchall()]
    if 'via' not in cols:
        c.execute("ALTER TABLE commands ADD COLUMN via TEXT DEFAULT 'mqtt'")

    conn.commit()
    conn.close()

# ============================================================
# SHARED STATE
# ============================================================

class SharedState:
    """Shared between MQTT bridge, serial bridge, and WebSocket clients."""
    def __init__(self):
        self.websocket_clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mqtt_connected = False
        self.serial_connected = False

    def broadcast(self, msg_json: str):
        """Thread-safe broadcast to all WebSocket clients."""
        if not self.loop:
            return
        dead = set()
        for ws in list(self.websocket_clients):
            try:
                asyncio.run_coroutine_threadsafe(
                    ws.send_text(msg_json), self.loop
                )
            except Exception:
                dead.add(ws)
        self.websocket_clients -= dead

    def store(self, mac, msg_type, data):
        """Store message in SQLite. Thread-safe (new connection each call)."""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()

        if msg_type in ("telemetry", "status"):
            # Map uptime from either field name
            uptime = data.get('uptime', data.get('uptime_s'))

            c.execute("""
                INSERT INTO telemetry (mac, ts, payload) VALUES (?, ?, ?)
            """, (mac, now, json.dumps(data)))
            c.execute("""
                INSERT INTO devices (mac, device_id, first_seen, last_seen, wifi_ssid, wifi_ip,
                    wifi_rssi, lte_connected, mqtt_connected, operator, band, cell_id, rsrp,
                    lat, lon, sats, temp_c, free_heap, uptime_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    device_id=excluded.device_id, last_seen=excluded.last_seen,
                    wifi_ssid=excluded.wifi_ssid, wifi_ip=excluded.wifi_ip,
                    wifi_rssi=excluded.wifi_rssi, lte_connected=excluded.lte_connected,
                    mqtt_connected=excluded.mqtt_connected, operator=excluded.operator,
                    band=excluded.band, cell_id=excluded.cell_id, rsrp=excluded.rsrp,
                    lat=excluded.lat, lon=excluded.lon, sats=excluded.sats,
                    temp_c=excluded.temp_c, free_heap=excluded.free_heap,
                    uptime_s=excluded.uptime_s
            """, (
                mac, data.get('device', mac), now, now,
                data.get('wifi_ssid'), data.get('wifi_ip'),
                data.get('wifi_rssi'),
                1 if data.get('lte_connected') else 0,
                1 if data.get('mqtt_connected') else 0,
                data.get('operator'), data.get('band'), data.get('cell_id'),
                data.get('rsrp'),
                data.get('lat'), data.get('lon'),
                data.get('sats'), data.get('temp_c'),
                data.get('free_heap'), uptime
            ))
        elif msg_type in ("response", "wifi_scan", "lte_status", "gnss_fix", "error", "status"):
            c.execute("""
                INSERT INTO events (mac, ts, event_type, payload) VALUES (?, ?, ?, ?)
            """, (mac, now, msg_type, json.dumps(data)))

        conn.commit()
        conn.close()


state = SharedState()

# ============================================================
# MQTT BRIDGE
# ============================================================

class MQTTBridge:
    """Bridges MQTT messages to WebSocket clients and stores in SQLite."""

    def __init__(self):
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"basecamp-{int(time.time())}"
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def connect(self):
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.client.loop_start()
        except Exception as e:
            print(f"[MQTT] WARNING: Could not connect to {MQTT_BROKER}:{MQTT_PORT} — {e}", flush=True)
            print("[MQTT] Dashboard will run without MQTT bridge. Serial bridge still active.", flush=True)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        client.subscribe(MQTT_TOPIC_ROOT)
        state.mqtt_connected = True
        print(f"[MQTT] Connected to {MQTT_BROKER}, subscribed to {MQTT_TOPIC_ROOT}", flush=True)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        state.mqtt_connected = False
        print(f"[MQTT] Disconnected (rc={rc})", flush=True)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode('utf-8', errors='replace')

        try:
            data = json.loads(payload)
        except:
            data = {"raw": payload}

        parts = topic.split('/')
        mac = parts[2] if len(parts) >= 4 else "unknown"
        msg_type = parts[3] if len(parts) >= 4 else "unknown"
        data['_topic_type'] = msg_type
        data['_mac'] = mac
        data['_ts'] = datetime.now().isoformat()
        data['_source'] = 'mqtt'

        state.store(mac, msg_type, data)
        state.broadcast(json.dumps(data))

    def publish_command(self, mac: str, command: str):
        topic = f"warlock/walter/{mac}/commands"
        self.client.publish(topic, command, qos=1)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


# ============================================================
# SERIAL BRIDGE
# ============================================================

class SerialBridge:
    """Reads from Walter's USB serial port, bridges to WebSocket clients."""

    # Known Walter MAC (hardcoded for now — single device)
    WALTER_MAC = "48:CA:43:0F:90:38"

    def __init__(self):
        self.port = SERIAL_PORT
        self.baud = SERIAL_BAUD
        self.ser: pyserial.Serial | None = None
        self.thread: threading.Thread | None = None
        self.running = False
        self._buffer = ""

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True, name="serial-bridge")
        self.thread.start()

    def _connect(self):
        try:
            self.ser = pyserial.Serial(self.port, self.baud, timeout=0.5)
            self.ser.setDTR(True)
            self.ser.setRTS(False)
            state.serial_connected = True
            print(f"[SERIAL] Connected to {self.port}", flush=True)
        except Exception as e:
            self.ser = None
            state.serial_connected = False

    def _read_loop(self):
        while self.running:
            try:
                if not self.ser or not self.ser.is_open:
                    self._connect()
                    if not self.ser:
                        time.sleep(5)
                        continue

                n = self.ser.inWaiting()
                if n:
                    raw = self.ser.read(n).decode('utf-8', errors='replace')
                    self._process(raw)
                else:
                    time.sleep(0.1)
            except Exception as e:
                if "Errno 5" in str(e) or "Input/output" in str(e):
                    # Device disconnected
                    pass
                else:
                    print(f"[SERIAL] Error: {e}", flush=True)
                self.ser = None
                state.serial_connected = False
                time.sleep(5)

    def _process(self, text):
        """Parse serial output for RESP: lines."""
        self._buffer += text

        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            line = line.strip()

            if not line.startswith("RESP:"):
                continue

            payload = line[5:]
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue

            msg_type = data.get('type', 'response')
            data['_topic_type'] = msg_type
            data['_mac'] = self.WALTER_MAC
            data['_ts'] = datetime.now().isoformat()
            data['_source'] = 'serial'

            state.store(self.WALTER_MAC, msg_type, data)
            state.broadcast(json.dumps(data))

    def send_command(self, command: str) -> bool:
        if self.ser and self.ser.is_open:
            self.ser.write((command + '\n').encode())
            self.ser.flush()
            return True
        return False

    def stop(self):
        self.running = False
        if self.ser:
            try:
                self.ser.close()
            except:
                pass


bridge_mqtt = MQTTBridge()
bridge_serial = SerialBridge()

# ============================================================
# AUTH
# ============================================================

security = HTTPBasic(auto_error=False)

def _check_bearer_token(request: Request) -> bool:
    """Check Authorization: Bearer <token> header."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        return valid_session(token)
    return False

def auth_operator(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    # Check Bearer token first (used by dashboard JS)
    if _check_bearer_token(request):
        return "operator"

    # Check session cookie (for WS and fetch from dashboard)
    session = request.cookies.get("basecamp_session")
    if valid_session(session):
        return "operator"

    # Fall back to Basic Auth
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )
    correct_user = secrets.compare_digest(credentials.username.encode(), OPERATOR_USER.encode())
    correct_pass = secrets.compare_digest(credentials.password.encode(), OPERATOR_PASS.encode())
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def auth_optional(request: Request):
    """Check if request is authenticated via cookie, return bool."""
    session = request.cookies.get("basecamp_session")
    return valid_session(session)

# ============================================================
# FASTAPI APP
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    state.loop = asyncio.get_event_loop()
    bridge_mqtt.connect()
    bridge_serial.start()
    print(f"[BASECAMP] Server started. DB={DB_PATH} MQTT={MQTT_BROKER} SERIAL={SERIAL_PORT}", flush=True)
    yield
    bridge_serial.stop()
    bridge_mqtt.stop()

app = FastAPI(title="Warlock Red Team — Base Camp", lifespan=lifespan)

# --- Health / Status ---

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mqtt": {
            "broker": MQTT_BROKER,
            "connected": state.mqtt_connected,
        },
        "serial": {
            "port": SERIAL_PORT,
            "connected": state.serial_connected,
        },
        "time": datetime.now().isoformat()
    }

# --- Device API ---

@app.get("/api/devices")
async def list_devices(operator: str = Depends(auth_operator)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM devices ORDER BY last_seen DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.get("/api/devices/{mac}/telemetry")
async def device_telemetry(mac: str, limit: int = 50, operator: str = Depends(auth_operator)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM telemetry WHERE mac=? ORDER BY id DESC LIMIT ?",
        (mac, limit)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.get("/api/devices/{mac}/events")
async def device_events(mac: str, limit: int = 50, operator: str = Depends(auth_operator)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM events WHERE mac=? ORDER BY id DESC LIMIT ?",
        (mac, limit)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# --- Command sending ---

class CommandRequest(BaseModel):
    command: str
    via: str = "auto"  # "auto", "serial", "mqtt"

@app.post("/api/devices/{mac}/command")
async def send_command(mac: str, req: CommandRequest, operator: str = Depends(auth_operator)):
    via = req.via
    sent_via = None

    # Auto: prefer serial (works without LTE), fall back to MQTT
    if via == "auto":
        if bridge_serial.send_command(req.command):
            sent_via = "serial"
        elif state.mqtt_connected:
            bridge_mqtt.publish_command(mac, req.command)
            sent_via = "mqtt"
        else:
            raise HTTPException(503, "No communication channel available (serial and MQTT both down)")
    elif via == "serial":
        if not bridge_serial.send_command(req.command):
            raise HTTPException(503, f"Serial port {SERIAL_PORT} not connected")
        sent_via = "serial"
    elif via == "mqtt":
        if not state.mqtt_connected:
            raise HTTPException(503, "MQTT broker not connected")
        bridge_mqtt.publish_command(mac, req.command)
        sent_via = "mqtt"

    # Log command
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO commands (mac, ts, command, operator, via) VALUES (?, ?, ?, ?, ?)",
        (mac, datetime.now().isoformat(), req.command, operator, sent_via)
    )
    conn.commit()
    conn.close()

    return {"status": "sent", "command": req.command, "mac": mac, "via": sent_via}

@app.get("/api/commands")
async def command_history(limit: int = 50, operator: str = Depends(auth_operator)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# --- WebSocket for live updates ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Auth via cookie OR query param token
    session = None
    if ws.cookies:
        session = ws.cookies.get("basecamp_session")
    if not session:
        # Try query param (embedded in page for browsers that don't send cookies on WS)
        token = ws.query_params.get("token")
        if token:
            session = token
    if not valid_session(session):
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()
    state.websocket_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        state.websocket_clients.discard(ws)

# --- Login ---

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(content=LOGIN_HTML)

@app.post("/login")
async def login_submit(request: Request):
    import urllib.parse
    body = await request.body()
    params = urllib.parse.parse_qs(body.decode())
    user = params.get("username", [""])[0]
    pw = params.get("password", [""])[0]
    correct_user = secrets.compare_digest(user.encode(), OPERATOR_USER.encode())
    correct_pass = secrets.compare_digest(pw.encode(), OPERATOR_PASS.encode())
    if correct_user and correct_pass:
        token = make_session_token()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            key="basecamp_session", value=token,
            httponly=True, samesite="strict", max_age=86400
        )
        return response
    return JSONResponse({"ok": False, "msg": "ACCESS DENIED"}, status_code=401)

@app.get("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("basecamp_session")
    return response

# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Check session cookie — redirect to login if invalid
    session = request.cookies.get("basecamp_session")
    if not valid_session(session):
        return HTMLResponse(
            content='<meta http-equiv="refresh" content="0; url=/login">',
            status_code=302, headers={"Location": "/login"}
        )

    token = make_session_token()
    html = DASHBOARD_HTML.replace("__SESSION_TOKEN__", token)
    response = HTMLResponse(content=html)
    response.set_cookie(
        key="basecamp_session", value=token,
        httponly=True, samesite="strict", max_age=86400
    )
    return response


# ============================================================
# LOGIN PAGE — CRT TERMINAL
# ============================================================

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WARLOCK // ACCESS TERMINAL</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: #000; color: #00ff41;
    font-family: 'Courier New', monospace;
    overflow: hidden;
    height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  /* CRT scanlines */
  body::before {
    content: " "; position: fixed; top:0; left:0; bottom:0; right:0;
    background: linear-gradient(rgba(18,16,16,0) 50%, rgba(0,0,0,0.4) 50%);
    background-size: 100% 3px; z-index: 100; pointer-events: none;
  }
  /* CRT vignette + flicker */
  body::after {
    content: " "; position: fixed; top:0; left:0; bottom:0; right:0;
    background: radial-gradient(ellipse at center, rgba(0,0,0,0) 40%, rgba(0,0,0,0.6) 100%);
    z-index: 99; pointer-events: none;
    animation: flicker 0.15s infinite;
  }
  @keyframes flicker { 0%{opacity:0.95} 50%{opacity:1} 100%{opacity:0.92} }

  .screen { width: 600px; z-index: 1; }
  .glow { text-shadow: 0 0 5px currentColor, 0 0 10px currentColor; }

  pre.ascii {
    color: #00ff41; font-size: 11px; line-height: 1.1;
    text-shadow: 0 0 8px #00ff41, 0 0 15px #00ff41;
    margin-bottom: 20px;
  }
  .boot-lines {
    font-size: 13px; color: #00aa28; margin-bottom: 15px;
    min-height: 120px;
  }
  .boot-lines .line { opacity: 0; animation: fadeIn 0.1s forwards; }
  @keyframes fadeIn { to { opacity: 1; } }

  .login-box {
    border: 1px solid #00ff41;
    padding: 25px 30px;
    background: rgba(0, 30, 0, 0.4);
    box-shadow: 0 0 20px rgba(0, 255, 65, 0.3), inset 0 0 20px rgba(0, 255, 65, 0.05);
    position: relative;
  }
  .login-box::before {
    content: "┌── SECURE TERMINAL ──┐";
    position: absolute; top: -13px; left: 50%; transform: translateX(-50%);
    background: #000; padding: 0 10px; font-size: 11px; color: #00aa28;
  }
  .field { margin-bottom: 15px; }
  .field label { display: block; font-size: 12px; color: #00aa28; margin-bottom: 4px; }
  .field input {
    background: transparent; border: none; border-bottom: 1px solid #00aa28;
    color: #00ff41; font-family: 'Courier New', monospace; font-size: 16px;
    width: 100%; padding: 5px 0; outline: none;
    text-shadow: 0 0 5px #00ff41;
  }
  .field input:focus { border-bottom-color: #00ff41; }
  .field input::after { content: "_"; }

  .btn {
    background: transparent; border: 1px solid #00ff41; color: #00ff41;
    font-family: 'Courier New', monospace; font-size: 14px;
    padding: 8px 30px; cursor: pointer; margin-top: 10px;
    text-shadow: 0 0 5px #00ff41;
    transition: all 0.1s;
    letter-spacing: 1px;
  }
  .btn:hover { background: #00ff41; color: #000; text-shadow: none; }

  .status-msg { font-size: 13px; margin-top: 15px; min-height: 20px; }
  .status-msg.granted { color: #00ff41; animation: pulse 0.3s 3; }
  .status-msg.denied { color: #ff0040; animation: glitch 0.1s 5; }
  @keyframes pulse { 50% { opacity: 0.3; } }
  @keyframes glitch {
    0% { transform: translateX(0); } 25% { transform: translateX(-3px); }
    50% { transform: translateX(3px); } 75% { transform: translateX(-2px); }
    100% { transform: translateX(0); }
  }

  .cursor { display: inline-block; width: 8px; height: 15px; background: #00ff41;
    animation: blink 1s steps(2) infinite; vertical-align: middle; }
  @keyframes blink { 0%,50% { opacity: 1; } 51%,100% { opacity: 0; } }
</style>
</head>
<body>
<div class="screen">
  <pre class="ascii glow">
 ██╗    ██╗ █████╗ ███████╗ ██████╗ ██╗  ██╗ ██████╗  ██████╗ ██╗  ██╗
 ██║    ██║██╔══██╗██╔════╝██╔═══██╗██║ ██╔╝██╔═══██╗██╔═══██╗██║ ██╔╝
 ██║ █╗ ██║███████║███████╗██║   ██║█████╔╝ ██║   ██║██║   ██║█████╔╝
 ██║███╗██║██╔══██║╚════██║██║   ██║██╔═██╗ ██║   ██║██║   ██║██╔═██╗
 ╚███╔███╔╝██║  ██║███████║╚██████╔╝██║  ██╗╚██████╔╝╚██████╔╝██║  ██╗
  ╚══╝╚══╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝
  </pre>
  <div class="boot-lines glow" id="boot"></div>
  <div class="login-box glow">
    <div class="field">
      <label>&gt; OPERATOR ID</label>
      <input type="text" id="username" autocomplete="off" spellcheck="false">
    </div>
    <div class="field">
      <label>&gt; ACCESS CODE</label>
      <input type="password" id="password" autocomplete="off">
    </div>
    <button class="btn glow" onclick="doLogin()">[ AUTHENTICATE ]</button>
    <div class="status-msg" id="status"></div>
  </div>
</div>

<script>
// Boot sequence
const bootLines = [
  "[ OK ] Initializing Warlock Base Camp v2.0",
  "[ OK ] MQTT bridge → broker.emqx.io:1883",
  "[ OK ] Serial bridge → /dev/ttyACM1",
  "[ OK ] Encryption module loaded",
  "[ OK ] Firewall rules applied",
  "[ ** ] Awaiting operator credentials...",
];
const bootDiv = document.getElementById('boot');
bootLines.forEach((line, i) => {
  setTimeout(() => {
    const div = document.createElement('div');
    div.className = 'line';
    div.textContent = line;
    bootDiv.appendChild(div);
  }, i * 250);
});

// Focus username after boot
setTimeout(() => document.getElementById('username').focus(), bootLines.length * 250 + 200);

// Enter key submits from password field
document.getElementById('password').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});
document.getElementById('username').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('password').focus();
});

function doLogin() {
  const u = document.getElementById('username').value;
  const p = document.getElementById('password').value;
  const st = document.getElementById('status');

  st.className = 'status-msg';
  st.innerHTML = '<span style="color:#00aa28">VERIFYING CREDENTIALS<span class="cursor"></span></span>';

  const body = new URLSearchParams();
  body.append('username', u);
  body.append('password', p);

  fetch('/login', { method: 'POST', body: body })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        st.className = 'status-msg granted glow';
        st.innerHTML = '*** ACCESS GRANTED ***<br>WELCOME, OPERATOR';
        setTimeout(() => window.location.href = '/', 1200);
      } else {
        st.className = 'status-msg denied';
        st.innerHTML = '*** ACCESS DENIED ***<br>CREDENTIALS INVALID';
        document.getElementById('password').value = '';
        document.getElementById('password').focus();
      }
    })
    .catch(() => {
      st.className = 'status-msg denied';
      st.textContent = '*** CONNECTION ERROR ***';
    });
}
</script>
</body>
</html>"""


# ============================================================
# DASHBOARD — CRT TERMINAL
# ============================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WARLOCK // BASE CAMP</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  :root {
    --bg: #000700; --panel: #001a05; --border: #00ff41; --accent: #00ff41;
    --dim: #008f1a; --red: #ff0040; --amber: #ffb000; --text: #00d030;
    --cyan: #00ffff; --magenta: #ff00ff;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Courier New', monospace; font-size: 13px;
    overflow: hidden; height: 100vh;
  }
  /* CRT scanlines */
  body::before {
    content:""; position:fixed; top:0; left:0; bottom:0; right:0;
    background: linear-gradient(rgba(18,16,16,0) 50%, rgba(0,0,0,0.35) 50%);
    background-size: 100% 3px; z-index:1000; pointer-events:none;
  }
  /* CRT vignette + flicker */
  body::after {
    content:""; position:fixed; top:0; left:0; bottom:0; right:0;
    background: radial-gradient(ellipse at center, rgba(0,0,0,0) 50%, rgba(0,0,0,0.5) 100%);
    z-index:999; pointer-events:none; animation: flicker 0.12s infinite;
  }
  @keyframes flicker { 0%{opacity:0.97} 50%{opacity:1} 100%{opacity:0.95} }
  .glow { text-shadow: 0 0 4px currentColor, 0 0 8px currentColor; }

  /* Header */
  .header {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 8px 20px; display:flex; justify-content:space-between; align-items:center;
    box-shadow: 0 0 15px rgba(0,255,65,0.2);
    position: relative; z-index: 10;
  }
  .header h1 {
    color: var(--accent); font-size: 15px; letter-spacing: 3px;
    text-shadow: 0 0 6px var(--accent), 0 0 12px var(--accent);
    animation: glitchText 5s infinite;
  }
  @keyframes glitchText {
    0%, 95%, 100% { text-shadow: 0 0 6px var(--accent), 0 0 12px var(--accent); }
    96% { text-shadow: 2px 0 var(--magenta), -2px 0 var(--cyan), 0 0 8px var(--accent); }
    97% { text-shadow: -2px 0 var(--magenta), 2px 0 var(--cyan); }
    98% { text-shadow: 1px 0 var(--cyan), -1px 0 var(--magenta); }
  }
  .header .status { display:flex; gap:15px; align-items:center; font-size: 11px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.green { background: var(--accent); box-shadow: 0 0 6px var(--accent); animation: pulse 2s infinite; }
  .dot.red { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .dot.amber { background: var(--amber); box-shadow: 0 0 6px var(--amber); animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
  .logout-btn { color: var(--dim); text-decoration: none; font-size: 11px; border: 1px solid var(--dim);
    padding: 2px 8px; cursor: pointer; }
  .logout-btn:hover { color: var(--red); border-color: var(--red); }

  /* Layout */
  .container { display: grid; grid-template-columns: 300px 1fr; height: calc(100vh - 42px); }
  .sidebar {
    background: var(--panel); border-right: 1px solid var(--border);
    overflow-y: auto; box-shadow: inset -10px 0 15px -10px rgba(0,255,65,0.1);
  }
  .sidebar h2 {
    padding: 10px 15px; font-size: 10px; color: var(--dim); letter-spacing: 2px;
    border-bottom: 1px solid rgba(0,255,65,0.3); text-transform: uppercase;
  }
  .device-card {
    padding: 10px 15px; border-bottom: 1px solid rgba(0,255,65,0.15);
    cursor: pointer; transition: all 0.15s; position: relative;
  }
  .device-card:hover { background: rgba(0,255,65,0.08); }
  .device-card.active {
    background: rgba(0,255,65,0.1);
    border-left: 3px solid var(--accent);
    box-shadow: inset 0 0 10px rgba(0,255,65,0.1);
  }
  .device-card .mac { color: var(--accent); font-size: 12px; font-weight: bold; }
  .device-card .ssid { color: var(--dim); font-size: 10px; }
  .device-card .conn { display: flex; gap: 6px; margin-top: 4px; font-size: 10px; }
  .device-card .lastseen { color: var(--dim); font-size: 9px; margin-top: 2px; }
  .tag { padding: 1px 6px; border-radius: 0; }
  .tag.lte-up { background: rgba(0,255,65,0.15); color: var(--accent); border: 1px solid var(--dim); }
  .tag.lte-down { background: rgba(255,0,64,0.1); color: var(--red); border: 1px solid rgba(255,0,64,0.3); }
  .tag.wifi-up { background: rgba(0,143,26,0.15); color: var(--dim); border: 1px solid var(--dim); }

  .main { display: flex; flex-direction: column; overflow: hidden; }
  .tabs { display: flex; border-bottom: 1px solid var(--border); background: var(--panel); }
  .tab {
    padding: 8px 20px; cursor: pointer; color: var(--dim);
    border-bottom: 2px solid transparent; letter-spacing: 1px; font-size: 12px;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent);
    text-shadow: 0 0 5px var(--accent); }
  .tab:hover { color: var(--text); }
  .tab-content { flex: 1; overflow-y: auto; padding: 15px; display: none; }
  .tab-content.active { display: block; }

  /* Telemetry */
  .telemetry-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap: 8px; }
  .stat-box {
    background: var(--panel); border: 1px solid rgba(0,255,65,0.3); padding: 10px;
    position: relative;
  }
  .stat-box::before { content: "+"; position: absolute; top: -1px; left: -1px; color: var(--dim); }
  .stat-box::after { content: "+"; position: absolute; bottom: -1px; right: -1px; color: var(--dim); }
  .stat-box .label { font-size: 9px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; }
  .stat-box .value { font-size: 16px; color: var(--accent); margin-top: 3px; }
  .stat-box .value.red { color: var(--red); }
  .stat-box .value.amber { color: var(--amber); }

  /* Console */
  .quick-cmds { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 10px; }
  .quick-cmds button {
    background: var(--panel); border: 1px solid var(--dim); color: var(--text);
    padding: 3px 12px; cursor: pointer; font-family: monospace; font-size: 11px;
    transition: all 0.1s; letter-spacing: 1px;
  }
  .quick-cmds button:hover { border-color: var(--accent); color: var(--accent);
    text-shadow: 0 0 4px var(--accent); box-shadow: 0 0 8px rgba(0,255,65,0.3); }
  .console {
    background: #000; border: 1px solid rgba(0,255,65,0.3);
    font-family: monospace; font-size: 12px;
    box-shadow: inset 0 0 30px rgba(0,255,65,0.05);
  }
  .console-output { height: calc(100vh - 260px); overflow-y: auto; padding: 10px; }
  .console-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
  .console-line .ts { color: var(--dim); }
  .console-line.send { color: var(--amber); }
  .console-line.recv { color: var(--accent); }
  .console-line.event { color: var(--text); }
  .console-line.error { color: var(--red); }
  .console-line.system { color: var(--dim); font-style: italic; }
  .console-input { display: flex; border-top: 1px solid rgba(0,255,65,0.3); }
  .console-input .prompt { color: var(--accent); padding: 10px 5px 10px 10px; font-weight: bold; }
  .console-input input {
    flex: 1; background: #000; border: none; color: var(--accent);
    padding: 10px 0; font-family: monospace; font-size: 13px; outline: none;
    text-shadow: 0 0 4px var(--accent); caret-color: transparent;
  }
  .console-input button {
    background: var(--accent); color: #000; border: none; padding: 0 20px;
    font-family: monospace; font-weight: bold; cursor: pointer;
  }
  .console-input button:hover { box-shadow: 0 0 10px var(--accent); }

  /* WiFi scan */
  .scan-results table { width: 100%; border-collapse: collapse; }
  .scan-results th {
    text-align: left; padding: 6px 10px; color: var(--dim); font-size: 10px;
    border-bottom: 1px solid var(--border); text-transform: uppercase; letter-spacing: 1px;
  }
  .scan-results td { padding: 5px 10px; border-bottom: 1px solid rgba(0,255,65,0.1); font-size: 12px; }
  .scan-results tr:hover { background: rgba(0,255,65,0.05); }
  .signal-bar { display: inline-block; width: 60px; color: var(--accent); }

  .via-badge { font-size: 9px; padding: 1px 5px; border: 1px solid; margin-left: 8px; }
  .via-serial { border-color: var(--dim); color: var(--dim); }
  .via-mqtt { border-color: var(--cyan); color: var(--cyan); }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--panel); }
  ::-webkit-scrollbar-thumb { background: var(--dim); }
  ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
</style>
</head>
<body>
<div class="header">
  <h1 class="glow">WARLOCK // BASE CAMP</h1>
  <div class="status">
    <span class="glow"><span class="dot amber" id="ws-dot"></span> <span id="ws-status">WS: connecting...</span></span>
    <span class="glow"><span class="dot red" id="serial-dot"></span> <span id="serial-status">SERIAL: --</span></span>
    <span class="glow"><span class="dot red" id="mqtt-dot"></span> <span id="mqtt-status">MQTT: --</span></span>
    <a class="logout-btn" href="/login" onclick="fetch('/logout');return true;">[X]</a>
  </div>
</div>
<div class="container">
  <div class="sidebar">
    <h2>Deployed Devices</h2>
    <div id="devices-list"><div style="padding:15px;color:var(--dim)">Loading devices...</div></div>
  </div>
  <div class="main">
    <div class="tabs">
      <div class="tab active" data-tab="console">Command Console</div>
      <div class="tab" data-tab="telemetry">Telemetry</div>
      <div class="tab" data-tab="scan">WiFi Recon</div>
    </div>

    <!-- Command Console Tab -->
    <div class="tab-content active" id="tab-console">
      <div class="quick-cmds">
        <button onclick="sendCmd('status')">status</button>
        <button onclick="sendCmd('wifi scan')">wifi scan</button>
        <button onclick="sendCmd('lte status')">lte status</button>
        <button onclick="sendCmd('gnss fix')">gnss fix</button>
        <button onclick="sendCmd('help')">help</button>
      </div>
      <div class="console">
        <div class="console-output" id="console-output"></div>
        <div class="console-input">
          <span class="prompt glow">root@warlock:~$</span>
          <input id="cmd-input" placeholder="" onkeydown="if(event.key==='Enter')submitCmd()" autofocus>
          <button onclick="submitCmd()">EXEC</button>
        </div>
      </div>
    </div>

    <!-- Telemetry Tab -->
    <div class="tab-content" id="tab-telemetry">
      <div class="telemetry-grid" id="telemetry-grid">
        <div style="color:var(--dim)">Select a device to view telemetry...</div>
      </div>
    </div>

    <!-- WiFi Scan Tab -->
    <div class="tab-content" id="tab-scan">
      <div class="scan-results" id="scan-results">
        <table>
          <thead><tr><th>SSID</th><th>BSSID</th><th>RSSI</th><th>Signal</th><th>Ch</th><th>Auth</th></tr></thead>
          <tbody id="scan-tbody"><tr><td colspan="6" style="color:var(--dim)">Send 'wifi scan' to scan...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<script>
let selectedMac = null;
let devices = {};

// --- WebSocket connection ---
const SESSION_TOKEN = "__SESSION_TOKEN__";
const wsScheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(wsScheme + '//' + location.host + '/ws?token=' + SESSION_TOKEN);

// --- Fallback: poll for data if WS fails (ad blockers, proxy issues) ---
let wsActive = false;
let pollTimer = null;

function startPolling() {
  if (pollTimer) return;
  console.log('[FALLBACK] WS failed, starting API polling');
  pollTimer = setInterval(pollDeviceData, 5000);
  pollDeviceData(); // immediate
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function pollDeviceData() {
  if (!selectedMac) return;
  api(`/api/devices/${selectedMac}/events?limit=1`)
    .then(r => r.json())
    .then(events => {
      if (events && events.length > 0) {
        const e = events[0];
        const data = JSON.parse(e.payload);
        data._topic_type = e.event_type;
        data._mac = e.mac;
        data._ts = e.ts;
        data._source = 'poll';
        handleMessage(data);
      }
    })
    .catch(() => {});
}

ws.onopen = () => {
  wsActive = true;
  document.getElementById('ws-dot').className = 'dot green';
  document.getElementById('ws-status').textContent = 'WS: connected';
  addConsoleLine('--', 'system', 'WebSocket connected');
  stopPolling();
};
ws.onclose = () => {
  wsActive = false;
  document.getElementById('ws-dot').className = 'dot red';
  document.getElementById('ws-status').textContent = 'WS: disconnected';
  addConsoleLine('--', 'system', 'WebSocket disconnected — using polling fallback');
  startPolling();
};
ws.onerror = () => {
  document.getElementById('ws-dot').className = 'dot red';
  document.getElementById('ws-status').textContent = 'WS: error';
};
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  handleMessage(data);
};

// Start polling after 3s if WS hasn't connected
setTimeout(() => { if (!wsActive) startPolling(); }, 3000);

// --- API helper: uses XMLHttpRequest to bypass Chromium credential-URL fetch restriction ---
function api(url, opts) {
  return new Promise((resolve, reject) => {
    var xhr = new XMLHttpRequest();
    var method = (opts && opts.method) || 'GET';
    xhr.open(method, url);
    xhr.setRequestHeader('Authorization', 'Bearer ' + SESSION_TOKEN);
    if (opts && opts.headers) {
      for (var k in opts.headers) {
        xhr.setRequestHeader(k, opts.headers[k]);
      }
    }
    xhr.onload = function() {
      var ok = xhr.status >= 200 && xhr.status < 300;
      resolve({
        ok: ok,
        status: xhr.status,
        json: function() { return Promise.resolve(JSON.parse(xhr.responseText)); },
        text: function() { return Promise.resolve(xhr.responseText); }
      });
    };
    xhr.onerror = function() { reject(new Error('XHR failed: ' + url)); };
    xhr.send(opts && opts.body ? opts.body : null);
  });
}

// --- Status polling ---
function pollStatus() {
  api('/api/health')
    .then(r => r.json())
    .then(data => {
      // Serial status
      const sd = document.getElementById('serial-dot');
      const ss = document.getElementById('serial-status');
      if (data.serial.connected) {
        sd.className = 'dot green';
        ss.textContent = 'SERIAL: ' + data.serial.port;
      } else {
        sd.className = 'dot red';
        ss.textContent = 'SERIAL: offline';
      }
      // MQTT status
      const md = document.getElementById('mqtt-dot');
      const ms = document.getElementById('mqtt-status');
      if (data.mqtt.connected) {
        md.className = 'dot green';
        ms.textContent = 'MQTT: connected';
      } else {
        md.className = 'dot red';
        ms.textContent = 'MQTT: offline';
      }
    })
    .catch(() => {});
}
setInterval(pollStatus, 5000);
pollStatus();

// --- Message handler ---
function handleMessage(data) {
  const mac = data._mac;
  const type = data._topic_type;
  const source = data._source || 'mqtt';

  // Update device list for any status/telemetry type
  if (type === 'telemetry' || type === 'status' || type === 'lte_status') {
    devices[mac] = { ...devices[mac], ...data, lastSeen: data._ts };
    renderDevices();
    if (mac === selectedMac) renderTelemetry(data);
  }

  // Console output
  const out = document.getElementById('console-output');
  const ts = new Date(data._ts).toLocaleTimeString();
  const viaTag = source === 'serial' ?
    '<span class="via-badge via-serial">serial</span>' :
    '<span class="via-badge via-mqtt">mqtt</span>';

  if (type === 'response' || type === 'wifi_scan' || type === 'lte_status' ||
      type === 'gnss_fix' || type === 'error' || type === 'help') {
    addConsoleLine(ts, 'recv', JSON.stringify(data, null, 2) + viaTag);
    if (data.type === 'wifi_scan') renderScan(data.networks);
  } else if (type === 'status' && source === 'serial') {
    // Show status responses from serial (command responses) in console
    addConsoleLine(ts, 'recv', JSON.stringify(data, null, 2) + viaTag);
  } else if (type === 'telemetry') {
    if (mac === selectedMac) {
      addConsoleLine(ts, 'event', `[TEL] uptime=${data.uptime||data.uptime_s||'?'}s lte=${data.lte_connected?'UP':'DOWN'} mqtt=${data.mqtt_connected?'UP':'DOWN'} temp=${data.temp_c||'?'}C${viaTag}`);
    }
  }
}

function addConsoleLine(ts, cls, text) {
  const out = document.getElementById('console-output');
  const div = document.createElement('div');
  div.className = 'console-line ' + cls;
  div.innerHTML = `<span class="ts">[${ts}]</span> ${text}`;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
}

// --- Device list ---
function renderDevices() {
  const list = document.getElementById('devices-list');
  const keys = Object.keys(devices);
  if (keys.length === 0) {
    list.innerHTML = '<div style="padding:15px;color:var(--dim)">No devices seen yet</div>';
    return;
  }
  list.innerHTML = '';
  for (const [mac, d] of Object.entries(devices)) {
    const card = document.createElement('div');
    card.className = 'device-card' + (mac === selectedMac ? ' active' : '');
    card.onclick = () => selectDevice(mac);
    const lteTag = d.lte_connected ?
      '<span class="tag lte-up">LTE UP</span>' :
      '<span class="tag lte-down">LTE DOWN</span>';
    const wifiTag = d.wifi_ssid ?
      `<span class="tag wifi-up">${d.wifi_ssid}</span>` : '';
    const devName = d.device || d.device_id || mac;
    const lastSeen = d.lastSeen ? formatTimeAgo(d.lastSeen) : '';
    card.innerHTML = `
      <div class="mac">${devName}</div>
      <div class="ssid">${mac}</div>
      <div class="conn">${lteTag} ${wifiTag}</div>
      <div class="lastseen">${lastSeen}</div>
    `;
    list.appendChild(card);
  }
}

function formatTimeAgo(iso) {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return d.toLocaleDateString();
}

function selectDevice(mac) {
  selectedMac = mac;
  renderDevices();
  document.getElementById('console-output').innerHTML = '';
  addConsoleLine('--', 'system', `Selected device: ${mac}`);
  // Render existing telemetry if we have it
  if (devices[mac]) {
    renderTelemetry(devices[mac]);
    // Load recent events
    loadEvents(mac);
  }
}

function loadEvents(mac) {
  api(`/api/devices/${mac}/events?limit=10`)
    .then(r => r.json())
    .then(events => {
      events.reverse().forEach(e => {
        try {
          const d = JSON.parse(e.payload);
          d._ts = e.ts;
          d._mac = e.mac;
          d._source = 'history';
          const ts = new Date(e.ts).toLocaleTimeString();
          if (d.type === 'wifi_scan') {
            // don't replay scans
          } else {
            addConsoleLine(ts, 'event', `[HIST] ${e.event_type}: ${JSON.stringify(d)}`);
          }
        } catch {}
      });
    })
    .catch(() => {});
}

// --- Telemetry rendering ---
function renderTelemetry(data) {
  const grid = document.getElementById('telemetry-grid');
  const uptime = data.uptime || data.uptime_s;
  const stats = [
    {label:'WiFi SSID', value: data.wifi_ssid || 'N/A'},
    {label:'WiFi IP', value: data.wifi_ip || 'N/A'},
    {label:'WiFi RSSI', value: data.wifi_rssi != null ? data.wifi_rssi+' dBm' : 'N/A'},
    {label:'LTE', value: data.lte_connected ? 'CONNECTED' : 'DOWN',
      cls: data.lte_connected ? '' : 'red'},
    {label:'MQTT', value: data.mqtt_connected ? 'CONNECTED' : 'DOWN',
      cls: data.mqtt_connected ? '' : 'red'},
    {label:'Uptime', value: uptime != null ? Math.floor(uptime/60)+'m' : '?'},
    {label:'Temp', value: data.temp_c != null ? data.temp_c+'\u00b0C' : '?',
      cls: data.temp_c > 50 ? 'amber' : ''},
    {label:'Free Heap', value: data.free_heap ? data.free_heap.toLocaleString() : '?'},
    {label:'GPS', value: (data.lat && data.lat !== 0) ?
      data.lat.toFixed(4)+', '+data.lon.toFixed(4) : 'No fix',
      cls: (data.lat && data.lat !== 0) ? '' : 'amber'},
    {label:'Sats', value: data.sats || '?'},
    {label:'Operator', value: data.operator || '--'},
    {label:'Band', value: data.band || '--'},
  ];
  grid.innerHTML = stats.map(s =>
    `<div class="stat-box"><div class="label">${s.label}</div>
     <div class="value ${s.cls||''}">${s.value}</div></div>`
  ).join('');
}

// --- WiFi scan rendering ---
function renderScan(networks) {
  const tbody = document.getElementById('scan-tbody');
  if (!networks || networks.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--dim)">No networks found</td></tr>';
    return;
  }
  networks.sort((a,b) => b.rssi - a.rssi);
  tbody.innerHTML = networks.map(n => {
    const bars = n.rssi >= -50 ? '\u2588\u2588\u2588\u2588\u2588' :
                 n.rssi >= -60 ? '\u2588\u2588\u2588\u2588\u2591' :
                 n.rssi >= -70 ? '\u2588\u2588\u2588\u2591\u2591' :
                 n.rssi >= -80 ? '\u2588\u2588\u2591\u2591\u2591' :
                 n.rssi >= -90 ? '\u2588\u2591\u2591\u2591\u2591' : '\u2591\u2591\u2591\u2591\u2591';
    return `<tr>
      <td>${n.ssid||'<hidden>'}</td>
      <td style="color:var(--dim)">${n.bssid}</td>
      <td style="color:var(--accent)">${n.rssi}</td>
      <td><span class="signal-bar">${bars}</span></td>
      <td>${n.channel}</td>
      <td>${n.auth}</td>
    </tr>`;
  }).join('');
}

// --- Command sending ---
function sendCmd(cmd) {
  if (!selectedMac) {
    // Auto-select first device
    const keys = Object.keys(devices);
    if (keys.length > 0) {
      selectedMac = keys[0];
      renderDevices();
    } else {
      addConsoleLine(new Date().toLocaleTimeString(), 'error', 'No device selected and none available');
      return;
    }
  }
  const ts = new Date().toLocaleTimeString();
  addConsoleLine(ts, 'send', `>>> ${cmd}`);
  api(`/api/devices/${selectedMac}/command`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: cmd, via: 'auto'})
  })
  .then(r => r.json())
  .then(data => {
    if (data.via === 'serial') {
      addConsoleLine(new Date().toLocaleTimeString(), 'system', `[sent via SERIAL]`);
    } else if (data.via === 'mqtt') {
      addConsoleLine(new Date().toLocaleTimeString(), 'system', `[sent via MQTT]`);
    }
  })
  .catch(err => {
    addConsoleLine(new Date().toLocaleTimeString(), 'error', `Send failed: ${err}`);
  });
}

function submitCmd() {
  const input = document.getElementById('cmd-input');
  const cmd = input.value.trim();
  if (!cmd) return;
  sendCmd(cmd);
  input.value = '';
}

// --- Tab switching ---
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  };
});

// --- Initial device load from DB ---
api('/api/devices')
  .then(r => {
    if (!r.ok) throw new Error('auth failed');
    return r.json();
  })
  .then(data => {
    if (data && data.length > 0) {
      data.forEach(d => {
        devices[d.mac] = {
          ...d,
          device: d.device_id,
          lastSeen: d.last_seen,
          uptime: d.uptime_s,
        };
      });
      renderDevices();
      // Auto-select the first device
      selectDevice(Object.keys(devices)[0]);
      addConsoleLine('--', 'system', `Loaded ${data.length} device(s) from database`);
    } else {
      renderDevices();
      addConsoleLine('--', 'system', 'No devices in database yet. Waiting for first telemetry...');
    }
  })
  .catch(err => {
    addConsoleLine('--', 'error', `Failed to load devices: ${err}`);
    document.getElementById('devices-list').innerHTML =
      '<div style="padding:15px;color:var(--red)">Failed to load. Check auth.</div>';
  });
</script>
</body>
</html>"""
