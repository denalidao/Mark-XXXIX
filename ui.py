from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c

class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # NVIDIA
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass

        # AMD (Linux)
        if _OS == "Linux":
            try:
                r = subprocess.run(
                    ["rocm-smi", "--showuse", "--csv"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                return float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                pass
            except Exception:
                pass

            # Intel GPU (Linux)
            try:
                r = subprocess.run(
                    ["intel_gpu_top", "-J", "-s", "500"],
                    capture_output=True, text=True, timeout=1
                )
                if r.returncode == 0 and "Render/3D" in r.stdout:
                    import re
                    m = re.search(r'"busy":\s*([\d.]+)', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        # macOS — powermetrics (GPU Engine)
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["sudo", "-n", "powermetrics", "-n", "1", "-i", "500",
                     "--samplers", "gpu_power"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0 and "GPU" in r.stdout:
                    import re
                    m = re.search(r'GPU\s+Active:\s+([\d.]+)%', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            candidates = ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                          "cpu-thermal", "zenpower", "it8688"]
            for name in candidates:
                if name in temps:
                    entries = temps[name]
                    if entries:
                        return entries[0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["osx-cpu-temp"], capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    import re
                    m = re.search(r"([\d.]+)", r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        if _OS == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    capture_output=True, text=True, timeout=3
                )
                if r.returncode == 0 and r.stdout.strip():
                    raw = float(r.stdout.strip().split("\n")[0])
                    return (raw / 10.0) - 273.15
            except Exception:
                pass

        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # corner brackets
        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw // 2, cx + fw // 2
        ht, hb = cy - fw // 2, cy + fw // 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # face
        if self._face_px:
            fsz    = int(fw * 0.62 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            oc    = (200, 0, 50) if self.muted else (0, 60, 110)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, "J.A.R.V.I.S")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # status text
        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            elif self.speaking:
                hgt = random.randint(3, 20)
                cl  = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl  = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class SetupOverlay(QWidget):
    """api_key (empty for local), os key, use_ollama flag."""

    done = pyqtSignal(str, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 18, 30, 18)
        layout.setSpacing(6)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _le_style = f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout()
        os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)

        sep0 = QFrame()
        sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setStyleSheet(f"color: {C.BORDER};")
        layout.addWidget(sep0)
        layout.addSpacing(2)

        layout.addWidget(_lbl("LOCAL OLLAMA (no Gemini API key)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        layout.addWidget(_lbl(
            "Uses the same env overrides as Aletheon: ALETHEON_LLM_ASSIST_OLLAMA_URL / _MODEL",
            7, color=C.TEXT_MED, align=Qt.AlignmentFlag.AlignLeft,
        ))
        layout.addWidget(_lbl("Ollama host", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._ollama_url = QLineEdit("http://127.0.0.1:11434")
        self._ollama_url.setFont(QFont("Courier New", 9))
        self._ollama_url.setFixedHeight(28)
        self._ollama_url.setStyleSheet(_le_style)
        layout.addWidget(self._ollama_url)
        layout.addWidget(_lbl("Model tag", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._ollama_model = QLineEdit("qwen2.5:7b")
        self._ollama_model.setFont(QFont("Courier New", 9))
        self._ollama_model.setFixedHeight(28)
        self._ollama_model.setStyleSheet(_le_style)
        layout.addWidget(self._ollama_model)

        local_btn = QPushButton("▸  CONNECT LOCAL OLLAMA")
        local_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        local_btn.setFixedHeight(34)
        local_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        local_btn.setStyleSheet(f"""
            QPushButton {{
                background: #001a0d; color: {C.GREEN};
                border: 1px solid {C.GREEN}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #002414; border: 1px solid {C.TEXT};
            }}
        """)
        local_btn.clicked.connect(self._submit_local)
        layout.addWidget(local_btn)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet(f"color: {C.BORDER};")
        layout.addWidget(sep1)
        layout.addSpacing(2)

        layout.addWidget(_lbl("GEMINI (cloud — real-time voice + native audio)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(_le_style)
        layout.addWidget(self._key_input)

        gem_btn = QPushButton("▸  INITIALISE WITH GEMINI")
        gem_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        gem_btn.setFixedHeight(34)
        gem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gem_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        gem_btn.clicked.connect(self._submit_gemini)
        layout.addWidget(gem_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit_local(self) -> None:
        self.done.emit("", self._sel_os, True)

    def _submit_gemini(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os, False)


class PttHoldButton(QPushButton):
    """Hold left mouse: record mono int16 @ 16 kHz; release to emit PCM for Whisper."""

    pcm_ready = pyqtSignal(bytes)

    _SR = 16000

    def __init__(self) -> None:
        super().__init__("Hold to speak (PTT)")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(32)
        self.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.setStyleSheet(f"""
            QPushButton {{
                background: #001218; color: {C.PRI};
                border: 2px solid {C.BORDER_B}; border-radius: 4px;
            }}
            QPushButton:pressed {{ background: #002028; border: 2px solid {C.PRI}; }}
        """)
        self.setToolTip(
            "Hold, speak, release — audio is transcribed locally (faster-whisper) "
            "then sent to Ollama."
        )
        self._recording = False
        self._chunks: list[bytes] = []
        self._stream = None

    def _callback(self, indata, _frames, _time, _status) -> None:
        if self._recording:
            self._chunks.append(indata.copy().tobytes())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                import sounddevice as sd
            except ImportError:
                print("[PTT] sounddevice not installed")
                super().mousePressEvent(event)
                return
            self._chunks = []
            self._recording = True
            try:
                self._stream = sd.InputStream(
                    samplerate=self._SR,
                    channels=1,
                    dtype="int16",
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as e:
                self._recording = False
                self._stream = None
                print(f"[PTT] start failed: {e}")
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._recording:
            self._recording = False
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            pcm = b"".join(self._chunks)
            self._chunks = []
            if len(pcm) >= 3200:
                self.pcm_ready.emit(pcm)
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    _log_sig   = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _ollama_models_sig = pyqtSignal(list, str)
    ollama_attached = pyqtSignal(object)

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S — MARK XXXIX")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command  = None
        self._muted           = False
        self._current_file: str | None = None
        self._ollama_backend = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self.hud, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._ollama_models_sig.connect(self._on_ollama_models_loaded)
        self.ollama_attached.connect(self._on_ollama_attached)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()
        else:
            self._try_show_ollama_model_selector()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay and self._overlay.isVisible():
            ow, oh = 480, 560
            cw = self.centralWidget()
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge("MARK XXXIX", C.PRI_DIM))
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        title = QLabel("J.A.R.V.I.S")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(title)
        sub = QLabel("Just A Rather Very Intelligent System")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFont(QFont("Courier New", 7))
        sub.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(sub)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Courier New", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addStretch()

        for txt, col in [
            ("AI CORE\nACTIVE",     C.GREEN),
            ("SEC\nCLEARED",        C.PRI),
            ("PROTOCOL\nXXXVIII",   C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {col}; background: {C.PANEL2};"
                f"border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 4px;"
            )
            lay.addWidget(lbl)

        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        self._ollama_model_wrap = QWidget()
        self._ollama_model_wrap.setVisible(False)
        om_lay = QVBoxLayout(self._ollama_model_wrap)
        om_lay.setContentsMargins(0, 0, 0, 0)
        om_lay.setSpacing(4)
        om_lay.addWidget(_sec("OLLAMA MODEL"))
        self._ollama_model_hint = QLabel("")
        self._ollama_model_hint.setFont(QFont("Courier New", 6))
        self._ollama_model_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._ollama_model_hint.setWordWrap(True)
        om_lay.addWidget(self._ollama_model_hint)
        om_row = QHBoxLayout()
        om_row.setSpacing(4)
        self._ollama_model_combo = QComboBox()
        self._ollama_model_combo.setFont(QFont("Courier New", 8))
        self._ollama_model_combo.setMinimumHeight(28)
        self._ollama_model_combo.setStyleSheet(f"""
            QComboBox {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
            }}
            QComboBox:hover {{ border: 1px solid {C.BORDER_B}; }}
        """)
        self._ollama_model_combo.currentTextChanged.connect(self._on_ollama_model_user_changed)
        ref_btn = QPushButton("↻")
        ref_btn.setFixedSize(28, 28)
        ref_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ref_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ref_btn.setToolTip("Refresh model list from Ollama")
        ref_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ border: 1px solid {C.PRI}; }}
        """)
        ref_btn.clicked.connect(self._refresh_ollama_models_async)
        om_row.addWidget(self._ollama_model_combo, stretch=1)
        om_row.addWidget(ref_btn)
        om_lay.addLayout(om_row)
        lay.addWidget(self._ollama_model_wrap)

        self._tts_backend_wrap = QWidget()
        self._tts_backend_wrap.setVisible(False)
        tts_lay = QVBoxLayout(self._tts_backend_wrap)
        tts_lay.setContentsMargins(0, 0, 0, 0)
        tts_lay.setSpacing(4)
        tts_lay.addWidget(_sec("VOICE OUTPUT (LOCAL)"))
        self._tts_hint = QLabel(
            "Ollama answers stay local; pick how spoken replies are played."
        )
        self._tts_hint.setFont(QFont("Courier New", 6))
        self._tts_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._tts_hint.setWordWrap(True)
        tts_lay.addWidget(self._tts_hint)
        self._tts_backend_combo = QComboBox()
        self._tts_backend_combo.setFont(QFont("Courier New", 8))
        self._tts_backend_combo.setMinimumHeight(28)
        self._tts_backend_combo.setStyleSheet(f"""
            QComboBox {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
            }}
            QComboBox:hover {{ border: 1px solid {C.BORDER_B}; }}
        """)
        self._tts_backend_combo.addItem("Windows (SAPI / pyttsx3)", "pyttsx3")
        self._tts_backend_combo.addItem("Gemini neural (uses API key)", "gemini")
        self._tts_backend_combo.addItem("Coqui local (TechGym TTS repo)", "coqui")
        self._tts_backend_combo.currentIndexChanged.connect(self._on_tts_backend_changed)
        tts_lay.addWidget(self._tts_backend_combo)
        self._coqui_cfg_wrap = QWidget()
        cq_lay = QVBoxLayout(self._coqui_cfg_wrap)
        cq_lay.setContentsMargins(0, 0, 0, 0)
        cq_lay.setSpacing(2)
        rl = QLabel("Coqui clone root (folder with TTS/)")
        rl.setFont(QFont("Courier New", 7))
        rl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        cq_lay.addWidget(rl)
        self._coqui_repo_edit = QLineEdit()
        self._coqui_repo_edit.setPlaceholderText(
            r"C:\path\to\TechGym-TTS-scout"
        )
        self._coqui_repo_edit.setFont(QFont("Courier New", 7))
        self._coqui_repo_edit.setMinimumHeight(24)
        self._coqui_repo_edit.setStyleSheet(
            f"background: #000d12; color: {C.TEXT}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 2px 6px;"
        )
        self._coqui_repo_edit.editingFinished.connect(self._persist_tts_settings)
        cq_lay.addWidget(self._coqui_repo_edit)
        ml = QLabel("Coqui model (registry — pick a preset or type your own)")
        ml.setFont(QFont("Courier New", 7))
        ml.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        cq_lay.addWidget(ml)
        self._coqui_model_combo = QComboBox()
        self._coqui_model_combo.setEditable(True)
        self._coqui_model_combo.setFont(QFont("Courier New", 7))
        self._coqui_model_combo.setMinimumHeight(28)
        self._coqui_model_combo.setStyleSheet(self._tts_backend_combo.styleSheet())
        self._coqui_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        try:
            from mark_llm_settings import list_coqui_tts_model_presets

            for mid in list_coqui_tts_model_presets():
                self._coqui_model_combo.addItem(mid)
        except Exception:
            self._coqui_model_combo.addItem("tts_models/en/ljspeech/tacotron2-DDC")
        _cm_le = self._coqui_model_combo.lineEdit()
        if _cm_le is not None:
            _cm_le.setPlaceholderText("tts_models/en/…")
            _cm_le.editingFinished.connect(self._persist_tts_settings)
        self._coqui_model_combo.activated.connect(self._on_coqui_model_combo_activated)
        cq_lay.addWidget(self._coqui_model_combo)
        tts_lay.addWidget(self._coqui_cfg_wrap)
        self._coqui_cfg_wrap.setVisible(False)

        self._coqui_gemini_failover_chk = QCheckBox(
            "If Coqui fails, try Gemini TTS (uses gemini_api_key)"
        )
        self._coqui_gemini_failover_chk.setFont(QFont("Courier New", 7))
        self._coqui_gemini_failover_chk.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        self._coqui_gemini_failover_chk.toggled.connect(self._on_coqui_failover_toggled)
        tts_lay.addWidget(self._coqui_gemini_failover_chk)

        self._tts_gemini_voice_lbl = QLabel("Gemini voice")
        self._tts_gemini_voice_lbl.setFont(QFont("Courier New", 7))
        self._tts_gemini_voice_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        tts_lay.addWidget(self._tts_gemini_voice_lbl)
        self._gemini_voice_combo = QComboBox()
        self._gemini_voice_combo.setFont(QFont("Courier New", 8))
        self._gemini_voice_combo.setMinimumHeight(28)
        self._gemini_voice_combo.setStyleSheet(self._tts_backend_combo.styleSheet())
        self._gemini_voice_combo.currentTextChanged.connect(self._on_gemini_voice_changed)
        tts_lay.addWidget(self._gemini_voice_combo)
        self._tts_gemini_hint = QLabel("")
        self._tts_gemini_hint.setFont(QFont("Courier New", 6))
        self._tts_gemini_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._tts_gemini_hint.setWordWrap(True)
        tts_lay.addWidget(self._tts_gemini_hint)
        lay.addWidget(self._tts_backend_wrap)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep3)

        self._ptt_wrap = QWidget()
        self._ptt_wrap.setVisible(False)
        ptt_lay = QVBoxLayout(self._ptt_wrap)
        ptt_lay.setContentsMargins(0, 0, 0, 0)
        ptt_lay.setSpacing(4)
        ptt_lay.addWidget(_sec("LOCAL VOICE (PTT)"))
        self._ptt_btn = PttHoldButton()
        self._ptt_btn.pcm_ready.connect(self._on_ptt_pcm)
        ptt_lay.addWidget(self._ptt_btn)
        lay.addWidget(self._ptt_wrap)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border: 1px solid {C.BORDER_B};
            }}
        """)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        return w

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("FatihMakes Industries  ·  MARK XXXIX  ·  CLASSIFIED"))
        lay.addStretch()
        lay.addWidget(_fl("© FATIHMAKES", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell JARVIS what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            self.on_text_command(msg)

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            # Call from Qt thread; JarvisOllama uses run_coroutine_threadsafe (JarvisLive uses it too).
            self.on_text_command(txt)

    def _on_ollama_attached(self, jarvis) -> None:
        self._ollama_backend = jarvis
        self._ptt_wrap.setVisible(True)

    def _on_ptt_pcm(self, pcm: bytes) -> None:
        if self._ollama_backend is None:
            return
        self._log.append_log("SYS: PTT — sending audio to local STT…")
        self._ollama_backend.feed_pcm(pcm)

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not d.get("os_system"):
            return False
        env_local = os.environ.get("MARK_LLM_PROVIDER", "").strip().lower() == "ollama"
        if env_local:
            return True
        prov = (d.get("llm_provider") or "").strip().lower()
        if prov == "ollama":
            return bool((d.get("ollama_model") or "").strip())
        if (d.get("ollama_model") or "").strip() and not (d.get("gemini_api_key") or "").strip():
            return True
        return bool((d.get("gemini_api_key") or "").strip())

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 480, 560
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str, use_ollama: bool):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data: dict = {}
        if API_FILE.exists():
            try:
                data = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if use_ollama:
            o_url = "http://127.0.0.1:11434"
            o_model = "qwen2.5:7b"
            if self._overlay:
                o_url = (self._overlay._ollama_url.text().strip() or o_url)
                o_model = (self._overlay._ollama_model.text().strip() or o_model)
            data["llm_provider"] = "ollama"
            data["ollama_url"] = o_url
            data["ollama_model"] = o_model
            data["os_system"] = os_name
            if (key or "").strip():
                data["gemini_api_key"] = (key or "").strip()
            log_extra = f"LOCAL_OLLAMA model={o_model}"
        else:
            data["llm_provider"] = "gemini"
            data["gemini_api_key"] = (key or "").strip()
            data["os_system"] = os_name
            log_extra = "GEMINI_CLOUD"
        API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(
            f"SYS: Initialised. OS={os_name.upper()}. Mode={log_extra}. JARVIS online."
        )
        self._try_show_ollama_model_selector()

    def _try_show_ollama_model_selector(self) -> None:
        try:
            from mark_llm_settings import is_ollama_mode, ollama_model_env_locked
        except ImportError:
            return
        if not hasattr(self, "_ollama_model_wrap"):
            return
        if not is_ollama_mode():
            self._ollama_model_wrap.setVisible(False)
            # Voice / TTS backend is independent of chat LLM — show panel in Gemini mode too.
            self._try_show_ollama_tts_controls()
            return
        self._ollama_model_wrap.setVisible(True)
        locked = ollama_model_env_locked()
        self._ollama_model_combo.setEnabled(not locked)
        self._ollama_model_hint.setText(
            "Model from Ollama /api/tags (Aletheon-style). "
            + (
                "MARK_OLLAMA_MODEL or ALETHEON_LLM_ASSIST_OLLAMA_MODEL is set — env overrides this list."
                if locked
                else "Selection is saved to config/api_keys.json."
            )
        )
        self._refresh_ollama_models_async()
        self._try_show_ollama_tts_controls()

    def _refresh_ollama_models_async(self) -> None:
        def work() -> None:
            try:
                from mark_llm_settings import get_ollama_model, list_ollama_models

                names = list_ollama_models()
                cur = get_ollama_model()
            except Exception:
                names, cur = [], "qwen2.5:7b"
            self._ollama_models_sig.emit(names, cur)

        threading.Thread(target=work, daemon=True).start()

    def _on_ollama_models_loaded(self, names: list, current: str) -> None:
        if not hasattr(self, "_ollama_model_combo"):
            return
        self._ollama_model_combo.blockSignals(True)
        self._ollama_model_combo.clear()
        ordered = list(dict.fromkeys([*(names or []), current]))
        for n in ordered:
            if n and isinstance(n, str):
                self._ollama_model_combo.addItem(n)
        idx = self._ollama_model_combo.findText(current)
        if idx >= 0:
            self._ollama_model_combo.setCurrentIndex(idx)
        elif self._ollama_model_combo.count() > 0:
            self._ollama_model_combo.setCurrentIndex(0)
        self._ollama_model_combo.blockSignals(False)

    def _on_ollama_model_user_changed(self, model_name: str) -> None:
        m = (model_name or "").strip()
        if not m:
            return
        self._persist_ollama_model(m)

    def _persist_ollama_model(self, model: str) -> None:
        if not API_FILE.exists():
            return
        try:
            data = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        data["ollama_model"] = model
        try:
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except OSError:
            return
        self._log.append_log(f"SYS: Ollama model → {model}")

    def _current_tts_backend_id(self) -> str:
        d = self._tts_backend_combo.currentData()
        if isinstance(d, str) and d.strip():
            return d.strip()
        return "pyttsx3"

    def _set_tts_backend_combo_by_id(self, backend_id: str) -> None:
        want = (backend_id or "pyttsx3").strip().lower()
        for i in range(self._tts_backend_combo.count()):
            d = self._tts_backend_combo.itemData(i)
            if isinstance(d, str) and d.strip().lower() == want:
                self._tts_backend_combo.setCurrentIndex(i)
                return
        self._tts_backend_combo.setCurrentIndex(0)

    def _try_show_ollama_tts_controls(self) -> None:
        try:
            from mark_llm_settings import (
                get_coqui_failover_to_gemini,
                get_coqui_model_name,
                get_coqui_tts_repo_path,
                get_gemini_api_key,
                get_gemini_live_voice_name,
                get_local_tts_backend,
                is_ollama_mode,
                list_gemini_tts_voice_names,
            )
        except ImportError:
            return
        if not hasattr(self, "_tts_backend_wrap"):
            return
        self._tts_backend_wrap.setVisible(True)
        self._tts_backend_combo.blockSignals(True)
        self._gemini_voice_combo.blockSignals(True)
        self._coqui_gemini_failover_chk.blockSignals(True)
        self._coqui_repo_edit.blockSignals(True)
        self._coqui_model_combo.blockSignals(True)
        backend = get_local_tts_backend()
        self._set_tts_backend_combo_by_id(backend)
        self._coqui_repo_edit.setText(get_coqui_tts_repo_path() or "")
        self._coqui_model_combo.setCurrentText(get_coqui_model_name() or "")
        self._coqui_gemini_failover_chk.setChecked(get_coqui_failover_to_gemini())
        self._gemini_voice_combo.clear()
        for name in list_gemini_tts_voice_names():
            self._gemini_voice_combo.addItem(name)
        cur = get_gemini_live_voice_name()
        vi = self._gemini_voice_combo.findText(cur)
        if vi >= 0:
            self._gemini_voice_combo.setCurrentIndex(vi)
        elif self._gemini_voice_combo.count() > 0:
            self._gemini_voice_combo.setCurrentIndex(0)
        self._tts_backend_combo.blockSignals(False)
        self._gemini_voice_combo.blockSignals(False)
        self._coqui_gemini_failover_chk.blockSignals(False)
        self._coqui_repo_edit.blockSignals(False)
        self._coqui_model_combo.blockSignals(False)
        self._update_tts_widgets_enabled(get_gemini_api_key())
        env_tb = os.environ.get("MARK_TTS_BACKEND", "").strip()
        base = (
            "How **spoken replies** are played (SAPI, Gemini TTS, Coqui). "
            "Applies in **Ollama and Gemini** chat modes."
        )
        if not is_ollama_mode():
            base += " Ollama **chat model** selector is hidden while cloud Gemini drives the LLM."
        if env_tb:
            base += (
                f" Note: MARK_TTS_BACKEND={env_tb!r} in the environment overrides "
                "``tts_backend`` in api_keys.json — spoken output may not match this panel."
            )
        self._tts_hint.setText(base)

    def _update_tts_widgets_enabled(self, gemini_key: str | None = None) -> None:
        if not hasattr(self, "_gemini_voice_combo"):
            return
        try:
            from mark_llm_settings import get_gemini_api_key
        except ImportError:
            return
        key = gemini_key if gemini_key is not None else get_gemini_api_key()
        bid = self._current_tts_backend_id()
        use_gem = bid == "gemini"
        self._gemini_voice_combo.setEnabled(use_gem)
        self._tts_gemini_voice_lbl.setEnabled(use_gem)
        self._gemini_voice_combo.setVisible(use_gem)
        self._tts_gemini_voice_lbl.setVisible(use_gem)
        if hasattr(self, "_coqui_gemini_failover_chk"):
            self._coqui_gemini_failover_chk.setEnabled(bid == "coqui")
        if hasattr(self, "_coqui_cfg_wrap"):
            self._coqui_cfg_wrap.setVisible(bid == "coqui")
            if hasattr(self, "_coqui_repo_edit"):
                self._coqui_repo_edit.setEnabled(bid == "coqui")
            if hasattr(self, "_coqui_model_combo"):
                self._coqui_model_combo.setEnabled(bid == "coqui")
        if use_gem:
            self._tts_gemini_hint.setText(
                (
                    "Uses ``gemini_api_key`` for speech only; chat stays on Ollama. "
                    "If you still hear Windows SAPI, check the terminal: Gemini TTS may "
                    "return 429 (free-tier quota) and the app falls back to Zira."
                )
                if key
                else "Set ``gemini_api_key`` in api_keys.json (same as full Gemini mode)."
            )
        elif bid == "coqui":
            self._tts_gemini_hint.setText(
                "Coqui uses **registry model ids** (dropdown presets), not Gemini voice names. "
                "Clone root + model save to api_keys.json. Same venv: ``pip install -e`` your TTS repo. "
                "Tortoise disabled. Gemini-after-Coqui: checkbox."
            )
        else:
            self._tts_gemini_hint.setText(
                "Optional: ``tts_voice_substring`` in api_keys.json for SAPI voice."
                + (
                    " You have ``gemini_api_key`` — select **Gemini neural** above to use the Gemini voice list."
                    if key
                    else ""
                )
            )

    def _on_tts_backend_changed(self, _idx: int) -> None:
        if self._current_tts_backend_id() == "coqui" and hasattr(self, "_coqui_repo_edit"):
            try:
                from mark_llm_settings import get_coqui_model_name, get_coqui_tts_repo_path

                self._coqui_repo_edit.blockSignals(True)
                self._coqui_model_combo.blockSignals(True)
                self._coqui_repo_edit.setText(get_coqui_tts_repo_path() or "")
                self._coqui_model_combo.setCurrentText(get_coqui_model_name() or "")
                self._coqui_repo_edit.blockSignals(False)
                self._coqui_model_combo.blockSignals(False)
            except Exception:
                pass
        self._update_tts_widgets_enabled()
        self._persist_tts_settings()

    def _on_coqui_failover_toggled(self, _checked: bool) -> None:
        self._persist_tts_settings()

    def _on_coqui_model_combo_activated(self, _index: int) -> None:
        self._persist_tts_settings()

    def _on_gemini_voice_changed(self, _text: str) -> None:
        # Picking a Gemini prebuilt voice implies Gemini TTS when a key exists;
        # otherwise the voice list has no effect and SAPI (e.g. Zira) still speaks.
        try:
            from mark_llm_settings import get_gemini_api_key
        except ImportError:
            self._persist_tts_settings()
            return
        if get_gemini_api_key() and self._current_tts_backend_id() == "pyttsx3":
            self._tts_backend_combo.blockSignals(True)
            self._set_tts_backend_combo_by_id("gemini")
            self._tts_backend_combo.blockSignals(False)
            self._update_tts_widgets_enabled(get_gemini_api_key())
        self._persist_tts_settings()

    def _persist_tts_settings(self) -> None:
        if not API_FILE.exists():
            return
        try:
            from mark_llm_settings import coqui_engine_disk_signature
        except ImportError:
            coqui_engine_disk_signature = None  # type: ignore[misc, assignment]
        try:
            data = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        prev_coqui_sig = (
            coqui_engine_disk_signature(dict(data))
            if coqui_engine_disk_signature is not None
            else ""
        )
        data["tts_backend"] = self._current_tts_backend_id()
        v = (self._gemini_voice_combo.currentText() or "").strip()
        if v:
            data["gemini_live_voice"] = v
        if hasattr(self, "_coqui_gemini_failover_chk"):
            data["coqui_failover_to_gemini"] = bool(
                self._coqui_gemini_failover_chk.isChecked()
            )
        # Coqui fields: only overwrite when non-empty. Empty QLineEdits must NOT wipe
        # api_keys.json (e.g. failover toggle or Gemini voice save runs before edits hydrate).
        if self._current_tts_backend_id() == "coqui" and hasattr(self, "_coqui_repo_edit"):
            rp = (self._coqui_repo_edit.text() or "").strip()
            mn = (self._coqui_model_combo.currentText() or "").strip()
            if rp:
                data["coqui_tts_repo_path"] = rp
            if mn:
                data["coqui_model_name"] = mn
        new_coqui_sig = (
            coqui_engine_disk_signature(data)
            if coqui_engine_disk_signature is not None
            else ""
        )
        try:
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except OSError:
            return
        tb = (data.get("tts_backend") or "").strip().lower()
        if tb in ("coqui", "techgym") and new_coqui_sig != prev_coqui_sig:
            try:
                from mark_coqui_tts import reset_coqui_engine_cache

                reset_coqui_engine_cache()
            except Exception:
                pass
        if data["tts_backend"] == "gemini":
            extra = f", Gemini voice={v}"
        elif data["tts_backend"] == "coqui":
            fo = bool(data.get("coqui_failover_to_gemini"))
            extra = (
                f" (Coqui; Gemini failover={'on' if fo else 'off'}; then SAPI if needed)"
            )
        else:
            extra = ""
        self._log.append_log(f"SYS: Voice output → {data['tts_backend']}{extra}")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
