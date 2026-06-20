/**
 * WARLOCK C2 FIRMWARE FOR WALTER
 * 
 * ESP32-S3 + Sequans GM02SP cellular C2 relay / drop box
 * 
 * Capabilities:
 *   - WiFi auto-connect to known networks (dual-homed with cellular)
 *   - LTE-M/NB-IoT cellular backhaul with MQTT C2 protocol
 *   - GNSS positioning (GPS + Galileo)
 *   - Serial command interface over USB
 *   - WiFi reconnaissance (AP scanning)
 *   - Time-sliced LTE/GNSS (Sequans limitation)
 * 
 * Author: Warlock
 * Target: DPTechnics Walter (ESP32-S3-WROOM-1-N16R2)
 * Library: WalterModem v1.5.0
 */

#include <WiFi.h>
#include <HardwareSerial.h>
#include <WalterModem.h>
#include <esp_mac.h>
#include <ArduinoJson.h>

// ============================================================
// CONFIGURATION
// ============================================================

// --- MQTT C2 Server ---
// TODO: Set to your own MQTT broker. For testing use a public one.
#define MQTT_HOST   "broker.emqx.io"
#define MQTT_PORT   1883
#define MQTT_USER   ""
#define MQTT_PASS   ""

// --- Cellular ---
#define CELLULAR_APN ""  // Empty = auto-detect from SIM
#define RADIO_TECH   WALTER_MODEM_RAT_LTEM

// --- Known WiFi Networks (synced from cyberdeck) ---
struct KnownNetwork {
  const char* ssid;
  const char* password;
};

KnownNetwork knownNetworks[] = {
  {"jb-wifi7",              "Dragon@123!@#"},
  {"GetOffMyLAN",           "Granger1!"},
  {"Silence_of_the_LANs",   "Granger1!"},
  {"DropItLikeItsHotspot",  ""},
};
const int numNetworks = sizeof(knownNetworks) / sizeof(KnownNetwork);

// --- Timing ---
#define TELEMETRY_INTERVAL_MS  60000   // 1 min between telemetry reports
#define GNSS_FIX_TIMEOUT_MS    120000  // 2 min max for GNSS fix
#define WIFI_CONNECT_TIMEOUT   15000   // 15s to connect to WiFi
#define SERIAL_BAUD            115200

// ============================================================
// STATE
// ============================================================

WalterModem modem;
WalterModemRsp rsp;

char deviceId[32];
char macStr[18];
char topicTelemetry[64];
char topicCommands[64];
char topicResponse[64];

bool lteReady = false;
bool mqttConnected = false;
bool gnssFixValid = false;
bool wifiConnected = false;

WMGNSSFixEvent lastFix = {};
unsigned long lastTelemetry = 0;

// ============================================================
// HELPERS
// ============================================================

void printBanner() {
  Serial.printf("\r\n");
  Serial.printf("========================================\r\n");
  Serial.printf("   WARLOCK C2 — WALTER DROP BOX\r\n");
  Serial.printf("   ESP32-S3 + Sequans GM02SP\r\n");
  Serial.printf("========================================\r\n");
  Serial.printf("Device ID: %s\r\n", deviceId);
  Serial.printf("MAC: %s\r\n", macStr);
  Serial.printf("Build: %s %s\r\n", __DATE__, __TIME__);
  Serial.printf("========================================\r\n\r\n");
}

void buildTopics() {
  sprintf(topicTelemetry, "warlock/walter/%s/telemetry", macStr);
  sprintf(topicCommands,  "warlock/walter/%s/commands",  macStr);
  sprintf(topicResponse,  "warlock/walter/%s/response",  macStr);
}

// ============================================================
// WIFI
// ============================================================

