/* LogicLab — captura passiva contínua de duas linhas no Arduino Mega
 *
 * Entradas: D2/INT4 (Fio A) e D3/INT5 (Fio B).
 *
 * As transições não ficam acumuladas na SRAM. A ISR coloca cada evento em um
 * buffer circular e o loop transmite continuamente ao aplicativo Python. Isso
 * permite capturas longas; o limite prático passa a ser o disco/RAM do Mac.
 *
 * IMPORTANTE: o aplicativo e este firmware usam 1.000.000 baud.
 */

#include <Arduino.h>
#include <avr/interrupt.h>
#include <util/atomic.h>

constexpr uint32_t SERIAL_BAUD = 1000000UL;
constexpr uint16_t STREAM_CAPACITY = 1024;  // 5.120 bytes dos 8 KB do Mega
constexpr uint8_t INPUT_MASK = _BV(PE4) | _BV(PE5);

struct __attribute__((packed)) Sample {
  uint32_t ticks;
  uint8_t bits;
};

Sample streamBuffer[STREAM_CAPACITY];
volatile uint16_t streamHead = 0;
volatile uint16_t streamTail = 0;
volatile uint32_t timer1High = 0;
volatile uint32_t droppedSamples = 0;
volatile uint32_t edgeCountA = 0;
volatile uint32_t edgeCountB = 0;
volatile bool capturing = false;
bool finishPending = false;
uint32_t captureOrigin = 0;
uint8_t initialBits = 0;
uint8_t finalBits = 0;
char command[10];
uint8_t commandLength = 0;

ISR(TIMER1_OVF_vect) { timer1High += 0x10000UL; }

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

inline __attribute__((always_inline)) void enqueueISR(uint32_t ticks, uint8_t bits) {
  uint16_t next = streamHead + 1;
  if (next == STREAM_CAPACITY) next = 0;
  if (next == streamTail) {
    ++droppedSamples;
    return;
  }
  streamBuffer[streamHead].ticks = ticks;
  streamBuffer[streamHead].bits = bits;
  streamHead = next;
}

inline __attribute__((always_inline)) void recordTransition() {
  if (!capturing) return;
  const uint8_t state = readInputLines();
  const uint8_t changed = state ^ finalBits;
  if (!changed) return;
  if (changed & 0x01) ++edgeCountA;
  if (changed & 0x02) ++edgeCountB;
  finalBits = state;
  PORTB |= _BV(PB7);
  enqueueISR(timerTicksISR(), state);
}

ISR(INT4_vect) { recordTransition(); }
ISR(INT5_vect) { recordTransition(); }

bool dequeueSample(Sample &sample) {
  bool available = false;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (streamTail != streamHead) {
      sample = streamBuffer[streamTail];
      uint16_t next = streamTail + 1;
      if (next == STREAM_CAPACITY) next = 0;
      streamTail = next;
      available = true;
    }
  }
  return available;
}

bool streamEmpty() {
  bool empty;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { empty = streamTail == streamHead; }
  return empty;
}

void startCapture() {
  EIMSK &= ~(_BV(INT4) | _BV(INT5));
  streamHead = streamTail = 0;
  droppedSamples = 0;
  edgeCountA = edgeCountB = 0;
  finishPending = false;
  initialBits = finalBits = readInputLines();
  captureOrigin = timerTicks();
  PORTB &= ~_BV(PB7);

  Serial.println(F("#CAPTURE,STREAM"));
  // O primeiro estado estabelece o nível anterior às transições.
  streamBuffer[0].ticks = captureOrigin;
  streamBuffer[0].bits = initialBits;
  streamHead = 1;

  EIFR = _BV(INTF4) | _BV(INTF5);
  capturing = true;
  EIMSK |= _BV(INT4) | _BV(INT5);
}

void stopCapture() {
  EIMSK &= ~(_BV(INT4) | _BV(INT5));
  if (capturing) {
    capturing = false;
    finishPending = true;
  }
}

void finishCapture() {
  Serial.print(F("#DEBUG,edgesA="));
  Serial.print(edgeCountA);
  Serial.print(F(",edgesB="));
  Serial.print(edgeCountB);
  Serial.print(F(",dropped="));
  Serial.print(droppedSamples);
  Serial.print(F(",initial="));
  Serial.print(initialBits);
  Serial.print(F(",final="));
  Serial.println(finalBits);
  Serial.println(F("#END"));
  finishPending = false;
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

void transmitStream() {
  Sample sample;
  // Um pequeno lote mantém STOP responsivo e deixa o RX ser consultado sempre.
  for (uint8_t sent = 0; sent < 24 && dequeueSample(sample); ++sent) {
    Serial.print(F("D,"));
    Serial.print((sample.ticks - captureOrigin) >> 1);
    Serial.print(',');
    Serial.println(sample.bits);
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  DDRB |= _BV(DDB7);
  PORTB &= ~_BV(PB7);
  DDRE &= ~INPUT_MASK;
  PORTE &= ~INPUT_MASK;

  // Timer1: 16 MHz / 8 = 2 MHz, resolução de 0,5 µs.
  TCCR1A = 0;
  TCCR1B = _BV(CS11);
  TCNT1 = 0;
  TIMSK1 = _BV(TOIE1);
  EICRB = (EICRB & ~(_BV(ISC41) | _BV(ISC51))) |
          _BV(ISC40) | _BV(ISC50);
  EIMSK &= ~(_BV(INT4) | _BV(INT5));
  Serial.println(F("#LogicLab STREAM pronto; 1000000 baud; D2/INT4 D3/INT5"));
}

void loop() {
  pollCommand();
  transmitStream();
  if (!capturing && finishPending && streamEmpty()) finishCapture();
}
