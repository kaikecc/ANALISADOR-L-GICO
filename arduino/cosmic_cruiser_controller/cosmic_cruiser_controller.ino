/*
 * Cosmic Cruiser GoTo — controle experimental de azimute e elevação
 *
 * Mega 2560:
 *   Serial/USB 115200: console
 *   Serial1 TX1 D18: RX da montagem (antigo Fio B)
 *   Serial1 RX1 D19: TX da montagem (antigo Fio A)
 *
 * Use conversão de nível 5 V -> 3,3 V no TX1. Desconecte o transmissor do
 * controlador original. O firmware inicia com os motores parados e só envia
 * movimento após comando explícito.
 */

#include <Arduino.h>
#include <math.h>

constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t MOUNT_BAUD = 57600;
constexpr uint8_t FRAME_SIZE = 14;
constexpr uint32_t COUNTS_PER_TURN = 1673280UL;
constexpr float COUNTS_PER_DEGREE = 4648.0f;
constexpr uint8_t CONFIRMED_MAX_SPEED = 0x09;
constexpr uint32_t QUERY_INTERVAL_MS = 250;
constexpr uint32_t RESPONSE_TIMEOUT_MS = 80;
constexpr uint32_t MOTION_TIMEOUT_MS = 240000UL;
constexpr float DEFAULT_TOLERANCE_DEG = 0.75f;

enum Axis : uint8_t {
  AZIMUTH,
  ELEVATION,
};

enum Direction : uint8_t {
  POSITIVE = 0x00,
  NEGATIVE = 0x01,
};

enum MotionMode : uint8_t {
  IDLE,
  MANUAL,
  GOTO_POSITION,
};

struct AxisState {
  uint32_t position;
  bool valid;
};

AxisState azimuth = {0, false};
AxisState elevation = {0, false};
MotionMode motionMode = IDLE;
Axis activeAxis = AZIMUTH;
Direction activeDirection = POSITIVE;
uint8_t activeSpeed = CONFIRMED_MAX_SPEED;
uint32_t targetCounts = 0;
uint32_t motionStartedAt = 0;
uint32_t lastQueryAt = 0;
char consoleLine[80];
uint8_t consoleLength = 0;

const __FlashStringHelper *axisName(Axis axis) {
  return axis == AZIMUTH ? F("AZIMUTE") : F("ELEVACAO");
}

uint8_t axisCommand(Axis axis) {
  // Confirmado pelas capturas: 0x80 = azimute; 0xC0 = elevação.
  return axis == AZIMUTH ? 0x80 : 0xC0;
}

AxisState &axisState(Axis axis) {
  return axis == AZIMUTH ? azimuth : elevation;
}

uint16_t crc16Kermit(const uint8_t *data, size_t length) {
  uint16_t crc = 0;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 1) ? (crc >> 1) ^ 0x8408 : crc >> 1;
    }
  }
  return crc;
}

void sendFrame(uint8_t frame[FRAME_SIZE]) {
  const uint16_t crc = crc16Kermit(frame, FRAME_SIZE - 2);
  frame[12] = crc & 0xFF;
  frame[13] = crc >> 8;
  Serial1.write(frame, FRAME_SIZE);
  Serial1.flush();
}

void sendPositionQuery() {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0x09,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0
  };
  sendFrame(frame);
  lastQueryAt = millis();
}

void sendAxisMotion(Axis axis, Direction direction, uint8_t speed) {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, axisCommand(axis),
    0x01, static_cast<uint8_t>(direction), speed,
    0, 0, 0, 0, 0, 0, 0
  };
  sendFrame(frame);
}

void sendAxisStop(Axis axis) {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, axisCommand(axis),
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0
  };
  sendFrame(frame);
}

void stopAll(const __FlashStringHelper *reason) {
  // Pare os dois eixos duas vezes. Isso replica a estratégia observada no
  // controlador e aumenta a segurança diante de perda de um quadro.
  sendAxisStop(AZIMUTH);
  delay(6);
  sendAxisStop(ELEVATION);
  delay(8);
  sendAxisStop(AZIMUTH);
  delay(6);
  sendAxisStop(ELEVATION);
  motionMode = IDLE;
  Serial.print(F("MOTORES PARADOS: "));
  Serial.println(reason);
}