void tryWifiConnect() {
  if (wifiConnected && WiFi.status() == WL_CONNECTED) return;
  
  Serial.println("[WiFi] Scanning for known networks...");
  int n = WiFi.scanNetworks();
  if (n == 0) {
    Serial.println("[WiFi] No networks found");
    return;
  }

  for (int i = 0; i < n; i++) {
    String foundSsid = WiFi.SSID(i);
    for (int j = 0; j < numNetworks; j++) {
      if (foundSsid == knownNetworks[j].ssid) {
        Serial.printf("[WiFi] Known network found: %s (%ddBm)\r\n", 
                      knownNetworks[j].ssid, WiFi.RSSI(i));
        
        WiFi.mode(WIFI_STA);
        WiFi.begin(knownNetworks[j].ssid, knownNetworks[j].password);
        
        unsigned long start = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT) {
          delay(500);
          Serial.print(".");
        }
        
        if (WiFi.status() == WL_CONNECTED) {
          wifiConnected = true;
          Serial.printf("\r\n[WiFi] Connected! IP: %s\r\n", WiFi.localIP().toString().c_str());
          WiFi.scanDelete();
          return;
        } else {
          Serial.printf("\r\n[WiFi] Failed to connect to %s\r\n", knownNetworks[j].ssid);
          wifiConnected = false;
        }
        break;
      }
    }
  }
  WiFi.scanDelete();
}

String wifiScanJson() {
  int n = WiFi.scanNetworks();
  String result = "{\"type\":\"wifi_scan\",\"networks\":[";
  
  for (int i = 0; i < n; i++) {
    if (i > 0) result += ",";
    // Determine auth type
    const char* auth = "open";
    switch (WiFi.encryptionType(i)) {
      case WIFI_AUTH_WPA2_PSK:  auth = "WPA2"; break;
      case WIFI_AUTH_WPA_WPA2_PSK: auth = "WPA/WPA2"; break;
      case WIFI_AUTH_WPA_PSK:   auth = "WPA"; break;
      case WIFI_AUTH_OPEN:      auth = "open"; break;
      default:                  auth = "encrypted"; break;
    }
    result += "{\"ssid\":\"" + WiFi.SSID(i) + "\",";
    result += "\"bssid\":\"" + WiFi.BSSIDstr(i) + "\",";
    result += "\"rssi\":" + String(WiFi.RSSI(i)) + ",";
    result += "\"channel\":" + String(WiFi.channel(i)) + ",";
    result += "\"auth\":\"" + String(auth) + "\"}";
  }
  result += "]}";
  WiFi.scanDelete();
  return result;
}

// ============================================================
// CELLULAR / LTE
// ============================================================

// Query full SIM diagnostics. Returns true if SIM detected (ICCID non-empty).
// attempt = retry number for logging (0 = runtime query, not a retry)
bool querySIM(int attempt) {
  bool found = false;
  WalterModemRsp simRsp;
  
  // SIM state via AT+CPIN?
  if (modem.getSIMState(&simRsp)) {
    const char* stateNames[] = {
      "READY", "PIN_REQUIRED", "PUK_REQUIRED", "PHONE_TO_SIM_PIN",
      "PHONE_TO_FIRST_SIM_PIN", "PHONE_TO_FIRST_SIM_PUK", "PIN2_REQUIRED",
      "PUK2_REQUIRED", "NETWORK_PIN", "NETWORK_PUK", "NETWORK_SUBSET_PIN",
      "NETWORK_SUBSET_PUK", "SERVICE_PROVIDER_PIN", "SERVICE_PROVIDER_PUK",
      "CORPORATE_SIM", "CORPORATE_SIM_PUK", "NOT_INSERTED"
    };
    int st = (int)simRsp.data.simState;
    const char* stateName = (st >= 0 && st <= 16) ? stateNames[st] : "UNKNOWN";
    if (attempt > 0) {
      Serial.printf("[INIT] SIM state (attempt %d): %s\r\n", attempt, stateName);
    } else {
      Serial.printf("[SIM] State: %s\r\n", stateName);
    }
    if (st == 0) found = true; // READY is a good sign even before ICCID
  }
  
  // ICCID via AT+CCID (requires NO_RF or FULL state)
  if (modem.getSIMCardID(&simRsp)) {
    if (simRsp.data.simCardID.iccid[0] != '\0') {
      if (attempt > 0) {
        Serial.printf("[INIT] ICCID: %s\r\n", simRsp.data.simCardID.iccid);
      } else {
        Serial.printf("[SIM] ICCID: %s\r\n", simRsp.data.simCardID.iccid);
      }
      found = true;
    } else {
      if (attempt > 0) {
        Serial.printf("[INIT] SIM read attempt %d: ICCID empty\r\n", attempt);
      }
    }
  } else {
    if (attempt > 0) {
      Serial.printf("[INIT] SIM read attempt %d: getSIMCardID failed\r\n", attempt);
    } else {
      Serial.println("[SIM] getSIMCardID failed");
    }
  }
  
  // IMSI
  if (found) {
    if (modem.getSIMCardIMSI(&simRsp)) {
      if (attempt > 0) {
        Serial.printf("[INIT] IMSI: %s\r\n", simRsp.data.imsi);
      } else {
        Serial.printf("[SIM] IMSI: %s\r\n", simRsp.data.imsi);
      }
    }
  }
  
  return found;
}

