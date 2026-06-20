"""
WARLOCK RED TEAM — BASE CAMP SERVER
=====================================
FastAPI backend with MQTT bridge for Walter C2 devices.

- Operator login (basic auth, configurable)
- WebSocket push of live telemetry/responses to dashboard
- MQTT bridge subscribes to all warlock/walter/+/+ topics
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
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# CONFIG
# ============================================================

MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.emqx.io")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_ROOT = "warlock/walter/+/+"

DB_PATH = os.environ.get("BASECAMP_DB", "/home/sem/Arduino/walter_c2/basecamp/basecamp.db")

# Operator credentials — change these! Or set via env.
OPERATOR_USER = os.environ.get("OPERATOR_USER", "admin")
OPERATOR_PASS = os.environ.get("OPERATOR_PASS", "warlock-c2-change-me")

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
            response TEXT
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# MQTT BRIDGE
# ============================================================

class MQTTBridge:
    """Bridges MQTT messages to WebSocket clients and stores in SQLite."""

    def __init__(self):
        self.websocket_clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"basecamp-{int(time.time())}"
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def connect(self):
        self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.client.loop_start()

    def _broadcast(self, msg_json: str):
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

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        client.subscribe(MQTT_TOPIC_ROOT)
        print(f"[MQTT] Connected to {MQTT_BROKER}, subscribed to {MQTT_TOPIC_ROOT}", flush=True)

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

        # Store in DB
        self._store(mac, msg_type, data)

        # Broadcast to WebSocket clients (thread-safe)
        self._broadcast(json.dumps(data))

    def _store(self, mac, msg_type, data):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()

        if msg_type == "telemetry":
            c.execute("""
                INSERT INTO telemetry (mac, ts, payload) VALUES (?, ?, ?)
            """, (mac, now, json.dumps(data)))
            c.execute("""
                INSERT INTO devices (mac, device_id, first_seen, last_seen, wifi_ssid, wifi_ip,
                    wifi_rssi, lte_connected, mqtt_connected, lat, lon, sats, temp_c, free_heap, uptime_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    device_id=excluded.device_id, last_seen=excluded.last_seen,
                    wifi_ssid=excluded.wifi_ssid, wifi_ip=excluded.wifi_ip,
                    wifi_rssi=excluded.wifi_rssi, lte_connected=excluded.lte_connected,
                    mqtt_connected=excluded.mqtt_connected, lat=excluded.lat, lon=excluded.lon,
                    sats=excluded.sats, temp_c=excluded.temp_c, free_heap=excluded.free_heap,
                    uptime_s=excluded.uptime_s
            """, (
                mac, data.get('device', ''), now, now,
                data.get('wifi_ssid'), data.get('wifi_ip'),
                data.get('wifi_rssi'),
                1 if data.get('lte_connected') else 0,
                1 if data.get('mqtt_connected') else 0,
                data.get('lat'), data.get('lon'),
                data.get('sats'), data.get('temp_c'),
                data.get('free_heap'), data.get('uptime')
            ))
        elif msg_type in ("response", "telemetry"):
            c.execute("""
                INSERT INTO events (mac, ts, event_type, payload) VALUES (?, ?, ?, ?)
            """, (mac, now, msg_type, json.dumps(data)))

        conn.commit()
        conn.close()

    def publish_command(self, mac: str, command: str):
        topic = f"warlock/walter/{mac}/commands"
        self.client.publish(topic, command, qos=1)

    def register_websocket(self, ws: WebSocket):
        self.websocket_clients.add(ws)

    def unregister_websocket(self, ws: WebSocket):
        self.websocket_clients.discard(ws)


bridge = MQTTBridge()

# ============================================================
# AUTH
# ============================================================

security = HTTPBasic()

def auth_operator(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username.encode(), OPERATOR_USER.encode())
    correct_pass = secrets.compare_digest(credentials.password.encode(), OPERATOR_PASS.encode())
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ============================================================
# FASTAPI APP
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bridge.loop = asyncio.get_event_loop()
    bridge.connect()
    print(f"[BASECAMP] Server started. DB={DB_PATH} MQTT={MQTT_BROKER}", flush=True)
    yield
    bridge.client.loop_stop()
    bridge.client.disconnect()

app = FastAPI(title="Warlock Red Team — Base Camp", lifespan=lifespan)

# --- API Routes ---

@app.get("/api/health")
async def health():
    return {"status": "ok", "broker": MQTT_BROKER, "time": datetime.now().isoformat()}

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

class CommandRequest(BaseModel):
    command: str

@app.post("/api/devices/{mac}/command")
async def send_command(mac: str, req: CommandRequest, operator: str = Depends(auth_operator)):
    bridge.publish_command(mac, req.command)
    # Log command
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO commands (mac, ts, command, operator) VALUES (?, ?, ?, ?)",
        (mac, datetime.now().isoformat(), req.command, operator)
    )
    conn.commit()
    conn.close()
    return {"status": "sent", "command": req.command, "mac": mac}

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
    await ws.accept()
    bridge.register_websocket(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        bridge.unregister_websocket(ws)

# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(operator: str = Depends(auth_operator)):
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WARLOCK RED TEAM — BASE CAMP</title>
<style>
  :root {
    --bg: #0a0e0f; --panel: #121819; --border: #1e2829; --accent: #00ff9f;
    --accent-dim: #00805a; --red: #ff3b3b; --amber: #ffb000; --text: #c8d0d0;
    --dim: #5a6868;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'Courier New',monospace; font-size:13px; }
  .header { background:var(--panel); border-bottom:1px solid var(--border); padding:10px 20px;
    display:flex; justify-content:space-between; align-items:center; }
  .header h1 { color:var(--accent); font-size:16px; letter-spacing:2px; }
  .header .status { display:flex; gap:15px; align-items:center; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .dot.green { background:var(--accent); box-shadow:0 0 6px var(--accent); }
  .dot.red { background:var(--red); }
  .container { display:grid; grid-template-columns:320px 1fr; height:calc(100vh - 48px); }
  .sidebar { background:var(--panel); border-right:1px solid var(--border); overflow-y:auto; }
  .sidebar h2 { padding:10px 15px; font-size:11px; color:var(--dim); text-transform:uppercase;
    border-bottom:1px solid var(--border); }
  .device-card { padding:12px 15px; border-bottom:1px solid var(--border); cursor:pointer;
    transition:background .15s; }
  .device-card:hover { background:rgba(0,255,159,0.05); }
  .device-card.active { background:rgba(0,255,159,0.08); border-left:3px solid var(--accent); }
  .device-card .mac { color:var(--accent); font-size:12px; font-weight:bold; }
  .device-card .ssid { color:var(--dim); font-size:11px; }
  .device-card .conn { display:flex; gap:8px; margin-top:4px; font-size:10px; }
  .tag { padding:1px 6px; border-radius:3px; }
  .tag.lte-up { background:rgba(0,255,159,0.15); color:var(--accent); }
  .tag.lte-down { background:rgba(255,59,59,0.15); color:var(--red); }
  .tag.wifi-up { background:rgba(0,128,90,0.15); color:var(--accent-dim); }
  .main { display:flex; flex-direction:column; overflow:hidden; }
  .tabs { display:flex; border-bottom:1px solid var(--border); background:var(--panel); }
  .tab { padding:10px 20px; cursor:pointer; color:var(--dim); border-bottom:2px solid transparent; }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); }
  .tab:hover { color:var(--text); }
  .tab-content { flex:1; overflow-y:auto; padding:15px; display:none; }
  .tab-content.active { display:block; }
  .telemetry-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; }
  .stat-box { background:var(--panel); border:1px solid var(--border); padding:12px; border-radius:4px; }
  .stat-box .label { font-size:10px; color:var(--dim); text-transform:uppercase; }
  .stat-box .value { font-size:18px; color:var(--accent); margin-top:4px; }
  .stat-box .value.red { color:var(--red); }
  .stat-box .value.amber { color:var(--amber); }
  .console { background:#050808; border:1px solid var(--border); border-radius:4px;
    font-family:monospace; font-size:12px; }
  .console-output { height:calc(100vh - 280px); overflow-y:auto; padding:10px; }
  .console-line { padding:1px 0; white-space:pre-wrap; word-break:break-all; }
  .console-line .ts { color:var(--dim); }
  .console-line.send { color:var(--amber); }
  .console-line.recv { color:var(--accent); }
  .console-line.event { color:var(--text); }
  .console-line.error { color:var(--red); }
  .console-input { display:flex; border-top:1px solid var(--border); }
  .console-input input { flex:1; background:#050808; border:none; color:var(--accent);
    padding:10px; font-family:monospace; font-size:13px; outline:none; }
  .console-input button { background:var(--accent); color:#000; border:none; padding:0 20px;
    font-family:monospace; font-weight:bold; cursor:pointer; }
  .scan-results table { width:100%; border-collapse:collapse; }
  .scan-results th { text-align:left; padding:6px 10px; color:var(--dim); font-size:11px;
    border-bottom:1px solid var(--border); text-transform:uppercase; }
  .scan-results td { padding:6px 10px; border-bottom:1px solid var(--border); }
  .signal-bar { display:inline-block; width:60px; }
  .quick-cmds { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .quick-cmds button { background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:4px 12px; border-radius:3px; cursor:pointer; font-family:monospace; font-size:11px; }
  .quick-cmds button:hover { border-color:var(--accent); color:var(--accent); }
  .spinner { color:var(--amber); }
</style>
</head>
<body>
<div class="header">
  <h1>WARLOCK RED TEAM — BASE CAMP</h1>
  <div class="status">
    <span><span class="dot green" id="ws-dot"></span> <span id="ws-status">WS: connecting...</span></span>
    <span><span class="dot green"></span> MQTT Bridge</span>
  </div>
</div>
<div class="container">
  <div class="sidebar">
    <h2>Deployed Devices</h2>
    <div id="devices-list"><div style="padding:15px;color:var(--dim)">Waiting for devices...</div></div>
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
          <input id="cmd-input" placeholder="enter command..." onkeydown="if(event.key==='Enter')submitCmd()" autofocus>
          <button onclick="submitCmd()">SEND</button>
        </div>
      </div>
    </div>

    <!-- Telemetry Tab -->
    <div class="tab-content" id="tab-telemetry">
      <div class="telemetry-grid" id="telemetry-grid">
        <div style="color:var(--dim)">Select a device and wait for telemetry...</div>
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

// WebSocket connection
const wsScheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(wsScheme + '//' + location.host + '/ws');

ws.onopen = () => {
  document.getElementById('ws-dot').className = 'dot green';
  document.getElementById('ws-status').textContent = 'WS: connected';
};
ws.onclose = () => {
  document.getElementById('ws-dot').className = 'dot red';
  document.getElementById('ws-status').textContent = 'WS: disconnected';
};
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  handleMessage(data);
};

function handleMessage(data) {
  const mac = data._mac;
  const type = data._topic_type;

  // Update device list
  if (type === 'telemetry' || type === 'status') {
    devices[mac] = { ...devices[mac], ...data, lastSeen: data._ts };
    renderDevices();
    if (mac === selectedMac) renderTelemetry(data);
  }

  // Console output
  const out = document.getElementById('console-output');
  const ts = new Date(data._ts).toLocaleTimeString();

  if (type === 'response') {
    addConsoleLine(ts, 'recv', JSON.stringify(data, null, 2));
    // Handle wifi scan response
    if (data.type === 'wifi_scan') renderScan(data.networks);
  } else if (type === 'telemetry') {
    // Only show telemetry in console if selected device
    if (mac === selectedMac) {
      addConsoleLine(ts, 'event', `[TEL] uptime=${data.uptime}s lte=${data.lte_connected?'UP':'DOWN'} mqtt=${data.mqtt_connected?'UP':'DOWN'} sats=${data.sats||'?'} temp=${data.temp_c}C`);
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

function renderDevices() {
  const list = document.getElementById('devices-list');
  if (Object.keys(devices).length === 0) {
    list.innerHTML = '<div style="padding:15px;color:var(--dim)">No devices online</div>';
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
    card.innerHTML = `
      <div class="mac">${d.device || mac}</div>
      <div class="ssid">${mac}</div>
      <div class="conn">${lteTag} ${wifiTag}</div>
    `;
    list.appendChild(card);
  }
}

function selectDevice(mac) {
  selectedMac = mac;
  renderDevices();
  document.getElementById('console-output').innerHTML = '';
  addConsoleLine('--', 'event', `Selected device: ${mac}`);
}

function renderTelemetry(data) {
  const grid = document.getElementById('telemetry-grid');
  const stats = [
    {label:'WiFi SSID', value: data.wifi_ssid || 'N/A'},
    {label:'WiFi IP', value: data.wifi_ip || 'N/A'},
    {label:'WiFi RSSI', value: data.wifi_rssi ? data.wifi_rssi+' dBm' : 'N/A'},
    {label:'LTE', value: data.lte_connected ? 'CONNECTED' : 'DOWN',
      cls: data.lte_connected ? '' : 'red'},
    {label:'MQTT', value: data.mqtt_connected ? 'CONNECTED' : 'DOWN',
      cls: data.mqtt_connected ? '' : 'red'},
    {label:'Uptime', value: data.uptime_s ? Math.floor(data.uptime_s/60)+'m' : '?'},
    {label:'Temp', value: data.temp_c ? data.temp_c+'°C' : '?',
      cls: data.temp_c > 50 ? 'amber' : ''},
    {label:'Free Heap', value: data.free_heap ? data.free_heap.toLocaleString() : '?'},
    {label:'GPS', value: (data.lat && data.lat !== 0) ?
      data.lat.toFixed(4)+', '+data.lon.toFixed(4) : 'No fix',
      cls: (data.lat && data.lat !== 0) ? '' : 'amber'},
    {label:'Sats', value: data.sats || '?'},
  ];
  grid.innerHTML = stats.map(s =>
    `<div class="stat-box"><div class="label">${s.label}</div>
     <div class="value ${s.cls||''}">${s.value}</div></div>`
  ).join('');
}

function renderScan(networks) {
  const tbody = document.getElementById('scan-tbody');
  if (!networks || networks.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--dim)">No networks found</td></tr>';
    return;
  }
  networks.sort((a,b) => b.rssi - a.rssi);
  tbody.innerHTML = networks.map(n => {
    const bars = n.rssi >= -50 ? '█████' :
                 n.rssi >= -60 ? '████░' :
                 n.rssi >= -70 ? '███░░' :
                 n.rssi >= -80 ? '██░░░' :
                 n.rssi >= -90 ? '█░░░░' : '░░░░░';
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

function sendCmd(cmd) {
  if (!selectedMac) { alert('Select a device first'); return; }
  const ts = new Date().toLocaleTimeString();
  addConsoleLine(ts, 'send', `>>> ${cmd}`);
  fetch(`/api/devices/${selectedMac}/command`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: cmd})
  });
}

function submitCmd() {
  const input = document.getElementById('cmd-input');
  const cmd = input.value.trim();
  if (!cmd) return;
  sendCmd(cmd);
  input.value = '';
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  };
});

// Initial device load
fetch('/api/devices').then(r => r.json()).then(data => {
  data.forEach(d => {
    devices[d.mac] = d;
    if (d.device_id) devices[d.mac].device = d.device_id;
  });
  renderDevices();
});
</script>
</body>
</html>"""
