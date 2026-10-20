#!/usr/bin/env python3
"""Captura DEBUGINIT do controlador Cosmic Cruiser pela USB."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports


def default_port() -> str:
    ports = list(list_ports.comports())
    preferred = [
        port.device
        for port in ports
        if "arduino" in f"{port.description} {port.device}".lower()
    ]
    if preferred:
        return preferred[0]
    if ports:
        return ports[0].device
    raise SystemExit("Nenhuma porta serial encontrada.")


def print_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("Nenhuma porta serial encontrada.")
        return
    print("Portas encontradas:")
    for port in ports:
        print(f"  {port.device}: {port.description}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None, help="Porta USB do Arduino, ex.: COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--out", default="captures/debuginit_montagem.csv")
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--list-ports", action="store_true")
    args = parser.parse_args()

    if args.list_ports:
        print_ports()
        return 0

    port = args.port or default_port()
    out_path = Path(args.out)
    log_path = out_path.with_suffix(".log")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Mantem o mesmo cabecalho do LogicLab para reaproveitar os decodificadores.
    # Nesta captura: fio_A_D2 = D19/RX1 e fio_B_D3 = D18/TX1.
    csv_lines: list[str] = ["tempo_us,fio_A_D2,fio_B_D3\n"]
    log_lines: list[str] = []

    try:
        conn = serial.Serial(port, args.baud, timeout=0.25, write_timeout=1.0)
    except serial.SerialException as exc:
        print(f"Erro ao abrir {port}: {exc}", file=sys.stderr)
        print("Feche a Arduino IDE, Monitor Serial e a interface da montagem e tente de novo.", file=sys.stderr)
        return 1

    with conn:
        print(f"Conectado em {port}. Aguardando reset do Mega...")
        time.sleep(11.0)
        conn.reset_input_buffer()
        conn.write(b"DEBUGINIT\n")
        conn.flush()
        print("DEBUGINIT enviado; capturando saida...")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            raw = conn.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            log_lines.append(line + "\n")
            print(line)
            if line.startswith("D,"):
                _, tempo_us, fio_a, fio_b = line.split(",", 3)
                csv_lines.append(f"{tempo_us},{fio_a},{fio_b}\n")
            if line.startswith("#MOUNT_DEBUG_END"):
                break

    out_path.write_text("".join(csv_lines), encoding="utf-8")
    log_path.write_text("".join(log_lines), encoding="utf-8")
    print(f"CSV salvo em {out_path}")
    print(f"Log salvo em {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
