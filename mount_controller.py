#!/usr/bin/env python3
"""Interface nativa para controlar a montagem altazimutal pelo Arduino Mega."""

from __future__ import annotations

import glob
import math
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import serial
from serial.tools import list_ports
import webview


POSITION_RE = re.compile(
    r"AZ:\s*(\d+)\s*cnt\s*\|\s*([-+]?\d+(?:[.,]\d+)?)\s*graus"
    r"\s+ALT:\s*(\d+)\s*cnt\s*\|\s*([-+]?\d+(?:[.,]\d+)?)\s*graus",
    re.IGNORECASE,
)
BNO_RE = re.compile(
    r"BNO:\s*heading\s*([-+]?\d+(?:[.,]\d+)?)\s*"
    r"roll\s*([-+]?\d+(?:[.,]\d+)?)\s*"
    r"pitch\s*([-+]?\d+(?:[.,]\d+)?)\s*"
    r"CAL\s*sys\s*(\d)\s*gyro\s*(\d)\s*accel\s*(\d)\s*mag\s*(\d)",
    re.IGNORECASE,
)
RTC_RE = re.compile(
    r"RTC UTC:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+OSF=(\d)",
    re.IGNORECASE,
)

OBSERVER_LATITUDE = -27.085263
OBSERVER_LONGITUDE = -48.919816
MOON_UPDATE_SECONDS = 5.0
MOON_TOLERANCE_DEGREES = 0.80


def _normalize_degrees(value: float) -> float:
    return value % 360.0


