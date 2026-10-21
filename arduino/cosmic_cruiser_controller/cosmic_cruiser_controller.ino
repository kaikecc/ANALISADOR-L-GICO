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
#include "sensors.h"

constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t MOUNT_BAUD = 57600;
constexpr uint8_t FRAME_SIZE = 14;
constexpr uint32_t COUNTS_PER_TURN = 1673280UL;
constexpr float COUNTS_PER_DEGREE = 4648.0f;
constexpr uint8_t CONFIRMED_MAX_SPEED = 0x09;
constexpr uint32_t QUERY_INTERVAL_MS = 285;
constexpr uint32_t RESPONSE_TIMEOUT_MS = 80;
constexpr uint8_t MOUNT_TX_PIN = 18;
// Intervalos entre inicios de quadros nas tres capturas Inicializacao-*.sr (1 MHz).
constexpr uint16_t STARTUP_TX_LOW_MS = 288;
constexpr uint16_t STARTUP_IDLE_TO_0D_MS = 1799;
constexpr uint16_t STARTUP_0D_TO_0B_MS = 2478;
constexpr uint8_t STARTUP_SHORT_GAP_MS = 12;
constexpr uint16_t STARTUP_0A_TO_0E_MS = 126;
constexpr uint16_t STARTUP_0E_TO_STOP_MS = 789;
constexpr uint8_t STARTUP_DOUBLE_STOP_MS = 6;
constexpr uint8_t STARTUP_STOP_TO_C0_MS = 18;
constexpr uint8_t STARTUP_AXIS_CONFIG_MS = 6;
constexpr uint8_t STARTUP_RESPONSE_TIMEOUT_MS = 100;
constexpr uint16_t STARTUP_KEEPALIVE_MS = 5085;
constexpr uint32_t MOTION_TIMEOUT_MS = 240000UL;
constexpr float DEFAULT_TOLERANCE_DEG = 0.75f;
constexpr float TRACK_TOLERANCE_DEG = 1.0f / 60.0f;
float activeToleranceDeg = DEFAULT_TOLERANCE_DEG;
constexpr uint8_t MAX_POSITION_FAILURES = 3;
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
uint32_t lastStartupKeepaliveAt = 0;
uint16_t lastMountRxBytes = 0;
bool lastMountHadBadCrc = false;
bool startupSequenceSent = false;
bool mountReady = false;
bool hasTransmitted = false;
uint32_t lastFrameStartedUs = 0;
// Menor intervalo observado: 5,885 ms. Inclui os ~2,43 ms do quadro.
constexpr uint32_t MIN_FRAME_INTERVAL_US = 6000;

void beginMountSerial() {
  Serial1.begin(MOUNT_BAUD, SERIAL_8N1);
#if defined(__AVR_ATmega2560__) && F_CPU == 16000000UL
  // O core AVR aplica a excecao de bootloader de 57600 tambem a Serial1:
  // UBRR=16, modo normal -> 58824 baud (+2,12%). U2X/34 -> 57143 (-0,79%).
  UCSR1A = _BV(U2X1);
  UBRR1H = 0;
  UBRR1L = 34;
#endif
}
char consoleLine[80];
uint8_t consoleLength = 0;
uint8_t positionFailures = 0;

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
  if (hasTransmitted) {
    while (micros() - lastFrameStartedUs < MIN_FRAME_INTERVAL_US) delayMicroseconds(20);
  }
  lastFrameStartedUs = micros();
  hasTransmitted = true;
  Serial1.write(frame, FRAME_SIZE);
  Serial1.flush();
}

void sendPositionQuery() {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0x09,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0
  };
  lastQueryAt = millis();
  sendFrame(frame);
}

void sendStartupRequest(uint8_t command) {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, command,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0
  };
  sendFrame(frame);
}

void sendStartupElevationReference() {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0xC1,
    0xF0, 0x61, 0x06, 0x00,
    0, 0, 0, 0, 0, 0
  };
  sendFrame(frame);
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

bool readMountFrame(uint8_t frame[FRAME_SIZE], uint32_t timeoutMs,
                    uint8_t expectedCommand) {
  uint8_t index = 0;
  lastMountRxBytes = 0;
  lastMountHadBadCrc = false;
  const uint32_t started = millis();
  while (millis() - started < timeoutMs) {
    while (Serial1.available() && millis() - started < timeoutMs) {
      const uint8_t value = Serial1.read();
      ++lastMountRxBytes;
      frame[index++] = value;
      if (index == FRAME_SIZE) {
        if (frame[0] == 0x01 && frame[1] == 0x01 && frame[2] == 0x00) {
          const uint16_t expected = crc16Kermit(frame, FRAME_SIZE - 2);
          const uint16_t received = static_cast<uint16_t>(frame[12]) |
                                   (static_cast<uint16_t>(frame[13]) << 8);
          if (expected == received) {
            if (frame[3] == expectedCommand) return true;
            index = 0;
            continue;  // Aguarde a resposta solicitada dentro do mesmo prazo.
          }
          lastMountHadBadCrc = true;
        }
        // Janela deslizante: preserve um cabecalho que comece dentro do ruido.
        memmove(frame, frame + 1, FRAME_SIZE - 1);
        index = FRAME_SIZE - 1;
      }
    }
  }
  return false;
}

