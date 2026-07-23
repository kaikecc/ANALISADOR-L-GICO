#!/usr/bin/env python3
"""LogicLab — analisador lógico serial para Arduino.

Protocolo do firmware: linhas ASCII ``D,<micros>,<bits>`` e ``#<mensagem>``.
"""

from __future__ import annotations

import csv
import functools
import glob
import html
import itertools
import json
import math
import queue
import statistics
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # permite abrir o modo demonstração sem pyserial
    serial = None
    list_ports = None


BG = "#0b1020"
PANEL = "#11182b"
PANEL_2 = "#151e34"
GRID = "#26324c"
TEXT = "#e7ecf7"
MUTED = "#8491aa"
ACCENT = "#35d0ba"
BLUE = "#5b8cff"
ORANGE = "#ffae57"
RED = "#ff5d73"
CHANNEL_COLORS = ["#00e5ff", "#ff3df2", "#ffd600", "#7cff00", "#ff6b35", "#8c7bff"]
BUILD_ID = "2026.07.22-r3"
DEBUG_LOG = Path(__file__).with_name("logiclab_debug.log")
WEB_VIEWER = Path(__file__).with_name("logiclab_graph.html")


@dataclass
class Sample:
    us: int
    bits: int


@dataclass
class RepetitionResult:
    channel: int
    transitions_per_cycle: int
    period_us: float
    frequency_hz: float
    similarity: float
    jitter_us: float
    estimated_cycles: float


class AnalyzerModel:
    def __init__(self, limit: int = 1_000_000):
        self.samples: deque[Sample] = deque(maxlen=limit)
        self.lock = threading.Lock()
        self.version = 0
        self.recording_file = None
        self.recording_path: Path | None = None
        self.malformed_samples = 0

    def add(self, us: int, bits: int):
        with self.lock:
            # A captura é limpa explicitamente antes de START. Um timestamp que
            # volta durante a mesma aquisição é uma linha serial corrompida;
            # descarte somente essa linha, nunca toda a volta já registrada.
            if self.samples and us < self.samples[-1].us:
                self.malformed_samples += 1
                self.version += 1
                return
            self.samples.append(Sample(us, bits))
            if self.recording_file is not None:
                self.recording_file.write(f"{us},{bits & 1},{(bits >> 1) & 1}\n")
            self.version += 1

    def start_recording(self, path: Path):
        with self.lock:
            if self.recording_file is not None:
                self.recording_file.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.recording_file = path.open("w", encoding="utf-8", buffering=64 * 1024)
            self.recording_file.write("tempo_us,fio_A_D2,fio_B_D3\n")
            self.recording_path = path

    def stop_recording(self) -> Path | None:
        with self.lock:
            if self.recording_file is not None:
                self.recording_file.flush()
                self.recording_file.close()
                self.recording_file = None
            return self.recording_path

    def flush_recording(self) -> Path | None:
        with self.lock:
            if self.recording_file is not None:
                self.recording_file.flush()
            return self.recording_path

    def replace(self, samples: list[Sample]):
        with self.lock:
            self.samples.clear()
            self.samples.extend(samples[-self.samples.maxlen:])
            self.version += 1

    def clear(self):
        with self.lock:
            self.samples.clear()
            self.malformed_samples = 0
            self.version += 1

    def malformed_count(self) -> int:
        with self.lock:
            return self.malformed_samples

    def current_version(self) -> int:
        with self.lock:
            return self.version

    def count(self) -> int:
        with self.lock:
            return len(self.samples)

    def snapshot(self) -> list[Sample]:
        with self.lock:
            return list(self.samples)

    def snapshot_tail(self, limit: int) -> list[Sample]:
        with self.lock:
            if limit <= 0 or limit >= len(self.samples):
                return list(self.samples)
            # deque não oferece slicing; islice evita materializar toda a
            # captura apenas para atualizar a janela recente do gráfico.
            start = len(self.samples) - limit
            return list(itertools.islice(self.samples, start, None))


class SerialWorker:
    def __init__(self, model: AnalyzerModel, events: queue.Queue):
        self.model, self.events = model, events
        self.port = None
        self.running = False
        self.thread = None
        self.tx_queue: queue.Queue[str] = queue.Queue()
        self.ready_at = 0.0

    def connect(self, name: str, baud: int):
        if serial is None:
            raise RuntimeError("pyserial não instalado (pip install -r requirements.txt)")
        try:
            self.port = serial.Serial(name, baud, timeout=0.1, write_timeout=1)
        except serial.SerialException as exc:
            if "Resource busy" in str(exc) or "Device or resource busy" in str(exc):
                raise RuntimeError(
                    "A porta está ocupada. Feche o Monitor Serial e o Plotter "
                    "Serial da Arduino IDE e tente novamente."
                ) from exc
            raise
        # Abrir a porta reinicia o Mega. Comandos enviados durante o bootloader
        # são descartados, portanto a fila TX só será liberada após 2 segundos.
        self.ready_at = time.monotonic() + 2.2
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        # Guarde a instância usada por esta thread. Assim, a finalização tardia
        # de uma conexão antiga não derruba uma conexão nova.
        port = self.port
        try:
            while self.running and self.port is port and port and port.is_open:
                if time.monotonic() >= self.ready_at:
                    while True:
                        try:
                            command = self.tx_queue.get_nowait()
                        except queue.Empty:
                            break
                        port.write((command + "\n").encode("ascii"))
                    port.flush()
                raw = port.readline().decode("ascii", errors="ignore").strip()
                if not raw:
                    continue
                if raw.startswith("D,"):
                    try:
                        _, stamp, bits = raw.split(",", 2)
                        self.model.add(int(stamp), int(bits, 0))
                    except ValueError:
                        self.events.put(("log", f"Pacote inválido: {raw}"))
                elif raw.startswith("#"):
                    message = raw[1:].strip()
                    self.events.put(("log", message))
                    if message == "END":
                        self.events.put(("capture_end", None))
                    if "LogicLab" in message or message == "PONG":
                        self.events.put(("ready", message))
        except Exception as exc:
            if self.running:
                self.events.put(("error", str(exc)))
        finally:
            try:
                if port and port.is_open:
                    port.close()
            except Exception:
                pass
            if self.port is port:
                self.port = None
                self.running = False
            self.events.put(("disconnected", None))

    def send(self, command: str):
        if self.port and self.port.is_open:
            self.tx_queue.put(command.strip())

    def send_immediate(self, command: str):
        """Envia comandos urgentes sem aguardar o ciclo da thread de leitura."""
        port = self.port
        if not port or not port.is_open:
            raise RuntimeError("Porta serial fechada")
        port.write((command.strip() + "\n").encode("ascii"))
        port.flush()

    def disconnect(self):
        self.running = False
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        while not self.tx_queue.empty():
            try:
                self.tx_queue.get_nowait()
            except queue.Empty:
                break