void stopOneAxis(Axis axis, const __FlashStringHelper *reason) {
  sendAxisStop(axis);
  delay(8);
  sendAxisStop(axis);
  if (motionMode != IDLE && activeAxis == axis) motionMode = IDLE;
  Serial.print(axisName(axis));
  Serial.print(F(" PARADO: "));
  Serial.println(reason);
}

float countsToDegrees(uint32_t counts) {
  return static_cast<float>(counts % COUNTS_PER_TURN) / COUNTS_PER_DEGREE;
}

uint32_t degreesToCounts(float degrees) {
  while (degrees < 0.0f) degrees += 360.0f;
  while (degrees >= 360.0f) degrees -= 360.0f;
  return static_cast<uint32_t>(lroundf(degrees * COUNTS_PER_DEGREE));
}

int32_t shortestPositionError(uint32_t target, uint32_t current) {
  int32_t error = static_cast<int32_t>(target) -
                  static_cast<int32_t>(current);
  const int32_t halfTurn = COUNTS_PER_TURN / 2;
  if (error > halfTurn) error -= COUNTS_PER_TURN;
  if (error < -halfTurn) error += COUNTS_PER_TURN;
  return error;
}

bool readMountFrame(uint8_t frame[FRAME_SIZE], uint32_t timeoutMs) {
  uint8_t index = 0;
  const uint32_t started = millis();
  while (millis() - started < timeoutMs) {
    while (Serial1.available()) {
      const uint8_t value = Serial1.read();
      if (index == 0 && value != 0x01) continue;
      if (index == 1 && value != 0x01) {
        index = value == 0x01 ? 1 : 0;
        continue;
      }
      if (index == 2 && value != 0x00) {
        index = 0;
        continue;
      }
      frame[index++] = value;
      if (index == FRAME_SIZE) {
        const uint16_t expected = crc16Kermit(frame, FRAME_SIZE - 2);
        const uint16_t received = frame[12] | (frame[13] << 8);
        return expected == received;
      }
    }
  }
  return false;
}

void printPositions() {
  Serial.print(F("AZ: "));
  Serial.print(azimuth.position);
  Serial.print(F(" cnt | "));
  Serial.print(countsToDegrees(azimuth.position), 4);
  Serial.print(F(" graus   ALT: "));
  Serial.print(elevation.position);
  Serial.print(F(" cnt | "));
  Serial.print(countsToDegrees(elevation.position), 4);
  Serial.println(F(" graus"));
}

bool updatePositions() {
  while (Serial1.available()) Serial1.read();
  sendPositionQuery();
  uint8_t frame[FRAME_SIZE];
  if (!readMountFrame(frame, RESPONSE_TIMEOUT_MS)) {
    Serial.println(F("AVISO: resposta ausente ou CRC invalido"));
    return false;
  }
  if (frame[3] != 0x09) {
    Serial.println(F("AVISO: resposta nao e de posicao"));
    return false;
  }

  // Resposta confirmada:
  // bytes 4–6 = azimute; bytes 8–10 = elevação.
  azimuth.position = (static_cast<uint32_t>(frame[4]) |
                     (static_cast<uint32_t>(frame[5]) << 8) |
                     (static_cast<uint32_t>(frame[6]) << 16))
                     % COUNTS_PER_TURN;
  elevation.position = (static_cast<uint32_t>(frame[8]) |
                       (static_cast<uint32_t>(frame[9]) << 8) |
                       (static_cast<uint32_t>(frame[10]) << 16))
                       % COUNTS_PER_TURN;
  azimuth.valid = elevation.valid = true;
  printPositions();
  return true;
}

bool validSpeed(int speed) {
  return speed >= 1 && speed <= 9;
}