def _signed_angle(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


def moon_horizontal_position(
    instant: datetime, latitude: float, longitude: float
) -> tuple[float, float]:
    """Posição topocêntrica aproximada da Lua: azimute norte→leste e altitude."""
    instant = instant.astimezone(timezone.utc)
    unix_seconds = instant.timestamp()
    julian_day = unix_seconds / 86400.0 + 2440587.5
    days = julian_day - 2451543.5
    rad = math.pi / 180.0

    ascending_node = _normalize_degrees(125.1228 - 0.0529538083 * days)
    inclination = 5.1454
    periapsis = _normalize_degrees(318.0634 + 0.1643573223 * days)
    semi_major = 60.2666  # Raios terrestres.
    eccentricity = 0.054900
    mean_anomaly = _normalize_degrees(115.3654 + 13.0649929509 * days)
    mean_anomaly_sun = _normalize_degrees(356.0470 + 0.9856002585 * days)
    periapsis_sun = _normalize_degrees(282.9404 + 0.0000470935 * days)
    longitude_sun = _normalize_degrees(mean_anomaly_sun + periapsis_sun)

    anomaly_rad = mean_anomaly * rad
    eccentric_anomaly = (
        mean_anomaly
        + (180.0 / math.pi)
        * eccentricity
        * math.sin(anomaly_rad)
        * (1.0 + eccentricity * math.cos(anomaly_rad))
    )
    for _ in range(4):
        e_rad = eccentric_anomaly * rad
        eccentric_anomaly -= (
            eccentric_anomaly
            - (180.0 / math.pi) * eccentricity * math.sin(e_rad)
            - mean_anomaly
        ) / (1.0 - eccentricity * math.cos(e_rad))

    e_rad = eccentric_anomaly * rad
    x_orbit = semi_major * (math.cos(e_rad) - eccentricity)
    y_orbit = semi_major * math.sqrt(1.0 - eccentricity**2) * math.sin(e_rad)
    true_anomaly = math.degrees(math.atan2(y_orbit, x_orbit))
    distance = math.hypot(x_orbit, y_orbit)

    lon = _normalize_degrees(true_anomaly + periapsis)
    lon_rad = lon * rad
    node_rad = ascending_node * rad
    inc_rad = inclination * rad
    x_ecliptic = distance * (
        math.cos(node_rad) * math.cos(lon_rad)
        - math.sin(node_rad) * math.sin(lon_rad) * math.cos(inc_rad)
    )
    y_ecliptic = distance * (
        math.sin(node_rad) * math.cos(lon_rad)
        + math.cos(node_rad) * math.sin(lon_rad) * math.cos(inc_rad)
    )
    z_ecliptic = distance * math.sin(lon_rad) * math.sin(inc_rad)
    ecliptic_longitude = math.degrees(math.atan2(y_ecliptic, x_ecliptic))
    ecliptic_latitude = math.degrees(
        math.atan2(z_ecliptic, math.hypot(x_ecliptic, y_ecliptic))
    )

    mean_longitude = _normalize_degrees(mean_anomaly + periapsis + ascending_node)
    elongation = _normalize_degrees(mean_longitude - longitude_sun)
    argument_latitude = _normalize_degrees(mean_longitude - ascending_node)
    sine = lambda angle: math.sin(angle * rad)
    cosine = lambda angle: math.cos(angle * rad)
    ecliptic_longitude += (
        -1.274 * sine(mean_anomaly - 2 * elongation)
        + 0.658 * sine(2 * elongation)
        - 0.186 * sine(mean_anomaly_sun)
        - 0.059 * sine(2 * mean_anomaly - 2 * elongation)
        - 0.057 * sine(mean_anomaly - 2 * elongation + mean_anomaly_sun)
        + 0.053 * sine(mean_anomaly + 2 * elongation)
        + 0.046 * sine(2 * elongation - mean_anomaly_sun)
        + 0.041 * sine(mean_anomaly - mean_anomaly_sun)
        - 0.035 * sine(elongation)
        - 0.031 * sine(mean_anomaly + mean_anomaly_sun)
        - 0.015 * sine(2 * argument_latitude - 2 * elongation)
        + 0.011 * sine(mean_anomaly - 4 * elongation)
    )
    ecliptic_latitude += (
        -0.173 * sine(argument_latitude - 2 * elongation)
        - 0.055 * sine(mean_anomaly - argument_latitude - 2 * elongation)
        - 0.046 * sine(mean_anomaly + argument_latitude - 2 * elongation)
        + 0.033 * sine(argument_latitude + 2 * elongation)
        + 0.017 * sine(2 * mean_anomaly + argument_latitude)
    )
    distance += -0.58 * cosine(mean_anomaly - 2 * elongation) - 0.46 * cosine(
        2 * elongation
    )

    lon_rad = ecliptic_longitude * rad
    lat_rad = ecliptic_latitude * rad
    x_ecliptic = distance * math.cos(lon_rad) * math.cos(lat_rad)
    y_ecliptic = distance * math.sin(lon_rad) * math.cos(lat_rad)
    z_ecliptic = distance * math.sin(lat_rad)
    obliquity = (23.4393 - 0.0000003563 * days) * rad
    x_equatorial = x_ecliptic
    y_equatorial = y_ecliptic * math.cos(obliquity) - z_ecliptic * math.sin(
        obliquity
    )
    z_equatorial = y_ecliptic * math.sin(obliquity) + z_ecliptic * math.cos(
        obliquity
    )

    sidereal = _normalize_degrees(
        280.46061837 + 360.98564736629 * (julian_day - 2451545.0) + longitude
    )
    sidereal_rad = sidereal * rad
    observer_lat = latitude * rad
    # Subtraia o vetor do observador (em raios terrestres) para incluir paralaxe.
    x_equatorial -= math.cos(observer_lat) * math.cos(sidereal_rad)
    y_equatorial -= math.cos(observer_lat) * math.sin(sidereal_rad)
    z_equatorial -= math.sin(observer_lat)

    right_ascension = math.atan2(y_equatorial, x_equatorial)
    declination = math.atan2(
        z_equatorial, math.hypot(x_equatorial, y_equatorial)
    )
    hour_angle = sidereal_rad - right_ascension
    altitude = math.asin(
        math.sin(observer_lat) * math.sin(declination)
        + math.cos(observer_lat) * math.cos(declination) * math.cos(hour_angle)
    )
    azimuth = math.atan2(
        -math.sin(hour_angle),
        math.tan(declination) * math.cos(observer_lat)
        - math.sin(observer_lat) * math.cos(hour_angle),
    )
    altitude_degrees = math.degrees(altitude)
    if altitude_degrees > -1.0:
        refraction = 1.02 / math.tan(
            math.radians(altitude_degrees + 10.3 / (altitude_degrees + 5.11))
        )
        altitude_degrees += refraction / 60.0
    return _normalize_degrees(math.degrees(azimuth)), altitude_degrees


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
        self.bno = {
            "available": None, "heading": None, "roll": None, "pitch": None,
            "cal_system": 0, "cal_gyro": 0, "cal_accel": 0, "cal_mag": 0,
            "sample": 0,
            "age_seconds": None,
        }
        self.bno_sample = 0
        self.bno_updated_at: float | None = None
        self.rtc = {
            "available": None, "valid": False, "utc": None, "age_seconds": None
        }
        self.rtc_datetime: datetime | None = None
        self.rtc_updated_at: float | None = None
        self.moon = {
            "tracking": False,
            "aligned": False,
            "az": None,
            "alt": None,
            "error_az": None,
            "error_alt": None,
            "status": "parado",
            "latitude": OBSERVER_LATITUDE,
            "longitude": OBSERVER_LONGITUDE,
            "offset_az": 0.0,
            "offset_alt": 0.0,
        }
        self.tracking_stop = threading.Event()
        self.tracking_thread: threading.Thread | None = None
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
            # Abrir a USB reinicia o Mega. A negociação da montagem, o RTC e o
            # BNO055 levam cerca de seis segundos antes de pollConsole() iniciar.
            time.sleep(11.0)
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
            now = datetime.now(timezone.utc)
            self._send_line(
                f"RTC SET {now.year} {now.month} {now.day} "
                f"{now.hour} {now.minute} {now.second}"
            )
            time.sleep(0.05)
            self._send_line("RTC")
            self._send_line("QUERY")
            self._event(f"Conectado a {port}; parada de segurança enviada.", "ok")
            return {"ok": True, "message": f"Conectado a {port}"}
        except Exception as exc:
            self.serial_port = None
            self.connected_port = ""
            self._event(f"Falha de conexão: {exc}", "error")
            return {"ok": False, "message": str(exc)}

    def disconnect(self) -> dict:
        self._stop_tracking(send_stop=False)
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
        bno_match = BNO_RE.search(line)
        if bno_match:
            values = [
                float(value.replace(",", ".")) for value in bno_match.groups()[:3]
            ]
            calibration = [int(value) for value in bno_match.groups()[3:]]
            with self.state_lock:
                self.bno_sample += 1
                self.bno_updated_at = time.monotonic()
                self.bno = {
                    "available": True,
                    "heading": values[0],
                    "roll": values[1],
                    "pitch": values[2],
                    "cal_system": calibration[0],
                    "cal_gyro": calibration[1],
                    "cal_accel": calibration[2],
                    "cal_mag": calibration[3],
                    "sample": self.bno_sample,
                    "age_seconds": 0.0,
                }
            # Telemetria chega cinco vezes por segundo; atualize os instrumentos
            # sem inundar o console visual.
            return
        elif "BNO:" in line.upper() and "INDISPONIVEL" in line.upper():
            with self.state_lock:
                self.bno["available"] = False
        rtc_match = RTC_RE.search(line)
        if rtc_match:
            rtc_time = datetime.strptime(
                rtc_match.group(1), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            valid = rtc_match.group(2) == "0"
            with self.state_lock:
                self.rtc_datetime = rtc_time
                self.rtc_updated_at = time.monotonic()
                self.rtc = {
                    "available": True,
                    "valid": valid,
                    "utc": rtc_time.isoformat().replace("+00:00", "Z"),
                    "age_seconds": 0.0,
                }
        elif "RTC:" in line.upper() and "INDISPONIVEL" in line.upper():
            with self.state_lock:
                self.rtc["available"] = False
                self.rtc["valid"] = False
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

    def initialize_mount(self) -> dict:
        return self._command("INIT")

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

    def _rtc_now(self) -> datetime | None:
        with self.state_lock:
            rtc_time = self.rtc_datetime
            updated_at = self.rtc_updated_at
            valid = bool(self.rtc.get("valid"))
        if not rtc_time or updated_at is None or not valid:
            return None
        return rtc_time + timedelta(seconds=time.monotonic() - updated_at)

    def align_moon(self) -> dict:
        instant = self._rtc_now()
        with self.state_lock:
            current_az = self.position["az"]
            current_alt = self.position["alt"]
        if instant is None:
            return {"ok": False, "message": "RTC DS3231 sem horário UTC válido."}
        if current_az is None or current_alt is None:
            self._send_line("QUERY")
            return {
                "ok": False,
                "message": "Posição indisponível; QUERY enviado. Tente novamente.",
            }
        moon_az, moon_alt = moon_horizontal_position(
            instant, OBSERVER_LATITUDE, OBSERVER_LONGITUDE
        )
        offset_az = _signed_angle(float(current_az), moon_az)
        offset_alt = float(current_alt) - moon_alt
        with self.state_lock:
            self.moon.update({
                "aligned": True,
                "offset_az": offset_az,
                "offset_alt": offset_alt,
                "az": moon_az,
                "alt": moon_alt,
                "status": "alinhado; pronto para rastrear",
            })
        message = (
            f"Lua alinhada: correção AZ {offset_az:+.3f}°, "
            f"ALT {offset_alt:+.3f}°."
        )
        self._event(message, "ok")
        return {"ok": True, "message": message}

    def start_moon_tracking(self, speed: int = 9) -> dict:
        speed = int(speed)
        if not 1 <= speed <= 9:
            return {"ok": False, "message": "Velocidade deve estar entre 1 e 9."}
        if not self.serial_port or not self.serial_port.is_open:
            return {"ok": False, "message": "Arduino não conectado."}
        with self.state_lock:
            aligned = bool(self.moon["aligned"])
        if not aligned:
            return {
                "ok": False,
                "message": "Centre a Lua manualmente e clique em Alinhar na Lua.",
            }
        if self._rtc_now() is None:
            return {"ok": False, "message": "RTC DS3231 sem horário UTC válido."}
        if self.tracking_thread and self.tracking_thread.is_alive():
            return {"ok": True, "message": "Rastreamento lunar já está ativo."}
        self.tracking_stop.clear()
        with self.state_lock:
            self.moon["tracking"] = True
            self.moon["status"] = "iniciando"
        self.tracking_thread = threading.Thread(
            target=self._moon_tracking_loop,
            args=(speed,),
            name="moon-tracking",
            daemon=True,
        )
        self.tracking_thread.start()
        self._event("Rastreamento lunar iniciado.", "ok")
        return {"ok": True, "message": "Rastreamento lunar iniciado."}

    def stop_moon_tracking(self) -> dict:
        self._stop_tracking(send_stop=True)
        return {"ok": True, "message": "Rastreamento lunar parado."}

    def _stop_tracking(self, send_stop: bool) -> None:
        self.tracking_stop.set()
        thread = self.tracking_thread
        if (
            thread
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=1.0)
        self.tracking_thread = None
        with self.state_lock:
            was_tracking = bool(self.moon["tracking"])
            self.moon["tracking"] = False
            self.moon["status"] = "parado"
        if send_stop and was_tracking:
            try:
                self._send_raw(b"!")
            except Exception:
                pass

    def _moon_tracking_loop(self, speed: int) -> None:
        last_rtc_query = 0.0
        try:
            while not self.tracking_stop.is_set():
                instant = self._rtc_now()
                now_monotonic = time.monotonic()
                if now_monotonic - last_rtc_query >= 30.0:
                    self._send_line("RTC")
                    last_rtc_query = now_monotonic
                if instant is None:
                    raise RuntimeError("RTC DS3231 perdeu o horário UTC válido.")

                moon_az, moon_alt = moon_horizontal_position(
                    instant, OBSERVER_LATITUDE, OBSERVER_LONGITUDE
                )
                with self.state_lock:
                    offset_az = float(self.moon["offset_az"])
                    offset_alt = float(self.moon["offset_alt"])
                    current_az = self.position["az"]
                    current_alt = self.position["alt"]
                    motion = dict(self.motion)
                    limits = dict(self.elevation_limits)
                target_az = _normalize_degrees(moon_az + offset_az)
                target_alt = moon_alt + offset_alt

                if moon_alt <= 0.0:
                    raise RuntimeError(
                        f"Lua abaixo do horizonte ({moon_alt:.2f}°)."
                    )
                if limits["enabled"] and not (
                    limits["min"] <= target_alt <= limits["max"]
                ):
                    raise RuntimeError(
                        f"Lua fora da trava de elevação: alvo {target_alt:.2f}°."
                    )

                error_az = (
                    _signed_angle(target_az, float(current_az))
                    if current_az is not None else None
                )
                error_alt = (
                    target_alt - float(current_alt)
                    if current_alt is not None else None
                )
                with self.state_lock:
                    self.moon.update({
                        "az": moon_az,
                        "alt": moon_alt,
                        "target_az": target_az,
                        "target_alt": target_alt,
                        "error_az": error_az,
                        "error_alt": error_alt,
                        "status": "rastreando",
                    })

                if current_az is None or current_alt is None:
                    self._send_line("QUERY")
                elif motion["az"] == "parado" and motion["alt"] == "parado":
                    candidates = [
                        ("AZ", abs(error_az), target_az),
                        ("ALT", abs(error_alt), target_alt),
                    ]
                    axis, magnitude, target = max(candidates, key=lambda item: item[1])
                    if magnitude >= MOON_TOLERANCE_DEGREES:
                        self.goto(axis, target, speed)
                    else:
                        self._send_line("QUERY")
                self.tracking_stop.wait(MOON_UPDATE_SECONDS)
        except Exception as exc:
            self._event(f"RASTREAMENTO LUA: {exc}", "error")
            try:
                self._send_raw(b"!")
            except Exception:
                pass
        finally:
            with self.state_lock:
                self.moon["tracking"] = False
                if self.moon["status"] == "rastreando":
                    self.moon["status"] = "parado"

    def emergency_stop(self) -> dict:
        self._stop_tracking(send_stop=False)
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
            bno = dict(self.bno)
            if self.bno_updated_at is not None:
                bno["age_seconds"] = max(
                    0.0, time.monotonic() - self.bno_updated_at
                )
            rtc = dict(self.rtc)
            if self.rtc_updated_at is not None:
                rtc["age_seconds"] = max(
                    0.0, time.monotonic() - self.rtc_updated_at
                )
                if self.rtc_datetime is not None and rtc.get("valid"):
                    current_rtc = self.rtc_datetime + timedelta(
                        seconds=rtc["age_seconds"]
                    )
                    rtc["utc"] = current_rtc.isoformat().replace("+00:00", "Z")
            moon = dict(self.moon)
        return {
            "connected": bool(self.serial_port and self.serial_port.is_open),
            "port": self.connected_port,
            "position": position,
            "motion": dict(self.motion),
            "elevation_limits": dict(self.elevation_limits),
            "bno": bno,
            "rtc": rtc,
            "moon": moon,
            "messages": messages,
            "sequence": self.sequence,
        }

    def close(self) -> None:
        self.disconnect()


def main() -> None:
    api = MountControllerAPI()
    html_path = Path(__file__).with_name("mount_controller.html").resolve()
    # Evita que o WebKit reutilize uma versão antiga da interface após uma
    # atualização do HTML/JavaScript.
    if sys.platform == "win32":
        page = html_path.as_uri()
    else:
        page = f"{html_path.as_uri()}?v={html_path.stat().st_mtime_ns}"
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
    gui = "cocoa" if sys.platform == "darwin" else None
    webview.start(gui=gui, debug=False)


if __name__ == "__main__":
    main()
