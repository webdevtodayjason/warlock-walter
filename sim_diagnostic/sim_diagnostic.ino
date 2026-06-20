/**
 * AT Passthrough Diagnostic
 * Sends raw AT commands and dumps the full modem response.
 */

#include <WalterModem.h>

WalterModem modem;
WalterModemRsp rsp;

void setup() {
  Serial.begin(115200);
  delay(2000);
  
  Serial.println("\r\n=== AT PASSTHROUGH DIAG ===\r\n");
  
  // Hard reset Sequans
  Serial.println("[*] Hard reset Sequans...");
  pinMode(45, OUTPUT);
  digitalWrite(45, LOW);
  delay(500);
  digitalWrite(45, HIGH);
  delay(3000);
  
  // Init
  if (!modem.begin(&Serial2)) {
    Serial.println("MODEM INIT FAILED");
    return;
  }
  
  delay(2000);
  
  // Test 1: AT (basic connectivity)
  Serial.println("\r\n--- AT ---");
  bool ok1 = modem.getSIMState(&rsp);
  Serial.printf("AT+CPIN? -> %s, simState=%d, type=%d\r\n", ok1?"OK":"ERR", (int)rsp.data.simState, (int)rsp.type);
  
  // Test 2: Check the response data type
  Serial.printf("rsp.type after CPIN: %d\r\n", (int)rsp.type);
  
  // Test 3: Try ICCID
  Serial.println("\r\n--- AT+SQNCCID ---");
  bool ok2 = modem.getSIMCardID(&rsp);
  Serial.printf("getSIMCardID -> %s\r\n", ok2?"OK":"ERR");
  // Dump the raw ICCID bytes as hex
  Serial.printf("ICCID hex: ");
  for(int i = 0; i < 25; i++) {
    Serial.printf("%02X ", (uint8_t)rsp.data.simCardID.iccid[i]);
  }
  Serial.println();
  Serial.printf("ICCID str: '%s'\r\n", rsp.data.simCardID.iccid);
  Serial.printf("rsp.type: %d\r\n", (int)rsp.type);
  
  // Test 4: Try after setting minimum power state
  Serial.println("\r\n--- Set OPSTATE_MINIMUM then retry ---");
  modem.setOpState(WALTER_MODEM_OPSTATE_MINIMUM);
  delay(3000);
  
  bool ok3 = modem.getSIMState(&rsp);
  Serial.printf("AT+CPIN? (after min) -> %s, simState=%d\r\n", ok3?"OK":"ERR", (int)rsp.data.simState);
  
  bool ok4 = modem.getSIMCardID(&rsp);
  Serial.printf("AT+SQNCCID (after min) -> %s\r\n", ok4?"OK":"ERR");
  Serial.printf("ICCID: '%s'\r\n", rsp.data.simCardID.iccid);
  
  // Test 5: Check RAT (confirms modem comms working on non-SIM commands)
  Serial.println("\r\n--- RAT check (non-SIM) ---");
  WalterModemRsp ratRsp;
  bool ok5 = modem.getRAT(&ratRsp);
  Serial.printf("getRAT -> %s, rat=%d\r\n", ok5?"OK":"ERR", (int)ratRsp.data.rat);
  
  // Test 6: Get opstate
  Serial.println("\r\n--- OpState ---");
  WalterModemRsp opRsp;
  bool ok6 = modem.getOpState(&opRsp);
  Serial.printf("getOpState -> %s, opState=%d\r\n", ok6?"OK":"ERR", (int)opRsp.data.opState);
  
  // Test 7: Check signal quality
  Serial.println("\r\n--- Signal ---");
  WalterModemRsp sigRsp;
  bool ok7 = modem.getRSSI(&sigRsp);
  Serial.printf("getRSSI -> %s\r\n", ok7?"OK":"ERR");
  
  Serial.println("\r\n=== DIAG COMPLETE ===");
  Serial.println("CME ERROR 10 = SIM not inserted");
  Serial.println("CME ERROR 13 = SIM failure");
  Serial.println("If CPIN? fails but RAT works -> SIM interface issue");
}

void loop() {
  delay(30000);
}
