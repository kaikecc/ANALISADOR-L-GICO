#!/usr/bin/env python3
"""LogicLab nativo: interface WebKit embutida em um processo Python."""

from __future__ import annotations

import queue
import glob
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import webview

from logic_analyzer import (AnalyzerModel, Sample, SerialWorker,
                            analyze_repetition, decode_hex_candidates, list_ports)


class LogicLabAPI:
    def __init__(self):
        self.model = AnalyzerModel()
        self.events: queue.Queue = queue.Queue()
        self.worker = SerialWorker(self.model, self.events)
        self.capturing = False
        self.messages: list[str] = []
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="logiclab")
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
            "export_replay": self.export_replay,
        }
        if action not in allowed:
            raise ValueError(f"Ação não permitida: {action}")
        job_id = uuid.uuid4().hex
        future = self.executor.submit(allowed[action], *(args or []))
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

    def connect(self, port, baud=115200):
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
        return {"ok": True, "message": "Desconectado"}

    def capture(self, start):
        if not self.worker.port:
            return {"ok": False, "message": "Conecte uma porta serial primeiro"}
        if start:
            self.model.clear()
            self.capturing = True
            self.worker.send("START")
            return {"ok": True, "message": "Captura iniciada"}
        self.capturing = False
        self.worker.send_immediate("STOP")
        return {"ok": True, "message": "STOP enviado • recebendo amostras da placa…"}

    def stop_capture(self):
        if not self.worker.port:
            self.capturing = False
            return {"ok": False, "message": "Porta serial fechada"}
        try:
            self.worker.send_immediate("STOP")
            self.capturing = False
            return {"ok": True, "message": "STOP confirmado • aguarde o recebimento das amostras"}
        except Exception as exc:
            self.capturing = False
            return {"ok": False, "message": f"Falha ao enviar STOP: {exc}"}

    def poll(self, known_version=-1):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "capture_end":
                self.capturing = False
            elif kind in ("log", "ready", "error"):
                self.messages.append(str(value))
                self.messages = self.messages[-20:]
        version = self.model.current_version()
        result = {
            "version": version,
            "connected": bool(self.worker.port),
            "capturing": self.capturing,
            "messages": self.messages[-5:],
        }
        if version != int(known_version):
            result["samples"] = [[s.us, s.bits & 1, (s.bits >> 1) & 1]
                                 for s in self.model.snapshot()[-20_000:]]
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

    def close(self):
        self.worker.disconnect()
        self.executor.shutdown(wait=False, cancel_futures=True)


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
