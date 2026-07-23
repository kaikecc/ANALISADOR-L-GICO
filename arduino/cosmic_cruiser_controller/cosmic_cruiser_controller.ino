/*
 * Cosmic Cruiser GoTo — controlador experimental de azimute
 *
 * Arduino Mega 2560:
 *   Serial  (USB)   = console no computador, 115200 baud
 *   Serial1 TX1 D18 = entrada RX da montagem (linha anteriormente chamada B)
 *   Serial1 RX1 D19 = saída TX da montagem  (linha anteriormente chamada A)
 *
 * ATENÇÃO:
 * - Desconecte o controlador original antes de habilitar TX1. Dois transmissores
 *   ligados à mesma linha podem causar dano elétrico.
 * - A linha da montagem parece trabalhar perto de 3 V. Use level shifter/buffer
 *   apropriado; não aplique diretamente os 5 V do TX do Mega.
 * - O firmware nunca movimenta o motor ao ligar. Todo movimento exige comando.
 * - Mantenha um meio físico de cortar a alimentação durante os primeiros testes.
 */

#include <Arduino.h>
#include <math.h>

constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t MOUNT_BAUD = 57600;
constexpr uint8_t FRAME_SIZE = 14;
constexpr uint32_t COUNTS_PER_TURN = 1673280UL;
constexpr float COUNTS_PER_DEGREE = 4648.0f;
constexpr uint8_t SPEED_MAXIMUM = 0x09;
constexpr uint32_t QUERY_INTERVAL_MS = 250;
constexpr uint32_t RESPONSE_TIMEOUT_MS = 80;
constexpr uint32_t MOTION_TIMEOUT_MS = 240000UL;  // parada após 4 minutos
constexpr float DEFAULT_TOLERANCE_DEG = 0.75f;

enum Direction : uint8_t {
  POSITIVE = 0x00,
  NEGATIVE = 0x01,
};

enum MotionMode : uint8_t {
  IDLE,
  MANUAL,
  GOTO_POSITION,
};

MotionMode motionMode = IDLE;
Direction activeDirection = POSITIVE;
uint32_t targetCounts = 0;
uint32_t lastPosition = 0;
bool positionValid = false;
uint32_t motionStartedAt = 0;
uint32_t lastQueryAt = 0;
char consoleLine[64];
uint8_t consoleLength = 0;

uint16_t crc16Kermit(const uint8_t *data, size_t length) {
  uint16_t crc = 0x0000;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 1) ? (crc >> 1) ^ 0x8408 : crc >> 1;
    }
  }
  return crc;
}

void finalizeFrame(uint8_t frame[FRAME_SIZE]) {
  const uint16_t crc = crc16Kermit(frame, FRAME_SIZE - 2);
  frame[12] = crc & 0xFF;
  frame[13] = crc >> 8;
}

void sendFrame(uint8_t frame[FRAME_SIZE]) {
  finalizeFrame(frame);
  Serial1.write(frame, FRAME_SIZE);
  Serial1.flush();
}

void sendConfiguration() {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0xC0,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00
  };
  sendFrame(frame);
}

void sendPositionQuery() {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0x09,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00
  };
  sendFrame(frame);
  lastQueryAt = millis();
}

void sendMotion(Direction direction, uint8_t speed = SPEED_MAXIMUM) {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0x80,
    0x01, static_cast<uint8_t>(direction), speed, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00
  };
  sendFrame(frame);
}

void stopMotor(const __FlashStringHelper *reason) {
  uint8_t frame[FRAME_SIZE] = {
    0x01, 0x01, 0x00, 0x80,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00
  };
  // Envie duas vezes para aumentar a segurança diante de uma perda serial.
  sendFrame(frame);
  delay(8);
  sendFrame(frame);
  motionMode = IDLE;
  Serial.print(F("PARADO: "));
  Serial.println(reason);
}

int32_t shortestPositionError(uint32_t target, uint32_t current) {
  int32_t error = static_cast<int32_t>(target) -
                  static_cast<int32_t>(current);
  const int32_t halfTurn = COUNTS_PER_TURN / 2;
  if (error > halfTurn) error -= COUNTS_PER_TURN;
  if (error < -halfTurn) error += COUNTS_PER_TURN;
  return error;
}

float countsToDegrees(uint32_t counts) {
  return static_cast<float>(counts % COUNTS_PER_TURN) / COUNTS_PER_DEGREE;
}

uint32_t degreesToCounts(float degrees) {
  while (degrees < 0.0f) degrees += 360.0f;
  while (degrees >= 360.0f) degrees -= 360.0f;
  return static_cast<uint32_t>(lroundf(degrees * COUNTS_PER_DEGREE));
}