// Runtime "sim" command — returns JSON with full SIM diagnostics
String simStatusJson() {
  String json = "{\"type\":\"sim_status\",";
  
  WalterModemRsp simRsp;
  
  // Op state
  // (we need to ensure NO_RF or FULL for ICCID read)
  
  // SIM state
  String stateStr = "unknown";
  if (modem.getSIMState(&simRsp)) {
    const char* stateNames[] = {
      "READY", "PIN_REQUIRED", "PUK_REQUIRED", "PHONE_TO_SIM_PIN",
      "PHONE_TO_FIRST_SIM_PIN", "PHONE_TO_FIRST_SIM_PUK", "PIN2_REQUIRED",
      "PUK2_REQUIRED", "NETWORK_PIN", "NETWORK_PUK", "NETWORK_SUBSET_PIN",
      "NETWORK_SUBSET_PUK", "SERVICE_PROVIDER_PIN", "SERVICE_PROVIDER_PUK",
      "CORPORATE_SIM", "CORPORATE_SIM_PUK", "NOT_INSERTED"
    };
    int st = (int)simRsp.data.simState;
    if (st >= 0 && st <= 16) stateStr = stateNames[st];
  }
  json += "\"state\":\"" + stateStr + "\",";
  
  // ICCID
  String iccidStr = "";
  if (modem.getSIMCardID(&simRsp)) {
    if (simRsp.data.simCardID.iccid[0] != '\0') {
      iccidStr = String(simRsp.data.simCardID.iccid);
    }
  }
  json += "\"iccid\":\"" + iccidStr + "\",";
  
  // IMSI
  String imsiStr = "";
  if (iccidStr.length() > 0 && modem.getSIMCardIMSI(&simRsp)) {
    imsiStr = String(simRsp.data.imsi);
  }
  json += "\"imsi\":\"" + imsiStr + "\",";
  
  // Detected?
  json += "\"detected\":" + String(iccidStr.length() > 0 ? "true" : "false");
  json += "}";
  
  return json;
}

bool lteCheckConnected() {
  WalterModemNetworkRegState regState = modem.getNetworkRegState();
  return (regState == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
          regState == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING);
}

bool lteConnect() {
  Serial.println("[LTE] Connecting to cellular network...");

  if (!modem.setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
    Serial.println("[LTE] ERROR: Could not set NO_RF state");
    return false;
  }

  if (!modem.definePDPContext(1, CELLULAR_APN)) {
    Serial.println("[LTE] ERROR: Could not create PDP context");
    return false;
  }

  if (!modem.setOpState(WALTER_MODEM_OPSTATE_FULL)) {
    Serial.println("[LTE] ERROR: Could not set FULL state");
    return false;
  }

  if (!modem.setNetworkSelectionMode(WALTER_MODEM_NETWORK_SEL_MODE_AUTOMATIC)) {
    Serial.println("[LTE] ERROR: Could not set automatic selection");
    return false;
  }

  // Wait for registration — process serial commands during the wait
  // so we can query status live while testing
  Serial.print("[LTE] Waiting for registration");
  unsigned long start = millis();
  while (!lteCheckConnected()) {
    Serial.print(".");
    delay(1000);
    // Process incoming serial commands during the wait
    processSerial();
    if (millis() - start > 300000) {  // 5 min timeout
      Serial.println("\r\n[LTE] TIMEOUT: Could not register on network");
      return false;
    }
  }

  Serial.println();
  
  // Get cell info
  if (modem.getCellInformation(WALTER_MODEM_SQNMONI_REPORTS_SERVING_CELL, &rsp)) {
    Serial.printf("[LTE] Registered! Operator: %s (%u%02u), Band %u, Cell %u\r\n",
                  rsp.data.cellInformation.netName,
                  rsp.data.cellInformation.cc,
                  rsp.data.cellInformation.nc,
                  rsp.data.cellInformation.band,
                  rsp.data.cellInformation.cid);
    Serial.printf("[LTE] Signal: RSRP=%.1f, RSRQ=%.1f\r\n",
                  rsp.data.cellInformation.rsrp,
                  rsp.data.cellInformation.rsrq);
  }
  
  lteReady = true;
  return true;
}