void waitFrom(uint32_t startedAt, uint32_t intervalMs) {
  while (millis() - startedAt < intervalMs) delay(1);
}

bool readStartupResponse(uint8_t expectedCommand, uint32_t timeoutMs) {
  uint8_t frame[FRAME_SIZE];
  return readMountFrame(frame, timeoutMs, expectedCommand);
}

void initializeMountProtocol(uint32_t txIdleStartedAt) {
  mountReady = false;
  startupSequenceSent = false;
  azimuth.valid = elevation.valid = false;
  positionFailures = 0;
  Serial.println(F("MONTAGEM: reproduzindo inicializacao do controle"));
  waitFrom(txIdleStartedAt, STARTUP_IDLE_TO_0D_MS);
  while (Serial1.available()) Serial1.read();

  const uint32_t command0DAt = millis();
  sendStartupRequest(0x0D);
  waitFrom(command0DAt, STARTUP_0D_TO_0B_MS);

  const uint32_t command0BAt = millis();
  sendStartupRequest(0x0B);
  waitFrom(command0BAt, STARTUP_SHORT_GAP_MS);

  const uint32_t first80At = millis();
  sendAxisStop(AZIMUTH);
  waitFrom(first80At, STARTUP_SHORT_GAP_MS);

  while (Serial1.available()) Serial1.read();
  const uint32_t command0AAt = millis();
  sendStartupRequest(0x0A);
  const bool response0A =
    readStartupResponse(0x0A, STARTUP_RESPONSE_TIMEOUT_MS);
  waitFrom(command0AAt, STARTUP_0A_TO_0E_MS);

  while (Serial1.available()) Serial1.read();
  const uint32_t command0EAt = millis();
  sendStartupRequest(0x0E);
  const bool response0E =
    readStartupResponse(0x0E, STARTUP_RESPONSE_TIMEOUT_MS);
  waitFrom(command0EAt, STARTUP_0E_TO_STOP_MS);

  const uint32_t firstStopAt = millis();
  sendAxisStop(AZIMUTH);
  waitFrom(firstStopAt, STARTUP_DOUBLE_STOP_MS);

  const uint32_t secondStopAt = millis();
  sendAxisStop(AZIMUTH);
  waitFrom(secondStopAt, STARTUP_STOP_TO_C0_MS);

  const uint32_t elevationStopAt = millis();
  sendAxisStop(ELEVATION);
  waitFrom(elevationStopAt, STARTUP_AXIS_CONFIG_MS);

  Serial.print(F("MONTAGEM: 0A "));
  Serial.print(response0A ? F("OK") : F("SEM RESPOSTA"));
  Serial.print(F(" | 0E "));
  Serial.println(response0E ? F("OK") : F("SEM RESPOSTA"));

  // Nao redefina referencias se a montagem nao confirmou o handshake.
  if (!response0A || !response0E) {
    Serial.println(F("MONTAGEM: inicializacao falhou; movimento bloqueado. Use INIT para tentar novamente."));
    return;
  }

  const uint32_t command81At = millis();
  sendStartupRequest(0x81);
  waitFrom(command81At, STARTUP_AXIS_CONFIG_MS);
  sendStartupElevationReference();

  startupSequenceSent = true;
  mountReady = true;
  lastStartupKeepaliveAt = millis();
  Serial.println(F("MONTAGEM: comunicacao confirmada; comandos liberados"));
}

void serviceStartupKeepalive() {
  if (!startupSequenceSent || motionMode != IDLE) return;
  if (millis() - lastStartupKeepaliveAt < STARTUP_KEEPALIVE_MS) return;
  while (Serial1.available()) Serial1.read();
  lastStartupKeepaliveAt = millis();
  sendStartupRequest(0x0A);
  readStartupResponse(0x0A, STARTUP_RESPONSE_TIMEOUT_MS);
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
  if (!readMountFrame(frame, RESPONSE_TIMEOUT_MS, 0x09)) {
    azimuth.valid = elevation.valid = false;
    if (positionFailures < MAX_POSITION_FAILURES) ++positionFailures;
    if (positionFailures >= MAX_POSITION_FAILURES) {
      if (motionMode != IDLE) stopAll(F("perda de comunicacao com a montagem"));
      mountReady = false;
      startupSequenceSent = false;
    }
    if (lastMountRxBytes == 0) {
      Serial.println(F("AVISO: montagem sem resposta; RX1 recebeu 0 bytes"));
    } else {
      Serial.print(F("AVISO: RX1 recebeu "));
      Serial.print(lastMountRxBytes);
      Serial.print(F(" bytes; quadro completo/CRC "));
      Serial.println(lastMountHadBadCrc ? F("invalido") : F("ausente"));
    }
    return false;
  }
  positionFailures = 0;

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
  if (speed != CONFIRMED_MAX_SPEED && speed != 4) {
    Serial.print(F("ATENCAO: velocidade 0x"));
    Serial.print(speed, HEX);
    Serial.println(F(" e experimental; 0x04 e 0x09 foram observadas."));
  }
}

