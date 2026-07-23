#!/usr/bin/env python3
"""LogicLab nativo: interface WebKit embutida em um processo Python."""

from __future__ import annotations

import queue
import glob
import csv
import statistics
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import webview

from logic_analyzer import (AnalyzerModel, Sample, SerialWorker,
                            analyze_repetition, decode_hex_candidates,
                            decode_uart_duplex, list_ports)


class LogicLabAPI:
    COSMIC_AZIMUTH_COUNTS = 1_673_280
    def __init__(self):
        self.model = AnalyzerModel()
        self.events: queue.Queue = queue.Queue()
        self.worker = SerialWorker(self.model, self.events)
        self.capturing = False
        self.finalizing = False
        self.last_saved_path = None
        self.messages: list[str] = []
        # Controles nunca esperam uma análise pesada terminar. STOP,
        # conectar/desconectar e portas possuem seu próprio pool.
        self.control_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="logiclab-control"
        )
        self.analysis_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="logiclab-analysis"
        )
        self.jobs = {}
        self.jobs_lock = threading.Lock()

    def submit(self, action, args=None):
        """Agenda operações potencialmente lentas fora da thread da UI."""
        allowed = {
            "ports": self.ports,
            "connect": self.connect,
            "disconnect": self.disconnect,
            "capture": self.capture,
            "stop_capture": self.stop_capture,
            "analyze_samples": self.analyze_samples,
            "decode_samples": self.decode_samples,
            "decode_uart_samples": self.decode_uart_samples,
            "export_replay": self.export_replay,
            "export_csv": self.export_csv,
        }
        if action not in allowed:
            raise ValueError(f"Ação não permitida: {action}")
        job_id = uuid.uuid4().hex
        analysis_actions = {"analyze_samples", "decode_samples",
                            "decode_uart_samples", "export_replay", "export_csv"}
        executor = (self.analysis_executor if action in analysis_actions
                    else self.control_executor)
        future = executor.submit(allowed[action], *(args or []))
        with self.jobs_lock:
            self.jobs[job_id] = future
        return job_id

    def job_status(self, job_id):
        with self.jobs_lock:
            future = self.jobs.get(job_id)
        if future is None:
            return {"done": True, "error": "Tarefa inexistente ou expirada"}
        if not future.done():
            return {"done": False}
        try:
            result = future.result()
            response = {"done": True, "result": result}
        except Exception as exc:
            response = {"done": True, "error": str(exc)}
        with self.jobs_lock:
            self.jobs.pop(job_id, None)
        return response

    def ports(self):
        detected = {}
        if list_ports is not None:
            for port in list_ports.comports():
                detected[port.device] = port.description if port.description not in (None, "n/a") else "Interface serial"
        for device in glob.glob("/dev/cu.*"):
            detected.setdefault(device, "Interface serial")
        return [{"device": device, "description": description}
                for device, description in sorted(
                    detected.items(),
                    key=lambda item: ("usbmodem" not in item[0] and "usbserial" not in item[0], item[0]),
                )]

    def connect(self, port, baud=1000000):
        try:
            if not port:
                return {"ok": False, "message": "Selecione uma porta serial antes de conectar"}
            if self.worker.port:
                self.worker.disconnect()
            self.worker.connect(str(port), int(baud))
            self.worker.send("PING")
            return {"ok": True, "message": f"Porta aberta: {port}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def disconnect(self):
        self.worker.disconnect()
        self.capturing = False
        self.finalizing = False
        path = self.model.stop_recording()
        if path:
            self.last_saved_path = str(path)
        return {"ok": True, "message": "Desconectado"}

    def capture(self, start):
        if not self.worker.port:
            return {"ok": False, "message": "Conecte uma porta serial primeiro"}
        if start:
            self.model.clear()
            target = (Path(__file__).with_name("captures") /
                      f"azimute_completo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            self.model.start_recording(target)
            self.capturing = True
            self.finalizing = False
            self.last_saved_path = None
            self.worker.send("START")
            return {"ok": True, "message": f"Captura contínua iniciada • salvando em {target.name}"}
        self.capturing = False
        self.finalizing = True
        self.worker.send_immediate("STOP")
        return {"ok": True, "message": "Finalizando captura • recebendo o buffer restante e salvando o CSV…"}

    def stop_capture(self):
        if not self.worker.port:
            self.capturing = False
            self.finalizing = False
            return {"ok": False, "message": "Porta serial fechada"}
        try:
            self.worker.send_immediate("STOP")
            self.capturing = False
            self.finalizing = True
            return {"ok": True, "message": "Finalizando captura • recebendo o buffer restante e salvando o CSV…"}
        except Exception as exc:
            self.capturing = False
            self.finalizing = False
            return {"ok": False, "message": f"Falha ao enviar STOP: {exc}"}

    def poll(self, known_version=-1):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "capture_end":
                self.capturing = False
                self.finalizing = False
                path = self.model.stop_recording()
                if path:
                    self.last_saved_path = str(path)
                    self.messages.append(f"CSV completo salvo: {path}")
            elif kind in ("log", "ready", "error"):
                self.messages.append(str(value))
                self.messages = self.messages[-20:]
            elif kind == "disconnected":
                self.capturing = False
                self.finalizing = False
                path = self.model.stop_recording()
                if path:
                    self.last_saved_path = str(path)
        version = self.model.current_version()
        result = {
            "version": version,
            "connected": bool(self.worker.port),
            "capturing": self.capturing,
            "finalizing": self.finalizing,
            "saved_path": self.last_saved_path,
            "stored_samples": self.model.count(),
            "malformed_samples": self.model.malformed_count(),
            "messages": self.messages[-5:],
        }
        if version != int(known_version):
            result["samples"] = [[s.us, s.bits & 1, (s.bits >> 1) & 1]
                                 for s in self.model.snapshot_tail(20_000)]
        return result

    def analyze_samples(self, rows):
        rows = rows[-20_000:]
        samples = [Sample(int(row[0]), int(row[1]) | (int(row[2]) << 1))
                   for row in rows]
        results = []
        for channel in (0, 1):
            result = analyze_repetition(samples, channel)
            if result:
                results.append({
                    "wire": "AB"[channel],
                    "transitions": result.transitions_per_cycle,
                    "period_us": result.period_us,
                    "frequency_hz": result.frequency_hz,
                    "similarity": result.similarity,
                    "jitter_us": result.jitter_us,
                    "cycles": result.estimated_cycles,
                })
        return results

    def decode_samples(self, rows, channel=1):
        rows = rows[-20_000:]
        samples = [Sample(int(row[0]), int(row[1]) | (int(row[2]) << 1))
                   for row in rows]
        return decode_hex_candidates(samples, int(channel))

    def decode_uart_samples(self, rows, baud=57600):
        rows = rows[-20_000:]
        samples = [Sample(int(row[0]), int(row[1]) | (int(row[2]) << 1))
                   for row in rows]
        decoded = decode_uart_duplex(samples, int(baud))
        self._annotate_cosmic_cruiser(decoded)
        return decoded

    def _annotate_cosmic_cruiser(self, decoded):
        """Acrescenta a interpretação inferida na volta azimutal completa."""
        modulus = self.COSMIC_AZIMUTH_COUNTS
        previous_position = None
        previous_time = None
        speeds = []
        position_frames = 0
        valid_crc_frames = 0
        for frame in decoded["events"]:
            values = frame["values"]
            crc_ok = bool(frame["checksum"].get("crc16_kermit"))
            if crc_ok:
                valid_crc_frames += 1
            info = {
                "label": "Quadro não identificado",
                "confidence": "baixa",
                "crc_ok": crc_ok,
            }
            if len(values) >= 4:
                command = values[3]
                if frame["wire"] == "B" and command == 0x09:
                    info.update(label="Consulta de posição/estado",
                                confidence="alta")
                elif frame["wire"] == "A" and command == 0x09 and len(values) >= 7:
                    position = values[4] | (values[5] << 8) | (values[6] << 16)
                    degrees = (position % modulus) * 360.0 / modulus
                    info.update(
                        label="Resposta de posição azimutal",
                        confidence="alta",
                        position_count=position,
                        position_degrees=degrees,
                    )
                    position_frames += 1
                    if previous_position is not None and previous_time is not None:
                        delta = ((position - previous_position + modulus // 2)
                                 % modulus) - modulus // 2
                        elapsed = (frame["start_us"] - previous_time) / 1_000_000
                        if elapsed > 0:
                            speed = delta * 360.0 / modulus / elapsed
                            if abs(speed) <= 5:
                                info["speed_deg_s"] = speed
                                speeds.append(speed)
                    previous_position = position
                    previous_time = frame["start_us"]
                elif frame["wire"] == "B" and command == 0x80:
                    payload = values[4:12]
                    if payload and all(value == 0 for value in payload):
                        info.update(label="Parar motor", confidence="alta")
                    else:
                        info.update(
                            label="Mover azimute — velocidade máxima (hipótese)",
                            confidence="média/alta",
                        )
                elif frame["wire"] == "B" and command == 0xC0:
                    info.update(
                        label="Configurar/habilitar motor (hipótese)",
                        confidence="média",
                    )
            frame["cosmic"] = info
        decoded["cosmic"] = {
            "mount": "Cosmic Cruiser GoTo",
            "axis": "azimute",
            "counts_per_turn": modulus,
            "counts_per_degree": modulus / 360,
            "position_frames": position_frames,
            "valid_crc_frames": valid_crc_frames,
            "median_speed_deg_s": statistics.median(speeds) if speeds else None,
        }

    def export_replay(self, rows):
        if not rows:
            return {"ok": False, "message": "Nenhuma amostra para exportar"}
        rows = rows[-20_000:]
        target = Path(__file__).with_name("arduino") / "pulse_replay" / "replay_capture.h"
        target.parent.mkdir(parents=True, exist_ok=True)
        origin = int(rows[0][0])
        previous = origin
        lines = [
            "#pragma once",
            "#include <Arduino.h>",
            "",
            "struct ReplayStep { uint32_t delayUs; uint8_t bits; };",
            f"// Gerado pelo LogicLab em {datetime.now().isoformat(timespec='seconds')}",
            f"// {len(rows)} estados; tempo original {int(rows[-1][0]) - origin} us",
            "const ReplayStep REPLAY_STEPS[] PROGMEM = {",
        ]
        for index, row in enumerate(rows):
            stamp = int(row[0])
            bits = int(row[1]) | (int(row[2]) << 1)
            delay = 0 if index == 0 else max(0, stamp - previous)
            lines.append(f"  {{{delay}UL, 0x{bits:02X}}},")
            previous = stamp
        lines.extend([
            "};",
            "const size_t REPLAY_STEP_COUNT = sizeof(REPLAY_STEPS) / sizeof(REPLAY_STEPS[0]);",
            "",
        ])
        target.write_text("\n".join(lines), encoding="utf-8")
        return {"ok": True, "message": f"Replay exportado: {target}", "path": str(target), "steps": len(rows)}

    def export_csv(self, rows=None):
        recorded = self.model.flush_recording()
        if recorded and recorded.exists():
            return {
                "ok": True,
                "message": f"CSV completo já salvo: {recorded}",
                "path": str(recorded),
                "samples": self.model.count(),
            }
        # Durante captura longa a interface exibe só as 20 mil amostras mais
        # recentes, mas o modelo Python conserva a captura completa.
        if not rows:
            rows = [[s.us, s.bits & 1, (s.bits >> 1) & 1]
                    for s in self.model.snapshot()]
        if not rows:
            return {"ok": False, "message": "Nenhuma amostra para exportar"}
        folder = Path(__file__).with_name("captures")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with target.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(["tempo_us", "fio_A_D2", "fio_B_D3"])
            writer.writerows(rows)
        return {
            "ok": True,
            "message": f"CSV exportado: {target}",
            "path": str(target),
            "samples": len(rows),
        }

    def close(self):
        self.finalizing = False
        self.model.stop_recording()
        self.worker.disconnect()
        self.control_executor.shutdown(wait=False, cancel_futures=True)
        self.analysis_executor.shutdown(wait=False, cancel_futures=True)


def main():
    api = LogicLabAPI()
    page = Path(__file__).with_name("logiclab_viewer.html").resolve().as_uri()
    window = webview.create_window(
        "LogicLab — Analisador Lógico",
        page,
        js_api=api,
        width=1280,
        height=780,
        min_size=(960, 620),
        background_color="#000000",
    )
    window.events.closed += api.close
    webview.start(gui="cocoa", debug=False)


if __name__ == "__main__":
    main()