bool lteDisconnect() {
  if (!modem.setOpState(WALTER_MODEM_OPSTATE_MINIMUM)) {
    Serial.println("[LTE] ERROR: Could not set MINIMUM state");
    return false;
  }

  WalterModemNetworkRegState regState = modem.getNetworkRegState();
  while (regState != WALTER_MODEM_NETWORK_REG_NOT_SEARCHING) {
    delay(100);
    regState = modem.getNetworkRegState();
  }

  Serial.println("[LTE] Disconnected");
  lteReady = false;
  return true;
}

// ============================================================
// MQTT C2
// ============================================================

static void mqttEventHandler(WMMQTTEventType event, const WMMQTTEventData* data, void* args) {
  switch (event) {
    case WALTER_MODEM_MQTT_EVENT_CONNECTED:
      if (data->rc == 0) {
        Serial.printf("[MQTT] Connected to broker\r\n");
        modem.mqttSubscribe(topicCommands);
        Serial.printf("[MQTT] Subscribed to %s\r\n", topicCommands);
        mqttConnected = true;
      } else {
        Serial.printf("[MQTT] Connection refused (code %d)\r\n", data->rc);
      }
      break;

    case WALTER_MODEM_MQTT_EVENT_DISCONNECTED:
      Serial.printf("[MQTT] Disconnected (code %d)\r\n", data->rc);
      mqttConnected = false;
      break;

    case WALTER_MODEM_MQTT_EVENT_SUBSCRIBED:
      Serial.printf("[MQTT] Subscribed to %s\r\n", data->topic);
      break;

    case WALTER_MODEM_MQTT_EVENT_PUBLISHED:
      break;

    case WALTER_MODEM_MQTT_EVENT_MESSAGE: {
      Serial.printf("[MQTT] Command received on '%s' (%ld bytes)\r\n", 
                    data->topic, data->msg_length);
      
      // Read the command
      static uint8_t cmdBuf[2048];
      memset(cmdBuf, 0, sizeof(cmdBuf));
      
      if (modem.mqttReceive(data->topic, data->mid, cmdBuf, data->msg_length)) {
        String cmd = String((char*)cmdBuf);
        cmd.trim();
        Serial.printf("[C2] Command: %s\r\n", cmd.c_str());
        handleCommand(cmd, true);
      }
      break;
    }

    default:
      break;
  }
}

static void networkEventHandler(WMNetworkEventType event, const WMNetworkEventData* data, void* args) {
  if (event == WALTER_MODEM_NETWORK_EVENT_REG_STATE_CHANGE) {
    switch (data->cereg.state) {
      case WALTER_MODEM_NETWORK_REG_REGISTERED_HOME:
        Serial.println("[NET] Registered (home)");
        break;
      case WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING:
        Serial.println("[NET] Registered (roaming)");
        break;
      case WALTER_MODEM_NETWORK_REG_DENIED:
        Serial.println("[NET] Registration DENIED");
        break;
      case WALTER_MODEM_NETWORK_REG_SEARCHING:
        Serial.println("[NET] Searching...");
        break;
      case WALTER_MODEM_NETWORK_REG_NOT_SEARCHING:
        Serial.println("[NET] Not searching");
        break;
      default:
        break;
    }
  }
}

void mqttPublish(const char* topic, const String& message) {
  if (!mqttConnected) return;
  modem.mqttPublish(topic, (uint8_t*)message.c_str(), message.length());
}