void printSpeedWarning(uint8_t speed) {
  if (speed != CONFIRMED_MAX_SPEED) {
    Serial.print(F("ATENCAO: velocidade 0x"));
    Serial.print(speed, HEX);
    Serial.println(F(" e experimental; somente 0x09 foi confirmada."));
  }
}

void beginManual(Axis axis, Direction direction, uint8_t speed) {
  // A montagem observada para o outro eixo antes de iniciar o escolhido.
  sendAxisStop(axis == AZIMUTH ? ELEVATION : AZIMUTH);
  delay(8);
  sendAxisMotion(axis, direction, speed);
  activeAxis = axis;
  activeDirection = direction;
  activeSpeed = speed;
  motionMode = MANUAL;
  motionStartedAt = millis();
  printSpeedWarning(speed);
  Serial.print(F("MOVIMENTO: "));
  Serial.print(axisName(axis));
  Serial.print(direction == POSITIVE ? F(" POSITIVO") : F(" NEGATIVO"));
  Serial.print(F(" | velocidade 0x"));
  Serial.println(speed, HEX);
}

void beginGoto(Axis axis, float targetDegrees, uint8_t speed) {
  if (!updatePositions()) {
    Serial.println(F("GOTO cancelado: posicao indisponivel"));
    return;
  }
  AxisState &state = axisState(axis);
  targetCounts = degreesToCounts(targetDegrees);
  const int32_t error = shortestPositionError(targetCounts, state.position);
  const int32_t tolerance =
    static_cast<int32_t>(DEFAULT_TOLERANCE_DEG * COUNTS_PER_DEGREE);
  if (abs(error) <= tolerance) {
    Serial.println(F("GOTO: eixo ja esta dentro da tolerancia"));
    return;
  }

  activeAxis = axis;
  activeDirection = error > 0 ? POSITIVE : NEGATIVE;
  activeSpeed = speed;
  sendAxisStop(axis == AZIMUTH ? ELEVATION : AZIMUTH);
  delay(8);
  sendAxisMotion(axis, activeDirection, speed);
  motionMode = GOTO_POSITION;
  motionStartedAt = millis();
  printSpeedWarning(speed);
  Serial.print(F("GOTO "));
  Serial.print(axisName(axis));
  Serial.print(F(": alvo "));
  Serial.print(targetDegrees, 4);
  Serial.print(F(" graus | velocidade 0x"));
  Serial.println(speed, HEX);
}

void serviceMotion() {
  if (motionMode == IDLE) return;
  if (millis() - motionStartedAt >= MOTION_TIMEOUT_MS) {
    stopAll(F("timeout de seguranca"));
    return;
  }
  if (millis() - lastQueryAt < QUERY_INTERVAL_MS) return;
  if (!updatePositions() || motionMode != GOTO_POSITION) return;

  const AxisState &state = axisState(activeAxis);
  const int32_t error = shortestPositionError(targetCounts, state.position);
  const int32_t tolerance =
    static_cast<int32_t>(DEFAULT_TOLERANCE_DEG * COUNTS_PER_DEGREE);
  const Direction requiredDirection = error > 0 ? POSITIVE : NEGATIVE;
  if (abs(error) <= tolerance || requiredDirection != activeDirection) {
    stopOneAxis(activeAxis, F("alvo atingido"));
    Serial.print(F("ERRO FINAL: "));
    Serial.print(static_cast<float>(error) / COUNTS_PER_DEGREE, 4);
    Serial.println(F(" graus"));
  }
}

void printHelp() {
  Serial.println(F(""));
  Serial.println(F("Comandos:"));
  Serial.println(F("  QUERY"));
  Serial.println(F("  AZ POS [1-9]       | AZ NEG [1-9]"));
  Serial.println(F("  ALT POS [1-9]      | ALT NEG [1-9]"));
  Serial.println(F("  AZ GOTO <graus> [1-9]"));
  Serial.println(F("  ALT GOTO <graus> [1-9]"));
  Serial.println(F("  AZ STOP            | ALT STOP"));
  Serial.println(F("  STOP               | ! (emergencia)"));
  Serial.println(F("  HELP"));
  Serial.println(F(""));
  Serial.println(F("Velocidade 9 = maxima confirmada; 1-8 = experimentais."));
}