class WaveCanvas(tk.Canvas):
    def __init__(self, master, model: AnalyzerModel, **kwargs):
        light_mode = kwargs.pop("light_mode", False)
        canvas_bg = "#000000"
        super().__init__(master, bg=canvas_bg, highlightthickness=0, **kwargs)
        self.model = model
        self.grid_color = "#ffffff"
        self.text_color = "#ffffff"
        self.muted_color = "#ffffff"
        self.channel_colors = ["#00e5ff", "#ff3df2", "#ffd600", "#7cff00", "#ff6b35", "#8c7bff"]
        self.zoom = 1.0
        self.channel_names = ["FIO A / D2 / INT4", "FIO B / D3 / INT5"]
        self.visible = [True, True]
        self.cursor_a = None
        self.cursor_b = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-1>", self._click)

    def _wheel(self, event):
        self.zoom = max(1, min(64, self.zoom * (1.25 if event.delta > 0 else 0.8)))
        self.redraw()

    def _click(self, event):
        if self.cursor_a is None or self.cursor_b is not None:
            self.cursor_a, self.cursor_b = event.x, None
        else:
            self.cursor_b = event.x
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
        left, top, bottom = 150, 34, 34
        active = [i for i, value in enumerate(self.visible) if value]
        if not active:
            return
        row_h = (h - top - bottom) / len(active)
        for x in range(left, w, 100):
            self.create_line(x, top, x, h - bottom, fill=self.grid_color, dash=(2, 5))
        for row, ch in enumerate(active):
            y0 = top + row * row_h
            hi, lo = y0 + row_h * .27, y0 + row_h * .73
            self.create_text(12, y0 + row_h / 2, text=self.channel_names[ch], anchor="w",
                             fill=self.channel_colors[ch], font=("TkDefaultFont", 9, "bold"))
            self.create_text(left - 9, hi, text="1", anchor="e", fill=self.muted_color)
            self.create_text(left - 9, lo, text="0", anchor="e", fill=self.muted_color)
            self.create_line(left, hi, w, hi, fill=self.grid_color, dash=(1, 5))
            self.create_line(left, lo, w, lo, fill=self.grid_color, dash=(1, 5))
            self.create_line(left, y0 + row_h, w, y0 + row_h, fill=self.grid_color)
        data = self.model.snapshot()
        if len(data) >= 2:
            total_span = max(1, data[-1].us - data[0].us)
            span = max(1, int(total_span / self.zoom))
            end_us, start_us = data[-1].us, data[-1].us - span
            shown = [s for s in data if s.us >= start_us]
            if shown:
                durations = [end - begin for ch in (0, 1)
                             for begin, end, _level in line_runs(shown, ch)]
                unit = estimate_time_unit(durations) if durations else 1.0
                for row, ch in enumerate(active):
                    y0 = top + row * row_h
                    hi, lo = y0 + row_h * .27, y0 + row_h * .73
                    points = []
                    prev = (shown[0].bits >> ch) & 1
                    points.extend((left, hi if prev else lo))
                    for sample in shown[1:]:
                        value = (sample.bits >> ch) & 1
                        x = left + (sample.us - start_us) / span * (w - left)
                        if value != prev:
                            points.extend((x, hi if prev else lo, x, hi if value else lo))
                            prev = value
                        else:
                            points.extend((x, hi if value else lo))
                    self.create_line(*points, fill=self.channel_colors[ch], width=3, joinstyle="miter")
                    # Rótulos de bits na unidade temporal inferida. Limita a
                    # quantidade para manter o osciloscópio legível.
                    first_cell = math.floor(start_us / unit)
                    last_cell = math.ceil(end_us / unit)
                    if 0 < last_cell - first_cell <= 80:
                        for cell in range(first_cell, last_cell):
                            cell_start = max(start_us, cell * unit)
                            cell_end = min(end_us, (cell + 1) * unit)
                            x1 = left + (cell_start - start_us) / span * (w - left)
                            x2 = left + (cell_end - start_us) / span * (w - left)
                            if x2 - x1 < 12:
                                continue
                            bit = level_at(data, (cell_start + cell_end) / 2, ch)
                            self.create_line(x1, y0 + 4, x1, y0 + row_h - 4,
                                             fill=self.grid_color, dash=(2, 4))
                            self.create_text((x1 + x2) / 2, y0 + 5, text=str(bit),
                                             anchor="n", fill=self.channel_colors[ch],
                                             font=("TkDefaultFont", 10, "bold"))
                for x in range(left, w, 100):
                    stamp = start_us + (x - left) / max(1, w - left) * span
                    self.create_text(x, h - 17, text=format_time(stamp), fill=self.muted_color,
                                     anchor="n", font=("TkDefaultFont", 8))
                self.create_text(left, 9, text=f"● SINAL  •  {len(data):,} amostras",
                                 anchor="nw", fill=self.channel_colors[0],
                                 font=("TkDefaultFont", 10, "bold"))
                self.create_text(w - 12, 9, text=f"bit ≈ {format_time(unit)}  •  janela {format_time(span)}  •  zoom {self.zoom:.1f}×",
                                 anchor="ne", fill=self.muted_color)
        else:
            self.create_text(left, 9, text="○ AGUARDANDO SINAL", anchor="nw",
                             fill=self.muted_color, font=("TkDefaultFont", 10, "bold"))
            self.create_text((left + w) / 2, h / 2,
                             text="Aguardando dados…\nUse Modo demo ou conecte o Arduino",
                             justify="center", fill=self.muted_color, font=("TkDefaultFont", 15))
        for label, x, color in (("A", self.cursor_a, BLUE), ("B", self.cursor_b, ORANGE)):
            if x is not None:
                self.create_line(x, top, x, h - bottom, fill=color, dash=(5, 3), width=2)
                self.create_text(x + 5, top + 3, text=label, anchor="nw", fill=color, font=("TkDefaultFont", 9, "bold"))
        if self.cursor_a is not None and self.cursor_b is not None:
            dx = abs(self.cursor_b - self.cursor_a)
            span_px = max(1, w - left)
            data = self.model.snapshot()
            if len(data) > 1:
                visible_us = (data[-1].us - data[0].us) / self.zoom
                delta = dx / span_px * visible_us
                freq = 1_000_000 / delta if delta else 0
                self.create_text((self.cursor_a + self.cursor_b) / 2, top + 8, text=f"Δ {format_time(delta)}  |  {freq:,.1f} Hz", fill=self.text_color, anchor="n")


class ClassicEventTable(tk.Listbox):
    """Tabela simples para Macs cujo ttk antigo não é renderizado."""

    def get_children(self):
        return tuple(range(self.size()))

    def delete(self, *items):
        if items:
            super().delete(0, tk.END)

    def insert(self, _parent, _position, values=()):
        stamp, proto, event, value = values
        super().insert(tk.END, f"{stamp:<14}  {proto:<20}  {event:<14}  {value}")