void publishTelemetry() {
  JsonDocument doc;
  doc["type"] = "telemetry";
  doc["device"] = deviceId;
  doc["uptime"] = millis() / 1000;
  
  // WiFi status
  if (wifiConnected) {
    doc["wifi_ssid"] = WiFi.SSID();
    doc["wifi_ip"] = WiFi.localIP().toString();
    doc["wifi_rssi"] = WiFi.RSSI();
  } else {
    doc["wifi_ssid"] = (const char*)nullptr;
  }
  
  // Cellular status
  if (lteReady) {
    doc["lte_connected"] = true;
  } else {
    doc["lte_connected"] = false;
  }
  
  // GNSS
  if (gnssFixValid) {
    doc["lat"] = lastFix.latitude;
    doc["lon"] = lastFix.longitude;
    doc["sats"] = lastFix.satCount;
    doc["confidence"] = lastFix.estimatedConfidence;
  }
  
  // Temperature
  doc["temp_c"] = temperatureRead();
  
  // Free heap
  doc["free_heap"] = ESP.getFreeHeap();
  
  String output;
  serializeJson(doc, output);
  
  Serial.printf("[TELEMETRY] %s\r\n", output.c_str());
  mqttPublish(topicTelemetry, output);
}

// ============================================================
// GNSS
// ============================================================

volatile bool gnssFixReceived = false;

static void gnssEventHandler(WMGNSSEventType type, const WMGNSSEventData* data, void* args) {
  switch (type) {
    case WALTER_MODEM_GNSS_EVENT_FIX:
      memcpy(&lastFix, &data->gnssfix, sizeof(WMGNSSFixEvent));
      Serial.printf("[GNSS] Fix: %.6f, %.6f (sats=%d, conf=%.1f)\r\n",
                    lastFix.latitude, lastFix.longitude,
                    lastFix.satCount, lastFix.estimatedConfidence);
      gnssFixValid = true;
      gnssFixReceived = true;
      break;

    case WALTER_MODEM_GNSS_EVENT_ASSISTANCE:
      if (data->assistance == WALTER_MODEM_GNSS_ASSISTANCE_TYPE_ALMANAC)
        Serial.println("[GNSS] Almanac updated");
      else if (data->assistance == WALTER_MODEM_GNSS_ASSISTANCE_TYPE_REALTIME_EPHEMERIS)
        Serial.println("[GNSS] Ephemeris updated");
      break;

    default:
      break;
  }
}

bool getGNSSFix() {
  Serial.println("[GNSS] Starting fix acquisition...");
  gnssFixReceived = false;

  // Disconnect LTE if connected (Sequans limitation)
  bool wasConnected = lteReady;
  if (wasConnected) {
    // Save MQTT state
    bool wasMqtt = mqttConnected;
    
    if (!lteDisconnect()) return false;
  }

  // Configure GNSS (only HOT_START available in WalterModem v1.5.0)
  modem.gnssConfig(WALTER_MODEM_GNSS_SENS_MODE_HIGH, WALTER_MODEM_GNSS_ACQ_MODE_HOT_START);

  // Request fix
  if (!modem.gnssPerformAction()) {
    Serial.println("[GNSS] ERROR: Could not request fix");
    return false;
  }

  // Wait for fix
  Serial.print("[GNSS] Acquiring");
  unsigned long start = millis();
  while (!gnssFixReceived) {
    Serial.print(".");
    delay(1000);
    if (millis() - start > GNSS_FIX_TIMEOUT_MS) {
      Serial.println("\r\n[GNSS] TIMEOUT: No fix");
      return false;
    }
  }
  Serial.println();

  // Reconnect LTE if we were connected
  if (wasConnected) {
    delay(1000);
    lteConnect();
    // MQTT will auto-reconnect in loop
  }

  return true;
}

// ============================================================
// COMMAND HANDLER
// ============================================================