bool parseAxis(const char *token, Axis &axis) {
  if (token && strcmp(token, "AZ") == 0) {
    axis = AZIMUTH;
    return true;
  }
  if (token && (strcmp(token, "ALT") == 0 ||
                strcmp(token, "EL") == 0)) {
    axis = ELEVATION;
    return true;
  }
  return false;
}

void executeConsoleCommand() {
  consoleLine[consoleLength] = '\0';
  char *save = nullptr;
  char *first = strtok_r(consoleLine, " ", &save);
  if (!first) {
    consoleLength = 0;
    return;
  }

  if (strcmp(first, "STOP") == 0) {
    stopAll(F("comando do usuario"));
  } else if (strcmp(first, "QUERY") == 0) {
    updatePositions();
  } else if (strcmp(first, "HELP") == 0) {
    printHelp();
  } else {
    Axis axis;
    if (!parseAxis(first, axis)) {
      Serial.println(F("ERRO: use AZ, ALT, QUERY, STOP ou HELP"));
      consoleLength = 0;
      return;
    }

    char *action = strtok_r(nullptr, " ", &save);
    if (!action) {
      Serial.println(F("ERRO: informe POS, NEG, GOTO ou STOP"));
      consoleLength = 0;
      return;
    }

    if (strcmp(action, "STOP") == 0) {
      stopOneAxis(axis, F("comando do usuario"));
    } else if (strcmp(action, "POS") == 0 ||
               strcmp(action, "NEG") == 0) {
      char *speedText = strtok_r(nullptr, " ", &save);
      const int speed = speedText ? atoi(speedText) : CONFIRMED_MAX_SPEED;
      if (!validSpeed(speed)) {
        Serial.println(F("ERRO: velocidade deve estar entre 1 e 9"));
      } else {
        beginManual(axis, strcmp(action, "POS") == 0 ? POSITIVE : NEGATIVE,
                    static_cast<uint8_t>(speed));
      }
    } else if (strcmp(action, "GOTO") == 0) {
      char *degreesText = strtok_r(nullptr, " ", &save);
      char *speedText = strtok_r(nullptr, " ", &save);
      if (!degreesText) {
        Serial.println(F("ERRO: use EIXO GOTO <0-359.999> [1-9]"));
      } else {
        const float degrees = atof(degreesText);
        const int speed = speedText ? atoi(speedText) : CONFIRMED_MAX_SPEED;
        if (degrees < 0.0f || degrees >= 360.0f) {
          Serial.println(F("ERRO: angulo deve estar entre 0 e 359.999"));
        } else if (!validSpeed(speed)) {
          Serial.println(F("ERRO: velocidade deve estar entre 1 e 9"));
        } else {
          beginGoto(axis, degrees, static_cast<uint8_t>(speed));
        }
      }
    } else {
      Serial.println(F("ERRO: acao desconhecida"));
    }
  }
  consoleLength = 0;
}

void pollConsole() {
  while (Serial.available()) {
    const char value = static_cast<char>(Serial.read());
    if (value == '!') {
      consoleLength = 0;
      stopAll(F("parada de emergencia"));
    } else if (value == '\n' || value == '\r') {
      if (consoleLength) executeConsoleCommand();
    } else if (consoleLength < sizeof(consoleLine) - 1) {
      consoleLine[consoleLength++] = toupper(value);
    } else {
      consoleLength = 0;
      Serial.println(F("ERRO: comando muito longo"));
    }
  }
}

void setup() {
  Serial.begin(USB_BAUD);
  Serial1.begin(MOUNT_BAUD, SERIAL_8N1);
  motionMode = IDLE;
  delay(250);
  Serial.println(F("Cosmic Cruiser AZ/ALT Controller pronto; motores PARADOS"));
  printHelp();
}

void loop() {
  pollConsole();
  serviceMotion();
}
