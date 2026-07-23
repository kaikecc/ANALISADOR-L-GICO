#!/usr/bin/env python3
"""Interface nativa para controlar a montagem altazimutal pelo Arduino Mega."""

from __future__ import annotations

import glob
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path

import serial
from serial.tools import list_ports
import webview


POSITION_RE = re.compile(
    r"AZ:\s*(\d+)\s*cnt\s*\|\s*([-+]?\d+(?:[.,]\d+)?)\s*graus"
    r"\s+ALT:\s*(\d+)\s*cnt\s*\|\s*([-+]?\d+(?:[.,]\d+)?)\s*graus",
    re.IGNORECASE,
)


class MountControllerAPI:
    """Ponte pequena e thread-safe entre a interface e a porta USB."""

    def __init__(self) -> None:
        self.serial_port: serial.Serial | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_reader = threading.Event()
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.messages: deque[dict] = deque(maxlen=100)
        self.position = {"az": None, "alt": None, "az_counts": None, "alt_counts": None}
        self.connected_port = ""
        self.motion = {"az": "parado", "alt": "parado"}
        self.elevation_limits = {"enabled": True, "min": 35.0, "max": 85.0}
        self.sequence = 0

    def _event(self, text: str, level: str = "info") -> None:
        with self.state_lock:
            self.sequence += 1
            self.messages.append({
                "id": self.sequence,
                "time": time.strftime("%H:%M:%S"),
                "text": text,
                "level": level,
            })

    def ports(self) -> list[dict]:
        detected = {}
        for port in list_ports.comports():
            identity = " ".join(filter(None, [
                port.device, port.description, port.manufacturer, port.product
            ]))
            identity_lower = identity.lower()
            is_usb_serial = (
                not port.device.startswith("/dev/cu.")
                or port.device.startswith(("/dev/cu.usb", "/dev/cu.wch", "/dev/cu.SLAB"))
                or any(name in identity_lower for name in (
                    "arduino", "ch340", "ch341", "cp210", "ftdi",
                ))
            )
            if "bluetooth" not in identity_lower and is_usb_serial:
                detected[port.device] = port.description or "Interface serial USB"
        # No macOS, somente dispositivos /dev/cu.* podem ser abertos para uma
        # conexão iniciada pelo computador. Bluetooth não é uma placa Arduino.
        for device in glob.glob("/dev/cu.usb*"):
            detected.setdefault(device, "Interface serial")
        return [
            {"device": device, "description": description}
            for device, description in sorted(
                detected.items(),
                key=lambda item: (
                    "usbmodem" not in item[0] and "usbserial" not in item[0],
                    item[0],
                ),
            )
        ]

    def connect(self, port: str, baud: int = 115200) -> dict:
        if not port:
            return {"ok": False, "message": "Selecione uma porta serial."}
        if "bluetooth" in str(port).lower():
            return {
                "ok": False,
                "message": "A porta Bluetooth não é o Arduino. Selecione uma porta USB.",
            }
        self.disconnect()
        try:
            connection = serial.Serial(str(port), int(baud), timeout=0.2, write_timeout=0.5)
            time.sleep(1.8)  # O Mega reinicia ao abrir a USB.
            connection.reset_input_buffer()
            self.serial_port = connection
            self.connected_port = str(port)
            self.stop_reader.clear()
            self.reader_thread = threading.Thread(
                target=self._read_loop,
                args=(connection,),
                name="mount-serial-reader",
                daemon=True,
            )
            self.reader_thread.start()
            # Estado inicial conservador: ambos os motores recebem parada.
            self._send_raw(b"!\n")
            time.sleep(0.08)
            self._send_line("QUERY")
            self._event(f"Conectado a {port}; parada de segurança enviada.", "ok")
            return {"ok": True, "message": f"Conectado a {port}"}
        except Exception as exc:
            self.serial_port = None
            self.connected_port = ""
            self._event(f"Falha de conexão: {exc}", "error")
            return {"ok": False, "message": str(exc)}

    def disconnect(self) -> dict:
        connection = self.serial_port
        reader = self.reader_thread
        self.stop_reader.set()
        if connection and connection.is_open:
            try:
                self._send_raw(b"!\n")
                time.sleep(0.04)
            except Exception:
                pass
        if self.serial_port is connection:
            self.serial_port = None
        self.connected_port = ""
        if connection:
            try:
                connection.close()
            except Exception:
                pass
        if reader and reader is not threading.current_thread() and reader.is_alive():
            reader.join(timeout=0.6)
        if self.reader_thread is reader:
            self.reader_thread = None
        self.motion = {"az": "parado", "alt": "parado"}
        return {"ok": True, "message": "Desconectado; motores parados."}

    def _read_loop(self, connection: serial.Serial) -> None:
        while not self.stop_reader.is_set() and self.serial_port is connection:
            if not connection.is_open:
                break
            try:
                raw = connection.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._parse_line(line)
            except (serial.SerialException, OSError) as exc:
                # Fechar a porta intencionalmente interrompe readline(). Isso
                # não deve invalidar uma conexão nova aberta logo depois.
                if not self.stop_reader.is_set() and self.serial_port is connection:
                    self._event(f"Conexão serial interrompida: {exc}", "error")
                    self.serial_port = None
                    self.connected_port = ""
                break

    def _parse_line(self, line: str) -> None:
        match = POSITION_RE.search(line)
        if match:
            altitude = float(match.group(4).replace(",", "."))
            with self.state_lock:
                self.position = {
                    "az_counts": int(match.group(1)),
                    "az": float(match.group(2).replace(",", ".")),
                    "alt_counts": int(match.group(3)),
                    "alt": altitude,
                }
            self._enforce_elevation_limits(altitude)
        upper = line.upper()
        level = "error" if "ERRO" in upper or "AVISO" in upper else "ok" if (
            "PRONTO" in upper or "ALVO ATINGIDO" in upper
        ) else "info"
        if "MOTORES PARADOS" in upper:
            self.motion = {"az": "parado", "alt": "parado"}
        elif "AZIMUTE PARADO" in upper:
            self.motion["az"] = "parado"
        elif "ELEVACAO PARADO" in upper:
            self.motion["alt"] = "parado"
        self._event(line, level)

    def _send_raw(self, payload: bytes) -> None:
        connection = self.serial_port
        if not connection or not connection.is_open:
            raise RuntimeError("Arduino não conectado.")
        with self.write_lock:
            connection.write(payload)
            connection.flush()

    def _send_line(self, command: str) -> None:
        self._send_raw((command + "\n").encode("ascii"))

    def query(self) -> dict:
        return self._command("QUERY")

    def move(self, axis: str, direction: str, speed: int) -> dict:
        axis = axis.upper()
        direction = direction.upper()
        speed = int(speed)
        if axis not in {"AZ", "ALT"} or direction not in {"POS", "NEG"}:
            return {"ok": False, "message": "Eixo ou direção inválidos."}
        if not 1 <= speed <= 9:
            return {"ok": False, "message": "A velocidade deve estar entre 1 e 9."}
        if axis == "ALT" and self.elevation_limits["enabled"]:
            altitude = self.position["alt"]
            if altitude is None:
                return {
                    "ok": False,
                    "message": "Consulte a posição antes de mover a elevação.",
                }
            minimum = self.elevation_limits["min"]
            maximum = self.elevation_limits["max"]
            if direction == "POS" and altitude >= maximum:
                return {
                    "ok": False,
                    "message": f"Limite máximo de elevação atingido ({maximum:.1f}°).",
                }
            if direction == "NEG" and altitude <= minimum:
                return {
                    "ok": False,
                    "message": f"Limite mínimo de elevação atingido ({minimum:.1f}°).",
                }
        result = self._command(f"{axis} {direction} {speed}")
        if result["ok"]:
            key = "az" if axis == "AZ" else "alt"
            self.motion[key] = "positivo" if direction == "POS" else "negativo"
        return result

    def stop_axis(self, axis: str) -> dict:
        axis = axis.upper()
        if axis not in {"AZ", "ALT"}:
            return {"ok": False, "message": "Eixo inválido."}
        result = self._command(f"{axis} STOP")
        if result["ok"]:
            self.motion["az" if axis == "AZ" else "alt"] = "parado"
        return result

    def goto(self, axis: str, degrees: float, speed: int) -> dict:
        axis = axis.upper()
        degrees = float(degrees)
        speed = int(speed)
        if axis not in {"AZ", "ALT"}:
            return {"ok": False, "message": "Eixo inválido."}
        if not 0 <= degrees < 360:
            return {"ok": False, "message": "O ângulo deve estar entre 0° e 359,999°."}
        if not 1 <= speed <= 9:
            return {"ok": False, "message": "A velocidade deve estar entre 1 e 9."}
        if axis == "ALT" and self.elevation_limits["enabled"]:
            minimum = self.elevation_limits["min"]
            maximum = self.elevation_limits["max"]
            if self.position["alt"] is None:
                return {
                    "ok": False,
                    "message": "Consulte a posição antes de comandar a elevação.",
                }
            if not minimum <= degrees <= maximum:
                return {
                    "ok": False,
                    "message": (
                        f"Alvo bloqueado pela trava de elevação "
                        f"({minimum:.1f}° a {maximum:.1f}°)."
                    ),
                }
        result = self._command(f"{axis} GOTO {degrees:.4f} {speed}")
        if result["ok"]:
            key = "az" if axis == "AZ" else "alt"
            current = self.position[key]
            if current is None:
                self.motion[key] = "goto"
            else:
                error = (degrees - float(current) + 180.0) % 360.0 - 180.0
                self.motion[key] = "goto_positivo" if error >= 0 else "goto_negativo"
        return result

    def emergency_stop(self) -> dict:
        try:
            self._send_raw(b"!")
            self.motion = {"az": "parado", "alt": "parado"}
            self._event("PARADA DE EMERGÊNCIA enviada.", "error")
            return {"ok": True, "message": "Parada de emergência enviada."}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def set_elevation_limits(
        self, enabled: bool, minimum: float, maximum: float
    ) -> dict:
        try:
            minimum = float(minimum)
            maximum = float(maximum)
        except (TypeError, ValueError):
            return {"ok": False, "message": "Informe limites numéricos válidos."}
        if not 0 <= minimum < maximum < 360:
            return {
                "ok": False,
                "message": "Use 0° ≤ mínimo < máximo < 360°.",
            }
        self.elevation_limits = {
            "enabled": bool(enabled),
            "min": minimum,
            "max": maximum,
        }
        state = "ativada" if enabled else "desativada"
        message = (
            f"Trava de elevação {state}: {minimum:.1f}° a {maximum:.1f}°."
        )
        self._event(message, "ok")
        if enabled and self.position["alt"] is not None:
            self._enforce_elevation_limits(float(self.position["alt"]))
        return {"ok": True, "message": message}

    def _enforce_elevation_limits(self, altitude: float) -> None:
        limits = self.elevation_limits
        if not limits["enabled"] or self.motion["alt"] == "parado":
            return
        moving = self.motion["alt"]
        reached_maximum = altitude >= limits["max"] and moving in {
            "positivo", "goto_positivo"
        }
        reached_minimum = altitude <= limits["min"] and moving in {
            "negativo", "goto_negativo"
        }
        if not reached_maximum and not reached_minimum:
            return
        # Marque como parado antes de transmitir para não repetir o comando
        # enquanto chegam linhas consecutivas de telemetria.
        self.motion["alt"] = "parado"
        limit = limits["max"] if reached_maximum else limits["min"]
        try:
            self._send_line("ALT STOP")
            self._event(
                f"TRAVA: elevação parada automaticamente em {altitude:.3f}° "
                f"(limite {limit:.1f}°).",
                "error",
            )
        except Exception as exc:
            self._event(f"Falha ao aplicar trava de elevação: {exc}", "error")

    def _command(self, command: str) -> dict:
        try:
            self._send_line(command)
            self._event(f"→ {command}")
            return {"ok": True, "message": f"Comando enviado: {command}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def poll(self, after: int = 0) -> dict:
        with self.state_lock:
            messages = [item for item in self.messages if item["id"] > int(after)]
            position = dict(self.position)
        return {
            "connected": bool(self.serial_port and self.serial_port.is_open),
            "port": self.connected_port,
            "position": position,
            "motion": dict(self.motion),
            "elevation_limits": dict(self.elevation_limits),
            "messages": messages,
            "sequence": self.sequence,
        }

    def close(self) -> None:
        self.disconnect()


def main() -> None:
    api = MountControllerAPI()
    page = Path(__file__).with_name("mount_controller.html").resolve().as_uri()
    window = webview.create_window(
        "LogicLab — Controle Altazimutal",
        page,
        js_api=api,
        width=1280,
        height=820,
        min_size=(940, 680),
        background_color="#0d1015",
    )
    window.events.closed += api.close
    webview.start(gui="cocoa", debug=False)


if __name__ == "__main__":
    main()