void handleCommand(const String& cmd, bool fromMqtt) {
  String response = "";
  
  if (cmd == "status") {
    JsonDocument doc;
    doc["type"] = "status";
    doc["device"] = deviceId;
    doc["uptime_s"] = millis() / 1000;
    doc["wifi_connected"] = wifiConnected;
    doc["wifi_ssid"] = wifiConnected ? WiFi.SSID() : "";
    doc["wifi_ip"] = wifiConnected ? WiFi.localIP().toString() : "";
    doc["lte_connected"] = lteReady;
    doc["mqtt_connected"] = mqttConnected;
    doc["gnss_valid"] = gnssFixValid;
    doc["temp_c"] = temperatureRead();
    doc["free_heap"] = ESP.getFreeHeap();
    
    if (gnssFixValid) {
      doc["lat"] = lastFix.latitude;
      doc["lon"] = lastFix.longitude;
    }
    
    serializeJson(doc, response);
    
  } else if (cmd == "wifi scan") {
    response = wifiScanJson();
    
  } else if (cmd.startsWith("wifi connect ")) {
    // Format: wifi connect <ssid> <password>
    String rest = cmd.substring(13);
    int spaceIdx = rest.indexOf(' ');
    String ssid = (spaceIdx > 0) ? rest.substring(0, spaceIdx) : rest;
    String pass = (spaceIdx > 0) ? rest.substring(spaceIdx + 1) : "";
    
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), pass.c_str());
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT) {
      delay(500);
    }
    
    if (WiFi.status() == WL_CONNECTED) {
      wifiConnected = true;
      response = "{\"type\":\"wifi_connect\",\"status\":\"ok\",\"ip\":\"" + 
                 WiFi.localIP().toString() + "\"}";
    } else {
      wifiConnected = false;
      response = "{\"type\":\"wifi_connect\",\"status\":\"failed\"}";
    }
    
  } else if (cmd == "gnss fix") {
    bool ok = getGNSSFix();
    if (ok) {
      JsonDocument doc;
      doc["type"] = "gnss_fix";
      doc["lat"] = lastFix.latitude;
      doc["lon"] = lastFix.longitude;
      doc["sats"] = lastFix.satCount;
      doc["confidence"] = lastFix.estimatedConfidence;
      serializeJson(doc, response);
    } else {
      response = "{\"type\":\"gnss_fix\",\"status\":\"failed\"}";
    }
    
  } else if (cmd == "sim") {
    // Ensure NO_RF state for ICCID read
    modem.setOpState(WALTER_MODEM_OPSTATE_NO_RF);
    delay(500);
    response = simStatusJson();
    
  } else if (cmd == "lte status") {
    WalterModemNetworkRegState reg = modem.getNetworkRegState();
    const char* regStr = "unknown";
    switch (reg) {
      case WALTER_MODEM_NETWORK_REG_REGISTERED_HOME:    regStr = "registered_home"; break;
      case WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING: regStr = "registered_roaming"; break;
      case WALTER_MODEM_NETWORK_REG_SEARCHING:          regStr = "searching"; break;
      case WALTER_MODEM_NETWORK_REG_DENIED:             regStr = "denied"; break;
      case WALTER_MODEM_NETWORK_REG_NOT_SEARCHING:      regStr = "not_searching"; break;
      default: break;
    }
    response = "{\"type\":\"lte_status\",\"registration\":\"" + String(regStr) + "\"}";
    
  } else if (cmd == "lte connect") {
    bool ok = lteConnect();
    response = "{\"type\":\"lte_connect\",\"status\":\"" + String(ok ? "ok" : "failed") + "\"}";
    
  } else if (cmd == "lte disconnect") {
    bool ok = lteDisconnect();
    response = "{\"type\":\"lte_disconnect\",\"status\":\"" + String(ok ? "ok" : "failed") + "\"}";
    
  } else if (cmd == "reboot") {
    response = "{\"type\":\"reboot\",\"status\":\"rebooting\"}";
    
  } else if (cmd == "help") {
    response = "{\"type\":\"help\",\"commands\":[";
    response += "\"status\",\"wifi scan\",\"wifi connect <ssid> <pass>\",";
    response += "\"sim\",\"gnss fix\",\"lte status\",\"lte connect\",\"lte disconnect\",";
    response += "\"reboot\",\"help\"]}";
    
  } else {
    response = "{\"type\":\"error\",\"msg\":\"unknown command: " + cmd + "\"}";
  }
  
  Serial.printf("[C2] Response: %s\r\n", response.c_str());
  
  // Send response via MQTT or serial
  if (fromMqtt && mqttConnected) {
    mqttPublish(topicResponse, response);
  }
  Serial.println("RESP:" + response);
}

// ============================================================
// SERIAL COMMAND INTERFACE
// ============================================================

String serialBuffer = "";

void processSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        serialBuffer.trim();
        handleCommand(serialBuffer, false);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }
}

