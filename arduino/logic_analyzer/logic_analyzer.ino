/* LogicLab — leitor passivo de protocolo proprietário de dois fios
 *
 * CAPTURA
 *   Fio A: D2 / PE4 / INT4
 *   Fio B: D3 / PE5 / INT5
 *
 * INT4 e INT5 disparam em qualquer mudança. Cada evento guarda o estado das
 * duas linhas e um timestamp de 0,5 us. Não há Serial.print() durante captura
 * e nunca dirige o barramento observado.
 */

#include <Arduino.h>
#include <avr/interrupt.h>
#include <util/atomic.h>

// 1.100 x 5 bytes = 5.500 bytes. Mantém margem da SRAM do Mega para
// pilha, buffers seriais e variáveis globais.
constexpr uint16_t CAPTURE_CAPACITY = 1100;
constexpr uint8_t INPUT_MASK = _BV(PE4) | _BV(PE5);

struct __attribute__((packed)) Sample {
  uint32_t ticks;
  uint8_t bits;
};

Sample samples[CAPTURE_CAPACITY];
volatile uint32_t timer1High = 0;
volatile uint16_t sampleCount = 0;
volatile bool capturing = false;
volatile bool bufferFull = false;
volatile uint16_t edgeCountA = 0;
volatile uint16_t edgeCountB = 0;
bool dumpPending = false;
char command[10];
uint8_t commandLength = 0;

ISR(TIMER1_OVF_vect) {
  timer1High += 0x10000UL;
}

inline __attribute__((always_inline)) uint32_t timerTicksISR() {
  uint32_t high = timer1High;
  const uint16_t low = TCNT1;
  if ((TIFR1 & _BV(TOV1)) && low < 0x8000) high += 0x10000UL;
  return high | low;
}

uint32_t timerTicks() {
  uint32_t value;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { value = timerTicksISR(); }
  return value;
}

inline __attribute__((always_inline)) uint8_t readInputLines() {
  return (PINE >> PE4) & 0x03;
}

inline __attribute__((always_inline)) void recordTransition() {
  if (!capturing) return;

  const uint8_t state = readInputLines();
  const uint16_t index = sampleCount;
  // Defesa contra qualquer inconsistência de estado: evita acesso fora do
  // buffer antes de consultar samples[index - 1].
  if (index == 0 || index >= CAPTURE_CAPACITY) {
    capturing = false;
    bufferFull = true;
    EIMSK &= ~(_BV(INT4) | _BV(INT5));
    return;
  }
  const uint8_t changed = state ^ samples[index - 1].bits;
  if (!changed) return;  // mesma mudança vista pelas 2 ISRs
  if (changed & 0x01) ++edgeCountA;
  if (changed & 0x02) ++edgeCountB;
  PORTB |= _BV(PB7);  // LED 13: pelo menos uma transição foi detectada

  samples[index].ticks = timerTicksISR();
  samples[index].bits = state;
  sampleCount = index + 1;

  if (sampleCount >= CAPTURE_CAPACITY) {
    capturing = false;
    bufferFull = true;
    EIMSK &= ~(_BV(INT4) | _BV(INT5));
  }
}

ISR(INT4_vect) { recordTransition(); }
ISR(INT5_vect) { recordTransition(); }

void startCapture() {
  EIMSK &= ~(_BV(INT4) | _BV(INT5));
  sampleCount = 1;
  edgeCountA = 0;
  edgeCountB = 0;
  bufferFull = false;
  dumpPending = false;
  samples[0].ticks = timerTicks();
  samples[0].bits = readInputLines();
  PORTB &= ~_BV(PB7);
  EIFR = _BV(INTF4) | _BV(INTF5);  // limpa flags antigas
  capturing = true;
  EIMSK |= _BV(INT4) | _BV(INT5);
}

void stopCapture() {
  EIMSK &= ~(_BV(INT4) | _BV(INT5));
  if (capturing || bufferFull) {
    capturing = false;
    dumpPending = true;
  }
}

void dumpCapture() {
  const uint16_t count = sampleCount;
  Serial.print(F("#CAPTURE,"));
  Serial.print(count);
  if (bufferFull) Serial.print(F(",FULL"));
  Serial.println();

  Serial.print(F("#DEBUG,edgesA="));
  Serial.print(edgeCountA);
  Serial.print(F(",edgesB="));
  Serial.print(edgeCountB);
  Serial.print(F(",initial="));
  Serial.print(samples[0].bits);
  Serial.print(F(",final="));
  Serial.print(samples[count - 1].bits);
  Serial.print(F(",duration_us="));
  Serial.println((samples[count - 1].ticks - samples[0].ticks) >> 1);

  const uint32_t origin = samples[0].ticks;
  for (uint16_t i = 0; i < count; ++i) {
    Serial.print(F("D,"));
    Serial.print((samples[i].ticks - origin) >> 1);
    Serial.print(',');
    Serial.println(samples[i].bits);
  }
  Serial.println(F("#END"));
  dumpPending = false;
}

void executeCommand() {
  command[commandLength] = '\0';
  if (strcmp(command, "START") == 0) startCapture();
  else if (strcmp(command, "STOP") == 0) stopCapture();
  else if (strcmp(command, "PING") == 0 && !capturing) Serial.println(F("#PONG"));
  commandLength = 0;
}

void pollCommand() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (commandLength) executeCommand();
    } else if (commandLength < sizeof(command) - 1) {
      command[commandLength++] = c;
    } else {
      commandLength = 0;
    }
  }
}

void setup() {
  Serial.begin(115200);
  DDRB |= _BV(DDB7);   // LED integrado do Mega (D13)
  PORTB &= ~_BV(PB7);

  // D2/D3 como entradas. Pull-ups desligados para observar o barramento sem
  // alterar seus níveis; use resistores externos quando necessários.
  DDRE &= ~INPUT_MASK;
  PORTE &= ~INPUT_MASK;

  // Timer1: modo normal, 16 MHz / 8 = 2 MHz (0,5 us por tick).
  TCCR1A = 0;
  TCCR1B = _BV(CS11);
  TCNT1 = 0;
  TIMSK1 = _BV(TOIE1);

  // ISC40/ISC50 = 1 e ISC41/ISC51 = 0: interrupção em qualquer mudança.
  EICRB = (EICRB & ~(_BV(ISC41) | _BV(ISC51))) |
          _BV(ISC40) | _BV(ISC50);
  EIMSK &= ~(_BV(INT4) | _BV(INT5));

  Serial.println(F("#LogicLab 2-Wire pronto; entradas D2/INT4 e D3/INT5"));
}

void loop() {
  // A captura acontece inteiramente nas interrupções INT4/INT5.
  pollCommand();
  if (!capturing && bufferFull) dumpPending = true;
  if (!capturing && dumpPending) dumpCapture();
}
