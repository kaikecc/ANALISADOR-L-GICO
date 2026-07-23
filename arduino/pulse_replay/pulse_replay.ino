/* LogicLab Pulse Replay
 *
 * Saídas: D8 = Fio A, D9 = Fio B. As saídas ficam em alta impedância até
 * receber "REPLAY" pela serial. Não conecte a um barramento que esteja sendo
 * dirigido por outro equipamento; use buffer/isolamento apropriado.
 */

#include "replay_capture.h"

constexpr uint8_t PIN_A = 8;
constexpr uint8_t PIN_B = 9;

void setLines(uint8_t bits) {
  // Mega 2560: D8=PH5 e D9=PH6. Acesso direto reduz a distorção dos
  // pulsos curtos causada pelo overhead de digitalWrite().
  const uint8_t levels = (bits & 0x01 ? _BV(PH5) : 0) |
                         (bits & 0x02 ? _BV(PH6) : 0);
  PORTH = (PORTH & ~(_BV(PH5) | _BV(PH6))) | levels;
}

void waitMicros(uint32_t value) {
  while (value > 16000UL) {
    delayMicroseconds(16000);
    value -= 16000UL;
  }
  if (value) delayMicroseconds((unsigned int)value);
}

void replayOnce() {
  ReplayStep step;
  memcpy_P(&step, &REPLAY_STEPS[0], sizeof(step));
  setLines(step.bits);
  pinMode(PIN_A, OUTPUT);
  pinMode(PIN_B, OUTPUT);
  noInterrupts();
  for (size_t index = 1; index < REPLAY_STEP_COUNT; ++index) {
    memcpy_P(&step, &REPLAY_STEPS[index], sizeof(step));
    waitMicros(step.delayUs);
    setLines(step.bits);
  }
  interrupts();
  pinMode(PIN_A, INPUT);
  pinMode(PIN_B, INPUT);
}

void setup() {
  pinMode(PIN_A, INPUT);
  pinMode(PIN_B, INPUT);
  Serial.begin(115200);
  Serial.println(F("Envie REPLAY para reproduzir uma vez"));
}

void loop() {
  if (Serial.available() && Serial.readStringUntil('\n') == "REPLAY") {
    Serial.println(F("START"));
    replayOnce();
    Serial.println(F("END"));
  }
}