// ============================================================
// SETUP
// ============================================================

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);

  // Get MAC and build device ID
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  sprintf(macStr, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  sprintf(deviceId, "walter-%02X%02X%02X", mac[3], mac[4], mac[5]);
  
  buildTopics();
  printBanner();

  // --- Initialize modem ---
  Serial.println("[INIT] Starting modem...");
  if (!modem.begin(&Serial2)) {
    Serial.println("[INIT] FATAL: Could not initialize modem");
    delay(5000);
    ESP.restart();
  }
  Serial.println("[INIT] Modem initialized");

  // Set event handlers
  modem.setNetworkEventHandler(networkEventHandler, NULL);
  modem.setMQTTEventHandler(mqttEventHandler, NULL);
  modem.setGNSSEventHandler(gnssEventHandler, NULL);

  // --- Print modem identity ---
  if (modem.getIdentity(&rsp)) {
    Serial.printf("[INIT] IMEI: %s\r\n", rsp.data.identity.imei);
    Serial.printf("[INIT] IMEISV: %s\r\n", rsp.data.identity.imeisv);
  }

  // --- Set op state to NO_RF so SIM queries work ---
  // getSIMCardID() requires FULL or NO_RF state. begin() leaves modem in MINIMUM.
  if (!modem.setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
    Serial.println("[INIT] WARNING: Could not set NO_RF state for SIM detection");
  }

  // --- Print SIM info (with retry — modem may need time to detect SIM) ---
  bool simFound = false;
  for (int attempt = 1; attempt <= 3 && !simFound; attempt++) {
    delay(1000 * attempt);  // progressive delay: 1s, 2s, 3s
    simFound = querySIM(attempt);
  }
  if (!simFound) {
    Serial.println("[INIT] WARNING: SIM not detected after 3 retries");
    Serial.println("[INIT] Will attempt LTE anyway");
  }

  // --- Set radio technology ---
  WalterModemRsp ratRsp;
  if (modem.getRAT(&ratRsp)) {
    if (ratRsp.data.rat != RADIO_TECH) {
      modem.setRAT(RADIO_TECH);
      Serial.println("[INIT] Switched radio technology to LTE-M");
    }
  }

  // --- Configure GNSS ---
  modem.gnssConfig();

  // --- Try WiFi first ---
  Serial.println("\r\n[BOOT] Attempting WiFi connection...");
  tryWifiConnect();

  // --- Try LTE (attempt regardless of SIM detection) ---
  Serial.println("\r\n[BOOT] Attempting cellular connection...");
  if (lteConnect()) {
    // Configure MQTT
    if (modem.mqttConfig(deviceId)) {
      Serial.println("[MQTT] Client configured, connecting...");
      modem.mqttConnect(MQTT_HOST, MQTT_PORT);
    }
  } else {
    Serial.println("[BOOT] Cellular failed — running in WiFi/serial mode");
    Serial.println("[BOOT] Use 'sim' command to check SIM status");
  }

  Serial.println("\r\n[BOOT] Initialization complete");
  Serial.println("\r\n[BOOT] Commands: status, sim, wifi scan, gnss fix, lte status, help");
  Serial.println();
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
  // Always process serial commands
  processSerial();

  // Check WiFi connectivity
  if (wifiConnected && WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Disconnected, will retry...");
    wifiConnected = false;
  }
  if (!wifiConnected) {
    // Periodic WiFi retry every 60s
    static unsigned long lastWifiRetry = 0;
    if (millis() - lastWifiRetry > 60000) {
      lastWifiRetry = millis();
      tryWifiConnect();
    }
  }

  // LTE maintenance
  if (lteReady && !lteCheckConnected()) {
    Serial.println("[LTE] Lost registration");
    lteReady = false;
    mqttConnected = false;
  }

  // MQTT maintenance
  if (lteReady && !mqttConnected) {
    static unsigned long lastMqttRetry = 0;
    if (millis() - lastMqttRetry > 10000) {
      lastMqttRetry = millis();
      Serial.println("[MQTT] Attempting reconnect...");
      if (modem.mqttConfig(deviceId)) {
        modem.mqttConnect(MQTT_HOST, MQTT_PORT);
      }
    }
  }

  // Periodic telemetry
  if (mqttConnected && (millis() - lastTelemetry > TELEMETRY_INTERVAL_MS || lastTelemetry == 0)) {
    lastTelemetry = millis();
    publishTelemetry();
  }

  // Small yield to keep things responsive
  delay(10);
}
