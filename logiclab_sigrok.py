"""Captura FX2 de oito canais via sigrok-cli, sem depender da porta serial."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from datetime import datetime

RATES = (20_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000,
         3_000_000, 4_000_000, 6_000_000, 8_000_000, 12_000_000,
         16_000_000, 24_000_000)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def find_sigrok():
    candidates = [os.environ.get('LOGICLAB_SIGROK', ''), shutil.which('sigrok-cli')]
    for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
        if root:
            candidates.extend(str(Path(root) / part / 'sigrok-cli.exe')
                              for part in ('sigrok/sigrok-cli', 'sigrok-cli'))
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(str(Path(os.environ['LOCALAPPDATA']) / 'LogicLab/sigrok-cli/sigrok-cli.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError('sigrok-cli não encontrado. Instale o sigrok-cli (o PulseView sozinho não basta) '
                       'ou defina LOGICLAB_SIGROK com o caminho de sigrok-cli.exe. '
                       'Download: https://sigrok.org/wiki/Downloads')


def scan_devices():
    result = subprocess.run([find_sigrok(), '--driver', 'fx2lafw', '--scan'],
                            capture_output=True, timeout=20, creationflags=NO_WINDOW)
    output = result.stdout.decode('utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-2000:] or 'Falha no scan USB')
    devices = []
    for line in output.splitlines():
        match = re.match(r'\s*(fx2lafw(?::\S+)?)\s+-\s+(.+)', line)
        if match and '8 channels' in match[2]:
            devices.append({'device': match[1], 'description': match[2]})
    return devices


def convert_binary(path, samplerate, channel_a, channel_b, model, cancelled=None, finished=None, progress=None):
    """Um byte/amostra, D0 no bit 0. Mantém frações de µs e o último estado."""
    table = bytes(((v >> channel_a) & 1) | (((v >> channel_b) & 1) << 1) for v in range(256))
    offset, previous, last_index = 0, None, -1
    with Path(path).open('rb') as fp:
        while True:
            if cancelled and cancelled.is_set():
                raise InterruptedError('Conversão cancelada; arquivo bruto preservado')
            chunk = fp.read(256 * 1024)
            if not chunk:
                if finished is not None and not finished.is_set():
                    finished.wait(0.05)
                    continue
                break
            if progress:
                progress(offset + len(chunk), chunk)
            mapped = chunk.translate(table)
            for run_number, run in enumerate(re.finditer(rb'([\x00-\x03])\1*', mapped)):
                if run_number % 1024 == 0 and cancelled and cancelled.is_set():
                    raise InterruptedError('Conversão interrompida; arquivo bruto preservado')
                state = run[1][0]
                if state != previous:
                    last_index = offset + run.start()
                    model.add(last_index * 1_000_000 / samplerate, state)
                    previous = state
            offset += len(chunk)
    if offset and last_index != offset - 1:
        model.add((offset - 1) * 1_000_000 / samplerate, previous)
    return offset


class SigrokWorker:
    def __init__(self, model, events):
        self.model, self.events = model, events
        self.port = None
        self.running = False
        self.samplerate, self.seconds = 1_000_000, 2.0
        self.channel_a, self.channel_b = 0, 1
        self.thread = None
        self.process = None
        self.received_samples = 0
        self.last_bits = None
        self.activity_mask = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.cancelled = threading.Event()

    def configure(self, samplerate, seconds, channel_a=0, channel_b=1):
        if self.running:
            raise RuntimeError('Aguarde a captura/conversão terminar')
        rate, duration = int(samplerate), float(seconds)
        a, b = int(channel_a), int(channel_b)
        if rate not in RATES or not math.isfinite(duration) or not 0.01 <= duration <= 300:
            raise ValueError('Selecione uma taxa válida e duração entre 0,01 e 300 segundos')
        if a not in range(8) or b not in range(8) or a == b:
            raise ValueError('Escolha dois canais diferentes entre D0 e D7')
        self.samplerate, self.seconds, self.channel_a, self.channel_b = rate, duration, a, b

    def connect(self, name, baud=None):
        devices = scan_devices()
        identifiers = {d['device'] for d in devices}
        if name not in identifiers and re.fullmatch(r'fx2lafw(?::conn=[\w.\-]+)?', str(name)) and len(devices) == 1:
            # O carregamento do firmware FX2 pode mudar o endereço USB entre
            # listar e conectar. Só resolva automaticamente se não há ambiguidade.
            name = devices[0]['device']
            self.events.put(('log', f'Endereço USB atualizado: {name}'))
        if name not in {d['device'] for d in devices}:
            raise RuntimeError('Analisador não encontrado. Atualize a lista, feche PulseView/Logic '
                               'e confira o driver WinUSB do dispositivo FX2. '
                               f'Seleção: {name}; detectados: {", ".join(sorted(identifiers)) or "nenhum"}')
        self.port = name
        self.events.put(('ready', 'Analisador USB FX2 selecionado; acesso será aberto ao capturar'))

    def send(self, command):
        if command == 'PING':
            return
        if command == 'STOP':
            self.send_immediate(command)
            return
        if command != 'START' or not self.port:
            raise RuntimeError('Selecione e conecte o analisador USB primeiro')
        if self.running:
            raise RuntimeError('Captura ou conversão ainda em andamento')
        self.running = True
        self.received_samples = 0
        self.last_bits = None
        self.activity_mask = 0
        self.stopped.clear()
        self.cancelled.clear()
        self.thread = threading.Thread(target=self._capture, daemon=True, name='logiclab-fx2')
        self.thread.start()

    def update_progress(self, count, chunk):
        reference = self.last_bits if self.last_bits is not None else chunk[0]
        for value in set(chunk):
            self.activity_mask |= reference ^ value
        self.last_bits = chunk[-1]
        self.received_samples = count

    def diagnostics(self):
        return {"received_samples": self.received_samples, "last_bits": self.last_bits,
                "activity_mask": self.activity_mask, "channel_a": self.channel_a,
                "channel_b": self.channel_b}

    def send_immediate(self, command):
        if command != 'STOP':
            raise ValueError('Comando USB inválido')
        self.stopped.set()
        self.cancelled.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def _capture(self):
        folder = Path(__file__).with_name('captures')
        stem = folder / f'fx2_{datetime.now():%Y%m%d_%H%M%S_%f}'
        raw, log, meta = stem.with_suffix('.bin'), stem.with_suffix('.log'), stem.with_suffix('.json')
        metadata = {'driver': self.port, 'samplerate_hz': self.samplerate,
                    'requested_seconds': self.seconds, 'numchannels': 8, 'unitsize': 1,
                    'channel_a': self.channel_a, 'channel_b': self.channel_b,
                    'raw': raw.name, 'csv': str(self.model.recording_path) if self.model.recording_path else None,
                    'status': 'capturing'}
        conversion = None
        acquisition_done = threading.Event()
        converted = []
        conversion_errors = []
        try:
            folder.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
            command = [find_sigrok(), '--driver', self.port, '--config', f'samplerate={self.samplerate}',
                       '--samples', str(round(self.samplerate * self.seconds)),
                       '--output-format', 'binary', '--output-file', str(raw)]
            # O sigrok grava os oito canais diretamente em disco: Python não
            # fica no caminho da aquisição a até 24 MB/s.
            with log.open('wb') as errors:
                with self.lock:
                    if self.stopped.is_set():
                        raise InterruptedError('Captura cancelada antes de iniciar')
                    self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                                    stderr=errors, creationflags=NO_WINDOW)
                def follow_capture():
                    try:
                        while not raw.exists():
                            if acquisition_done.wait(0.05) or self.cancelled.is_set():
                                return
                        converted.append(convert_binary(raw, self.samplerate, self.channel_a,
                                                        self.channel_b, self.model, self.cancelled,
                                                        acquisition_done, self.update_progress))
                    except Exception as exc:
                        conversion_errors.append(exc)
                conversion = threading.Thread(target=follow_capture, daemon=True, name='logiclab-fx2-convert')
                conversion.start()
                try:
                    code = self.process.wait(timeout=self.seconds + 30)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                    raise RuntimeError('Tempo esgotado na captura USB; verifique conexão e driver')
                finally:
                    acquisition_done.set()
            if self.cancelled.is_set():
                raise InterruptedError('Captura interrompida; arquivos já gravados preservados')
            if code and not self.stopped.is_set():
                raise RuntimeError(log.read_text(encoding='utf-8', errors='replace')[-2000:] or f'sigrok terminou com erro {code}')
            if not raw.exists() or not raw.stat().st_size:
                raise RuntimeError('Nenhuma amostra USB recebida. Confira driver WinUSB e conexão.')
            conversion.join()
            if conversion_errors:
                raise conversion_errors[0]
            count = converted[0] if converted else 0
            partial = self.stopped.is_set() or count < round(self.samplerate * self.seconds)
            metadata.update(status='partial' if partial else 'complete', samples=count,
                            actual_seconds=count / self.samplerate)
            self.events.put(('log', f'USB: {count:,} amostras; A=D{self.channel_a}, B=D{self.channel_b}; '
                             f'{"captura parcial; " if partial else ""}8 canais brutos em {raw}'))
        except InterruptedError as exc:
            metadata.update(status='partial', conversion_cancelled=True,
                            raw_samples=raw.stat().st_size if raw.exists() else 0)
            self.events.put(('log', str(exc)))
        except Exception as exc:
            metadata.update(status='error', error=str(exc))
            self.events.put(('error', str(exc)))
        finally:
            acquisition_done.set()
            if conversion and conversion.is_alive():
                self.cancelled.set()
                conversion.join()
            metadata['input_diagnostics'] = self.diagnostics()
            metadata['converted_states_in_memory'] = self.model.count()
            try:
                meta.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
            except OSError:
                pass
            self.model.stop_recording()
            with self.lock:
                self.process = None
            self.running = False
            self.events.put(('capture_end', metadata))

    def disconnect(self):
        self.cancelled.set()
        self.send_immediate('STOP')
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.thread and self.thread.is_alive():
            raise RuntimeError('Aguarde a finalização do analisador USB')
        self.port = None