bool readMountFrame(uint8_t frame[FRAME_SIZE], uint32_t timeoutMs) {
  uint8_t index = 0;
  const uint32_t started = millis();
  while (millis() - started < timeoutMs) {
    while (Serial1.available()) {
      const uint8_t value = Serial1.read();
      // Sincroniza pelo cabeçalho 01 01 00.
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

void printPosition() {
  Serial.print(F("POSICAO: "));
  Serial.print(lastPosition);
  Serial.print(F(" contagens | "));
  Serial.print(countsToDegrees(lastPosition), 4);
  Serial.println(F(" graus"));
}

bool updatePosition() {
  // Remova qualquer resposta antiga para que a leitura corresponda à consulta
  // enviada logo abaixo.
  while (Serial1.available()) Serial1.read();
  sendPositionQuery();
  uint8_t frame[FRAME_SIZE];
  if (!readMountFrame(frame, RESPONSE_TIMEOUT_MS)) {
    Serial.println(F("AVISO: resposta ausente ou CRC invalido"));
    return false;
  }
  if (frame[3] != 0x09) {
    Serial.print(F("AVISO: resposta inesperada 0x"));
    Serial.println(frame[3], HEX);
    return false;
  }
  lastPosition = static_cast<uint32_t>(frame[4]) |
                 (static_cast<uint32_t>(frame[5]) << 8) |
                 (static_cast<uint32_t>(frame[6]) << 16);
  lastPosition %= COUNTS_PER_TURN;
  positionValid = true;
  printPosition();
  return true;
}

void beginManualMotion(Direction direction) {
  sendConfiguration();
  delay(8);
  sendMotion(direction);
  activeDirection = direction;
  motionMode = MANUAL;
  motionStartedAt = millis();
  Serial.println(direction == POSITIVE
                   ? F("MOVIMENTO MANUAL: sentido positivo")
                   : F("MOVIMENTO MANUAL: sentido negativo"));
}

void beginGoto(float targetDegrees) {
  if (!updatePosition()) {
    Serial.println(F("GOTO cancelado: nao foi possivel ler a posicao"));
    return;
  }
  targetCounts = degreesToCounts(targetDegrees);
  const int32_t error = shortestPositionError(targetCounts, lastPosition);
  const int32_t tolerance =
    static_cast<int32_t>(DEFAULT_TOLERANCE_DEG * COUNTS_PER_DEGREE);
  if (abs(error) <= tolerance) {
    Serial.println(F("GOTO: posicao ja esta dentro da tolerancia"));
    return;
  }
  activeDirection = error > 0 ? POSITIVE : NEGATIVE;
  sendConfiguration();
  delay(8);
  sendMotion(activeDirection);
  motionMode = GOTO_POSITION;
  motionStartedAt = millis();
  Serial.print(F("GOTO iniciado: "));
  Serial.print(targetDegrees, 4);
  Serial.print(F(" graus | sentido "));
  Serial.println(activeDirection == POSITIVE ? F("positivo") : F("negativo"));
}

void serviceMotion() {
  if (motionMode == IDLE) return;
  if (millis() - motionStartedAt >= MOTION_TIMEOUT_MS) {
    stopMotor(F("timeout de seguranca"));
    return;
  }
  if (millis() - lastQueryAt < QUERY_INTERVAL_MS) return;
  if (!updatePosition() || motionMode != GOTO_POSITION) return;

  const int32_t error = shortestPositionError(targetCounts, lastPosition);
  const int32_t tolerance =
    static_cast<int32_t>(DEFAULT_TOLERANCE_DEG * COUNTS_PER_DEGREE);
  const Direction requiredDirection = error > 0 ? POSITIVE : NEGATIVE;

  if (abs(error) <= tolerance || requiredDirection != activeDirection) {
    stopMotor(F("alvo atingido"));
    Serial.print(F("ERRO FINAL: "));
    Serial.print(static_cast<float>(error) / COUNTS_PER_DEGREE, 4);
    Serial.println(F(" graus"));
  }
}

void printHelp() {
  Serial.println(F(""));
  Serial.println(F("Comandos:"));
  Serial.println(F("  QUERY        - consultar posicao atual"));
  Serial.println(F("  POS          - mover continuamente no sentido positivo"));
  Serial.println(F("  NEG          - mover continuamente no sentido negativo"));
  Serial.println(F("  GOTO <graus> - ir para azimute absoluto de 0 a 360 graus"));
  Serial.println(F("  STOP          - parada imediata (tambem: caractere !)"));
  Serial.println(F("  HELP          - mostrar esta ajuda"));
  Serial.println(F(""));
}

void executeConsoleCommand() {
  consoleLine[consoleLength] = '\0';
  char *argument = strchr(consoleLine, ' ');
  if (argument) {
    *argument = '\0';
    ++argument;
  }

  if (strcmp(consoleLine, "STOP") == 0) {
    stopMotor(F("comando do usuario"));
  } else if (strcmp(consoleLine, "QUERY") == 0) {
    updatePosition();
  } else if (strcmp(consoleLine, "POS") == 0) {
    beginManualMotion(POSITIVE);
  } else if (strcmp(consoleLine, "NEG") == 0) {
    beginManualMotion(NEGATIVE);
  } else if (strcmp(consoleLine, "GOTO") == 0 && argument) {
    const float degrees = atof(argument);
    if (degrees < 0.0f || degrees >= 360.0f) {
      Serial.println(F("ERRO: informe GOTO entre 0 e 359.9999 graus"));
    } else {
      beginGoto(degrees);
    }
  } else if (strcmp(consoleLine, "HELP") == 0) {
    printHelp();
  } else if (consoleLength) {
    Serial.println(F("Comando desconhecido. Envie HELP."));
  }
  consoleLength = 0;
}

void pollConsole() {
  while (Serial.available()) {
    const char value = static_cast<char>(Serial.read());
    // Emergência de um único caractere, sem aguardar newline.
    if (value == '!') {
      consoleLength = 0;
      stopMotor(F("parada de emergencia"));
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
  Serial.println(F("Cosmic Cruiser Controller pronto; motor PARADO"));
  printHelp();
}

void loop() {
  pollConsole();
  serviceMotion();
}