bool requireMountReady() {
  if (mountReady) return true;
  Serial.println(F("ERRO: montagem indisponivel; use INIT e confira as respostas 0A/0E"));
  return false;
}

void beginManual(Axis axis, Direction direction, uint8_t speed) {
  if (!requireMountReady()) return;
  // A montagem observada para o outro eixo antes de iniciar o escolhido.
  sendAxisStop(axis == AZIMUTH ? ELEVATION : AZIMUTH);
  delay(8);
  sendAxisMotion(axis, direction, speed);
  activeAxis = axis;
  activeDirection = direction;
  activeSpeed = speed;
  motionMode = MANUAL;
  motionStartedAt = millis();
  lastQueryAt = motionStartedAt;
  positionFailures = 0;
  printSpeedWarning(speed);
  Serial.print(F("MOVIMENTO: "));
  Serial.print(axisName(axis));
  Serial.print(direction == POSITIVE ? F(" POSITIVO") : F(" NEGATIVO"));
  Serial.print(F(" | velocidade 0x"));
  Serial.println(speed, HEX);
}

void beginGoto(Axis axis, float targetDegrees, uint8_t speed, bool tracking = false) {
  if (!requireMountReady()) return;
  if (!updatePositions()) {
    Serial.println(F("GOTO cancelado: posicao indisponivel"));
    return;
  }
  AxisState &state = axisState(axis);
  targetCounts = degreesToCounts(targetDegrees);
  activeToleranceDeg = tracking ? TRACK_TOLERANCE_DEG : DEFAULT_TOLERANCE_DEG;
  const int32_t error = shortestPositionError(targetCounts, state.position);
  const int32_t tolerance =
    static_cast<int32_t>(activeToleranceDeg * COUNTS_PER_DEGREE);
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
  lastQueryAt = motionStartedAt;
  positionFailures = 0;
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
    static_cast<int32_t>(activeToleranceDeg * COUNTS_PER_DEGREE);
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
  Serial.println(F("TRACK_ARCMIN=1 | AZ/ALT TRACK <graus> [1-9] | tolerancia 1 arcmin"));
  Serial.println(F("  QUERY | INIT (reinicializa com motores parados)"));
  Serial.println(F("  AZ POS [1-9]       | AZ NEG [1-9]"));
  Serial.println(F("  ALT POS [1-9]      | ALT NEG [1-9]"));
  Serial.println(F("  AZ GOTO <graus> [1-9]"));
  Serial.println(F("  ALT GOTO <graus> [1-9]"));
  Serial.println(F("  AZ STOP            | ALT STOP"));
  Serial.println(F("  STOP               | ! (emergencia)"));
  Serial.println(F("  BNO | RTC | RTC SET ano mes dia hora minuto segundo (UTC)"));
  Serial.println(F("  HELP"));
  Serial.println(F(""));
  Serial.println(F("Velocidades 4 e 9 observadas; demais experimentais."));
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
  } else if (strcmp(first, "INIT") == 0) {
    stopAll(F("reinicializacao solicitada"));
    initializeMountProtocol(millis());
  } else if (strcmp(first, "QUERY") == 0) {
    updatePositions();
  } else if (strcmp(first, "BNO") == 0) {
    printBno();
  } else if (strcmp(first, "RTC") == 0) {
    rtcCommand(save);
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
    } else if (strcmp(action, "GOTO") == 0 || strcmp(action, "TRACK") == 0) {
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
          beginGoto(axis, degrees, static_cast<uint8_t>(speed), strcmp(action, "TRACK") == 0);
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

  // O controle original mantem o fio B baixo durante a entrada em operacao,
  // depois o leva ao nivel ocioso alto e inicia os quadros ~1,799 s mais tarde.
  pinMode(MOUNT_TX_PIN, OUTPUT);
  digitalWrite(MOUNT_TX_PIN, LOW);
  delay(STARTUP_TX_LOW_MS);
  digitalWrite(MOUNT_TX_PIN, HIGH);
  beginMountSerial();
  const uint32_t txIdleStartedAt = millis();

  motionMode = IDLE;
  initializeMountProtocol(txIdleStartedAt);
  beginSensors();
  printBno();
  printRtc();

  Serial.println(F("Cosmic Cruiser AZ/ALT: console pronto; verifique status da montagem acima"));
  printHelp();
}

void loop() {
  pollConsole();
  serviceMotion();
  serviceStartupKeepalive();
  serviceSensors();
}
