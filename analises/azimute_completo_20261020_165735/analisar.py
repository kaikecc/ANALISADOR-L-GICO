"""Análise offline da captura original; não envia comandos nem altera firmware."""
from bisect import bisect_right
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'captures/azimute_completo_20261020_165735.csv'
OUT = Path(__file__).resolve().parent


def crc16(data):
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc


def decode(rows, channel, hold_bits):
    times = [r[0] for r in rows]
    bit = 1_000_000 / 57600
    busy = -1
    decoded = []
    errors = 0
    def level(t):
        return rows[bisect_right(times, t) - 1][channel + 1]
    for previous, current in zip(rows, rows[1:]):
        t = current[0]
        if t < busy or previous[channel + 1] != 1 or current[channel + 1] != 0:
            continue
        if level(t + .5 * bit) != 0 or level(t + 9.5 * bit) != 1:
            errors += 1
            continue
        value = sum(level(t + (1.5 + i) * bit) << i for i in range(8))
        decoded.append((t, value))
        # Stop validado em 9,5 bits: não ignorar a próxima borda depois disso.
        # Não inserir bytes nem alterar bits para forçar CRC válido.
        busy = t + hold_bits * bit
    groups = []
    for item in decoded:
        if not groups or item[0] - groups[-1][-1][0] > 25 * bit:
            groups.append([])
        groups[-1].append(item)
    frames = []
    for group in groups:
        values = [value for _, value in group]
        valid = (len(values) == 14 and values[:3] == [1, 1, 0]
                 and crc16(values[:12]) == int.from_bytes(bytes(values[12:]), 'little'))
        frames.append(dict(wire='AB'[channel], start_us=group[0][0],
                           end_us=group[-1][0] + 10 * bit, values=values,
                           hex=' '.join(f'{v:02X}' for v in values), valid=valid,
                           command=f'{values[3]:02X}' if len(values) > 3 else ''))
    return frames, dict(bytes=len(decoded), framing_errors=errors,
                       frames=len(frames), valid=sum(f['valid'] for f in frames))


def main():
    with SOURCE.open(encoding='utf-8-sig', newline='') as fp:
        rows = [tuple(map(int, row)) for row in list(csv.reader(fp))[1:]]
    assert all(a[0] <= b[0] for a, b in zip(rows, rows[1:]))
    assert all(t >= 0 and a in (0, 1) and b in (0, 1) for t, a, b in rows)
    events, quality = [], {}
    for channel in (0, 1):
        frames, stats = decode(rows, channel, 9.5)
        _, old = decode(rows, channel, 9.8)
        quality['AB'[channel]] = {'stop_9_5': stats, 'hold_9_8': old}
        events.extend(frames)
    events.sort(key=lambda f: f['start_us'])
    requests = [f for f in events if f['wire'] == 'B' and f['command'] == '09']
    responses = [f for f in events if f['wire'] == 'A']
    pairs = [(q, next((r for r in responses if 0 < r['start_us'] - q['start_us'] < 10_000), None)) for q in requests]
    latencies = [r['start_us'] - q['start_us'] for q, r in pairs if r]
    periods = [b['start_us'] - a['start_us'] for a, b in zip(requests, requests[1:]) if b['start_us'] - a['start_us'] < 1_000_000]
    summary = dict(source=str(SOURCE.relative_to(ROOT)), sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                   samples=len(rows), last_transition_s=rows[-1][0] / 1e6, quality=quality,
                   position_queries=len(requests), matched_responses=len(latencies),
                   response_start_latency_us=dict(min=min(latencies), median=statistics.median(latencies), max=max(latencies)),
                   query_period_median_us=statistics.median(periods),
                   valid_commands=dict(Counter(f['command'] for f in events if f['wire'] == 'B' and f['valid'])))
    (OUT / 'resumo.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    with (OUT / 'quadros.csv').open('w', encoding='utf-8', newline='') as fp:
        writer = csv.writer(fp)
        writer.writerow(['tempo_s', 'fio', 'comando', 'crc_valido', 'bytes', 'hex'])
        for f in events:
            writer.writerow([f['start_us'] / 1e6, f['wire'], f['command'], f['valid'], len(f['values']), f['hex']])
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