class ClassicWaveView(tk.Label):
    """Osciloscópio textual compatível com o Tk 8.5 do macOS.

    Nesse runtime, Canvas e Text podem abrir normalmente mas deixar a região
    de desenho totalmente branca. Label continua sendo renderizado pelo Aqua,
    então o traçado é montado como texto monoespaçado e aplicado de uma vez.
    """

    def __init__(self, master, model: AnalyzerModel, **kwargs):
        super().__init__(master, bg="#000000", fg="#ffffff", relief="solid",
                         borderwidth=1, highlightthickness=1,
                         highlightbackground="#ffffff", highlightcolor="#ffffff",
                         font=("Menlo", 11), padx=12, pady=10,
                         justify="left", anchor="nw", **kwargs)
        self.model = model
        self.zoom = 1.0
        self.visible = [True, True]
        self.channel_names = ["FIO A / D2 / INT4", "FIO B / D3 / INT5"]
        self.cursor_a = self.cursor_b = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<MouseWheel>", self._wheel)

    def _wheel(self, event):
        self.zoom = max(1, min(64, self.zoom * (1.25 if event.delta > 0 else 0.8)))
        self.redraw()

    def redraw(self):
        data = self.model.snapshot()
        width = max(320, self.winfo_width())
        height = max(220, self.winfo_height())
        pixels = bytearray(b"\x00\x00\x00" * width * height)

        def rgb(color):
            color = color.lstrip("#")
            return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))

        def line(x1, y1, x2, y2, color, thickness=1):
            color = bytes(rgb(color))
            dx, dy = abs(x2 - x1), -abs(y2 - y1)
            sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
            error = dx + dy
            while True:
                for oy in range(-(thickness // 2), thickness // 2 + 1):
                    yy = y1 + oy
                    if 0 <= x1 < width and 0 <= yy < height:
                        offset = (yy * width + x1) * 3
                        pixels[offset:offset + 3] = color
                if x1 == x2 and y1 == y2:
                    break
                twice = 2 * error
                if twice >= dy:
                    error += dy
                    x1 += sx
                if twice <= dx:
                    error += dx
                    y1 += sy

        left, top, bottom = 28, 20, 22
        for x in range(left, width, 80):
            line(x, top, x, height - bottom, "#ffffff")
        active = [i for i, enabled in enumerate(self.visible) if enabled]
        row_height = max(1, (height - top - bottom) // max(1, len(active)))
        for row, _channel in enumerate(active):
            y0 = top + row * row_height
            line(left, y0 + row_height // 4, width - 1, y0 + row_height // 4, "#ffffff")
            line(left, y0 + 3 * row_height // 4, width - 1, y0 + 3 * row_height // 4, "#ffffff")
            line(left, y0 + row_height - 1, width - 1, y0 + row_height - 1, "#ffffff")

        if len(data) < 2:
            # Uma pequena marca visível confirma que a região do gráfico foi
            # renderizada, mesmo antes da primeira captura.
            line(left, height // 2, width - 28, height // 2, "#ffffff", 2)
            self._set_bitmap(width, height, pixels)
            return
        # Exibir a captura inteira quando há centenas de transições faz as
        # linhas se fundirem visualmente numa faixa preenchida. A janela inicial
        # preserva o formato de onda quadrada; a roda do mouse amplia/reduz.
        data = data[-80:]
        total_span = max(1, data[-1].us - data[0].us)
        span = max(1, int(total_span / self.zoom))
        end_us, start_us = data[-1].us, data[-1].us - span
        colors = ["#00e5ff", "#ff3df2"]
        for row, channel in enumerate(active):
            y0 = top + row * row_height
            high, low = y0 + row_height // 4, y0 + 3 * row_height // 4
            previous = level_at(data, start_us, channel)
            previous_y = high if previous else low
            # Amostra uma vez por coluna de tela. Isso mantém o custo limitado
            # ao tamanho visível, mesmo com dezenas de milhares de transições.
            for x in range(left + 1, width):
                stamp = start_us + (x - left) / max(1, width - left - 1) * span
                value = level_at(data, stamp, channel)
                y = high if value else low
                line(x - 1, previous_y, x, previous_y, colors[channel], 3)
                if y != previous_y:
                    line(x, previous_y, x, y, colors[channel], 3)
                previous_y = y
        self._set_bitmap(width, height, pixels)

    def _set_bitmap(self, width, height, pixels):
        ppm = f"P6\n{width} {height}\n255\n".encode("ascii") + pixels
        self._image = tk.PhotoImage(data=ppm, format="PPM")
        self.configure(image=self._image, text="")


def format_time(us: float) -> str:
    if us < 1_000:
        return f"{us:.1f} µs"
    if us < 1_000_000:
        return f"{us / 1_000:.2f} ms"
    return f"{us / 1_000_000:.2f} s"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"LogicLab {BUILD_ID} — Arduino Protocol Analyzer")
        self.geometry("1280x780")
        self.minsize(960, 620)
        self.configure(bg=BG)
        self.model = AnalyzerModel()
        self.events = queue.Queue()
        self.worker = SerialWorker(self.model, self.events)
        self.capturing = False
        self.demo = False
        self.demo_us = 0
        self.demo_last_bits = None
        self.drawn_version = -1
        self.last_board_message = "nenhuma resposta recebida"
        self.debug_var = tk.StringVar(value="Diagnóstico iniciando…")
        self.port_lookup: dict[str, str] = {}
        self._style()
        if sys.platform == "darwin" and tk.TkVersion < 8.6:
            self._build_classic()
        else:
            self._build()
        self.bind("<Command-d>" if sys.platform == "darwin" else "<Control-d>", lambda _e: self.toggle_demo())
        self.refresh_ports()
        self.after(30, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.log_debug(f"Aplicativo iniciado | Python={sys.version.split()[0]} | Tk={tk.TkVersion}")

    def _style(self):
        s = ttk.Style(self)
        # O Python fornecido pelo macOS ainda usa Tk 8.5. Nesse runtime o tema
        # clam pode deixar frames e botões transparentes/invisíveis; o tema
        # Aqua nativo mantém toda a interface renderizável.
        if sys.platform == "darwin" and tk.TkVersion < 8.6:
            s.theme_use("aqua")
        else:
            s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_2, bordercolor=GRID)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"), foreground=TEXT)
        s.configure("TButton", background=PANEL_2, foreground=TEXT, padding=(12, 8), borderwidth=0)
        s.map("TButton", background=[("active", GRID)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#071713", font=("TkDefaultFont", 10, "bold"))
        s.map("Accent.TButton", background=[("active", "#55e3cf")])
        s.configure("TCombobox", padding=6)
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=27, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, relief="flat")

    def _build(self):
        header = ttk.Frame(self, padding=(20, 14))
        header.pack(fill="x")
        ttk.Label(header, text="LogicLab", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  ANALISADOR LÓGICO", style="Muted.TLabel").pack(side="left")
        self.status_dot = tk.Label(header, text="●  DESCONECTADO", bg=BG, fg=MUTED, font=("TkDefaultFont", 9, "bold"))
        self.status_dot.pack(side="right")

        bar = ttk.Frame(self, style="Panel.TFrame", padding=12)
        bar.pack(fill="x", padx=20)
        port_group = ttk.Frame(bar, style="Panel.TFrame")
        port_group.pack(side="left", padx=(0, 8))
        ttk.Label(port_group, text="PORTA SERIAL", background=PANEL, foreground=MUTED).pack(anchor="w")
        self.port_var = tk.StringVar()
        # Campo editável: além da lista automática, permite informar manualmente
        # /dev/cu.usbmodem..., COM3 etc. em sistemas onde a descoberta falhar.
        self.port_box = ttk.Combobox(port_group, textvariable=self.port_var, width=38, state="normal")
        self.port_box.pack(anchor="w", pady=(3, 0))
        self.port_box.bind("<Button-1>", lambda _event: self.refresh_ports(quiet=True))
        self.port_box.bind("<<ComboboxSelected>>", self.port_selected)
        self.baud_var = tk.StringVar(value="115200")
        ttk.Combobox(bar, textvariable=self.baud_var, values=("115200", "230400", "500000", "1000000"), width=10, state="readonly").pack(side="left", padx=(0, 8), pady=(17, 0))
        ttk.Button(bar, text="↻", command=self.refresh_ports).pack(side="left", padx=(0, 8), pady=(17, 0))
        self.connect_btn = ttk.Button(bar, text="Conectar", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=(0, 18), pady=(17, 0))
        self.capture_btn = ttk.Button(bar, text="▶  Capturar", style="Accent.TButton", command=self.toggle_capture)
        self.capture_btn.pack(side="left", padx=(0, 8), pady=(17, 0))
        ttk.Button(bar, text="Limpar", command=self.clear).pack(side="left", padx=(0, 8), pady=(17, 0))
        ttk.Button(bar, text="Importar CSV", command=self.import_csv).pack(side="left", padx=(0, 8), pady=(17, 0))
        ttk.Button(bar, text="Exportar CSV", command=self.export_csv).pack(side="left", pady=(17, 0))
        ttk.Button(bar, text="Debug", command=self.show_diagnostics).pack(side="left", padx=(8, 0), pady=(17, 0))
        ttk.Button(bar, text="Modo demo", command=self.toggle_demo).pack(side="right", pady=(17, 0))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=20, pady=14)
        sidebar = ttk.Frame(body, style="Panel.TFrame", padding=14, width=210)
        main = ttk.Frame(body)
        body.add(sidebar, weight=0)
        body.add(main, weight=1)
        ttk.Label(sidebar, text="CANAIS", background=PANEL, foreground=MUTED).pack(anchor="w", pady=(0, 8))
        self.channel_vars = []
        for i, name in enumerate(["FIO A  D2 / INT4", "FIO B  D3 / INT5"]):
            var = tk.BooleanVar(value=True)
            self.channel_vars.append(var)
            cb = tk.Checkbutton(sidebar, text=name, variable=var, command=self.channels_changed, bg=PANEL, fg=CHANNEL_COLORS[i], selectcolor=PANEL_2, activebackground=PANEL, activeforeground=CHANNEL_COLORS[i], anchor="w", relief="flat", pady=6)
            cb.pack(fill="x")
        ttk.Separator(sidebar).pack(fill="x", pady=14)
        ttk.Label(sidebar, text="DECODIFICADOR", background=PANEL, foreground=MUTED).pack(anchor="w", pady=(0, 7))
        self.protocol_var = tk.StringVar(value="Proprietário 2 fios")
        ttk.Label(sidebar, text="Proprietário 2 fios", background=PANEL,
                  foreground=TEXT).pack(anchor="w")
        ttk.Label(sidebar, text="Unidade temporal e quadros\ndetectados automaticamente",
                  background=PANEL, foreground=MUTED).pack(anchor="w", pady=(6, 0))
        ttk.Button(sidebar, text="Analisar protocolo", command=self.decode).pack(fill="x", pady=(12, 0))
        ttk.Separator(sidebar).pack(fill="x", pady=14)
        ttk.Label(sidebar, text="DIAGNÓSTICO", background=PANEL, foreground=MUTED).pack(anchor="w")
        ttk.Label(sidebar, textvariable=self.debug_var, background=PANEL, foreground=TEXT,
                  justify="left", wraplength=190).pack(anchor="w", fill="x", pady=(6, 0))

        self.wave = WaveCanvas(main, self.model, height=390)
        self.wave.pack(fill="both", expand=True)
        lower = ttk.Frame(main, style="Panel.TFrame")
        lower.pack(fill="x", pady=(10, 0))
        columns = ("tempo", "protocolo", "evento", "valor")
        self.table = ttk.Treeview(lower, columns=columns, show="headings", height=7)
        for key, title, width in (("tempo", "TEMPO", 130), ("protocolo", "PROTOCOLO", 100), ("evento", "EVENTO", 180), ("valor", "VALOR", 420)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="w")
        self.table.pack(fill="x", padx=8, pady=8)
        self.footer_var = tk.StringVar(value="Pronto • clique duas vezes no gráfico para posicionar os cursores A/B")
        ttk.Label(self, textvariable=self.footer_var, style="Muted.TLabel").pack(fill="x", padx=22, pady=(0, 10))

    def _build_classic(self):
        """Interface compatível com o Tk 8.5 incluído no Python do macOS."""
        # No Tk 8.5/Aqua atual, fundos escuros podem ser ignorados enquanto o
        # foreground claro é mantido, produzindo texto branco sobre branco.
        # Esta variante usa contraste claro explícito em todos os widgets.
        legacy_bg = "#eef2f7"
        legacy_panel = "#ffffff"
        legacy_field = "#f7f9fc"
        legacy_text = "#172033"
        legacy_muted = "#5f6d84"
        legacy_grid = "#bcc6d6"
        self.configure(bg=legacy_bg)
        label = dict(bg=legacy_bg, fg=legacy_text)
        button = dict(bg=legacy_field, fg=legacy_text, activebackground="#dfe6f1",
                      activeforeground=legacy_text, relief="solid", borderwidth=1,
                      padx=10, pady=7)

        header = tk.Frame(self, bg=legacy_bg)
        header.pack(fill="x", padx=20, pady=(14, 8))
        tk.Label(header, text=f"LogicLab {BUILD_ID}", font=("TkDefaultFont", 18, "bold"), **label).pack(side="left")
        tk.Label(header, text="  PROTOCOLO PROPRIETÁRIO 2 FIOS", bg=legacy_bg, fg=legacy_muted).pack(side="left")
        self.status_dot = tk.Label(header, text="●  DESCONECTADO", bg=legacy_bg, fg=legacy_muted,
                                   font=("TkDefaultFont", 9, "bold"))
        self.status_dot.pack(side="right")

        bar = tk.Frame(self, bg=legacy_panel, padx=12, pady=10,
                       highlightthickness=1, highlightbackground=legacy_grid)
        bar.pack(fill="x", padx=20)
        self.port_var = tk.StringVar()
        self.port_box = tk.Entry(bar, textvariable=self.port_var, width=34,
                                 bg=legacy_field, fg=legacy_text, insertbackground=legacy_text,
                                 relief="solid", borderwidth=1)
        self.port_box.pack(side="left", ipady=7, padx=(0, 8))
        self.port_box.bind("<Button-1>", lambda _event: self.refresh_ports(quiet=True))
        self.baud_var = tk.StringVar(value="115200")
        tk.Entry(bar, textvariable=self.baud_var, width=9, bg=legacy_field, fg=legacy_text,
                 insertbackground=legacy_text, relief="solid", borderwidth=1).pack(side="left", ipady=7, padx=(0, 8))
        tk.Button(bar, text="↻", command=self.refresh_ports, **button).pack(side="left", padx=(0, 8))
        self.connect_btn = tk.Button(bar, text="Conectar", command=self.toggle_connection, **button)
        self.connect_btn.pack(side="left", padx=(0, 12))
        self.capture_btn = tk.Button(bar, text="▶ Capturar", command=self.toggle_capture,
                                     bg=ACCENT, fg="#071713", activebackground="#55e3cf",
                                     relief="flat", padx=10, pady=7)
        self.capture_btn.pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Limpar", command=self.clear, **button).pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Importar CSV", command=self.import_csv, **button).pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Exportar CSV", command=self.export_csv, **button).pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Debug", command=self.show_diagnostics, **button).pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Modo demo", command=self.toggle_demo, **button).pack(side="right")

        # PanedWindow do Tk 8.5/Aqua pode deixar o segundo painel sem pintura
        # embora ele tenha geometria. Um Frame simples é estável nesse runtime.
        body = tk.Frame(self, bg=legacy_grid, borderwidth=0)
        body.pack(fill="both", expand=True, padx=20, pady=14)
        sidebar = tk.Frame(body, bg=legacy_panel, width=215, padx=14, pady=14)
        # O fundo pertence ao painel, não apenas ao widget de desenho. Assim
        # ele permanece preto mesmo se o Tk/Aqua falhar ao compor uma imagem.
        main = tk.Frame(body, bg="#000000", highlightthickness=2,
                        highlightbackground="#ffffff")
        sidebar.pack(side="left", fill="y", padx=(0, 4))
        sidebar.pack_propagate(False)
        main.pack(side="left", fill="both", expand=True)
        tk.Label(sidebar, text="CANAIS", bg=legacy_panel, fg=legacy_muted).pack(anchor="w", pady=(0, 8))
        self.channel_vars = []
        names = ["FIO A  D2 / INT4", "FIO B  D3 / INT5"]
        for i, name in enumerate(names):
            var = tk.BooleanVar(value=True)
            self.channel_vars.append(var)
            tk.Checkbutton(sidebar, text=name, variable=var, command=self.channels_changed,
                           bg=legacy_panel, fg=CHANNEL_COLORS[i], selectcolor=legacy_field,
                           activebackground=legacy_panel, activeforeground=CHANNEL_COLORS[i],
                           anchor="w", relief="flat", pady=5).pack(fill="x")
        tk.Frame(sidebar, bg=legacy_grid, height=1).pack(fill="x", pady=12)
        tk.Label(sidebar, text="DECODIFICADOR", bg=legacy_panel, fg=legacy_muted).pack(anchor="w")
        self.protocol_var = tk.StringVar(value="Proprietário 2 fios")
        tk.Label(sidebar, text="Proprietário 2 fios", bg=legacy_panel, fg=legacy_text).pack(anchor="w", pady=(6, 0))
        tk.Label(sidebar, text="Unidade temporal e quadros\ndetectados automaticamente",
                 bg=legacy_panel, fg=legacy_muted, justify="left").pack(anchor="w", pady=(6, 8))
        tk.Button(sidebar, text="Analisar protocolo", command=self.decode, **button).pack(fill="x")
        tk.Frame(sidebar, bg=legacy_grid, height=1).pack(fill="x", pady=12)
        tk.Label(sidebar, text="DIAGNÓSTICO", bg=legacy_panel, fg=legacy_muted).pack(anchor="w")
        tk.Label(sidebar, textvariable=self.debug_var, bg=legacy_panel, fg=legacy_text,
                 justify="left", wraplength=185).pack(anchor="w", fill="x", pady=(6, 0))

        self.wave = ClassicWaveView(main, self.model, height=18)
        self.wave.pack(fill="both", expand=True)
        self.table = ClassicEventTable(main, height=7, bg=legacy_panel, fg=legacy_text,
                                       selectbackground=BLUE, relief="flat",
                                       font=("Menlo", 11))
        self.table.pack(fill="x", pady=(10, 0))
        self.footer_var = tk.StringVar(value="Pronto • ⌘D ativa o modo demonstração")
        tk.Label(self, textvariable=self.footer_var, bg=legacy_bg, fg=legacy_muted,
                 anchor="w").pack(fill="x", padx=22, pady=(0, 10))

    def refresh_ports(self, quiet=False):
        detected = list(list_ports.comports()) if list_ports else []
        self.port_lookup = {}
        displays = []
        for p in detected:
            description = p.description if p.description and p.description != "n/a" else "Interface serial"
            display = f"{description}  —  {p.device}"
            displays.append(display)
            self.port_lookup[display] = p.device
        ports = [p.device for p in detected]
        # Fallback útil no macOS quando o programa foi iniciado com um Python
        # sem pyserial: ainda mostra dispositivos para seleção manual.
        for device in sorted(glob.glob("/dev/cu.*")):
            if device not in ports:
                ports.append(device)
                display = f"Interface serial  —  {device}"
                displays.append(display)
                self.port_lookup[display] = device
        current_device = self.selected_port()
        if isinstance(self.port_box, ttk.Combobox):
            self.port_box["values"] = displays
        if ports and current_device not in ports:
            selected = next(
                (d for d in displays if "arduino mega" in d.lower()),
                next((d for d in displays if "usbmodem" in d or "usbserial" in d), displays[0]),
            )
            self.port_var.set(selected)
        elif not ports:
            self.port_var.set("")
        if serial is None:
            self.footer_var.set(
                "pyserial não carregado • abra pelo iniciar.command para conectar"
            )
        elif not quiet:
            self.footer_var.set(
                f"{len(ports)} porta(s) encontrada(s) • campo também aceita digitação manual"
            )

    def selected_port(self) -> str:
        value = self.port_var.get().strip()
        return self.port_lookup.get(value, value)

    def port_selected(self, _event=None):
        device = self.selected_port()
        self.footer_var.set(f"Interface selecionada: {device} • clique em Conectar para testar")

    def toggle_connection(self):
        if self.worker.port:
            self.worker.disconnect()
            self._set_connected(False)
            return
        port = self.selected_port()
        if not port:
            messagebox.showinfo("Sem porta", "Conecte o Arduino ou use o Modo demo.")
            return
        try:
            self.worker.connect(port, int(self.baud_var.get()))
            self.connect_btn.configure(text="Desconectar")
            self.status_dot.configure(text="●  VERIFICANDO ARDUINO…", fg=ORANGE)
            self.footer_var.set("Porta aberta • aguardando reset e resposta PING/PONG…")
            self.worker.send("PING")
        except Exception as exc:
            messagebox.showerror("Falha ao conectar", str(exc))

    def _set_connected(self, value: bool):
        self.connect_btn.configure(text="Desconectar" if value else "Conectar")
        self.status_dot.configure(text="●  CONECTADO" if value else "●  DESCONECTADO", fg=ACCENT if value else MUTED)

    def toggle_capture(self):
        starting = not self.capturing
        if starting and not self.worker.port and not self.demo:
            messagebox.showinfo(
                "Sem fonte de dados",
                "Conecte o Arduino ou ative o Modo demo antes de capturar.",
            )
            return
        if starting:
            # O firmware reinicia seus timestamps em zero a cada captura. Dados
            # antigos não podem permanecer no modelo, que pressupõe ordem temporal.
            self.clear()
        self.capturing = starting
        self.capture_btn.configure(text="■  Parar" if self.capturing else "▶  Capturar")
        if self.worker.port:
            self.worker.send("START" if self.capturing else "STOP")
        self.footer_var.set("Capturando sinais…" if self.capturing else f"Captura pausada • {len(self.model.snapshot()):,} amostras")

    def toggle_demo(self):
        self.demo = not self.demo
        if self.demo:
            self.clear()
            self.demo_us = 0
            self.demo_last_bits = None
            self.capturing = True
            self.capture_btn.configure(text="■  Parar")
            self.status_dot.configure(text="●  DEMONSTRAÇÃO", fg=ORANGE)
            self.title(f"LogicLab {BUILD_ID} — Modo demonstração ativo")
            self.footer_var.set("Modo demonstração ativo")
        else:
            self.capturing = False
            self.capture_btn.configure(text="▶  Capturar")
            self.status_dot.configure(text="●  DESCONECTADO", fg=MUTED)
            self.title(f"LogicLab {BUILD_ID} — Arduino Protocol Analyzer")
            self.footer_var.set("Modo demonstração desativado")

    def _demo_sample(self):
        self.demo_us += 10
        t = self.demo_us
        # Duas vias independentes, ociosas em alto, semelhantes à captura da foto.
        cycle = t % 8_000
        request = "1011001010010110"
        response = "110010100111000101101001"
        a = int(request[min(len(request) - 1, max(0, (cycle - 600) // 80))]) if 600 <= cycle < 600 + len(request) * 80 else 1
        b = int(response[min(len(response) - 1, max(0, (cycle - 3_000) // 80))]) if 3_000 <= cycle < 3_000 + len(response) * 80 else 1
        bits = a | (b << 1)
        if bits != self.demo_last_bits:
            self.model.add(t, bits)
            self.demo_last_bits = bits

    def _tick(self):
        if self.demo and self.capturing:
            for _ in range(35):
                self._demo_sample()
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                if str(value) != "END":
                    self.last_board_message = str(value)
                self.footer_var.set(str(value))
            elif kind == "ready":
                self._set_connected(True)
                self.status_dot.configure(text="●  ARDUINO ATIVO", fg=ACCENT)
                self.footer_var.set(f"Comunicação confirmada: {value}")
            elif kind == "error":
                messagebox.showerror("Erro serial", str(value))
            elif kind == "capture_end":
                self.capturing = False
                self.capture_btn.configure(text="▶  Capturar")
                self.footer_var.set(f"Captura recebida • {len(self.model.snapshot()):,} transições • clique em Analisar protocolo")
            elif kind == "disconnected" and not self.demo and not self.worker.port:
                self._set_connected(False)
        # Redesenhar milhares de pontos continuamente, mesmo sem dados novos,
        # saturava o loop gráfico do Tk no macOS e deixava a janela branca.
        version = self.model.current_version()
        if version != self.drawn_version:
            self.wave.redraw()
            self.drawn_version = version
        self.debug_var.set(self.debug_status())
        self.after(75, self._tick)

    def debug_status(self) -> str:
        data = self.model.snapshot()
        edges_a = edges_b = 0
        for previous, current in zip(data, data[1:]):
            changed = previous.bits ^ current.bits
            edges_a += bool(changed & 0x01)
            edges_b += bool(changed & 0x02)
        port_state = "ABERTA" if self.worker.port else "FECHADA"
        capture_state = "ATIVA — dados ficam no Mega até Parar" if self.capturing else "PARADA"
        if len(data) > 1:
            span = format_time(data[-1].us - data[0].us)
            levels = f"A={(data[-1].bits & 1)}  B={((data[-1].bits >> 1) & 1)}"
        else:
            span, levels = "—", "A=?  B=?"
        return (
            f"Serial: {port_state}\n"
            f"Captura: {capture_state}\n"
            f"Amostras recebidas: {len(data)}\n"
            f"Transições A/B: {edges_a}/{edges_b}\n"
            f"Janela: {span}  {levels}\n"
            f"Placa: {self.last_board_message}"
        )

    def channels_changed(self):
        self.wave.visible = [v.get() for v in self.channel_vars]
        self.wave.redraw()

    def clear(self):
        self.model.clear()
        self.table.delete(*self.table.get_children())
        self.wave.cursor_a = self.wave.cursor_b = None
        self.footer_var.set("Captura limpa")

    def export_csv(self):
        data = self.model.snapshot()
        if not data:
            messagebox.showinfo("Sem dados", "Faça uma captura antes de exportar.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(["tempo_us", "fio_A_D2", "fio_B_D3"])
            for s in data:
                writer.writerow([s.us, s.bits & 1, (s.bits >> 1) & 1])
        self.footer_var.set(f"Exportado: {path}")

    def import_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("Todos", "*")])
        if not path:
            return
        try:
            samples, discarded = read_capture_csv(path)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("CSV inválido", str(exc))
            return
        self.model.replace(samples)
        self.table.delete(*self.table.get_children())
        self.wave.cursor_a = self.wave.cursor_b = None
        self.wave.redraw()
        self.update_idletasks()
        self.drawn_version = self.model.current_version()
        note = f" • {discarded:,} amostras antigas ignoradas" if discarded else ""
        self.footer_var.set(f"CSV carregado • {len(samples):,} amostras{note}")
        edges_a, edges_b = count_edges(samples)
        duration = samples[-1].us - samples[0].us
        details = (
            f"ARQUIVO VALIDADO\n\n"
            f"Arquivo: {path}\n"
            f"Amostras carregadas: {len(samples):,}\n"
            f"Amostras antigas ignoradas: {discarded:,}\n"
            f"Intervalo: {samples[0].us} a {samples[-1].us} µs\n"
            f"Duração: {format_time(duration)}\n"
            f"Transições A/B: {edges_a:,}/{edges_b:,}\n"
            f"Painel: {self.wave.winfo_width()}×{self.wave.winfo_height()} px\n"
            f"Renderizador: {type(self.wave).__name__}\n"
            f"Python/Tk: {sys.version.split()[0]} / {tk.TkVersion}\n"
            f"Build: {BUILD_ID}\n\n"
            f"A rotina de desenho foi executada. Consulte logiclab_debug.log "
            f"se o painel continuar sem traços."
        )
        self.log_debug(details.replace("\n", " | "))
        browser_note = ""
        if tk.TkVersion < 8.6:
            try:
                write_web_viewer(WEB_VIEWER, samples, Path(path).name)
                webbrowser.open(WEB_VIEWER.resolve().as_uri())
                browser_note = (
                    "\n\nTk 8.5 detectado: o gráfico foi aberto automaticamente "
                    "no navegador porque o painel Aqua não suporta a pintura corretamente."
                )
                self.log_debug(f"Visualizador web aberto: {WEB_VIEWER}")
            except OSError as exc:
                browser_note = f"\n\nFalha ao criar o visualizador web: {exc}"
                self.log_debug(browser_note.strip())
        messagebox.showinfo("Validação da importação", details + browser_note)

    def log_debug(self, message: str):
        try:
            with DEBUG_LOG.open("a", encoding="utf-8") as fp:
                fp.write(f"{datetime.now().isoformat(timespec='seconds')} | {message}\n")
        except OSError:
            pass

    def show_diagnostics(self):
        data = self.model.snapshot()
        edges_a, edges_b = count_edges(data)
        repetitions = [result for channel in (0, 1)
                       if (result := analyze_repetition(data, channel))]
        repetition_text = ("Repetição: " + " | ".join(
            f"Fio {'AB'[r.channel]}: {r.transitions_per_cycle} bordas, "
            f"{format_time(r.period_us)}, {r.frequency_hz:.2f} Hz, "
            f"{r.similarity:.1%}, jitter {r.jitter_us:.2f} µs"
            for r in repetitions
        ) if repetitions else "Repetição: não confirmada")
        first = data[0].us if data else "—"
        last = data[-1].us if data else "—"
        details = (
            f"LogicLab {BUILD_ID}\n"
            f"Python {sys.version.split()[0]} | Tk {tk.TkVersion}\n"
            f"Renderizador: {type(self.wave).__name__}\n"
            f"Widget visível: {bool(self.wave.winfo_ismapped())}\n"
            f"Painel: {self.wave.winfo_width()}×{self.wave.winfo_height()} px\n"
            f"Amostras: {len(data):,}\n"
            f"Tempo inicial/final: {first}/{last} µs\n"
            f"Transições A/B: {edges_a:,}/{edges_b:,}\n"
            f"Versão do modelo: {self.model.current_version()}\n"
            f"Versão desenhada: {self.drawn_version}\n"
            f"{repetition_text}\n"
            f"Log: {DEBUG_LOG}"
        )
        self.log_debug(details.replace("\n", " | "))
        messagebox.showinfo("Diagnóstico LogicLab", details)

    def decode(self):
        self.table.delete(*self.table.get_children())
        data = self.model.snapshot()
        proto = "Proprietário 2 fios"
        if len(data) < 3:
            messagebox.showinfo("Sem dados", "Capture sinais antes de decodificar.")
            return
        events = decode_proprietary(data)
        for stamp, event, value in events[:1000]:
            self.table.insert("", "end", values=(format_time(stamp), proto, event, value))
        self.footer_var.set(f"{len(events)} eventos {proto} encontrados")
        repetitions = [result for channel in (0, 1)
                       if (result := analyze_repetition(data, channel))]
        if repetitions:
            summary = "\n\n".join(
                f"FIO {'AB'[result.channel]} — REPETIÇÃO CONFIRMADA\n"
                f"Transições por ciclo: {result.transitions_per_cycle}\n"
                f"Período: {format_time(result.period_us)}\n"
                f"Frequência: {result.frequency_hz:.2f} Hz\n"
                f"Similaridade: {result.similarity:.1%}\n"
                f"Jitter: {result.jitter_us:.2f} µs\n"
                f"Ciclos observados: ≈ {result.estimated_cycles:.1f}"
                for result in repetitions
            )
        else:
            summary = "Nenhuma repetição confiável foi detectada."
        self.log_debug(f"Análise de repetição | {summary.replace(chr(10), ' | ')}")
        messagebox.showinfo("Análise do trem de pulsos", summary)

    def _close(self):
        self.worker.disconnect()
        self.destroy()


def level_at(data: list[Sample], stamp: float, channel: int) -> int:
    lo, hi = 0, len(data) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if data[mid].us <= stamp:
            lo = mid
        else:
            hi = mid - 1
    return (data[lo].bits >> channel) & 1


def count_edges(data: list[Sample]) -> tuple[int, int]:
    edges_a = edges_b = 0
    for previous, current in zip(data, data[1:]):
        changed = previous.bits ^ current.bits
        edges_a += bool(changed & 0x01)
        edges_b += bool(changed & 0x02)
    return edges_a, edges_b


def analyze_repetition(data: list[Sample], channel: int) -> RepetitionResult | None:
    """Detecta repetição comparando a sequência de intervalos entre bordas.

    A tolerância acompanha a unidade temporal do sinal. Exige pelo menos três
    ciclos e 80% de coincidência para evitar classificar ruído como protocolo.
    """
    edges = [sample.us for previous, sample in zip(data, data[1:])
             if ((previous.bits ^ sample.bits) >> channel) & 1]
    if len(edges) < 16:
        return None
    intervals = [end - begin for begin, end in zip(edges, edges[1:])][-5_000:]
    edges = edges[-(len(intervals) + 1):]
    base = statistics.median(intervals)
    tolerance = max(2.0, base * 0.08)
    maximum_lag = min(400, len(intervals) // 3)
    candidates = []
    for lag in range(4, maximum_lag + 1):
        compared = len(intervals) - lag
        matches = sum(abs(intervals[index] - intervals[index - lag]) <= tolerance
                      for index in range(lag, len(intervals)))
        score = matches / compared
        if score >= 0.80:
            candidates.append((score, lag))
    if not candidates:
        return None
    best_score = max(score for score, _lag in candidates)
    # Prefere o menor período fundamental com qualidade próxima do melhor;
    # isso evita retornar 2× ou 3× o ciclo verdadeiro.
    similarity, lag = min(
        ((score, lag) for score, lag in candidates if score >= best_score - 0.02),
        key=lambda item: item[1],
    )
    periods = [edges[index] - edges[index - lag]
               for index in range(lag, len(edges))]
    period_us = statistics.median(periods)
    jitter = statistics.pstdev(periods) if len(periods) > 1 else 0.0
    return RepetitionResult(
        channel=channel,
        transitions_per_cycle=lag,
        period_us=period_us,
        frequency_hz=1_000_000 / period_us if period_us else 0.0,
        similarity=similarity,
        jitter_us=jitter,
        estimated_cycles=len(intervals) / lag,
    )


def read_capture_csv(path: str) -> tuple[list[Sample], int]:
    """Lê CSV e repara timestamps isolados truncados pela transmissão serial."""
    rows: list[Sample] = []
    with open(path, newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        required = {"tempo_us", "fio_A_D2", "fio_B_D3"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Colunas esperadas: tempo_us, fio_A_D2, fio_B_D3")
        for line, row in enumerate(reader, start=2):
            try:
                us = int(row["tempo_us"])
                a = int(row["fio_A_D2"])
                b = int(row["fio_B_D3"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Valor inválido na linha {line}") from exc
            if us < 0 or a not in (0, 1) or b not in (0, 1):
                raise ValueError(f"Valor fora do intervalo na linha {line}")
            rows.append(Sample(us, a | (b << 1)))
    if not rows:
        raise ValueError("O CSV não contém amostras")
    repaired = 0
    for index in range(1, len(rows) - 1):
        if rows[index].us < rows[index - 1].us < rows[index + 1].us:
            rows[index].us = (rows[index - 1].us + rows[index + 1].us) // 2
            repaired += 1
    # Reinícios reais continuam separando capturas; preserve o trecho mais
    # recente, mas timestamps isolados já não eliminam milhares de amostras.
    start = 0
    for index in range(1, len(rows)):
        if rows[index].us < rows[index - 1].us:
            start = index
    return rows[start:], start


def write_web_viewer(path: Path, samples: list[Sample], source_name: str):
    # Uma janela curta impede que centenas de bordas ocupem o mesmo pixel e
    # pareçam uma área preenchida. O traçado permanece uma linha em degraus.
    samples = samples[-80:]
    payload = [[sample.us, sample.bits & 1, (sample.bits >> 1) & 1]
               for sample in samples]
    title = html.escape(source_name)
    document = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>LogicLab — Gráfico</title>
<style>
html,body{margin:0;background:#050505;color:#fff;font:14px -apple-system,BlinkMacSystemFont,Arial}
header{padding:16px 22px;border-bottom:1px solid #555;display:flex;gap:24px;align-items:center}
.a{color:#00e5ff}.b{color:#ff3df2}.muted{color:#bbb}canvas{display:block;width:100vw;height:calc(100vh - 74px)}
</style></head><body><header><strong>LogicLab WEB_VIEW_BUILD</strong>
<span>WEB_VIEW_TITLE</span><span class="a">● Fio A</span><span class="b">● Fio B</span>
<span class="muted" id="info"></span></header><canvas id="scope"></canvas>
<script>const data=WEB_VIEW_DATA;const c=document.getElementById('scope'),x=c.getContext('2d');
function draw(){const d=devicePixelRatio||1,w=innerWidth,h=innerHeight-74;c.width=w*d;c.height=h*d;x.scale(d,d);
x.fillStyle='#000';x.fillRect(0,0,w,h);const L=72,R=20,T=28,B=42,pw=w-L-R,ph=h-T-B;
x.strokeStyle='#fff';x.lineWidth=1;x.font='12px Menlo';x.fillStyle='#fff';
for(let i=0;i<=10;i++){const xx=L+pw*i/10;x.globalAlpha=i?0.32:1;x.beginPath();x.moveTo(xx,T);x.lineTo(xx,T+ph);x.stroke();
const us=data[0][0]+(data[data.length-1][0]-data[0][0])*i/10;x.globalAlpha=1;x.fillText((us/1000).toFixed(1)+' ms',xx-24,T+ph+24)}
for(let r=0;r<2;r++){const y0=T+r*ph/2,hi=y0+ph*.12,lo=y0+ph*.38;x.strokeStyle='#fff';x.globalAlpha=.55;
[hi,lo].forEach(y=>{x.beginPath();x.moveTo(L,y);x.lineTo(L+pw,y);x.stroke()});x.globalAlpha=1;x.fillText(r?'B':'A',22,y0+ph*.25);x.fillText('1',50,hi+4);x.fillText('0',50,lo+4);
x.strokeStyle=r?'#ff3df2':'#00e5ff';x.lineWidth=3;x.beginPath();let prev=data[0][r+1],py=prev?hi:lo;x.moveTo(L,py);
for(let i=1;i<data.length;i++){const xx=L+(data[i][0]-data[0][0])/(data[data.length-1][0]-data[0][0]||1)*pw,n=data[i][r+1],ny=n?hi:lo;x.lineTo(xx,py);if(n!==prev)x.lineTo(xx,ny);prev=n;py=ny}x.lineTo(L+pw,py);x.stroke()}}
document.getElementById('info').textContent=data.length.toLocaleString('pt-BR')+' amostras • '+((data.at(-1)[0]-data[0][0])/1000).toFixed(2)+' ms';}
addEventListener('resize',draw);draw();</script></body></html>"""
    document = document.replace("WEB_VIEW_BUILD", BUILD_ID)
    document = document.replace("WEB_VIEW_TITLE", title)
    document = document.replace("WEB_VIEW_DATA", json.dumps(payload, separators=(",", ":")))
    path.write_text(document, encoding="utf-8")


def line_runs(data: list[Sample], channel: int):
    """Retorna trechos de nível constante de uma das duas vias."""
    runs = []
    start = data[0].us
    level = (data[0].bits >> channel) & 1
    for sample in data[1:]:
        current = (sample.bits >> channel) & 1
        if current != level:
            if sample.us > start:
                runs.append((start, sample.us, level))
            start, level = sample.us, current
    if data[-1].us > start:
        runs.append((start, data[-1].us, level))
    return runs


def estimate_time_unit(durations: list[float]) -> float:
    """Estima a menor unidade que explica os pulsos como múltiplos inteiros."""
    clean = sorted(d for d in durations if d >= 1)
    if not clean:
        return 1.0
    useful = clean[:max(3, int(len(clean) * 0.7))]
    candidates = {d / divisor for d in useful for divisor in range(1, 7) if d / divisor >= 1}
    best = min(candidates, key=lambda unit: (
        statistics.median(abs(d / unit - round(d / unit)) for d in useful)
        + 0.002 * statistics.median(max(1, round(d / unit)) for d in useful)
    ))
    return round(best, 2)


def bits_to_hex(bits: str, reverse_each_byte: bool = False) -> str:
    values = []
    for index in range(0, len(bits) - 7, 8):
        chunk = bits[index:index + 8]
        if reverse_each_byte:
            chunk = chunk[::-1]
        values.append(f"{int(chunk, 2):02X}")
    return " ".join(values) if values else "—"


def decode_hex_candidates(data: list[Sample], channel: int) -> dict:
    """Converte pulsos em candidatos binários/HEX/DEC sem assumir protocolo.

    Trechos ociosos longos separam quadros. Para cada quadro são oferecidos
    todos os oito alinhamentos possíveis e as duas ordens de bits por byte.
    """
    runs = line_runs(data, channel)
    if len(runs) < 2:
        return {"channel": channel, "unit_us": 0, "gap_us": 0, "frames": []}
    durations = [end - start for start, end, _level in runs]
    unit = estimate_time_unit(durations)
    gap = max(20 * unit, 400.0)
    raw_frames: list[list[tuple[float, float, int]]] = []
    current = []
    for run in runs:
        start, end, level = run
        if end - start >= gap:
            if current:
                raw_frames.append(current)
                current = []
        else:
            current.append(run)
    if current:
        raw_frames.append(current)

    frames = []
    for number, frame in enumerate(raw_frames[:64], 1):
        bits = "".join(
            str(level) * max(1, min(256, round((end - start) / unit)))
            for start, end, level in frame
        )
        alignments = []
        for offset in range(min(8, len(bits))):
            usable = bits[offset:offset + ((len(bits) - offset) // 8) * 8]
            if not usable:
                continue
            msb = [int(usable[index:index + 8], 2)
                   for index in range(0, len(usable), 8)]
            lsb = [int(usable[index:index + 8][::-1], 2)
                   for index in range(0, len(usable), 8)]
            alignments.append({
                "offset": offset,
                "hex_msb": " ".join(f"{value:02X}" for value in msb),
                "dec_msb": " ".join(str(value) for value in msb),
                "ascii_msb": "".join(chr(value) if 32 <= value <= 126 else "." for value in msb),
                "hex_lsb": " ".join(f"{value:02X}" for value in lsb),
                "dec_lsb": " ".join(str(value) for value in lsb),
                "ascii_lsb": "".join(chr(value) if 32 <= value <= 126 else "." for value in lsb),
            })
        frames.append({
            "number": number,
            "start_us": frame[0][0],
            "bits": bits,
            "alignments": alignments,
        })
    signatures: dict[str, dict] = {}
    for frame in frames:
        signature = frame["bits"]
        if signature not in signatures:
            index = len(signatures)
            signatures[signature] = {"label": chr(65 + index) if index < 26 else f"P{index + 1}", "count": 0}
        signatures[signature]["count"] += 1
    for frame in frames:
        group = signatures[frame["bits"]]
        frame["pattern"] = group["label"]
        frame["repeat_count"] = group["count"]
    return {
        "channel": channel,
        "unit_us": unit,
        "gap_us": gap,
        "unique_patterns": len(signatures),
        "frames": frames,
    }


def _crc16_modbus(payload: list[int]) -> int:
    crc = 0xFFFF
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _crc16_kermit(payload: list[int]) -> int:
    crc = 0x0000
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def decode_uart(data: list[Sample], channel: int, baud: int = 57600) -> dict:
    """Decodifica UART 8N1 por amostragem no centro de cada bit."""
    if len(data) < 2:
        return {"channel": channel, "baud": baud, "frames": [], "bytes": 0}
    bit_us = 1_000_000 / baud
    decoded = []
    busy_until = -1.0
    falling_edges = 0
    framing_errors = 0
    for previous, current in zip(data, data[1:]):
        start = current.us
        if start < busy_until:
            continue
        previous_level = (previous.bits >> channel) & 1
        current_level = (current.bits >> channel) & 1
        if previous_level != 1 or current_level != 0:
            continue
        falling_edges += 1
        if level_at(data, start + 0.5 * bit_us, channel) != 0:
            framing_errors += 1
            continue
        value = sum(
            level_at(data, start + (1.5 + index) * bit_us, channel) << index
            for index in range(8)
        )
        if level_at(data, start + 9.5 * bit_us, channel) != 1:
            framing_errors += 1
            continue
        decoded.append((start, value))
        busy_until = start + 9.8 * bit_us

    groups = []
    current_group = []
    # Dentro de um quadro os bytes podem ter pequenos intervalos adicionais;
    # uma pausa de 25 bits ainda fica bem abaixo dos gaps de requisição/resposta.
    gap_us = 25 * bit_us
    for item in decoded:
        if current_group and item[0] - current_group[-1][0] > gap_us:
            groups.append(current_group)
            current_group = []
        current_group.append(item)
    if current_group:
        groups.append(current_group)

    signatures = {}
    frames = []
    for number, group in enumerate(groups, 1):
        values = [value for _stamp, value in group]
        signature = bytes(values).hex()
        if signature not in signatures:
            signatures[signature] = {
                "label": chr(65 + len(signatures)) if len(signatures) < 26 else f"P{len(signatures) + 1}",
                "count": 0,
            }
        signatures[signature]["count"] += 1
        numeric = []
        for offset in range(max(0, len(values) - 1)):
            little = values[offset] | (values[offset + 1] << 8)
            big = (values[offset] << 8) | values[offset + 1]
            signed_little = little - 65536 if little >= 32768 else little
            numeric.append({
                "offset": offset,
                "u16le": little,
                "s16le": signed_little,
                "u16be": big,
            })
        checksum = {
            "sum8_zero": bool(values) and sum(values) & 0xFF == 0,
            "sum8_ff": len(values) > 1 and sum(values) & 0xFF == 0xFF,
            "xor_zero": bool(values) and (
                functools.reduce(lambda left, right: left ^ right, values, 0) == 0
            ),
            "crc16_modbus": len(values) > 2 and _crc16_modbus(values[:-2]) in (
                values[-2] | (values[-1] << 8),
                (values[-2] << 8) | values[-1],
            ),
            "crc16_kermit": len(values) > 2 and _crc16_kermit(values[:-2]) in (
                values[-2] | (values[-1] << 8),
                (values[-2] << 8) | values[-1],
            ),
        }
        frames.append({
            "number": number,
            "start_us": group[0][0],
            "end_us": group[-1][0] + 10 * bit_us,
            "values": values,
            "hex": " ".join(f"{value:02X}" for value in values),
            "decimal": " ".join(str(value) for value in values),
            "ascii": "".join(chr(value) if 32 <= value <= 126 else "." for value in values),
            "numeric": numeric,
            "checksum": checksum,
            "signature": signature,
        })
    for frame in frames:
        group = signatures[frame["signature"]]
        frame["pattern"] = group["label"]
        frame["repeat_count"] = group["count"]
        del frame["signature"]
    confidence = len(decoded) / max(1, len(decoded) + framing_errors)
    return {
        "channel": channel,
        "wire": "AB"[channel],
        "baud": baud,
        "bit_us": bit_us,
        "format": "8N1",
        "confidence": confidence,
        "falling_edges": falling_edges,
        "framing_errors": framing_errors,
        "bytes": len(decoded),
        "unique_patterns": len(signatures),
        "frames": frames,
    }


def decode_uart_duplex(data: list[Sample], baud: int = 57600) -> dict:
    channels = [decode_uart(data, channel, baud) for channel in (0, 1)]
    events = sorted(
        [
            {**frame, "wire": channel["wire"]}
            for channel in channels
            for frame in channel["frames"]
        ],
        key=lambda frame: frame["start_us"],
    )
    return {"baud": baud, "format": "8N1", "channels": channels, "events": events}


def decode_proprietary(data: list[Sample]):
    """Análise neutra: duas vias assíncronas, sem presumir formato conhecido."""
    out = []
    for channel, wire in ((0, "A"), (1, "B")):
        repetition = analyze_repetition(data, channel)
        if repetition:
            out.append((
                data[0].us,
                f"REPETIÇÃO FIO {wire}",
                f"{repetition.transitions_per_cycle} transições/ciclo • "
                f"período {format_time(repetition.period_us)} • "
                f"{repetition.frequency_hz:.2f} Hz • "
                f"similaridade {repetition.similarity:.1%} • "
                f"jitter {repetition.jitter_us:.2f} µs • "
                f"≈ {repetition.estimated_cycles:.1f} ciclos",
            ))
        runs = line_runs(data, channel)
        active_durations = [end - start for start, end, _level in runs]
        if len(active_durations) < 2:
            continue
        unit = estimate_time_unit(active_durations)
        gap = max(8 * unit, 400.0)
        out.append((data[0].us, f"FIO {wire} / BASE", f"unidade ≈ {format_time(unit)} • separação ≥ {format_time(gap)}"))

        frames, current = [], []
        for run in runs:
            start, end, level = run
            if level == 1 and end - start >= gap:
                if current:
                    frames.append(current)
                    current = []
            else:
                current.append(run)
        if current:
            frames.append(current)

        for number, frame in enumerate(frames, 1):
            bits = "".join(
                str(level) * max(1, min(64, round((end - start) / unit)))
                for start, end, level in frame
            )
            timings = " ".join(f"{level}:{format_time(end-start)}" for start, end, level in frame[:12])
            if len(frame) > 12:
                timings += " …"
            stamp = frame[0][0]
            out.append((stamp, f"QUADRO {wire} #{number}", f"bits={bits[:128]}{'…' if len(bits)>128 else ''}"))
            out.append((stamp, "HEX CANDIDATO", f"MSB: {bits_to_hex(bits)}  |  LSB: {bits_to_hex(bits, True)}"))
            out.append((stamp, "PULSOS", timings))
    return sorted(out, key=lambda event: (event[0], event[1]))


if __name__ == "__main__":
    App().mainloop()
