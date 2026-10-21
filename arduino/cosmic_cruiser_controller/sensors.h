#pragma once
#include <SoftwareWire.h>
#include <Wire.h>

// RTC por software em D22/D23; BNO055 no Wire (SDA D20, SCL D21).
constexpr uint8_t SENSOR_SDA_PIN = 22;
constexpr uint8_t SENSOR_SCL_PIN = 23;
// Pull-ups EXTERNOS em ambos os barramentos.
SoftwareWire sensorWire(SENSOR_SDA_PIN, SENSOR_SCL_PIN, false, true);
uint8_t bnoAddress = 0x28;
bool bnoReady = false;

template <typename Bus>
bool busRead(Bus &bus, uint8_t address, uint8_t reg, uint8_t *data, uint8_t size) {
  bus.beginTransmission(address);
  bus.write(reg);
  if (bus.endTransmission(false) != 0) return false;
  if (bus.requestFrom(address, size) != size) return false;
  for (uint8_t i = 0; i < size; ++i) data[i] = bus.read();
  return true;
}

template <typename Bus>
bool busWrite(Bus &bus, uint8_t address, uint8_t reg, const uint8_t *data, uint8_t size) {
  bus.beginTransmission(address);
  bus.write(reg);
  bus.write(data, size);
  return bus.endTransmission() == 0;
}

bool sensorRead(uint8_t address, uint8_t reg, uint8_t *data, uint8_t size) {
  return address == 0x68 ? busRead(sensorWire, address, reg, data, size)
                         : busRead(Wire, address, reg, data, size);
}

bool sensorWrite(uint8_t address, uint8_t reg, const uint8_t *data, uint8_t size) {
  return address == 0x68 ? busWrite(sensorWire, address, reg, data, size)
                         : busWrite(Wire, address, reg, data, size);
}

bool bnoWrite(uint8_t reg, uint8_t value) {
  return sensorWrite(bnoAddress, reg, &value, 1);
}

void beginSensors() {
  Wire.begin();
  // O core AVR ativa pull-ups internos para 5 V em begin(); desabilite-os.
  digitalWrite(SDA, LOW);
  digitalWrite(SCL, LOW);
  Wire.setClock(50000);
  Wire.setWireTimeout(5000, true);  // microssegundos; recupera TWI no timeout

  sensorWire.begin();
  sensorWire.setClock(50000);
  sensorWire.setTimeout(5);  // ms; sensor ausente nao deve bloquear a UART.
  uint8_t id = 0;
  for (bnoAddress = 0x28; bnoAddress <= 0x29; ++bnoAddress) {
    if (sensorRead(bnoAddress, 0, &id, 1) && id == 0xA0) break;
  }
  if (bnoAddress > 0x29) return;
  if (!bnoWrite(0x3D, 0)) return;  // CONFIG
  delay(25);
  if (!bnoWrite(0x07, 0) || !bnoWrite(0x3E, 0) ||
      !bnoWrite(0x3B, 0) || !bnoWrite(0x3F, 0) ||
      !bnoWrite(0x3D, 0x0C)) return;  // graus, NDOF
  delay(20);
  bnoReady = true;
}

void printBno() {
  uint8_t data[6], cal;
  if (!bnoReady || !sensorRead(bnoAddress, 0x1A, data, 6) ||
      !sensorRead(bnoAddress, 0x35, &cal, 1)) {
    Serial.println(F("BNO: INDISPONIVEL"));
    return;
  }
  Serial.print(F("BNO: heading "));
  Serial.print((data[0] | (uint16_t(data[1]) << 8)) / 16.0f, 4);
  Serial.print(F(" roll "));
  Serial.print(int16_t(data[2] | (uint16_t(data[3]) << 8)) / 16.0f, 4);
  Serial.print(F(" pitch "));
  Serial.print(int16_t(data[4] | (uint16_t(data[5]) << 8)) / 16.0f, 4);
  Serial.print(F(" CAL sys ")); Serial.print((cal >> 6) & 3);
  Serial.print(F(" gyro ")); Serial.print((cal >> 4) & 3);
  Serial.print(F(" accel ")); Serial.print((cal >> 2) & 3);
  Serial.print(F(" mag ")); Serial.println(cal & 3);
}

uint8_t fromBcd(uint8_t value) { return (value >> 4) * 10 + (value & 15); }
uint8_t toBcd(uint8_t value) { return (value / 10) * 16 + value % 10; }
bool validDate(int year, int month, int day, int hour, int minute, int second) {
  const uint8_t days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  if (year < 2000 || year > 2099 || month < 1 || month > 12 ||
      hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 59)
    return false;
  return day >= 1 && day <= days[month-1] + (month == 2 && year % 4 == 0);
}

void printRtc() {
  uint8_t data[7], status;
  if (!sensorRead(0x68, 0, data, 7) || !sensorRead(0x68, 0x0F, &status, 1)) {
    Serial.println(F("RTC: INDISPONIVEL")); return;
  }
  int hour = fromBcd(data[2] & 0x3F);
  if (data[2] & 0x40) hour = fromBcd(data[2] & 0x1F) % 12 + ((data[2] & 0x20) ? 12 : 0);
  int year = 2000 + fromBcd(data[6]);
  int month = fromBcd(data[5] & 0x1F), day = fromBcd(data[4]);
  int minute = fromBcd(data[1]), second = fromBcd(data[0]);
  if ((data[5] & 0x80) || !validDate(year, month, day, hour, minute, second)) {
    Serial.println(F("RTC: INDISPONIVEL; data invalida, use RTC SET")); return;
  }
  char line[48];
  snprintf(line, sizeof(line), "RTC UTC: %04d-%02d-%02dT%02d:%02d:%02dZ OSF=%d",
           year, month, day, hour, minute, second, (status >> 7) & 1);
  Serial.println(line);
}

void rtcCommand(char *args) {
  if (!args || !*args) { printRtc(); return; }
  int y, m, d, h, n, s;
  char extra;
  if (sscanf(args, "SET %d %d %d %d %d %d %c", &y, &m, &d, &h, &n, &s, &extra) != 6 ||
      !validDate(y, m, d, h, n, s)) {
    Serial.println(F("ERRO: RTC SET ano mes dia hora minuto segundo (UTC, 2000-2099)")); return;
  }
  uint8_t status, control;
  if (!sensorRead(0x68, 0x0F, &status, 1) || !sensorRead(0x68, 0x0E, &control, 1)) {
    Serial.println(F("RTC: INDISPONIVEL")); return;
  }
  uint8_t data[] = {toBcd(s), toBcd(n), toBcd(h), 1, toBcd(d), toBcd(m), toBcd(y-2000)};
  control &= ~0x80;  // habilita oscilador em bateria
  status &= ~0x80;   // OSF so e limpo apos ajuste explicito
  if (!sensorWrite(0x68, 0, data, 7) || !sensorWrite(0x68, 0x0E, &control, 1) ||
      !sensorWrite(0x68, 0x0F, &status, 1)) {
    Serial.println(F("RTC: INDISPONIVEL; falha ao ajustar")); return;
  }
  printRtc();
}

void serviceSensors() {
  static uint32_t lastSample = 0;
  if (millis() - lastSample < 1000) return;
  lastSample = millis();
  printBno();
}
