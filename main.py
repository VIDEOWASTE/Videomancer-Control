"""
main.py  —  Videomancer Control
Full companion app for the LZX Industries Videomancer.

Tabs:
  Programs   – browse & load FPGA programs
  Parameters – live 12-channel control (sliders + toggles)
  Presets    – factory & user preset management
  Snapshots  – save/restore full device state as local JSON files

Run:
    pip3 install PyQt6 pyserial
    python3 main.py
"""

APP_VERSION = "2.1"
GITHUB_REPO = "VIDEOWASTE/VIDEOMANCER-Control-Interface"

import sys
import json
import time
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QListWidget, QListWidgetItem,
    QLineEdit, QStatusBar, QFrame, QSplitter, QTextEdit, QGroupBox,
    QTabWidget, QSlider, QCheckBox, QScrollArea, QGridLayout,
    QSizePolicy, QMessageBox, QInputDialog, QDialog, QDialogButtonBox,
)
import math
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, pyqtSignal, QThread, QRectF, QPointF
from PyQt6.QtGui import (QColor, QTextCharFormat, QTextCursor, QFont,
                          QPainter, QPen, QLinearGradient, QPainterPath)

try:
    import serial.tools.list_ports as list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

from serial_worker import SerialWorker


# ── Update checker ────────────────────────────────────────────────────

class _UpdateChecker(QThread):
    """Background thread that checks GitHub releases for a newer version."""
    from PyQt6.QtCore import pyqtSignal
    update_available = pyqtSignal(str, str)  # (new_version, download_url)

    def run(self):
        try:
            from urllib.request import urlopen, Request
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = Request(url, headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": "VideomancerControl"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            tag = data.get("tag_name", "")
            remote_ver = tag.lstrip("v")
            local_ver = APP_VERSION.lstrip("v")
            if self._is_newer(remote_ver, local_ver):
                html_url = data.get("html_url", "")
                self.update_available.emit(remote_ver, html_url)
        except Exception:
            pass  # Network errors are non-fatal

    @staticmethod
    def _is_newer(remote: str, local: str) -> bool:
        """Compare version strings like '1.0.1' > '1.0.0-rc1'."""
        def parse(v):
            # Split on '-' to separate pre-release
            parts = v.split("-", 1)
            nums = [int(x) for x in re.findall(r"\d+", parts[0])]
            # Pre-release (rc, beta, alpha) sorts before release
            is_pre = len(parts) > 1
            return (nums, 0 if is_pre else 1, parts[1] if is_pre else "")
        try:
            r = parse(remote)
            l = parse(local)
            return r > l
        except Exception:
            return False


# ── Multi-device: global registry of ports claimed by open windows ────
_claimed_ports: set = set()
_app_windows: list = []      # all open VideomancerApp windows


# ── Design tokens — dark purple/violet, high contrast ─────────────────
BG       = "#0f0d1f"   # near-black with cool violet undertone
SURFACE  = "#1e1a38"   # dark purple-tinted surface
SURFACE2 = "#2d2650"   # slightly lighter panel
BORDER   = "#9955cc"   # vivid purple border
ACCENT   = "#ffffff"   # white
ACCENT2  = "#c040c0"   # bright magenta-purple (character hands/hat brim)
DIM      = "#7733bb"   # mid vivid purple
TEXT     = "#ffffff"   # near-white
TEXT_DIM = "#d8cfee"   # muted purple-grey
ERROR    = "#ff4466"
WARN     = "#e0d0ff"

PARAM_RANGE = 1023   # 0–1023, centre = 512

# ── Video Monitor (floating capture card preview) ─────────────────────

class _VideoDisplay(QWidget):
    """Minimal video display — caches scaled pixmap for fast repaint."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self._text = ""
        self._cached_pixmap = None
        self._cached_size = None
        self._draw_x = 0
        self._draw_y = 0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def setFrame(self, image, _ref=None):
        self._image = image
        self._text = ""
        # Scale once here, paint just blits the cached pixmap
        ww, wh = self.width(), self.height()
        iw, ih = image.width(), image.height()
        scale = min(ww / iw, wh / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        self._draw_x = (ww - dw) // 2
        self._draw_y = (wh - dh) // 2
        from PyQt6.QtGui import QPixmap
        self._cached_pixmap = QPixmap.fromImage(
            image.scaled(dw, dh, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.FastTransformation))
        self.update()

    def setText(self, text):
        self._text = text
        self._image = None
        self._cached_pixmap = None
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self._cached_pixmap:
            p.drawPixmap(self._draw_x, self._draw_y, self._cached_pixmap)
        elif self._text:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()


class _CaptureThread(QThread):
    """Background thread using OpenCV for capture — native C++ decode, no pipe.
    Falls back to FFmpeg pipe for DeckLink devices."""

    def __init__(self, device_index, device_name="", fmt="cv2",
                 decklink_format=None):
        super().__init__()
        self._index = device_index
        self._name = device_name
        self._fmt = fmt
        self._decklink_format = decklink_format  # e.g. "Hp30" for 1080p30
        self._running = False
        self.error_msg = ""
        self._latest_qimg = None
        self._latest_id = 0
        self._cap_w = 0
        self._cap_h = 0

    def run(self):
        self._running = True
        self.error_msg = ""

        if self._fmt == "decklink":
            self._run_ffmpeg()
            return

        # OpenCV capture — all decoding happens in C++
        try:
            import cv2
        except ImportError:
            self.error_msg = "OpenCV not installed"
            return

        cap = cv2.VideoCapture(self._index)
        if not cap.isOpened():
            self.error_msg = f"Cannot open device {self._index}"
            return

        # Minimize internal buffering — always grab the latest frame
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FPS, 30)
        self._cap_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._cap_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        from PyQt6.QtGui import QImage
        import numpy as np
        while self._running:
            ret, frame = cap.read()
            if not ret:
                continue
            h, w, ch = frame.shape
            self._cap_w = w
            self._cap_h = h
            # contiguous copy via numpy (faster than QImage.copy)
            rgb = np.ascontiguousarray(frame)
            img = QImage(rgb.data, w, h, w * ch,
                         QImage.Format.Format_BGR888)
            img._numpy_ref = rgb  # prevent GC
            self._latest_qimg = img
            self._latest_id += 1

        cap.release()

    def _run_ffmpeg(self):
        """FFmpeg pipe capture for DeckLink (Blackmagic) devices."""
        import subprocess

        # Build ffmpeg command — format_code is required for DeckLink
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        if self._decklink_format:
            cmd += ["-format_code", self._decklink_format]
        cmd += [
            "-f", "decklink", "-i", str(self._index),
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=8 * 1024 * 1024)
        except FileNotFoundError:
            self.error_msg = "FFmpeg not installed"
            return

        # Wait for ffmpeg to start, read stderr for resolution info
        time.sleep(2)
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace").strip()
            self.error_msg = f"DeckLink capture failed\n{err}" if err else \
                "DeckLink capture failed — no signal or wrong format"
            return

        # Detect resolution from stderr (ffmpeg prints stream info there)
        w, h = 1920, 1080  # safe default
        try:
            import os
            # Non-blocking read of what ffmpeg has printed so far
            fd = proc.stderr.fileno()
            os.set_blocking(fd, False)
            try:
                info = proc.stderr.read(4096)
            except (BlockingIOError, OSError):
                info = b""
            os.set_blocking(fd, True)
            if info:
                import re
                # Match "1920x1080" or similar resolution in stream info
                m = re.search(r"(\d{3,4})x(\d{3,4})", info.decode(errors="replace"))
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
        except Exception:
            pass

        from PyQt6.QtGui import QImage
        self._cap_w, self._cap_h = w, h
        frame_size = w * h * 3
        buf = bytearray(frame_size)

        while self._running:
            view = memoryview(buf)
            filled = 0
            while filled < frame_size and self._running:
                n = proc.stdout.readinto(view[filled:])
                if not n:
                    self._running = False
                    break
                filled += n
            if filled == frame_size:
                img = QImage(bytes(buf), w, h, w * 3,
                             QImage.Format.Format_BGR888)
                self._latest_qimg = img.copy()
                self._latest_id += 1

        proc.terminate()

    def stop(self):
        self._running = False


def _find_capture_devices_native() -> list:
    """Enumerate video devices using macOS native AVFoundation API via Swift.
    Returns list of (index, name, manufacturer) — index matches OpenCV index.
    This is the only reliable way to get correct OpenCV indices on macOS."""
    import subprocess
    devices = []
    try:
        result = subprocess.run(
            ["xcrun", "swift", "-e", """
import AVFoundation
let session = AVCaptureDevice.DiscoverySession(
    deviceTypes: [.external, .builtInWideAngleCamera],
    mediaType: .video, position: .unspecified)
for (i, d) in session.devices.enumerated() {
    print("\\(i)|\\(d.localizedName)|\\(d.manufacturer)")
}
"""],
            capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                try:
                    devices.append((int(parts[0]), parts[1], parts[2]))
                except ValueError:
                    pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return devices


def _find_capture_devices() -> list:
    """Find capture card devices for the monitor window.

    Uses macOS native AVFoundation enumeration for correct OpenCV index mapping,
    plus ffmpeg DeckLink detection for Blackmagic pro cards.

    Returns list of (type, id, name) tuples.
      type="cv2"     → id is OpenCV integer index
      type="decklink" → id is device name (for ffmpeg -f decklink)
    """
    import subprocess

    _CAPTURE_KEYWORDS = [
        "blackmagic", "decklink", "ultrastudio", "intensity",
        "elgato", "cam link", "camlink", "hd60", "4k60",
        "magewell", "avermedia", "game capture",
        "usb video", "usb3", "usb 3", "hdmi capture", "sdi",
        "video capture", "thunderbolt",
    ]
    _EXCLUDED = [
        "facetime", "isight", "built-in", "macbook",
        "microphone", "capture screen", "screen",
        "obs", "virtual", "iphone", "ipad", "ndi",
        "airplay",
    ]

    devices = []
    seen_names = set()

    # 1. Native macOS enumeration — correct OpenCV indices
    for idx, name, manufacturer in _find_capture_devices_native():
        lower = name.lower()
        mfr_lower = manufacturer.lower()
        if any(e in lower for e in _EXCLUDED):
            continue
        if any(k in lower for k in _CAPTURE_KEYWORDS) \
                or any(k in mfr_lower for k in _CAPTURE_KEYWORDS):
            devices.append(("cv2", idx, name))
            seen_names.add(name)

    # 2. DeckLink devices via ffmpeg (for Blackmagic cards not in AVFoundation video)
    for d in _find_decklink_devices():
        if d[2] not in seen_names:
            seen_names.add(d[2])
            devices.append(d)

    return devices


def _find_decklink_devices() -> list:
    """Use ffmpeg to list DeckLink devices (Blackmagic pro capture cards).
    Only works if FFmpeg was built with --enable-decklink."""
    import subprocess
    devices = []
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "decklink",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5)
        # If FFmpeg doesn't support decklink, stderr will say "Unknown input format"
        if "Unknown input format" in result.stderr:
            return []
        for line in result.stderr.splitlines():
            # Format: [decklink ...] 'UltraStudio Recorder 3G'
            if "'" in line and "[decklink" in line.lower():
                parts = line.split("'")
                if len(parts) >= 2:
                    name = parts[1]
                    if name and name.lower() != "decklink":
                        devices.append(("decklink", name, name))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return devices



def _find_decklink_formats(device_name: str) -> list:
    """Query available input formats for a DeckLink device.
    Returns list of (format_code, description) tuples, e.g.
    [("Hp30", "1080p 30"), ("hp60", "1080p 59.94"), ...]"""
    import subprocess, re
    formats = []
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "decklink",
             "-list_formats", "1", "-i", device_name],
            capture_output=True, text=True, timeout=5)
        for line in result.stderr.splitlines():
            # Lines like:  [decklink @ ...] 8       720x486 at 30000/1001 fps (interlaced, lower field first, 8 bit)     ntsc
            # Or:          [decklink @ ...] 14      1920x1080 at 30000/1001 fps (progressive, 8 bit)     Hp30
            if "[decklink" in line.lower() and "x" in line:
                m = re.search(
                    r"(\d{3,4})x(\d{3,5})\s+at\s+([\d/\.]+)\s+fps\s+"
                    r"\(([^)]+)\)\s+(\S+)",
                    line)
                if m:
                    w, h = m.group(1), m.group(2)
                    fps_str = m.group(3)
                    flags = m.group(4)
                    code = m.group(5)
                    # Build human-readable description
                    try:
                        if "/" in fps_str:
                            num, den = fps_str.split("/", 1)
                            fps = float(num) / float(den)
                        else:
                            fps = float(fps_str)
                        fps_label = f"{fps:.2f}".rstrip("0").rstrip(".")
                    except (ValueError, ZeroDivisionError):
                        fps_label = fps_str
                    scan = "i" if "interlaced" in flags else "p"
                    desc = f"{w}x{h}{scan} {fps_label}fps"
                    formats.append((code, desc))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return formats




class MonitorWindow(QWidget):
    """Floating window showing live video from capture cards.
    Supports: UVC devices (OpenCV), AVFoundation, DeckLink (Blackmagic)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("VIDEOMANCER — Monitor")
        self.resize(640, 480)
        self.setMinimumSize(320, 240)
        self.setStyleSheet(f"background:#000000;")

        self._cap_thread = None   # Capture thread
        self._source_type = None  # "cv2", "avf", or "decklink"
        self._display_timer = QTimer()
        self._display_timer.setInterval(33)  # ~30fps grab
        self._display_timer.timeout.connect(self._grab_frame)
        self._last_grabbed_id = 0
        self._current_res_index = 0  # start at highest, auto-scale down
        self._current_dev = None
        self._adaptive_checks = 0
        self._stable_count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setFixedHeight(36)
        toolbar.setStyleSheet(f"background:{BG};")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(8, 4, 8, 4)
        tl.setSpacing(6)

        tl.addWidget(QLabel("Source:"))
        self._source_combo = QComboBox()
        self._source_combo.setMinimumWidth(250)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        tl.addWidget(self._source_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(28)
        refresh_btn.clicked.connect(self._do_refresh)
        tl.addWidget(refresh_btn)

        # DeckLink format selector — hidden unless a DeckLink device is active
        self._fmt_label = QLabel("Format:")
        self._fmt_label.setVisible(False)
        tl.addWidget(self._fmt_label)
        self._fmt_combo = QComboBox()
        self._fmt_combo.setMinimumWidth(160)
        self._fmt_combo.setVisible(False)
        tl.addWidget(self._fmt_combo)

        self._screenshot_btn = QPushButton("📷")
        self._screenshot_btn.setFixedWidth(28)
        self._screenshot_btn.setToolTip("Save screenshot")
        self._screenshot_btn.clicked.connect(self._take_screenshot)
        self._screenshot_btn.setEnabled(False)
        tl.addWidget(self._screenshot_btn)

        tl.addStretch()

        self._status_lbl = QLabel("Scanning...")
        self._status_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:10px;background:transparent;")
        tl.addWidget(self._status_lbl)

        lay.addWidget(toolbar)

        # Video display — custom widget, no QLabel overhead
        self._video_lbl = _VideoDisplay()
        self._video_lbl.setText("Scanning for capture devices...")
        lay.addWidget(self._video_lbl, stretch=1)

        self._frame_count = 0
        self._fps_time = 0.0
        self._last_frame = None
        self._devices = []  # list of (type, id, name) tuples
        self._scanned = False  # only scan when window is shown

    def showEvent(self, event):
        super().showEvent(event)
        if not self._scanned:
            self._scanned = True
            QTimer.singleShot(100, self._refresh_sources)

    def _do_refresh(self):
        self._video_lbl.setText("Scanning for capture devices...")
        self._status_lbl.setText("Scanning...")
        self._fmt_combo.setVisible(False)
        self._fmt_label.setVisible(False)
        QTimer.singleShot(50, self._refresh_sources)

    def _refresh_sources(self):
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        self._active_index = -1

        # Collect from all backends — only devices that exist right now
        self._devices = []
        seen_names = set()

        # 1. DeckLink via ffmpeg -f decklink (requires ffmpeg --enable-decklink)
        for d in _find_decklink_devices():
            if d[2] not in seen_names:
                seen_names.add(d[2])
                self._devices.append(d)

        # 2. Capture cards via AVFoundation names + OpenCV index probing
        for d in _find_capture_devices():
            if d[2] not in seen_names:
                seen_names.add(d[2])
                self._devices.append(d)

        # Populate dropdown — clean names, no emojis
        if not self._devices:
            self._source_combo.addItem("No capture devices found", None)
            self._status_lbl.setText("No devices")
            self._video_lbl.setText(
                "No capture devices found\n\n"
                "Cheap USB capture cards will be your friend.\n"
                "Don't kill the reference!")
        else:
            self._source_combo.addItem("Select source", None)
            for i, d in enumerate(self._devices):
                self._source_combo.addItem(d[2], i)
            self._status_lbl.setText(f"{len(self._devices)} device{'s' if len(self._devices) != 1 else ''}")

        self._source_combo.blockSignals(False)

    def _on_source_changed(self, _idx):
        self._stop_capture()
        # Clear green dot from all items
        for i in range(self._source_combo.count()):
            d = self._source_combo.itemData(i)
            if d is not None and isinstance(d, int) and 0 <= d < len(self._devices):
                self._source_combo.setItemText(i, self._devices[d][2])
        data = self._source_combo.currentData()
        if data is None or not isinstance(data, int) or data < 0:
            self._fmt_combo.setVisible(False)
            self._fmt_label.setVisible(False)
            return
        if data >= len(self._devices):
            return
        dev = self._devices[data]
        # Green dot on active
        cur = self._source_combo.currentIndex()
        self._source_combo.setItemText(cur, f"\u2022 {dev[2]}")

        if dev[0] == "decklink":
            # Populate format selector for this DeckLink device
            self._fmt_combo.blockSignals(True)
            self._fmt_combo.clear()
            self._fmt_combo.addItem("Auto-detect", None)
            formats = _find_decklink_formats(dev[1])
            for code, desc in formats:
                self._fmt_combo.addItem(f"{desc}  ({code})", code)
            self._fmt_combo.blockSignals(False)
            self._fmt_combo.setVisible(True)
            self._fmt_label.setVisible(True)
            # Disconnect any prior connection, then connect
            try:
                self._fmt_combo.currentIndexChanged.disconnect()
            except TypeError:
                pass
            self._fmt_combo.currentIndexChanged.connect(
                lambda _: self._start_decklink_with_format(dev))
            # Start capture with auto-detect
            self._start_capture(dev)
        else:
            self._fmt_combo.setVisible(False)
            self._fmt_label.setVisible(False)
            self._start_capture(dev)

    def _start_decklink_with_format(self, dev):
        """Restart DeckLink capture with the selected format."""
        self._stop_capture()
        self._start_capture(dev)

    def _start_capture(self, dev):
        dtype, did, dname = dev
        self._source_type = dtype
        self._video_lbl.setText(f"Connecting to {dname}...")

        if dtype == "decklink":
            fmt_code = self._fmt_combo.currentData() if self._fmt_combo.isVisible() else None
            self._cap_thread = _CaptureThread(did, dname, fmt="decklink",
                                              decklink_format=fmt_code)
        elif dtype == "cv2":
            self._cap_thread = _CaptureThread(did, dname, fmt="cv2")
        else:
            return

        self._cap_thread.start()
        self._screenshot_btn.setEnabled(True)
        self._frame_count = 0
        self._fps_time = time.monotonic()
        self._last_grabbed_id = 0
        self._display_timer.start()
        QTimer.singleShot(5000, lambda: self._check_started(dname))

    def _stop_capture(self):
        self._display_timer.stop()
        if self._cap_thread is not None:
            self._cap_thread.stop()
            self._cap_thread.wait(3000)
            self._cap_thread = None
        self._screenshot_btn.setEnabled(False)
        self._status_lbl.setText("")
        self._source_type = None

    def _grab_frame(self):
        """Timer-driven: grab latest QImage from capture thread."""
        cap = self._cap_thread
        if not cap or not cap._latest_qimg:
            return
        fid = cap._latest_id
        if fid == self._last_grabbed_id:
            return
        self._last_grabbed_id = fid

        self._video_lbl.setFrame(cap._latest_qimg, None)

        self._frame_count += 1
        elapsed = time.monotonic() - self._fps_time
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self._status_lbl.setText(f"{fps:.1f} fps  ({cap._cap_w}x{cap._cap_h})")
            self._frame_count = 0
            self._fps_time = time.monotonic()

    def _check_started(self, dname):
        """Check if capture produced any frames after timeout."""
        if self._cap_thread and self._cap_thread._latest_id == 0:
            err = getattr(self._cap_thread, 'error_msg', '')
            self._stop_capture()
            if self._source_type == "decklink" or "decklink" in dname.lower() \
                    or "ultrastudio" in dname.lower():
                hint = (
                    f"Could not capture from {dname}\n\n"
                    "Check:\n"
                    "1. Blackmagic Desktop Video drivers are installed\n"
                    "2. FFmpeg has DeckLink support "
                    "(brew install ffmpeg --with-decklink)\n"
                    "3. A valid signal is connected to the input\n"
                    "4. Try selecting a specific format above "
                    "to match your input signal")
            else:
                hint = (
                    f"Could not capture from {dname}\n\n"
                    "Check that the device is connected and has a signal.")
            if err:
                hint += f"\n\n{err}"
            self._video_lbl.setText(hint)
            self._status_lbl.setText("Capture failed")

    def _take_screenshot(self):
        img = self._video_lbl._image
        if not img or img.isNull():
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = Path.home() / "Documents" / f"videomancer_capture_{ts}.png"
        img.copy().save(str(path), "PNG")  # copy for thread safety
        self._status_lbl.setText(f"Saved: {path.name}")

    closed = pyqtSignal()

    def closeEvent(self, event):
        self._stop_capture()
        self.closed.emit()
        event.accept()


STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Goldplay","SF Pro Display","Segoe UI","Helvetica Neue","Arial",sans-serif;
    font-size: 14px;
}}
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 18px;
    padding: 10px 8px 8px 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 5px;
    color: {TEXT_DIM};
    font-family: "Goldplay","SF Pro Display",sans-serif;
    font-size: 10px;
    letter-spacing: 2px;
    font-weight: bold;
}}
QPushButton {{
    background: {SURFACE2};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {TEXT};
    padding: 7px 18px;
    font-size: 13px;
}}
QPushButton:hover  {{ background: {DIM}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #111; }}
QPushButton:disabled {{ background: #0f0f0f; border-color: #1a1a1a; color: #444; }}
QPushButton#primary {{
    background: #333333; color: #ffffff;
    font-weight: bold; border: 1px solid #555555; border-radius: 4px;
}}
QPushButton#primary:hover    {{ background: #444444; border-color: #ffffff; }}
QPushButton#primary:disabled {{ background: #1a1a1a; color: #444; border-color: #222; }}
QPushButton#danger {{
    background: #220000; border-color: {ERROR}; color: {ERROR};
}}
QPushButton#danger:hover {{ background: #330000; }}
QTabWidget::pane {{
    border: none;
    background: {BG};
    padding: 4px;
}}
QTabBar {{
    padding: 12px 4px 4px 4px;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: {SURFACE};
    border: 2px solid #444444;
    color: #777777;
    padding: 8px 18px;
    font-family: "Goldplay","SF Pro Display",sans-serif;
    font-size: 16px;
    font-weight: bold;
    letter-spacing: 2px;
    margin-right: 4px;
    border-radius: 4px;
    min-width: 80px;
}}
QTabBar::tab:selected {{
    background: {SURFACE2};
    color: #ffffff;
    border: 2px solid {ACCENT2};
}}
QTabBar::tab:hover:!selected {{
    color: #cccccc;
    background: {SURFACE2};
    border: 2px solid {DIM};
}}
QComboBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT}; padding: 7px 12px; font-size: 13px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE2}; border: 1px solid {BORDER}; color: {TEXT};
    selection-background-color: {DIM}; selection-color: {ACCENT};
}}
QLineEdit {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT}; padding: 7px 12px; font-size: 13px;
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT}; outline: none; font-size: 15px; font-weight: bold;
}}
QListWidget::item {{ padding: 9px 14px; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:hover {{ background: {DIM}; }}
QListWidget::item:selected {{ background: {SURFACE2}; color: {ACCENT}; border-left: 2px solid {ACCENT}; }}
QScrollBar:vertical {{ background: {BG}; width: 6px; border: none; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 3px; min-height: 20px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT2}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 6px; border: none; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 3px; min-width: 20px; }}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT2}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSlider::groove:vertical {{ background: {BORDER}; width: 4px; border-radius: 2px; }}
QSlider::handle:vertical {{
    background: {ACCENT}; border: 2px solid {ACCENT2};
    width: 14px; height: 14px; margin: 0 -5px; border-radius: 7px;
}}
QSlider::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QSlider::sub-page:vertical {{ background: {ACCENT2}; border-radius: 2px; }}
QSlider::groove:horizontal {{ background: {BORDER}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; border: 2px solid {ACCENT2};
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {TEXT_DIM}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT2}; border-radius: 2px; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 20px; height: 20px;
    border: 2px solid {BORDER}; border-radius: 4px;
    background: {SURFACE};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT_DIM}; font-size: 11px;
}}
QStatusBar {{
    background: {BG}; border-top: 1px solid {BORDER};
    color: #ffffff; font-size: 12px; padding: 2px 10px;
}}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {BORDER}; }}
QDialog {{ background: {SURFACE}; color: {TEXT}; }}
"""


# ── Shared helpers ─────────────────────────────────────────────────────

def pill(text, color=None):
    c = color or ACCENT2
    lbl = QLabel(text)
    if c == "#c040c0":
        # OFFLINE pill: solid purple bg, white border + text
        lbl.setStyleSheet("""
            QLabel {
                background: #7c3aed; border: 2px solid #ffffff;
                border-radius: 8px; color: #ffffff;
                padding: 1px 8px; font-size: 9px;
                letter-spacing: 1px; font-weight: bold;
            }
        """)
    else:
        lbl.setStyleSheet(f"""
            QLabel {{
                background: {c}22; border: 1px solid {c};
                border-radius: 8px; color: {c};
                padding: 1px 8px; font-size: 9px;
                letter-spacing: 1px; font-weight: bold;
            }}
        """)
    return lbl


def hsep():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    return f


# ── Connection bar ─────────────────────────────────────────────────────

class ConnectionBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("ConnectionBar{background:transparent;border:none;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Port combo kept hidden for API compat
        self.port_combo = QComboBox()
        self.port_combo.setVisible(False)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setVisible(False)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.port_combo.showPopup = self._combo_popup

        # Port shown as plain text in both states
        self._port_lbl = QLabel("")
        self._port_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:10px;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        lay.addWidget(self._port_lbl)

        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumWidth(90)
        self.connect_btn.setFixedHeight(24)
        self.connect_btn.setStyleSheet(
            f"QPushButton{{font-size:10px;padding:2px 10px;}}"
        )
        self.connect_btn.clicked.connect(self._toggle)
        lay.addWidget(self.connect_btn)

        # Status pill kept for API but hidden
        self.status_pill = pill("OFFLINE", "#c040c0")
        self.status_pill.setVisible(False)
        lay.addWidget(self.status_pill)

        # Prog label and refresh are placed externally on the tab row
        self._prog_lbl = QLabel("")
        self._prog_lbl.setStyleSheet(
            f"color:#ffffff;font-size:13px;font-weight:bold;letter-spacing:2px;"
            f"background:transparent;border:none;"
        )
        self._prog_lbl.setVisible(False)

        self.data_refresh_btn = QPushButton("↻  Refresh")
        self.data_refresh_btn.setFixedHeight(26)
        self.data_refresh_btn.setFixedWidth(100)
        self.data_refresh_btn.setEnabled(False)
        self.data_refresh_btn.setStyleSheet(
            f"QPushButton{{background:{SURFACE2};border:1px solid #7c3aed;"
            f"border-radius:5px;color:#ffffff;font-size:11px;font-weight:bold;padding:0 10px;}}"
            f"QPushButton:hover{{background:#7c3aed;color:#ffffff;}}"
            f"QPushButton:disabled{{color:{BORDER};border-color:{BORDER};background:{SURFACE};}}"
        )

        self._connected = False
        self.on_connect    = None   # callbacks set by main window
        self.on_disconnect = None
        self.refresh_ports()

    def _combo_popup(self):
        """Refresh ports when the dropdown is opened."""
        self.refresh_ports()
        QComboBox.showPopup(self.port_combo)

    def refresh_ports(self):
        self.port_combo.clear()
        if HAS_SERIAL:
            for p in list_ports.comports():
                self.port_combo.addItem(p.device)
        if self.port_combo.count() == 0:
            for d in ["/dev/tty.usbmodem101", "/dev/ttyACM0", "COM3"]:
                self.port_combo.addItem(d)

    def find_videomancer_port(self, exclude: set = None) -> Optional[str]:
        """Scan ports for a Videomancer device by USB VID/PID or name.
        Skips ports in *exclude* (used to avoid claiming a port already
        owned by another window)."""
        if not HAS_SERIAL:
            return None
        skip = exclude or set()
        for p in list_ports.comports():
            if p.device in skip:
                continue
            desc = (p.description or "").lower()
            mfr  = (p.manufacturer or "").lower()
            name = (p.device or "").lower()
            vid  = getattr(p, 'vid', None)
            pid  = getattr(p, 'pid', None)
            # RP2040 USB CDC VID:PID = 2E8A:000A
            if vid == 0x2E8A:
                return p.device
            # Videomancer uses RP2040 USB CDC — look for known identifiers
            if any(k in desc or k in mfr for k in
                   ["videomancer", "lzx", "pico", "rp2040", "usbmodem"]):
                return p.device
            # On Mac, USB modems show as cu.usbmodem*
            if "usbmodem" in name and name.startswith("/dev/cu."):
                return p.device
            # On Windows, look for USB Serial Device on COM ports
            if "usb serial" in desc or "usb serial" in mfr:
                return p.device
        return None

    @staticmethod
    def find_all_videomancer_ports() -> List[str]:
        """Return all serial ports that look like a Videomancer device.
        Deduplicates macOS tty/cu pairs — prefers cu. for outgoing connections."""
        if not HAS_SERIAL:
            return []
        ports = []
        seen_ids = set()
        for p in list_ports.comports():
            desc = (p.description or "").lower()
            mfr  = (p.manufacturer or "").lower()
            name = (p.device or "").lower()
            vid  = getattr(p, 'vid', None)
            match = False
            if vid == 0x2E8A:
                match = True
            elif any(k in desc or k in mfr for k in
                     ["videomancer", "lzx", "pico", "rp2040", "usbmodem"]):
                match = True
            elif "usbmodem" in name and name.startswith("/dev/cu."):
                match = True
            elif "usb serial" in desc or "usb serial" in mfr:
                match = True
            if match:
                # Deduplicate macOS tty/cu pairs by location or serial_number
                port_id = getattr(p, 'serial_number', None) or getattr(p, 'location', None) or p.device
                # Skip /dev/tty.* if we already have the /dev/cu.* for same device
                if name.startswith("/dev/tty."):
                    cu_equiv = p.device.replace("/dev/tty.", "/dev/cu.")
                    if cu_equiv in ports:
                        continue
                    port_id = cu_equiv  # group with cu variant
                if port_id not in seen_ids:
                    seen_ids.add(port_id)
                    ports.append(p.device)
        return ports

    def try_auto_connect(self):
        """Try to find and connect to a Videomancer automatically."""
        port = self.find_videomancer_port(exclude=_claimed_ports)
        if port and not self._connected:
            # Select in combo
            idx = self.port_combo.findText(port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            else:
                self.port_combo.insertItem(0, port)
                self.port_combo.setCurrentIndex(0)
            if self.on_connect:
                self.on_connect(port)
            return True
        return False

    def set_connected(self, port):
        self._connected = True
        self.connect_btn.setText("DISCONNECT")
        self.connect_btn.setObjectName("")
        self.connect_btn.style().polish(self.connect_btn)
        self._port_lbl.setText(f"● {port}")
        self._port_lbl.setStyleSheet(
            f"color:#ffffff;font-size:10px;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )

    def set_disconnected(self):
        self._connected = False
        self.connect_btn.setText("CONNECT")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.style().polish(self.connect_btn)
        self._port_lbl.setText("No device")
        self._port_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:10px;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        self.status_pill.setText("OFFLINE")
        self.status_pill.setStyleSheet("""
            QLabel {
                background:#7c3aed;border:2px solid #ffffff;
                border-radius:8px;color:#ffffff;
                padding:1px 8px;font-size:9px;letter-spacing:1px;font-weight:bold;
            }
        """)

    def _toggle(self):
        if self._connected:
            if self.on_disconnect:
                self.on_disconnect()
        else:
            port = self.port_combo.currentText().strip()
            if port and self.on_connect:
                self.on_connect(port)


_LOGO_IMG_B64 = """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"""

# ── Splash widget (disconnected state) ────────────────────────────────

_SPLASH_IMG_B64 = """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2i1MpgGmV3rzPHV3h7ByH/FT/D+lfjsjtV/kp5z5+ECpxtZHG2ONoYxow1rRgAeACLkLhdTiI22gERF9gEREBERAREQFb+vq80dgkiY8iSpPdjB6c3fLh71cCjvaZXCa8RUjTwp48uHg53E/LChtfypxsGuqJ5zyj3/sLSUxaajEOn6GMAAdw13DzGfzUPKWdGVTKvTdK4O3nxt7p48C3gPlhVXg+umMi5TPWY/UVldV2XVdCgF5rwCbVWNAJ3qeQcPNpXpXSqaH00rCSN5jhkexebkb0THkISWzDsVVRquzVpbezvQ+lQu4Y+rVS4+WFrQPNbEuwDUifs+wxZefR7pUxHe5fZfw8vW+OVxCeoyCREXwYGfpIqQs2q6drt0gTWMQ72ee5PKcY/8AM+axaWX36S6nDb9omr7sgyUtXHv8cHdfEce7f+axBQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAWf3Yd2KyaM0+dealozHf7tDijglb61HSnByR9mR/AnqG4HAlwUZdi3s/G/VNLtH1rRf8AVELhJaqCZn/a3ggiZ4POIdB9o8fqj1s50BERBbe0zV9s0Hoe6aru78U1vgMgYHYdM/kyNv7TnEAe1apdY6jumrdU3HUl5nM1fcKh08zs8ASeTR0aBgAdAAFkx+kF2l/rXUlJs2tk5dSWkipuJaeD6lzfUZ57jHZ9r/FqxatFDLcbjDRw/WkdjPRo5kn2DK927dVyuKKY3mRdeza0B0jrvUNyGHcgHiftH3dP+CvtfOkpYKSlipadgZFE0NaP+fPK+i6/peBTg41Nmnr3+c945C4RFIRGwIiL6CIiAiIgIiIChy91QrrtVVYziWVzm548M8Pkpau03o1pq5wSHRwuc0+eOHzUMnmqPxnens2rXtn9PuBaQA7BwTgHH/Piq5pK/wAllqXNe10lJKR3jBzB+8PP8VlXs72BUuvex7aW0zYqfU01VU3ahqJBgbznd33Tj9x8cMZz0IafbiLqC0XTT96q7LeqGahuNHIYqinmbhzHDofxBHAggjgqZi5NzFuxdtTtMCYYJY54GTwvD45BvMcORC5VjbNLs7vZLTM/1SC+AHoftNH4+4q+V1vT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DEjGvHg4ZXhnstomH0ltpePVsYafiFUEWGuxaufmpifcKFPpOwyg4o3Rn9iR355Xjm0LaX57qorI/wCJrh+CulFp3NIwbv57UT7hZc2gIgCYbm/yD4R+OVSrro2roKOWrdW0room7x3t5pPkOB4qSVZ206vMdNBbWuwZD3knjgch7zn4KE1bRtNxsau9NG20ctp7+4lYRC46IFwucjlSLRaJsz4Y53TVkge0OA7xoGCPJqjpS9pebv8AT9DLn+6Df8Pq/krTwrjY2TdrpvURVMRExv8Az2D502nLHT/2duhcfGQb/wCOVVGNaxgYxoa0cgBgBcouh2rFq1/bpiPZAIiLLI5C4REiNgREX0EREFg7SLR3dS2607DuS4bN5O6H3/j7V8dlGurvs615btV2d2ZaR+JYS7DaiE8HxO8iPgQDzAV/1lLDWUktLUMD4pW7rm/89VEV6t01ruElHMMlh9Vw5Ob0IXOOKNL9Be/EW49Wrr5T+4207PtW2XXOkbfqiwVPf0NdEHtzjfjd9qN46OacgjxHUcVXlrb7Jm2yfZbqo2y8TSS6UukgFXHnPosnITsHlwDgOYA5loC2PUVVTVtHDWUc8VRTTxtlhmieHMkY4Za5pHAgggghVMfZWRts2bWTaloSq01eGiOQ/S0VWGgvpZwPVePLoR1BI4cCL3RBqH2haPvuhNXV2mNR0hpq+jfg44slZ9mRh+01w4g/HBBCt9bP+0psXtO13SojBiotRULS63V5b1591IeZjcf8J4jqDrS1PYrvpm/1lhv1BNQXKikMc8Eow5p/AgjBBHAggjgUFNREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERARVPTFgvWp75TWTT9sqblcal27FT07N5zvE+QHMk4AHEkBZr7BuyHZ7Mymvm010V3uIw9tpidmlhPMCRw4ykdRwZzHrjigxd2O7FNfbUalrtP2owWwO3ZbpWZjpmY54djLz0wwEjrgcVnDsR7NOgtnBhuVXCNR6gZhwrq2IbkLvGKLiGdOJLneBHJTVRUtNRUkVHR08NNTQsDIoYmBjI2gYDWtHAAeAX1QEREBCePLKoWudYaa0RYJb5qm709soY+G/KfWe77rGj1nu/ZaCVg52gO1VqLWDqix6H9J0/YXAskqN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CIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiILi2f621PoLUMd90pd57dWsG64sOWSt6skYeD2+RHnzAKza2L9r3SWo44LZr6FmmbqcN9Kbl1FKfHPF0XsdkD73RYBog3I26to7jRRV1vq6espJm70U8Egkjkb4tcOBHsX3Wo/QW0LWuhKs1OktSXC1Fzt58UUmYZD+3G7LHe8FZGaA7a+oqNsdNrbS9HdYxwNVb5DTy+0sdvNcfIbgQZyIoP0d2qdjmoRGye+1FiqH4+iulM6MA/6Rm8we9wUsad1VpjUcYk0/qK0XZh60VZHN/ukoKwiIgIiICIqPqLVWmNORmTUGorRaWDrW1kcP8AvEIKwihHWPan2N6da5sN/qL7UN/ubXSuk/237sZ9zlBWvu2vqCsbJTaJ0tR2thGG1VwkNRL7Qxu61p9pcEGblfWUlvo5a2vqoKSlhbvSzTSBjGN8S48APasdNr/a60Npds9v0dGdVXRoIEsbtyijd5yc3+PqAg8t4LCPX20TW+vKv0jVupbhdMO3mxSSbsLD+zE3DG+4BWqgvjavtX1xtOuXpWqrxJNAx+9BQw5jpYP3I88+m87LvEqx0RAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBcsc5jw9ji1zTkEHBBREF26W1/ru2V1LT23WupKKEysaY6e6TxtLcjhhrgMcFllsr1dqushtJrNT3qoMkjg/va+V+96zueXcURBL/62uv8A4nW/69381Ee1nVuqqJl19C1NeqbcdHud1Xys3clmcYdw5lEQYm6u1/ru43Wtp7hrXUlZC2eRrY57pPI0N3jwALsYVnvc57y97i5zjkknJJREHCIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiIP/9k="""


_SPLASH_PM_CACHE: dict = {}  # cache processed pixmaps by size

def _make_transparent(pm, threshold=30):
    """Replace near-black pixels using fast bytearray scan — cached per size."""
    from PyQt6.QtGui import QImage, QPixmap
    cache_key = (pm.width(), pm.height(), threshold)
    if cache_key in _SPLASH_PM_CACHE:
        return _SPLASH_PM_CACHE[cache_key]
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    ptr = img.bits()
    ptr.setsize(h * img.bytesPerLine())
    arr = bytearray(ptr)
    bpl = img.bytesPerLine()
    for y in range(h):
        row = y * bpl
        for x in range(w):
            off = row + x * 4
            b, g, r = arr[off], arr[off+1], arr[off+2]
            if r < threshold and g < threshold and b < threshold:
                arr[off] = arr[off+1] = arr[off+2] = arr[off+3] = 0
    from PyQt6.QtCore import QByteArray
    img2 = QImage(bytes(arr), w, h, bpl, QImage.Format.Format_ARGB32)
    result = QPixmap.fromImage(img2)
    _SPLASH_PM_CACHE[cache_key] = result
    return result


class _SplashWidget(QWidget):
    """Disconnected state — shows Videomancer character artwork."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(16)

        # Character image
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtCore import QByteArray
        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_lbl.setStyleSheet("background:transparent;border:none;")
        try:
            raw = QByteArray.fromBase64(QByteArray(_SPLASH_IMG_B64.encode()))
            pm = QPixmap()
            pm.loadFromData(raw)
            pm = _make_transparent(pm)
            # Scale to max 340px wide keeping aspect ratio
            pm = pm.scaledToWidth(340, Qt.TransformationMode.SmoothTransformation)
            img_lbl.setPixmap(pm)
        except Exception:
            img_lbl.setText("VIDEOMANCER")
        lay.addWidget(img_lbl)

        self._sub = QLabel("Connect your Videomancer to view spells")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub.setStyleSheet(
            f"color:{TEXT_DIM};font-size:13px;letter-spacing:1.5px;"
            f"background:transparent;border:none;"
        )
        lay.addWidget(self._sub)

    def set_status(self, text: str, color: str = TEXT_DIM):
        self._sub.setText(text)
        self._sub.setStyleSheet(
            f"color:{color};font-size:13px;letter-spacing:1.5px;"
            f"background:transparent;border:none;"
        )


# ── Programs tab ───────────────────────────────────────────────────────

class ProgramsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: List[str] = []
        self._active: Optional[str] = None
        self._connected = False

        # Outer stack — splash OR browser
        self._stack = QWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        # ── Splash (disconnected) ──
        self._splash = _SplashWidget()

        # ── Browser layout ──
        self._browser = QWidget()
        lay = QHBoxLayout(self._browser)
        lay.setContentsMargins(8, 20, 8, 8)
        lay.setSpacing(8)

        # Left: search + list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍  filter programs…")
        self.search.textChanged.connect(self._filter)
        ll.addWidget(self.search)

        self.count_lbl = QLabel("—")
        self.count_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;")
        ll.addWidget(self.count_lbl)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double)
        self.list_widget.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.list_widget, stretch=1)

        self.load_more_btn = QPushButton("Load more…")
        self.load_more_btn.setVisible(False)
        ll.addWidget(self.load_more_btn)

        lay.addWidget(left, stretch=3)

        # Right: detail — no box, transparent, centered
        right = QWidget()
        right.setStyleSheet("background:transparent;border:none;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 8, 12, 8)
        rl.setSpacing(8)
        rl.setAlignment(Qt.AlignmentFlag.AlignTop)

        rl.addStretch(1)

        self.name_lbl = QLabel("—")
        self.name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_lbl.setStyleSheet(
            f"color:#ffffff;font-size:28px;font-weight:bold;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        self.name_lbl.setWordWrap(True)
        rl.addWidget(self.name_lbl)

        # Active indicator — big and prominent
        self.active_pill = QLabel("▶  RUNNING")
        self.active_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.active_pill.setStyleSheet(
            f"color:{ACCENT2};font-size:22px;font-weight:bold;letter-spacing:3px;"
            "background:transparent;border:none;"
        )
        self.active_pill.setVisible(False)
        rl.addWidget(self.active_pill)

        # Program description
        self.desc_lbl = QLabel("")
        self.desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.desc_lbl.setWordWrap(True)
        self.desc_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:12px;font-style:italic;"
            f"background:transparent;border:none;"
        )
        self.desc_lbl.setVisible(False)
        rl.addWidget(self.desc_lbl)

        rl.addSpacing(16)

        self.load_btn = QPushButton("⬤  LOAD PROGRAM")
        self.load_btn.setObjectName("primary")
        self.load_btn.setFixedHeight(52)
        self.load_btn.setStyleSheet(
            f"QPushButton{{background:{DIM};border:2px solid {ACCENT2};"
            f"border-radius:8px;color:#ffffff;font-size:16px;"
            f"font-weight:bold;letter-spacing:2px;padding:0 24px;}}"
            f"QPushButton:hover{{background:{ACCENT2};border-color:#ffffff;}}"
            f"QPushButton:disabled{{background:#1a1a1a;color:#444;border-color:#333;}}"
        )
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self._on_load)
        rl.addWidget(self.load_btn)

        self.loading_note = QLabel("Loading… video output paused ~2 s")
        self.loading_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_note.setStyleSheet(f"color:{WARN};font-size:10px;background:transparent;border:none;")
        self.loading_note.setVisible(False)
        rl.addWidget(self.loading_note)

        rl.addStretch(1)

        # Character always visible at bottom
        self._char_lbl = QLabel()
        self._char_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._char_lbl.setStyleSheet("background:transparent;border:none;")
        try:
            from PyQt6.QtGui import QPixmap
            from PyQt6.QtCore import QByteArray
            raw = QByteArray.fromBase64(QByteArray(_SPLASH_IMG_B64.encode()))
            pm = QPixmap()
            pm.loadFromData(raw)
            pm = _make_transparent(pm)
            pm = pm.scaledToWidth(200, Qt.TransformationMode.SmoothTransformation)
            self._char_lbl.setPixmap(pm)
        except Exception:
            pass
        rl.addWidget(self._char_lbl)

        lay.addWidget(right, stretch=2)

        # Stack: show splash or browser
        stack_lay = QVBoxLayout(self._stack)
        stack_lay.setContentsMargins(0, 0, 0, 0)
        stack_lay.addWidget(self._splash)
        stack_lay.addWidget(self._browser)
        self._browser.setVisible(False)

        self._selected: Optional[str] = None

    # callbacks set by main window
    on_load_program = None

    def _show_splash(self, show: bool):
        self._splash.setVisible(show)
        self._browser.setVisible(not show)

    def set_connected(self, v: bool):
        self._connected = v
        if not v:
            self._show_splash(True)   # disconnected → always show splash
            self._splash.set_status("Connect your Videomancer to view spells", TEXT_DIM)
        else:
            self._splash.set_status("Connecting…  fetching programs", ACCENT2)
        # Stay on splash until first program page arrives
        self.load_btn.setEnabled(v and bool(self._selected or self._active))

    def add_page(self, names, more, total):
        existing = set(self._all)
        self._all.extend(n for n in names if n not in existing)
        self._rebuild(self.search.text())
        self.load_more_btn.setVisible(more)
        self.count_lbl.setText(
            f"{len(self._all)} / {total} loaded" if more else f"{total} programs"
        )
        if self._active:
            self._highlight_active()
        # First page arrived — now reveal the browser
        if not self._browser.isVisible():
            self._show_splash(False)

    def clear(self):
        self._all.clear()
        self.list_widget.clear()
        self.load_more_btn.setVisible(False)
        self.count_lbl.setText("—")

    def set_active(self, name: str):
        self._active = name
        self._highlight_active()
        if self._selected == name:
            self.active_pill.setVisible(True)
        elif not self._selected:
            # Nothing selected — show the active program in the panel
            self.name_lbl.setText(name)
            self.active_pill.setVisible(True)
            self.desc_lbl.setVisible(False)
            self.load_btn.setText("⬤  RELOAD PROGRAM")
            self.load_btn.setEnabled(self._connected)

    def set_loading_program(self, loading: bool):
        self.loading_note.setVisible(loading)
        self.load_btn.setEnabled(not loading and self._connected)

    def _highlight_active(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            raw = item.data(Qt.ItemDataRole.UserRole)
            if raw == self._active:
                item.setText(f"▶  {raw}")
                item.setForeground(QColor('#ffffff'))
            else:
                item.setText(raw)
                item.setForeground(QColor(TEXT))

    def _filter(self, text):
        self._rebuild(text)

    def _rebuild(self, filt):
        self.list_widget.clear()
        fl = filt.lower().strip()
        for name in self._all:
            if fl and fl not in name.lower():
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)
        if self._active:
            self._highlight_active()

    def _on_select(self, item, _prev):
        if not item:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        self._selected = name
        self.name_lbl.setText(name)
        self.active_pill.setVisible(name == self._active)
        self.desc_lbl.setText("")
        self.desc_lbl.setVisible(False)
        self.load_btn.setText(
            "⬤  RELOAD PROGRAM" if name == self._active else "⬤  LOAD PROGRAM"
        )
        self.load_btn.setEnabled(self._connected)

    def set_program_description(self, desc: str):
        """Show a description for the currently selected program."""
        if desc:
            self.desc_lbl.setText(desc)
            self.desc_lbl.setVisible(True)
        else:
            self.desc_lbl.setVisible(False)

    def _on_double(self, item):
        name = item.data(Qt.ItemDataRole.UserRole)
        if name and self.on_load_program:
            if name == self._active:
                return  # already running
            reply = QMessageBox.question(
                self, "Load Program",
                f'Load \u201c{name}\u201d?\nVideo output will pause briefly.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.on_load_program(name)

    def _on_load(self):
        if self._selected and self.on_load_program:
            self.on_load_program(self._selected)

    def selected(self):
        return self._selected


# ── Operator definitions ───────────────────────────────────────────────

OPERATORS = [
    (0,  "Disabled"),
    (1,  "Free LFO"),
    (2,  "Sync LFO"),
    (3,  "CV Input"),
    (4,  "Audio Input"),
    (5,  "Random"),
    (6,  "Envelope"),
    (7,  "Sample & Hold"),
    (8,  "Trigger Env"),
    (9,  "Step Seq"),
    (10, "FFT Band"),
    (11, "H Displace"),
    (12, "Turing Machine"),
    (13, "Bouncing Ball"),
    (14, "Logistic Map"),
    (15, "Euclidean Rhythm"),
    (16, "Motion LFO"),
    (17, "V Gradient"),
    (18, "Comparator"),
    (19, "Pendulum"),
    (20, "Drift"),
    (21, "Ring Mod"),
    (22, "Cellular"),
    (23, "Pulse Width"),
    (24, "Peak Hold"),
    (25, "Field Accum"),
    (26, "Slew Limiter"),
    (27, "Perlin Noise"),
    (28, "Wavefolder"),
    (29, "Clock Div"),
    (30, "Prob Gate"),
    (31, "Quantizer"),
    (32, "Mouse"),
    (33, "Keyboard"),
    (34, "Gamepad"),
    (35, "Tablet"),
    (36, "Joystick"),
    (37, "Sensor"),
    (38, "MIDI Turing"),
]

# Operator index → (time_label, space_label, slope_label)
OP_LABELS = {
    0:  ("Time",    "Space",  "Slope"),
    1:  ("Rate",    "Depth",  "Wave"),
    2:  ("Division","Depth",  "Wave"),
    3:  ("Slew",    "Gain",   "Channel"),
    4:  ("—",       "Gain",   "Channel"),
    5:  ("Rise",    "Gain",   "Fall"),
    6:  ("Attack",  "Release","Channel"),
    7:  ("Rate",    "Gain",   "Channel"),
    8:  ("Attack",  "Release","Curve"),
    9:  ("Rate",    "Depth",  "Pattern"),
    10: ("Slew",    "Gain",   "Band"),
    11: ("Freq",    "Depth",  "Wave"),
    12: ("Rate",    "Gain",   "Mutate"),
    13: ("Gravity", "Gain",   "Bounce"),
    14: ("Rate",    "Gain",   "Chaos"),
    15: ("Rate",    "Gain",   "Density"),
    16: ("Division","Depth",  "Wave"),
    17: ("Freq",    "Depth",  "Wave"),
    18: ("Thresh",  "Gain",   "Channel"),
    19: ("Length",  "Gain",   "Damp"),
    20: ("Rate",    "Gain",   "Range"),
    21: ("Slew",    "Gain",   "Channel"),
    22: ("Rate",    "Gain",   "Rule"),
    23: ("Rate",    "Depth",  "Width"),
    24: ("Decay",   "Gain",   "Channel"),
    25: ("Rate",    "Gain",   "Leak"),
    26: ("Rise",    "Gain",   "Fall"),
    27: ("Speed",   "Gain",   "Detail"),
    28: ("Rate",    "Folds",  "Symmetry"),
    29: ("Division","Gain",   "Duty"),
    30: ("Rate",    "Prob",   "Length"),
    31: ("Levels",  "Gain",   "Channel"),
    32: ("Slew",    "Gain",   "Axis"),
    33: ("Attack",  "Release","Curve"),
    34: ("Slew",    "Gain",   "Axis"),
    35: ("Slew",    "Gain",   "Axis"),
    36: ("Slew",    "Gain",   "Axis"),
    37: ("Slew",    "Gain",   "Axis"),
    38: ("Slew",    "Gain",   "Mutate"),
}

def _knob_style(accent=ACCENT2):
    return f"""
        QSlider::groove:horizontal {{
            background:{BORDER}; height:3px; border-radius:1px;
        }}
        QSlider::handle:horizontal {{
            background:{accent}; border:1px solid #ffffff;
            width:12px; height:12px; margin:-5px 0; border-radius:6px;
        }}
        QSlider::handle:horizontal:hover {{ background:{ACCENT2}; }}
        QSlider::sub-page:horizontal {{ background:{ACCENT2}; border-radius:1px; }}
    """


# ── Rotary knob widget ────────────────────────────────────────────────

class KnobWidget(QWidget):
    """Rotary knob — sub-pixel drag accumulation, cached fraction, fast paint."""
    from PyQt6.QtCore import pyqtSignal
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None, size=60):
        from PyQt6.QtCore import Qt, QTimer
        super().__init__(parent)
        self._value       = 0
        self._target      = 0       # target from poll
        self._display_frac = 0.0    # smoothed display fraction (never jumps)
        self._min   = 0
        self._max   = PARAM_RANGE
        self._size  = size
        self._drag_y = None
        self._drag_v = 0.0
        self._frac   = 0.0
        self._user_dragging = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # Smooth interpolation timer — 60fps
        self._smooth_timer = QTimer()
        self._smooth_timer.setInterval(16)  # 60fps when active
        self._smooth_timer.timeout.connect(self._smooth_step)

    def value(self):    return self._value
    def minimum(self):  return self._min
    def maximum(self):  return self._max

    def setValue(self, v: int, animate: bool = True):
        v = max(self._min, min(self._max, int(v)))
        if self._user_dragging:
            return
        if v == self._value:
            return
        self._value = v
        new_frac = (v - self._min) / max(1, self._max - self._min)
        self._frac = new_frac
        if not animate:
            self._display_frac = new_frac
        # When animate=True, _frac is set but _display_frac is NOT —
        # _smooth_step will glide _display_frac toward _frac.
        if not self._smooth_timer.isActive():
            self._smooth_timer.start()
        self.update()
        self.valueChanged.emit(v)

    def _ensure_timer(self):
        if not self._smooth_timer.isActive():
            self._smooth_timer.start()

    def _smooth_step(self):
        """Timer-driven glide — only runs when animating."""
        if self._user_dragging:
            self._smooth_timer.stop()
            return
        diff = self._frac - self._display_frac
        glide = 0.8
        if abs(diff) > 0.001:
            self._display_frac += diff * glide
            self.update()
        elif self._display_frac != self._frac:
            self._display_frac = self._frac
            self._tss_glide = False
            self.update()
        else:
            self._tss_glide = False
            self._smooth_timer.stop()  # settled — stop until needed

    def setRange(self, mn, mx):
        self._min, self._max = mn, mx
        self._frac = (self._value - mn) / max(1, mx - mn)
        self._display_frac = self._frac  # sync on range change

    def paintEvent(self, _e):
        s    = self._size
        cx   = cy = s * 0.5
        # Scale all dimensions relative to knob size
        pad   = max(2, s * 0.08)
        arc_w = max(2, s * 0.07)
        dot_r = max(2, s * 0.09)
        r     = cx - pad
        frac  = self._display_frac

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Body
        p.setPen(QPen(QColor(BORDER), max(1, s * 0.03)))
        p.setBrush(QColor(SURFACE2))
        p.drawEllipse(QPointF(cx, cy), r - arc_w * 0.5 - 1, r - arc_w * 0.5 - 1)

        # Arc geometry — computed once, reused for track + value + dot
        rect     = QRectF(pad, pad, s - pad * 2, s - pad * 2)
        qt_start = 225 * 16   # 7 o'clock in Qt coords
        qt_span  = 270 * 16

        # Track (sub-pixel QPainterPath)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(BORDER), arc_w,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        track_path = QPainterPath()
        track_path.arcMoveTo(rect, 225.0)
        track_path.arcTo(rect, 225.0, -270.0)
        p.drawPath(track_path)

        # Value arc (sub-pixel QPainterPath)
        if frac > 0.001:
            p.setPen(QPen(QColor(ACCENT), arc_w,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            val_path = QPainterPath()
            val_path.arcMoveTo(rect, 225.0)
            val_path.arcTo(rect, 225.0, -frac * 270.0)
            p.drawPath(val_path)

        # Dot — exact arc tip: angle = 225 - frac*270 in standard math degrees
        a  = math.radians(225.0 - frac * 270.0)
        ix = cx + r * math.cos(a)
        iy = cy - r * math.sin(a)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT if frac > 0.001 else BORDER))
        p.drawEllipse(QPointF(ix, iy), dot_r, dot_r)
        p.end()

    def mousePressEvent(self, e):
        self._drag_y = e.globalPosition().y()
        self._drag_accum = float(self._value)
        self._user_dragging = True
        self._display_frac = self._frac

    def mouseMoveEvent(self, e):
        if self._drag_y is None:
            return
        cur_y = e.globalPosition().y()
        dy = self._drag_y - cur_y
        if dy == 0:
            return
        self._drag_y = cur_y
        # Fine sensitivity — float accumulator for sub-pixel smoothness
        self._drag_accum += dy * (self._max - self._min) / 300.0
        self._drag_accum = max(float(self._min), min(float(self._max), self._drag_accum))
        # Update frac from float ��� direct feedback + timer coalesces
        self._frac = (self._drag_accum - self._min) / max(1, self._max - self._min)
        self._display_frac = self._frac
        self.update()  # Qt coalesces — no double paint, zero latency
        # Emit integer value change directly (like v1.0)
        newi = int(round(self._drag_accum))
        if newi != self._value:
            self._value = newi
            self.valueChanged.emit(newi)

    def mouseReleaseEvent(self, e):
        self._drag_y = None
        self._user_dragging = False

    def wheelEvent(self, e):

        step = 1 if e.angleDelta().y() > 0 else -1
        mult = 1 if (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 8
        self.setValue(self._value + step * mult)


# ── Smooth vertical fader ──────────────────────────────────────────────

class SmoothFader(QWidget):
    """Vertical fader with buttery smooth interpolation and momentum."""
    from PyQt6.QtCore import pyqtSignal
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtCore import Qt, QTimer
        self._value        = 0
        self._display_val  = 0.0   # smoothed display value
        self._min          = 0
        self._max          = PARAM_RANGE
        self._dragging     = False
        self._drag_y       = None
        self._drag_base    = 0.0
        self._velocity     = 0.0   # momentum
        self._last_drag_y  = None
        self.setMinimumHeight(200)
        self.setMinimumWidth(40)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def setRange(self, mn, mx):
        self._min, self._max = mn, mx

    def setValue(self, v: int, animate: bool = True):
        v = max(self._min, min(self._max, int(v)))
        if v == self._value:
            return
        self._value = v
        if not animate or self._dragging:
            self._display_val = float(v)
        else:
            if not self._timer.isActive():
                self._timer.start()
        self.valueChanged.emit(v)
        self.update()

    def value(self):
        return self._value

    def _step(self):
        if self._dragging:
            self._timer.stop()
            return
        target = float(self._value)
        diff = target - self._display_val
        if abs(diff) > 0.5:
            self._display_val += diff * 0.8
            self._velocity = 0.0
            self.update()
        elif self._display_val != target:
            self._display_val = target
            self._velocity = 0.0
            self.update()
        else:
            self._timer.stop()

    def _frac(self):
        return (self._display_val - self._min) / max(1, self._max - self._min)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = float(self.width()), float(self.height())
        track_x = w * 0.5
        track_w = 8.0
        frac = self._frac()
        fill_h = h * frac
        handle_w, handle_h = 22.0, 22.0
        half_h = handle_h * 0.5
        # Clamp so handle never clips outside widget bounds
        hy = h * (1.0 - frac)
        hy = max(half_h, min(h - half_h, hy))
        hx = track_x - handle_w * 0.5

        # Dark "off" track above handle
        off_h = max(0.0, hy - half_h)
        if off_h > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#0d0b1e"))
            p.drawRoundedRect(QRectF(track_x - track_w * 0.5, 0.0, track_w, off_h), 4.0, 4.0)

        # Filled track below handle
        if fill_h > 0:
            grad = QLinearGradient(0.0, h, 0.0, h - fill_h)
            grad.setColorAt(0, QColor("#7733bb"))
            grad.setColorAt(1, QColor("#c040c0"))
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(track_x - track_w * 0.5, h - fill_h, track_w, fill_h), 4.0, 4.0)

        # Handle body
        handle_color = "#c040c0" if self._dragging else "#7733bb"
        border_color = "#c040c0" if self._dragging else "#9955cc"
        p.setBrush(QColor(handle_color))
        p.setPen(QPen(QColor(border_color), 2))
        p.drawRoundedRect(QRectF(hx, hy - half_h, handle_w, handle_h), 4.0, 4.0)

        # Grip lines — 3 horizontal notches across centre
        line_color = QColor("#ffffff" if self._dragging else "#9988cc")
        line_color.setAlpha(160)
        p.setPen(QPen(line_color, 1))
        cx = hx + handle_w * 0.5
        cy = hy
        for offset in (-3.0, 0.0, 3.0):
            lx = cx + offset
            p.drawLine(QPointF(lx, cy - 6.0), QPointF(lx, cy + 6.0))

        p.end()

    def mousePressEvent(self, e):
        self._dragging = True
        self._drag_y   = e.globalPosition().y()
        self._drag_base = float(self._value)
        self._last_drag_y = self._drag_y
        self._velocity = 0.0
        self.update()

    def mouseMoveEvent(self, e):
        if not self._dragging:
            return
        dy = self._drag_y - e.globalPosition().y()  # up = increase
        sensitivity = (self._max - self._min) / max(self.height(), 1)
        new_val = self._drag_base + dy * sensitivity
        new_val = max(float(self._min), min(float(self._max), new_val))
        # Capture velocity for momentum — averaged for smoothness
        if self._last_drag_y is not None:
            raw_vel = (self._last_drag_y - e.globalPosition().y()) * sensitivity
            self._velocity = self._velocity * 0.6 + raw_vel * 0.4
        self._last_drag_y = e.globalPosition().y()
        # Update display from float for smooth visual
        self._display_val = new_val
        self.update()
        # Only emit integer value change for device commands
        int_val = round(new_val)
        if int_val != self._value:
            self._value = int_val
            self.valueChanged.emit(int_val)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        # Clamp velocity for a gentle natural coast
        self._velocity = max(-50, min(50, self._velocity))

    def wheelEvent(self, e):

        step = 8 if (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 32
        direction = 1 if e.angleDelta().y() > 0 else -1
        self.setValue(self._value + direction * step)


# ── Horizontal fader (compact) ────────────────────────────────────────

class HorizontalFader(QWidget):
    """Compact horizontal fader with the same look as SmoothFader."""
    from PyQt6.QtCore import pyqtSignal
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtCore import Qt, QTimer
        self._value       = 0
        self._display_val = 0.0
        self._min         = 0
        self._max         = PARAM_RANGE
        self._dragging    = False
        self._drag_x      = None
        self._drag_base   = 0.0
        self._velocity    = 0.0
        self._last_drag_x = None
        self.setFixedHeight(28)
        self.setMinimumWidth(140)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def setRange(self, mn, mx):
        self._min, self._max = mn, mx

    def setValue(self, v: int, animate: bool = True):
        v = max(self._min, min(self._max, int(v)))
        if v == self._value:
            return
        self._value = v
        if not animate or self._dragging:
            self._display_val = float(v)
        else:
            if not self._timer.isActive():
                self._timer.start()
        self.valueChanged.emit(v)
        self.update()

    def value(self):
        return self._value

    def _step(self):
        if self._dragging:
            self._timer.stop()
            return
        target = float(self._value)
        diff = target - self._display_val
        if abs(diff) > 0.5:
            self._display_val += diff * 0.8
            self._velocity = 0.0
            self.update()
        elif self._display_val != target:
            self._display_val = target
            self._velocity = 0.0
            self.update()
        else:
            self._timer.stop()

    def _frac(self):
        return (self._display_val - self._min) / max(1, self._max - self._min)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track_y = h // 2
        track_h = 6
        handle_w, handle_h = 18, 22
        half_w = handle_w // 2
        frac = self._frac()

        # Usable track area — inset by half the handle so it never clips
        left_edge = half_w + 1
        right_edge = w - half_w - 1
        track_range = right_edge - left_edge
        hx = int(left_edge + track_range * frac)
        hy = track_y - handle_h // 2

        # Dark "off" track to right of handle
        off_start = hx + half_w
        off_w = max(0, w - half_w - off_start)
        if off_w > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#0d0b1e"))
            p.drawRoundedRect(off_start, track_y - track_h//2, off_w, track_h, 3, 3)

        # Filled track left of handle
        fill_w = max(0, hx - half_w - left_edge + half_w)
        if fill_w > 0:
            grad = QLinearGradient(left_edge - half_w, 0, left_edge - half_w + fill_w, 0)
            grad.setColorAt(0, QColor("#7733bb"))
            grad.setColorAt(1, QColor("#c040c0"))
            p.setBrush(grad)
            p.drawRoundedRect(left_edge - half_w, track_y - track_h//2, fill_w, track_h, 3, 3)

        # Handle
        handle_color = "#c040c0" if self._dragging else "#7733bb"
        border_color = "#c040c0" if self._dragging else "#9955cc"
        p.setBrush(QColor(handle_color))
        p.setPen(QPen(QColor(border_color), 2))
        p.drawRoundedRect(hx - half_w, hy, handle_w, handle_h, 4, 4)

        # Grip lines — 3 vertical notches
        line_color = QColor("#ffffff" if self._dragging else "#9988cc")
        line_color.setAlpha(160)
        p.setPen(QPen(line_color, 1))
        cy = track_y
        for offset in (-3, 0, 3):
            lx = hx + offset
            p.drawLine(lx, cy - 6, lx, cy + 6)

        p.end()

    def mousePressEvent(self, e):
        self._dragging = True
        self._drag_x = e.globalPosition().x()
        self._drag_base = float(self._value)
        self._last_drag_x = self._drag_x
        self._velocity = 0.0
        self.update()

    def mouseMoveEvent(self, e):
        if not self._dragging:
            return
        dx = e.globalPosition().x() - self._drag_x  # right = increase
        sensitivity = (self._max - self._min) / max(self.width(), 1)
        new_val = self._drag_base + dx * sensitivity
        new_val = max(float(self._min), min(float(self._max), new_val))
        if self._last_drag_x is not None:
            raw_vel = (e.globalPosition().x() - self._last_drag_x) * sensitivity
            self._velocity = self._velocity * 0.6 + raw_vel * 0.4
        self._last_drag_x = e.globalPosition().x()
        # Update display from float for smooth visual
        self._display_val = new_val
        self.update()
        # Only emit integer value change for device commands
        int_val = round(new_val)
        if int_val != self._value:
            self._value = int_val
            self.valueChanged.emit(int_val)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        self._velocity = max(-50, min(50, self._velocity))

    def wheelEvent(self, e):

        step = 8 if (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 32
        direction = 1 if e.angleDelta().x() or e.angleDelta().y() > 0 else -1
        if e.angleDelta().y() != 0:
            direction = 1 if e.angleDelta().y() > 0 else -1
        self.setValue(self._value + direction * step)


# ── Modulation activity bar ───────────────────────────────────────────

# ── Poof animation overlay ────────────────────────────────────────────

class PoofOverlay(QWidget):
    """Particle burst animation triggered on program load."""

    def __init__(self, parent=None):
        super().__init__(parent)
        import random
        self._random = random
        self._particles = []
        self._frame = 0
        self._max_frames = 30  # ~500ms at 60fps
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background:transparent;border:none;")

        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def trigger(self, center_x=None, center_y=None):
        """Spawn particles from a center point and start animating."""
        import random
        w, h = self.width(), self.height()
        cx = center_x if center_x is not None else w // 2
        cy = center_y if center_y is not None else h // 2

        self._particles = []
        for _ in range(24):
            angle = random.uniform(0, 6.283)
            speed = random.uniform(2, 8)
            import math
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed
            size = random.uniform(3, 10)
            # Purple/magenta palette
            color = random.choice([
                "#c040c0", "#7c3aed", "#a855f7", "#9955cc",
                "#ffffff", "#e0d0ff", "#ff66ff",
            ])
            self._particles.append({
                "x": float(cx), "y": float(cy),
                "vx": vx, "vy": vy,
                "size": size, "color": color,
                "alpha": 255,
            })
        self._frame = 0
        self.show()
        self.raise_()
        self._timer.start()

    def _step(self):
        self._frame += 1
        if self._frame > self._max_frames:
            self._timer.stop()
            self.hide()
            return
        progress = self._frame / self._max_frames
        for pt in self._particles:
            pt["x"] += pt["vx"]
            pt["y"] += pt["vy"]
            pt["vy"] += 0.15  # gentle gravity
            pt["vx"] *= 0.97  # drag
            pt["vy"] *= 0.97
            pt["alpha"] = max(0, int(255 * (1.0 - progress)))
            pt["size"] *= 0.97
        self.update()

    def paintEvent(self, e):

        from PyQt6.QtCore import Qt, QPointF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for pt in self._particles:
            c = QColor(pt["color"])
            c.setAlpha(pt["alpha"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(pt["x"], pt["y"]),
                          pt["size"], pt["size"])
        p.end()


# ── Sparkle ring animation ────────────────────────────────────────────

class SparkleRing(QWidget):
    """Expanding ring with sparkles — secondary poof effect."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = 0
        self._max_frames = 20
        self._cx = 0
        self._cy = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background:transparent;border:none;")

        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def trigger(self, cx=None, cy=None):
        w, h = self.width(), self.height()
        self._cx = cx if cx is not None else w // 2
        self._cy = cy if cy is not None else h // 2
        self._frame = 0
        self.show()
        self.raise_()
        self._timer.start()

    def _step(self):
        self._frame += 1
        if self._frame > self._max_frames:
            self._timer.stop()
            self.hide()
            return
        self.update()

    def paintEvent(self, e):

        from PyQt6.QtCore import Qt, QPointF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        progress = self._frame / self._max_frames
        radius = 20 + progress * 60
        alpha = max(0, int(200 * (1.0 - progress)))

        # Ring
        c = QColor("#c040c0")
        c.setAlpha(alpha)
        p.setPen(QPen(c, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(self._cx, self._cy), radius, radius)

        # Inner glow
        c2 = QColor("#7c3aed")
        c2.setAlpha(alpha // 2)
        p.setPen(QPen(c2, 1))
        p.drawEllipse(QPointF(self._cx, self._cy), radius * 0.6, radius * 0.6)

        p.end()


class ModBar(QWidget):
    """Rolling waveform showing live LFO output with smooth interpolation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._max = PARAM_RANGE
        self._active = False
        self._history = []
        self._history_len = 600  # longer time window — slower scroll
        self._display_val = 0.0  # smoothed current value
        self._target_val = 0.0
        self.setFixedHeight(40)
        self.setMinimumWidth(10)

        # Smooth render timer — only active when animating
        self._render_timer = QTimer()
        self._render_timer.setInterval(16)
        self._render_timer.timeout.connect(self._interpolate)

    def setValue(self, v: int):
        v = max(0, min(self._max, v))
        self._target_val = float(v)
        if self._active and not self._render_timer.isActive():
            self._render_timer.start()

    def _interpolate(self):
        if not self._active:
            self._render_timer.stop()
            return
        diff = self._target_val - self._display_val
        if abs(diff) > 0.1:
            self._display_val += diff * 0.1
        elif abs(diff) > 0.01:
            self._display_val = self._target_val
        # Record interpolated value every frame for smooth waveform
        self._history.append(self._display_val)
        if len(self._history) > self._history_len:
            self._history = self._history[-self._history_len:]
        self.update()

    def setActive(self, active: bool):
        self._active = active
        if not active:
            self._render_timer.stop()
            self._history.clear()
            self._display_val = 0.0
            self._target_val = 0.0
        elif not self._render_timer.isActive():
            self._render_timer.start()
        self.update()

    def paintEvent(self, e):


        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1a1433"))
        p.drawRoundedRect(0, 0, w, h, 3, 3)

        if not self._active or len(self._history) < 2:
            p.end()
            return

        # Build smooth point list
        n = len(self._history)
        step_x = w / max(1, self._history_len - 1)
        start_x = w - (n - 1) * step_x
        points = []
        for i, val in enumerate(self._history):
            x = start_x + i * step_x
            y = h - (val / self._max) * (h - 2)
            points.append((x, y))

        # Build cubic bezier curve through points
        from PyQt6.QtCore import QPointF
        def _smooth_path(pts):
            path = QPainterPath()
            path.moveTo(QPointF(pts[0][0], pts[0][1]))
            if len(pts) == 2:
                path.lineTo(QPointF(pts[1][0], pts[1][1]))
                return path
            for i in range(1, len(pts)):
                x0, y0 = pts[i - 1]
                x1, y1 = pts[i]
                # Control points at 1/3 intervals for smooth cubic
                tension = step_x * 0.4
                path.cubicTo(
                    QPointF(x0 + tension, y0),
                    QPointF(x1 - tension, y1),
                    QPointF(x1, y1),
                )
            return path

        curve = _smooth_path(points)

        # Filled area under curve
        fill_path = QPainterPath(curve)
        fill_path.lineTo(QPointF(points[-1][0], h))
        fill_path.lineTo(QPointF(points[0][0], h))
        fill_path.closeSubpath()

        fill_color = QColor("#7c3aed")
        fill_color.setAlpha(60)
        p.setBrush(fill_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(fill_path)

        # Waveform line
        p.setPen(QPen(QColor("#c040c0"), 1.5, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(curve)

        p.end()


# ── Single channel modulation card ────────────────────────────────────

class ChannelCard(QWidget):
    """
    Expanded card for one of the 12 parameter channels.
    Shows: value control + operator selector + Time/Space/Slope knobs.
    """

    def __init__(self, index: int, parent=None, hide_tss=False):
        super().__init__(parent)
        self.index = index
        self._updating = False
        self._is_toggle = 7 <= (index + 1) <= 11
        self._op_index = 0  # current operator index in OPERATORS list
        # Parameter info from program
        self._param_min = 0
        self._param_max = 100
        self._param_step = 0
        self._param_values = []
        self._param_type = ""

        self.on_manual_change = None
        self.on_mod_change    = None

        self.setStyleSheet(f"""
            QWidget {{
                background: {SURFACE};
                border: 2px solid {BORDER};
                border-radius: 8px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(3)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── Top row: centered "P1" + param name ──
        top_hdr = QHBoxLayout()
        top_hdr.setSpacing(4)
        top_hdr.addStretch(1)

        _title_fs = "17px" if (index + 1) <= 6 else "11px"
        self._num_lbl = QLabel(f"{index+1}")
        self._num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._num_lbl.setStyleSheet(
            f"color:#ffffff;font-weight:bold;font-size:{_title_fs};"
            f"background:transparent;border:none;"
        )
        top_hdr.addWidget(self._num_lbl)

        self._param_name_lbl = QLabel("")
        self._param_name_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter)
        self._param_name_lbl.setStyleSheet(
            f"color:#ffffff;font-size:{_title_fs};font-weight:bold;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        self._param_name_lbl.setVisible(False)
        top_hdr.addWidget(self._param_name_lbl)
        top_hdr.addStretch(1)
        root.addLayout(top_hdr)

        # ── LFO / operator row: ‹ DISABLED › ──
        hdr = QHBoxLayout()
        hdr.setSpacing(3)

        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedSize(20, 20)
        self._prev_btn.setStyleSheet(
            f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:3px;color:{TEXT};font-size:12px;font-weight:bold;padding:0;}}"
            f"QPushButton:hover{{background:{DIM};}}"
        )
        self._prev_btn.clicked.connect(self._op_prev)
        hdr.addWidget(self._prev_btn)

        self._op_lbl = QLabel(OPERATORS[0][1])
        self._op_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._op_lbl.setStyleSheet(
            f"color:{TEXT};font-size:10px;background:{SURFACE2};"
            f"border:1px solid {BORDER};border-radius:3px;padding:2px 4px;"
        )
        hdr.addWidget(self._op_lbl, stretch=1)

        self._next_btn = QPushButton("›")
        self._next_btn.setFixedSize(20, 20)
        self._next_btn.setStyleSheet(
            f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:3px;color:{TEXT};font-size:12px;font-weight:bold;padding:0;}}"
            f"QPushButton:hover{{background:{DIM};}}"
        )
        self._next_btn.clicked.connect(self._op_next)
        hdr.addWidget(self._next_btn)

        self.op_combo = QComboBox()
        self.op_combo.setVisible(False)
        for op_id, op_name in OPERATORS:
            self.op_combo.addItem(op_name, op_id)

        # hdr not added to root — LFO row built at bottom of knob cards

        # ── Manual value ──
        is_fader = (index + 1) == 12
        if self._is_toggle:
            root.addStretch(1)
            val_row = QHBoxLayout()
            val_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.toggle = QPushButton("OFF")
            self.toggle.setCheckable(True)
            self.toggle.setFixedHeight(40)
            self.toggle.setMaximumWidth(80)
            self.toggle.setStyleSheet(f"""
                QPushButton {{
                    background:{SURFACE2}; border:2px solid {BORDER};
                    border-radius:5px; color:{TEXT_DIM};
                    font-size:11px; font-weight:bold; padding:2px 8px;
                }}
                QPushButton:checked {{
                    background:#7c3aed; border:2px solid #ffffff;
                    color:#ffffff;
                }}
            """)
            self.toggle.toggled.connect(self._on_toggle)
            val_row.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignCenter)
            root.addLayout(val_row)
            self.val_lbl = None
            self.slider  = None
            self.knob    = None
        elif is_fader:
            # P12 keeps a horizontal slider (will be replaced with vertical in tab)
            val_row = QHBoxLayout()
            val_row.setSpacing(4)
            m_lbl = QLabel("FADER")
            m_lbl.setStyleSheet(
                f"color:{ACCENT2};font-size:11px;font-weight:bold;min-width:14px;"
                f"background:transparent;border:none;"
            )
            val_row.addWidget(m_lbl)
            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(0, PARAM_RANGE)
            self.slider.setValue(0)
            self.slider.setFixedHeight(28)
            self.slider.valueChanged.connect(self._on_slide)
            val_row.addWidget(self.slider, stretch=1)
            self.val_lbl = QLabel("0%")
            self.val_lbl.setStyleSheet(
                f"color:{TEXT_DIM};font-size:12px;min-width:36px;"
                f"background:transparent;border:none;"
            )
            val_row.addWidget(self.val_lbl)
            root.addLayout(val_row)
            self.toggle = None
            self.knob   = None
        else:
            # P1-P6: top=name, knob, TSS, then LFO at bottom
            knob_row = QHBoxLayout()
            knob_row.setContentsMargins(0, 0, 0, 0)
            knob_row.setSpacing(4)
            knob_row.addStretch(1)
            _knob_spacer = QLabel("")
            _knob_spacer.setFixedWidth(36)
            _knob_spacer.setStyleSheet("background:transparent;border:none;")
            knob_row.addWidget(_knob_spacer)
            self.knob = KnobWidget(size=46)
            self.knob.setRange(0, PARAM_RANGE)
            self.knob.setValue(0)
            self.knob.valueChanged.connect(self._on_knob)
            knob_row.addWidget(self.knob)
            self.val_lbl = QLabel("0%")
            self.val_lbl.setFixedWidth(36)
            self.val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.val_lbl.setStyleSheet(
                f"color:{TEXT_DIM};font-size:12px;"
                f"background:transparent;border:none;"
            )
            knob_row.addWidget(self.val_lbl)
            knob_row.addStretch(1)
            root.addLayout(knob_row)
            self.slider = None
            self.toggle = None

            root.addSpacing(4)

            # TSS row — above LFO
            self._tss_labels = []
            self._tss_sliders = []
            tss_row = QHBoxLayout()
            tss_row.setSpacing(0)
            tss_row.addStretch(1)
            for field, lbl_txt in [("t", "Time"), ("sp", "Space"), ("sl", "Slope")]:
                col = QVBoxLayout()
                col.setAlignment(Qt.AlignmentFlag.AlignCenter)
                col.setSpacing(3)
                mini = KnobWidget(size=32)
                mini.setRange(0, PARAM_RANGE)
                mini.setValue(0)
                _field = field
                mini.valueChanged.connect(lambda v, f=_field: self._on_tss(f, v))
                self._tss_sliders.append(mini)
                col.addWidget(mini, alignment=Qt.AlignmentFlag.AlignCenter)
                lbl = QLabel(lbl_txt)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(
                    f"color:#ffffff;font-size:11px;font-weight:bold;"
                    f"background:transparent;border:none;"
                )
                self._tss_labels.append(lbl)
                col.addWidget(lbl)
                tss_row.addLayout(col)
                tss_row.addStretch(1)
            root.addLayout(tss_row)

            # LFO operator dropdown at the very bottom
            root.addSpacing(8)
            self.op_combo.setVisible(True)
            self.op_combo.setFixedHeight(26)
            self.op_combo.setMaximumWidth(180)
            self.op_combo.setStyleSheet(
                f"QComboBox{{background:{SURFACE2};border:1px solid {BORDER};"
                f"border-radius:2px;color:{TEXT};font-size:11px;padding:0px 4px;}}"
                f"QComboBox:hover{{border-color:{ACCENT};}}"
                f"QComboBox::drop-down{{border:none;width:12px;}}"
                f"QComboBox QAbstractItemView{{background:{SURFACE2};border:1px solid {BORDER};"
                f"color:{TEXT};selection-background-color:{DIM};font-size:11px;}}"
            )
            self.op_combo.currentIndexChanged.connect(self._on_op_changed)
            lfo_row2 = QHBoxLayout()
            lfo_row2.addStretch(1)
            lfo_row2.addWidget(self.op_combo)
            lfo_row2.addStretch(1)
            root.addLayout(lfo_row2)

            # Live modulation output bar
            root.addSpacing(6)
            self._out_bar = ModBar()
            self._out_bar.setFixedWidth(190)
            self._out_bar.setFixedHeight(70)
            bar_row = QHBoxLayout()
            bar_row.addStretch(1)
            bar_row.addWidget(self._out_bar)
            bar_row.addStretch(1)
            root.addLayout(bar_row)
            return  # skip old TSS section

        # ── TSS mini knobs for toggle/fader channels ──
        self._tss_labels = []
        self._tss_sliders = []

        root.addSpacing(6)
        tss_row = QHBoxLayout()
        tss_row.setSpacing(0)
        tss_row.addStretch(1)
        for field, lbl_txt in [("t", "Time"), ("sp", "Space"), ("sl", "Slope")]:
            col = QVBoxLayout()
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.setSpacing(2)
            mini = KnobWidget(size=24)
            mini.setRange(0, PARAM_RANGE)
            mini.setValue(0)
            _field = field
            mini.valueChanged.connect(lambda v, f=_field: self._on_tss(f, v))
            self._tss_sliders.append(mini)
            col.addWidget(mini, alignment=Qt.AlignmentFlag.AlignCenter)
            lbl = QLabel(lbl_txt)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color:#ffffff;font-size:11px;font-weight:bold;"
                f"background:transparent;border:none;"
            )
            col.addWidget(lbl)
            tss_row.addLayout(col)
            tss_row.addStretch(1)
        root.addLayout(tss_row)

        root.addSpacing(4)
        # LFO operator dropdown for toggle/fader channels
        self.op_combo.setVisible(True)
        self.op_combo.setFixedHeight(24)
        self.op_combo.setMaximumWidth(180)
        self.op_combo.setStyleSheet(
            f"QComboBox{{background:{SURFACE2};border:1px solid {BORDER};"
            f"border-radius:2px;color:{TEXT};font-size:11px;padding:0px 4px;}}"
            f"QComboBox:hover{{border-color:{ACCENT};}}"
            f"QComboBox::drop-down{{border:none;width:12px;}}"
            f"QComboBox QAbstractItemView{{background:{SURFACE2};border:1px solid {BORDER};"
            f"color:{TEXT};selection-background-color:{DIM};font-size:11px;}}"
        )
        self.op_combo.currentIndexChanged.connect(self._on_op_changed)
        lfo_row3 = QHBoxLayout()
        lfo_row3.addStretch(1)
        lfo_row3.addWidget(self.op_combo)
        lfo_row3.addStretch(1)
        root.addLayout(lfo_row3)

        # Live modulation output bar
        root.addSpacing(2)
        self._out_bar = ModBar()
        self._out_bar.setFixedWidth(140)
        self._out_bar.setFixedHeight(35)
        bar_row2 = QHBoxLayout()
        bar_row2.addStretch(1)
        bar_row2.addWidget(self._out_bar)
        bar_row2.addStretch(1)
        root.addLayout(bar_row2)

    def set_param_label(self, name: str, lo: int = 0, hi: int = 100):
        """Set parameter name — shown below knob or above toggle button."""
        self.set_param_info({"name": name, "min": lo, "max": hi})

    def set_param_info(self, p: dict):
        """Apply full parameter info — name, min, max, step, values, etc."""
        name = p.get("name", "")
        self._param_min = p.get("min", 0)
        self._param_max = p.get("max", 100)
        self._param_step = p.get("step", 0)
        self._param_values = p.get("values", p.get("enum", []))
        self._param_type = p.get("type", "")

        if not hasattr(self, '_param_name_lbl'):
            return
        if name and name not in (f"P{self.index+1}", f"{self.index+1}", ""):
            self._num_lbl.setText(f"{self.index+1} -")
            self._param_name_lbl.setText(name.upper())
            self._param_name_lbl.setVisible(True)
        else:
            self._num_lbl.setText(f"{self.index+1}")
            self._param_name_lbl.setVisible(False)

    def _format_value(self, raw: int) -> str:
        """Format a raw 0-1023 value — always smooth 0-100%."""
        return f"{round(raw / 10.23)}%"

    def set_manual(self, value: int, silent: bool = True):
        self._updating = silent
        pct = self._format_value(value)
        if self._is_toggle:
            on = value > 0
            self.toggle.setChecked(on)
            self.toggle.setText("ON" if on else "OFF")
        elif self.knob:
            if self.knob._user_dragging:
                self._updating = False
                return  # never touch knob during active drag
            self.knob.blockSignals(silent)
            self.knob.setValue(value)
            self.knob.blockSignals(False)
            if self.val_lbl:
                self.val_lbl.setText(pct)
        elif self.slider:
            if hasattr(self.slider, '_dragging') and self.slider._dragging:
                self._updating = False
                return  # never touch fader during active drag
            if hasattr(self.slider, 'setValue') and hasattr(self.slider, '_display_val'):
                self.slider.setValue(value, animate=True)
            else:
                self.slider.setValue(value)
            if self.val_lbl:
                self.val_lbl.setText(pct)
        self._updating = False

    def _op_prev(self):
        self._op_index = (self._op_index - 1) % len(OPERATORS)
        self._apply_op_index()

    def _op_next(self):
        self._op_index = (self._op_index + 1) % len(OPERATORS)
        self._apply_op_index()

    def _apply_op_index(self):
        op_id, op_name = OPERATORS[self._op_index]
        self._op_lbl.setText(op_name)
        self._update_tss_labels(op_id)
        self._update_tss_enabled(op_id)
        self.op_combo.blockSignals(True)
        self.op_combo.setCurrentIndex(self._op_index)
        self.op_combo.blockSignals(False)
        if not self._updating and self.on_mod_change:
            self.on_mod_change(self.index, "sr", op_id)

    def set_operator(self, op_id: int, silent: bool = True):
        self._updating = silent
        idx = next((i for i, (oid, _) in enumerate(OPERATORS) if oid == op_id), 0)
        self._op_index = idx
        self._op_lbl.setText(OPERATORS[idx][1])
        self._update_tss_labels(op_id)
        self._update_tss_enabled(op_id)
        self.op_combo.blockSignals(True)
        self.op_combo.setCurrentIndex(idx)
        self.op_combo.blockSignals(False)
        self._updating = False

    def set_tss(self, t: int, sp: int, sl: int, silent: bool = True):
        if not self._tss_sliders:
            return
        self._updating = silent
        for knob, val in zip(self._tss_sliders, (t, sp, sl)):
            if knob._user_dragging:
                continue
            val = max(knob._min, min(knob._max, int(val)))
            if val == knob._value:
                continue
            knob._value = val
            knob._frac = (val - knob._min) / max(1, knob._max - knob._min)
            # Use slower glide for TSS — spread over full poll interval
            if not knob._smooth_timer.isActive():
                knob._smooth_timer.start()
        self._updating = False

    def _on_tss(self, field: str, value: int):
        if self._updating:
            return
        if self.on_mod_change:
            self.on_mod_change(self.index, field, value)
        else:
            print(f"[TSS] on_mod_change not set for P{self.index+1} {field}={value}")

    def set_output(self, value: int):
        """Show the actual output value via the modulation bar."""
        if hasattr(self, "_out_bar"):
            self._out_bar.setValue(value)
            self._out_bar.setActive(True)

    def get_manual(self) -> int:
        if self._is_toggle:
            return PARAM_RANGE if self.toggle.isChecked() else 0
        if self.knob:
            return self.knob.value()
        return self.slider.value() if self.slider else 0

    def get_operator(self) -> int:
        return OPERATORS[self._op_index][0]

    def get_tss(self):
        if self._tss_sliders:
            return (self._tss_sliders[0].value(),
                    self._tss_sliders[1].value(),
                    self._tss_sliders[2].value())
        return (0, 0, 0)

    def set_enabled_controls(self, v: bool):
        if self.slider:
            self.slider.setEnabled(v)
        if hasattr(self, "knob") and self.knob:
            self.knob.setEnabled(v)
        if self.toggle:
            self.toggle.setEnabled(v)
        self._prev_btn.setEnabled(v)
        self._next_btn.setEnabled(v)

    def _update_tss_labels(self, op_id: int):
        labels = OP_LABELS.get(op_id, ("Time", "Space", "Slope"))
        for i, lbl in enumerate(self._tss_labels):
            lbl.setText(labels[i])

    def _update_tss_enabled(self, op_id: int):
        """Style the operator combo to indicate active LFO vs disabled."""
        active = op_id != 0
        # ModBar stays active — shows output regardless of operator
        if active:
            # Lit up — purple background, white text
            self.op_combo.setStyleSheet(
                f"QComboBox{{background:#7c3aed;border:1px solid #a855f7;"
                f"border-radius:2px;color:#ffffff;font-size:11px;font-weight:bold;padding:0px 4px;}}"
                f"QComboBox:hover{{border-color:#ffffff;}}"
                f"QComboBox::drop-down{{border:none;width:12px;}}"
                f"QComboBox QAbstractItemView{{background:{SURFACE2};border:1px solid {BORDER};"
                f"color:{TEXT};selection-background-color:{DIM};font-size:11px;}}"
            )
        else:
            # Disabled state — dim, blends into card
            self.op_combo.setStyleSheet(
                f"QComboBox{{background:{SURFACE2};border:1px solid {BORDER};"
                f"border-radius:2px;color:{TEXT_DIM};font-size:11px;padding:0px 4px;}}"
                f"QComboBox:hover{{border-color:{ACCENT};}}"
                f"QComboBox::drop-down{{border:none;width:12px;}}"
                f"QComboBox QAbstractItemView{{background:{SURFACE2};border:1px solid {BORDER};"
                f"color:{TEXT};selection-background-color:{DIM};font-size:11px;}}"
            )

    def _on_op_changed(self, _idx):
        op_id = self.op_combo.currentData()
        self._update_tss_labels(op_id)
        self._update_tss_enabled(op_id)
        if not self._updating and self.on_mod_change:
            # Map op_id (0-38) to 0-1023 range
            val = int(op_id / 38 * PARAM_RANGE)
            self.on_mod_change(self.index, "sr", op_id)

    def _on_knob(self, value: int):
        if self._updating:
            return
        if self.val_lbl:
            self.val_lbl.setText(self._format_value(value))
        if self.on_manual_change:
            self.on_manual_change(self.index, value)

    def _on_slide(self, value: int):
        if self._updating:
            return
        if self.val_lbl:
            self.val_lbl.setText(self._format_value(value))
        if self.on_manual_change:
            self.on_manual_change(self.index, value)

    def _on_toggle(self, checked):
        if self._updating:
            return
        value = PARAM_RANGE if checked else 0
        self.toggle.setText("ON" if checked else "OFF")
        if self.on_manual_change:
            self.on_manual_change(self.index, value)



# ── Parameters tab ─────────────────────────────────────────────────────

class ParametersTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = False
        self._last_sent: dict = {}   # deduplication: key → last sent value

        self.on_param_change = None   # (index, value)
        self.on_mod_change   = None   # (index, field, value)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # Keep stubs so external calls don't crash
        self.refresh_btn = QPushButton()
        self.refresh_btn.setVisible(False)
        self.prog_lbl = QLabel()
        self.prog_lbl.setVisible(False)

        # ── Transport bar ── inline title + controls, no QGroupBox chrome
        transport_grp = QWidget()
        transport_grp.setStyleSheet(
            f"QWidget{{background:{SURFACE};border:1px solid {BORDER};border-radius:6px;}}"
        )
        tl = QHBoxLayout(transport_grp)
        tl.setContentsMargins(12, 8, 12, 8)
        tl.setSpacing(6)

        transport_title = QLabel("MOTION")
        transport_title.setStyleSheet(
            f"color:#ffffff;font-size:12px;font-weight:bold;letter-spacing:2px;"
            f"background:transparent;border:none;"
        )
        tl.addWidget(transport_title)

        tl.addSpacing(4)

        self.tap_btn = QPushButton("◉  TAP")
        self.tap_btn.setEnabled(False)
        self.tap_btn.setFixedHeight(30)
        self._tap_base_style = (
            f"QPushButton{{background:{SURFACE2};border:2px solid {BORDER};"
            f"border-radius:4px;color:{TEXT};font-weight:bold;padding:7px 18px;}}"
            f"QPushButton:pressed{{background:{SURFACE2};border:2px solid {BORDER};color:{TEXT};}}"
        )
        self.tap_btn.setStyleSheet(self._tap_base_style)
        self.tap_btn.clicked.connect(lambda: self._transport("tap"))
        tl.addWidget(self.tap_btn)

        tl.addSpacing(6)

        bpm_lbl = QLabel("BPM")
        bpm_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;background:transparent;border:none;")
        tl.addWidget(bpm_lbl)

        self.bpm_slider = HorizontalFader()
        self.bpm_slider.setRange(2000, 30000)   # 20.00 – 300.00 BPM ×100
        self.bpm_slider.setValue(12000)
        self.bpm_slider.setFixedWidth(140)
        self.bpm_slider.setEnabled(False)
        self.bpm_slider.valueChanged.connect(self._on_bpm_slider)
        tl.addWidget(self.bpm_slider)

        self.bpm_display = QLabel("120.0")
        self.bpm_display.setStyleSheet(
            f"color:#ffffff;font-size:14px;font-weight:bold;min-width:50px;"
            f"background:transparent;border:none;"
        )
        tl.addWidget(self.bpm_display)

        tl.addSpacing(6)

        self.stop_btn = QPushButton("◼  STOP")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setFixedHeight(30)
        self.stop_btn.clicked.connect(lambda: self._transport("stop"))
        tl.addWidget(self.stop_btn)

        self.start_btn = QPushButton("▶  PLAY")
        self.start_btn.setObjectName("primary")
        self.start_btn.setEnabled(False)
        self.start_btn.setFixedHeight(30)
        self.start_btn.clicked.connect(lambda: self._transport("start"))
        tl.addWidget(self.start_btn)

        tl.addStretch()

        root.addWidget(transport_grp)
        root.addSpacing(4)

        # ── Motion panel ──────────────────────────────────────────
        # Per manual: P1-P11 are parameter knobs, P12 is crossfader
        # Time/Space/Slope are GLOBAL macro controls (one set total)
        self.channels: List[ChannelCard] = []

        # ── Main panel: [P1-P6 | P12 fader] over [P7-P11 full width] ──
        panel = QWidget()
        panel.setStyleSheet(f"background:{BG};border:none;")
        panel_v = QVBoxLayout(panel)
        panel_v.setContentsMargins(0, 0, 0, 0)
        panel_v.setSpacing(10)

        # Top row: P1-P6 knobs (left) + P12 fader (right)
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        # P1-P6 parameter knobs — 3+3 grid matching front panel
        knobs_grid = QGridLayout()
        knobs_grid.setHorizontalSpacing(6)
        knobs_grid.setVerticalSpacing(6)
        for col in range(3):
            knobs_grid.setColumnStretch(col, 1)
        knobs_grid.setRowStretch(0, 1)
        knobs_grid.setRowStretch(1, 1)
        for i in range(6):
            card = ChannelCard(i)
            card.setMinimumHeight(200)
            card.setMaximumHeight(380)
            card.on_manual_change = self._manual_changed
            card.on_mod_change    = self._mod_changed
            self.channels.append(card)
            row, col = divmod(i, 3)
            knobs_grid.addWidget(card, row, col)
        top_row.addLayout(knobs_grid, stretch=5)

        # Right: P12 vertical fader — narrow, full height of knobs row
        fader_widget = QWidget()
        fader_widget.setMaximumWidth(118)
        fader_widget.setStyleSheet(f"""
            QWidget {{
                background:{SURFACE};
                border:1px solid {BORDER};
                border-radius:12px;
            }}
        """)
        fader_v = QVBoxLayout(fader_widget)
        fader_v.setContentsMargins(8, 8, 8, 8)
        fader_v.setSpacing(4)

        self._p12_name_lbl = QLabel("")
        self._p12_name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._p12_name_lbl.setStyleSheet(
            f"color:#ffffff;font-size:13px;font-weight:bold;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        fader_v.addWidget(self._p12_name_lbl)
        fader_v.addSpacing(2)

        # Create P12 ChannelCard but use its slider vertically
        card12 = ChannelCard(11)
        card12.on_manual_change = self._manual_changed
        card12.on_mod_change    = self._mod_changed
        card12.setStyleSheet("background:transparent;border:none;")

        # Replace its horizontal slider with a smooth vertical fader
        if card12.slider:
            card12.slider.setParent(None)
            vert_slider = SmoothFader()
            vert_slider.setRange(0, PARAM_RANGE)
            vert_slider.setValue(0, animate=False)
            vert_slider.valueChanged.connect(card12._on_slide)
            card12.slider = vert_slider
            fader_v.addWidget(vert_slider, stretch=1,
                              alignment=Qt.AlignmentFlag.AlignHCenter)
        fader_v.addSpacing(6)

        self.val_lbl_12 = QLabel("0%")
        self.val_lbl_12.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.val_lbl_12.setStyleSheet(
            f"color:#ffffff;font-size:12px;font-weight:bold;"
            f"background:transparent;border:none;"
        )
        fader_v.addWidget(self.val_lbl_12)

        # Wire val_lbl_12 to update when slider moves
        if card12.slider:
            card12.slider.valueChanged.connect(
                lambda v: self.val_lbl_12.setText(card12._format_value(v))
            )

        # TSS knobs for P12 — replace card's hidden ones with visible ones
        card12._tss_sliders = []
        card12._tss_labels = []
        fader_v.addSpacing(4)
        tss12_row = QHBoxLayout()
        tss12_row.setSpacing(0)
        tss12_row.addStretch(1)
        for field, lbl_txt in [("t", "Time"), ("sp", "Space"), ("sl", "Slope")]:
            col12 = QVBoxLayout()
            col12.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col12.setSpacing(2)
            mini12 = KnobWidget(size=28)
            mini12.setRange(0, PARAM_RANGE)
            mini12.setValue(0)
            _f = field
            mini12.valueChanged.connect(lambda v, f=_f: card12._on_tss(f, v))
            card12._tss_sliders.append(mini12)
            col12.addWidget(mini12, alignment=Qt.AlignmentFlag.AlignCenter)
            tss_lbl12 = QLabel(lbl_txt)
            tss_lbl12.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tss_lbl12.setStyleSheet(
                f"color:#ffffff;font-size:11px;font-weight:bold;"
                f"background:transparent;border:none;"
            )
            card12._tss_labels.append(tss_lbl12)
            col12.addWidget(tss_lbl12)
            tss12_row.addLayout(col12)
            tss12_row.addStretch(1)
        fader_v.addLayout(tss12_row)
        fader_v.addSpacing(2)

        # Op combo for P12
        card12.op_combo.setVisible(True)
        card12.op_combo.setFixedHeight(26)
        card12.op_combo.setMaximumWidth(180)
        card12.op_combo.setStyleSheet(
            f"QComboBox{{background:{SURFACE2};border:1px solid {BORDER};"
            f"border-radius:2px;color:{TEXT};font-size:11px;padding:0px 4px;}}"
            f"QComboBox:hover{{border-color:{ACCENT};}}"
            f"QComboBox::drop-down{{border:none;width:12px;}}"
            f"QComboBox QAbstractItemView{{background:{SURFACE2};border:1px solid {BORDER};"
            f"color:{TEXT};selection-background-color:{DIM};font-size:11px;}}"
        )
        lfo_row12 = QHBoxLayout()
        lfo_row12.addStretch(1)
        lfo_row12.addWidget(card12.op_combo)
        lfo_row12.addStretch(1)
        fader_v.addLayout(lfo_row12)

        # LFO waveform for P12
        fader_v.addSpacing(2)
        card12._out_bar = ModBar()
        card12._out_bar.setFixedWidth(90)
        card12._out_bar.setMinimumHeight(40)
        fader_v.addWidget(card12._out_bar, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Wrap fader in a column that extends 80px below the knobs row
        fader_col = QVBoxLayout()
        fader_col.addWidget(fader_widget, stretch=1)
        top_row.addLayout(fader_col, stretch=1)
        panel_v.addLayout(top_row, stretch=1)
        panel_v.addSpacing(4)

        # P7-P11 switches — full width to edges
        sw_container = QWidget()
        sw_container.setStyleSheet(f"""
            QWidget#sw_container {{
                background: {SURFACE};
                border: 2px solid {BORDER};
                border-radius: 8px;
            }}
        """)
        sw_container.setObjectName("sw_container")
        sw_inner = QHBoxLayout(sw_container)
        sw_inner.setContentsMargins(8, 8, 8, 10)
        sw_inner.setSpacing(6)
        for i in range(6, 11):
            card = ChannelCard(i)
            card.setMinimumHeight(180)
            card.setMaximumHeight(280)
            card.setStyleSheet("QWidget { background: transparent; border: none; }")
            card.on_manual_change = self._manual_changed
            card.on_mod_change    = self._mod_changed
            self.channels.append(card)
            sw_inner.addWidget(card, stretch=1)
            if i < 10:
                div = QFrame()
                div.setFrameShape(QFrame.Shape.VLine)
                div.setStyleSheet(f"background:{BORDER};max-width:1px;border:none;min-height:0px;")
                div.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
                sw_inner.addWidget(div)
        panel_v.addWidget(sw_container, stretch=0)

        # Append P12 last so channels list order matches device: P1-P11, P12
        self.channels.append(card12)

        root.addWidget(panel, stretch=1)

        self._set_enabled(False)

    # ------------------------------------------------------------------

    def set_connected(self, v: bool):
        self._connected = v
        self._set_enabled(v)
        self.refresh_btn.setEnabled(v)
        self.start_btn.setEnabled(v)
        self.stop_btn.setEnabled(v)
        self.tap_btn.setEnabled(v)
        self.bpm_slider.setEnabled(v)
        if not v:
            if hasattr(self, '_p12_name_lbl'):
                self._p12_name_lbl.setText("")
            # Reset all channels to zero and clear param names
            for card in self.channels:
                card.set_manual(0, silent=True)
                card.set_output(0)
                card._num_lbl.setText(f"{card.index+1}")
                if hasattr(card, '_param_name_lbl'):
                    card._param_name_lbl.setVisible(False)
                if hasattr(card, '_tss_sliders') and card._tss_sliders:
                    card.set_tss(0, 0, 0, silent=True)

    def set_program(self, name: str):
        self.prog_lbl.setText(name or "No program loaded")

    def apply_param_labels(self, params: list):
        """Update channel card labels from program info parameter names."""
        for i, card in enumerate(self.channels):
            if i < len(params):
                p = params[i]
                card.set_param_info(p)
            else:
                card.set_param_info({"name": "", "min": 0, "max": 100})
        # Update the P12 fader title from param name
        if hasattr(self, '_p12_name_lbl'):
            if 11 < len(params):
                name = params[11].get("name", "INTENSITY")
                self._p12_name_lbl.setText(name.upper())
            else:
                self._p12_name_lbl.setText("INTENSITY")

    def set_tss_panel(self, ch: int, t: int, sp: int, sl: int):
        """Update per-card TSS sliders from device state."""
        if ch < len(self.channels):
            self.channels[ch].set_tss(t, sp, sl, silent=True)

    def apply_state(self, m: list, t: list, sp: list, sl: list, sr: list):
        """Apply full state including TSS panel — skip recently edited channels."""
        now = __import__('time').monotonic()
        for i in range(min(12, len(m))):
            last_edit = self._last_sent.get(f"edit_time_{i}", 0)
            if now - last_edit < 3.0:
                continue
            card = self.channels[i]
            card.set_manual(m[i] if i < len(m) else 0)
            card.set_operator(sr[i] if i < len(sr) else 0)
            self.set_tss_panel(i,
                t[i]  if i < len(t)  else 0,
                sp[i] if i < len(sp) else 0,
                sl[i] if i < len(sl) else 0,
            )

    def apply_modulation_status(self, modulators: list):
        """Apply state from modulation status — skip unchanged and recently edited."""
        now = __import__('time').monotonic()
        # Cache previous state to skip unchanged channels
        if not hasattr(self, '_prev_mod'):
            self._prev_mod = [None] * 12
        for i, mod in enumerate(modulators[:12]):
            # Skip if this channel's data is identical to last poll
            if mod == self._prev_mod[i]:
                continue
            self._prev_mod[i] = mod

            card = self.channels[i]
            last_edit = self._last_sent.get(f"edit_time_{i}", 0)
            recently_edited = now - last_edit < 3.0
            m = mod.get("m", 0)
            if card._is_toggle:
                card.set_manual(m)
                s = mod.get("s", 0)
                if s != card.get_operator():
                    card.set_operator(s)
                if "o" in mod:
                    card.set_output(mod["o"])
            else:
                o = mod.get("o", m)
                if not recently_edited:
                    card.set_manual(m)
                    s = mod.get("s", 0)
                    if s != card.get_operator():
                        card.set_operator(s)
                # Always update output bar — shows live modulation even during edits
                card.set_output(o)

            # Update TSS from fast poll if present
            if "t" in mod or "sp" in mod or "sl" in mod:
                t_val  = mod.get("t", 0)
                sp_val = mod.get("sp", 0)
                sl_val = mod.get("sl", 0)
                card.set_tss(t_val, sp_val, sl_val, silent=True)

    def set_transport_state(self, state: str):
        """Update play/stop button styling based on transport state."""
        playing = state.lower() in ("playing", "running", "started")
        if playing:
            self.start_btn.setStyleSheet(
                f"QPushButton{{background:#7c3aed;border:2px solid #a855f7;"
                f"color:#ffffff;font-weight:bold;border-radius:4px;padding:7px 18px;}}"
            )
            self.stop_btn.setStyleSheet("")
        else:
            self.stop_btn.setStyleSheet(
                f"QPushButton{{background:#7c3aed;border:2px solid #a855f7;"
                f"color:#ffffff;font-weight:bold;border-radius:4px;padding:7px 18px;}}"
            )
            self.start_btn.setStyleSheet("")
            self.start_btn.setObjectName("primary")
            self.start_btn.style().polish(self.start_btn)

    def flash_tap(self):
        """Briefly light up the TAP button — color only, no bounce."""
        self.tap_btn.setStyleSheet(
            f"QPushButton{{background:#c040c0;border:2px solid #ffffff;"
            f"border-radius:4px;color:#ffffff;font-weight:bold;padding:7px 18px;}}"
            f"QPushButton:pressed{{background:#c040c0;border:2px solid #ffffff;color:#ffffff;}}"
        )
        QTimer.singleShot(80, self._reset_tap_style)

    def _reset_tap_style(self):
        self.tap_btn.setStyleSheet(self._tap_base_style)

    def set_bpm(self, bpm: float, bpm_x100: int = None):
        self.bpm_display.setText(f"{bpm:.2f}")
        if bpm_x100 is not None:
            # Flash tap button when BPM changes from device
            old = self.bpm_slider.value()
            if old != bpm_x100:
                self.flash_tap()
            self.bpm_slider.blockSignals(True)
            self.bpm_slider.setValue(bpm_x100)
            self.bpm_slider.blockSignals(False)

    def _set_enabled(self, v: bool):
        for card in self.channels:
            card.set_enabled_controls(v)

    def _manual_changed(self, index: int, value: int):
        """Direct send — no timer, deduplicate by tracking last sent value."""
        last = self._last_sent.get(f"m{index}")
        if last == value:
            return
        self._last_sent[f"m{index}"] = value
        self._last_sent[f"edit_time_{index}"] = __import__('time').monotonic()
        if self.on_param_change:
            self.on_param_change(index, value)

    def _mod_changed(self, index: int, field: str, value: int):
        """Direct send — no timer, deduplicate."""
        key = f"{field}{index}"
        last = self._last_sent.get(key)
        if last == value:
            return
        self._last_sent[key] = value
        self._last_sent[f"edit_time_{index}"] = __import__('time').monotonic()
        if self.on_mod_change:
            self.on_mod_change(index, field, value)

    def _flush_manual(self, index: int):
        value = self.channels[index].get_manual()
        if self.on_param_change:
            self.on_param_change(index, value)

    def _flush_mod(self, index: int, field: str):
        card = self.channels[index]
        if field == "sr":
            value = card.get_operator()
        else:
            t, sp, sl = card.get_tss()
            value = {"t": t, "sp": sp, "sl": sl}[field]
        if self.on_mod_change:
            self.on_mod_change(index, field, value)

    def _on_bpm_slider(self, val):
        self.bpm_display.setText(f"{val/100:.2f}")
        mw = self.window()
        if hasattr(mw, "_worker") and mw._worker:
            mw._worker.send(f"transport bpm {val}")

    def _transport(self, action: str):
        if action == "tap":
            self.flash_tap()
        mw = self.window()
        if hasattr(mw, "_send_transport"):
            mw._send_transport(action)

    def _on_refresh(self):
        mw = self.window()
        if hasattr(mw, "_request_state"):
            mw._request_state()


# ── Presets tab ────────────────────────────────────────────────────────

class PresetsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = False
        self._factory: List[dict] = []
        self._user: List[dict] = []

        # callbacks
        self.on_apply   = None   # (index, type_str)
        self.on_save    = None   # (index, name)
        self.on_delete  = None   # (index)
        self.on_rename  = None   # (index, new_name)
        self.on_refresh = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Top bar
        top = QHBoxLayout()
        self.prog_lbl = QLabel("No program loaded")
        self.prog_lbl.setStyleSheet(
            f"color:{ACCENT};font-size:13px;font-weight:bold;"
        )
        top.addWidget(self.prog_lbl)
        top.addStretch()

        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(lambda: self.on_refresh and self.on_refresh())
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        root.addWidget(hsep())

        # Splitter: factory | user
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Factory
        fac_grp = QGroupBox("Factory Presets  (read-only)")
        fl = QVBoxLayout(fac_grp)
        fl.setContentsMargins(6, 6, 6, 6)
        self.factory_list = QListWidget()
        fl.addWidget(self.factory_list)
        apply_fac_btn = QPushButton("Apply Selected")
        apply_fac_btn.clicked.connect(self._apply_factory)
        fl.addWidget(apply_fac_btn)
        splitter.addWidget(fac_grp)

        # User
        usr_grp = QGroupBox("User Presets")
        ul = QVBoxLayout(usr_grp)
        ul.setContentsMargins(6, 6, 6, 6)
        self.user_list = QListWidget()
        ul.addWidget(self.user_list)

        btn_row = QHBoxLayout()
        apply_usr_btn = QPushButton("Apply")
        apply_usr_btn.clicked.connect(self._apply_user)
        btn_row.addWidget(apply_usr_btn)

        save_btn = QPushButton("Save Current")
        save_btn.clicked.connect(self._save_preset)
        btn_row.addWidget(save_btn)

        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(self._rename_preset)
        btn_row.addWidget(rename_btn)

        del_btn = QPushButton("Delete")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._delete_preset)
        btn_row.addWidget(del_btn)

        ul.addLayout(btn_row)

        self.flash_lbl = QLabel("")
        self.flash_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;")
        ul.addWidget(self.flash_lbl)

        splitter.addWidget(usr_grp)
        root.addWidget(splitter, stretch=1)

    def set_connected(self, v: bool):
        self._connected = v
        self.refresh_btn.setEnabled(v)

    def set_program(self, name: str):
        self.prog_lbl.setText(name or "No program loaded")

    def populate(self, factory: list, user: list, flash_free: int = 0):
        self._factory = factory
        self._user    = user

        self.factory_list.clear()
        for i, p in enumerate(factory):
            item = QListWidgetItem(p.get("n", f"Factory {i}"))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.factory_list.addItem(item)

        self.user_list.clear()
        for i, p in enumerate(user):
            item = QListWidgetItem(p.get("n", f"User {i}"))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.user_list.addItem(item)

        if flash_free:
            kb = flash_free // 1024
            self.flash_lbl.setText(f"Flash free: {kb} KB")

    def _apply_factory(self):
        items = self.factory_list.selectedItems()
        if not items:
            return
        idx = items[0].data(Qt.ItemDataRole.UserRole)
        if self.on_apply:
            self.on_apply(idx, "factory")

    def _apply_user(self):
        items = self.user_list.selectedItems()
        if not items:
            return
        idx = items[0].data(Qt.ItemDataRole.UserRole)
        if self.on_apply:
            self.on_apply(idx, "user")

    def _save_preset(self):
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:", text="My Preset"
        )
        if ok and name.strip():
            # Use next available user slot
            idx = len(self._user)
            if self.on_save:
                self.on_save(idx, name.strip())

    def _rename_preset(self):
        items = self.user_list.selectedItems()
        if not items:
            return
        idx  = items[0].data(Qt.ItemDataRole.UserRole)
        old  = items[0].text()
        name, ok = QInputDialog.getText(
            self, "Rename Preset", "New name:", text=old
        )
        if ok and name.strip() and self.on_rename:
            self.on_rename(idx, name.strip())

    def _delete_preset(self):
        items = self.user_list.selectedItems()
        if not items:
            return
        idx  = items[0].data(Qt.ItemDataRole.UserRole)
        name = items[0].text()
        reply = QMessageBox.question(
            self, "Delete Preset",
            f'Delete user preset "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self.on_delete:
            self.on_delete(idx)


# ── Serial console ─────────────────────────────────────────────────────

class ConsoleWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)



        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumHeight(60)
        self.text.setMinimumHeight(60)
        self.text.setFixedHeight(60)
        lay.addWidget(self.text)

        self._fmts = {
            "ok":    self._fmt("#aaaaaa"),
            "error": self._fmt(ERROR),
            "cmd":   self._fmt("#ffffff"),
            "log":   self._fmt("#666666"),
        }

    def _copy_all(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text.toPlainText())

    def _fmt(self, color: str):
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        return f

    def append(self, prefix: str, key: str, payload: str):
        # Skip high-frequency modulation logs to prevent event loop flooding
        if key == "modulation" and prefix == "ok":
            return
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        ts   = time.strftime("%H:%M:%S")
        fmt  = self._fmts.get(prefix, self._fmts["log"])
        if prefix == "ok":
            line = f"[{ts}] @{key}: {payload[:120]}"
        elif prefix == "error":
            line = f"[{ts}] !{key}: {payload[:120]}"
        elif prefix == "cmd":
            line = f"[{ts}] > {key}"
        else:
            line = f"[{ts}]  {payload[:120]}"
        cursor.insertText(line + "\n", fmt)
        self.text.setTextCursor(cursor)
        self.text.ensureCursorVisible()


# ── Snapshot manager ──────────────────────────────────────────────────

class SnapshotManager:
    """
    Saves and loads Videomancer state snapshots as JSON files.
    Default folder: ~/Documents/VideomancerSnapshots/
    """

    def __init__(self, folder: Optional[Path] = None):
        self.folder = folder or (
            Path.home() / "Documents" / "VideomancerSnapshots"
        )
        self.folder.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def save(self, label: str, program: str, parameters: List[int],
             presets: dict, settings: dict, tss: dict = None) -> Path:
        """Write a snapshot file and return its path."""
        ts   = datetime.now()
        slug = re.sub(r"[^\w\-]", "_", label.strip())[:40] or "snapshot"
        fname = f"{ts.strftime('%Y%m%d_%H%M%S')}_{slug}.json"
        data  = {
            "version":    2,
            "timestamp":  ts.isoformat(),
            "label":      label.strip(),
            "program":    program,
            "parameters": parameters,
            "presets":    presets,
            "settings":   settings,
        }
        if tss:
            data["t"]  = tss.get("t",  [0]*12)
            data["sp"] = tss.get("sp", [0]*12)
            data["sl"] = tss.get("sl", [0]*12)
            data["sr"] = tss.get("sr", [0]*12)
        path = self.folder / fname
        path.write_text(json.dumps(data, indent=2))
        return path

    def list_snapshots(self) -> List[dict]:
        """Return metadata for all snapshots, newest first."""
        results = []
        for p in sorted(self.folder.glob("*.json"), reverse=True):
            try:
                data = json.loads(p.read_text())
                results.append({
                    "path":      p,
                    "label":     data.get("label", p.stem),
                    "program":   data.get("program", "—"),
                    "timestamp": data.get("timestamp", ""),
                    "data":      data,
                })
            except Exception:
                pass
        return results

    def load(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def delete(self, path: Path):
        path.unlink(missing_ok=True)

    def open_folder(self):
        """Open the snapshots folder in Finder (macOS)."""
        os.system(f'open "{self.folder}"')


# ── Snapshots tab ──────────────────────────────────────────────────────

class SnapshotsTab(QWidget):
    """
    Save / browse / restore full device state snapshots.
    Each snapshot captures: program, 12 parameters, user presets, settings.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected   = False
        self._manager     = SnapshotManager()
        self._snapshots: List[dict] = []

        # callbacks set by main window
        self.on_capture  = None   # () → triggers data collection
        self.on_restore  = None   # (snapshot_dict)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ── Top bar ──
        top = QHBoxLayout()

        self.save_btn = QPushButton("⬤  Save Snapshot")
        self.save_btn.setObjectName("primary")
        self.save_btn.setFixedHeight(34)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._on_save)
        top.addWidget(self.save_btn)

        top.addSpacing(6)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Snapshot label  (optional)")
        self.label_edit.setFixedHeight(34)
        top.addWidget(self.label_edit, stretch=1)

        top.addSpacing(6)

        folder_btn = QPushButton("📁  Open Folder")
        folder_btn.setFixedHeight(34)
        folder_btn.clicked.connect(lambda: self._manager.open_folder())
        top.addWidget(folder_btn)

        root.addLayout(top)
        root.addWidget(hsep())

        # ── Snapshot list + detail ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: list
        left_grp = QGroupBox("Saved Snapshots")
        ll = QVBoxLayout(left_grp)
        ll.setContentsMargins(6, 6, 6, 6)
        ll.setSpacing(6)

        self.snap_list = QListWidget()
        self.snap_list.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.snap_list, stretch=1)

        btn_row = QHBoxLayout()
        self.refresh_list_btn = QPushButton("↻  Refresh")
        self.refresh_list_btn.clicked.connect(self.reload_list)
        btn_row.addWidget(self.refresh_list_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self.delete_btn)
        ll.addLayout(btn_row)

        folder_lbl = QLabel(str(self._manager.folder))
        folder_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:9px;"
        )
        folder_lbl.setWordWrap(True)
        ll.addWidget(folder_lbl)

        splitter.addWidget(left_grp)

        # Right: detail + restore
        right_grp = QGroupBox("Snapshot Detail")
        rl = QVBoxLayout(right_grp)
        rl.setContentsMargins(12, 12, 12, 12)
        rl.setSpacing(8)

        self.detail_label = QLabel("—")
        self.detail_label.setStyleSheet(
            f"color:{ACCENT};font-size:16px;font-weight:bold;"
        )
        self.detail_label.setWordWrap(True)
        rl.addWidget(self.detail_label)

        self.detail_prog = QLabel("")
        self.detail_prog.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        rl.addWidget(self.detail_prog)

        self.detail_ts = QLabel("")
        self.detail_ts.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;")
        rl.addWidget(self.detail_ts)

        self.detail_params = QLabel("")
        self.detail_params.setStyleSheet(
            f"color:{TEXT_DIM};font-size:10px;"
        )
        self.detail_params.setWordWrap(True)
        rl.addWidget(self.detail_params)

        rl.addSpacing(8)

        self.restore_btn = QPushButton("⬤  RESTORE TO DEVICE")
        self.restore_btn.setObjectName("primary")
        self.restore_btn.setFixedHeight(36)
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self._on_restore)
        rl.addWidget(self.restore_btn)

        self.restore_note = QLabel(
            "Restores: program → parameters → user presets → settings"
        )
        self.restore_note.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;")
        self.restore_note.setWordWrap(True)
        rl.addWidget(self.restore_note)

        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet(f"color:{WARN};font-size:10px;")
        rl.addWidget(self.progress_lbl)

        rl.addStretch()
        splitter.addWidget(right_grp)
        splitter.setSizes([320, 280])
        root.addWidget(splitter, stretch=1)

        self.reload_list()

    # ------------------------------------------------------------------

    def set_connected(self, v: bool):
        self._connected = v
        self.save_btn.setEnabled(v)
        self._update_restore_btn()

    def reload_list(self):
        self._snapshots = self._manager.list_snapshots()
        self.snap_list.clear()
        for s in self._snapshots:
            ts_str = ""
            if s["timestamp"]:
                try:
                    dt = datetime.fromisoformat(s["timestamp"])
                    ts_str = dt.strftime("%Y-%m-%d  %H:%M")
                except Exception:
                    ts_str = s["timestamp"][:16]
            text = f"{s['label']}\n  {s['program']}  ·  {ts_str}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.snap_list.addItem(item)
        if not self._snapshots:
            item = QListWidgetItem("No snapshots yet — save one to get started")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor(TEXT_DIM))
            self.snap_list.addItem(item)

    def populate_for_save(self, label: str, program: str,
                          parameters: List[int], presets: dict,
                          settings: dict, tss: dict = None):
        """Called by main window after collecting all device data."""
        path = self._manager.save(label, program, parameters, presets, settings, tss=tss)
        self.reload_list()
        self.save_btn.setEnabled(self._connected)
        self.progress_lbl.setText(f"Saved: {path.name}")
        QTimer.singleShot(4000, lambda: self.progress_lbl.setText(""))

    def set_restore_progress(self, text: str):
        self.progress_lbl.setText(text)

    # ------------------------------------------------------------------

    def _on_save(self):
        if self.on_capture:
            label = self.label_edit.text().strip() or \
                    datetime.now().strftime("snapshot %Y-%m-%d %H:%M")
            self.save_btn.setEnabled(False)
            self.progress_lbl.setText("Collecting device state…")
            self.on_capture(label)

    def _on_select(self, item, _prev):
        if not item:
            return
        s = item.data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        self.delete_btn.setEnabled(True)
        self._update_restore_btn()

        data = s["data"]
        ts_str = ""
        if s["timestamp"]:
            try:
                dt = datetime.fromisoformat(s["timestamp"])
                ts_str = dt.strftime("%A %d %B %Y  %H:%M:%S")
            except Exception:
                ts_str = s["timestamp"]

        self.detail_label.setText(s["label"])
        self.detail_prog.setText(f"Program:  {s['program']}")
        self.detail_ts.setText(ts_str)

        params = data.get("parameters", [])
        if params:
            rows = []
            for i, v in enumerate(params):
                rows.append(f"P{i+1:02d}: {v:4d}")
            self.detail_params.setText("  ".join(rows[:6]) + "\n" + "  ".join(rows[6:]))
        else:
            self.detail_params.setText("")

    def _on_delete(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        reply = QMessageBox.question(
            self, "Delete Snapshot",
            f'Delete snapshot "{s["label"]}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._manager.delete(s["path"])
            self.reload_list()
            self.detail_label.setText("—")
            self.detail_prog.setText("")
            self.detail_ts.setText("")
            self.detail_params.setText("")
            self.delete_btn.setEnabled(False)
            self._update_restore_btn()

    def _on_restore(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        reply = QMessageBox.question(
            self, "Restore Snapshot",
            f'Restore "{s["label"]}" to the connected Videomancer?\n\n'
            f'This will load program "{s["program"]}" and overwrite '
            f'all current parameters and user presets.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self.on_restore:
            self.restore_btn.setEnabled(False)
            self.on_restore(s["data"])

    def _update_restore_btn(self):
        has_selection = bool(self.snap_list.selectedItems() and
                             self.snap_list.selectedItems()[0]
                             .data(Qt.ItemDataRole.UserRole))
        self.restore_btn.setEnabled(self._connected and has_selection)


# ── System tab ─────────────────────────────────────────────────────────

def _timing_to_fps(timing: str) -> str:
    """Convert a timing string like '720p5994' to a human framerate."""
    t = timing.lower().strip()
    table = {
        "ntsc":       "29.97  (480i)",
        "pal":        "25  (576i)",
        "480p":       "59.94  (480p)",
        "576p":       "50  (576p)",
        "720p50":     "50  (720p)",
        "720p5994":   "59.94  (720p)",
        "720p60":     "60  (720p)",
        "1080i50":    "25  (1080i)",
        "1080i5994":  "29.97  (1080i)",
        "1080i60":    "30  (1080i)",
        "1080p2398":  "23.98  (1080p)",
        "1080p24":    "24  (1080p)",
        "1080p25":    "25  (1080p)",
        "1080p2997":  "29.97  (1080p)",
        "1080p30":    "30  (1080p)",
    }
    return table.get(t, f"{timing}")


class SystemTab(QWidget):
    """
    System settings — video routing, status, firmware, MIDI.
    Key 1 = video source (0=analog, 1=hdmi)
    """

    # Shared transparent label style — kills black boxes behind text
    _TRANSPARENT = "background:transparent;border:none;"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = False
        self.on_send = None   # (cmd_str) callback

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # ── Top bar ──
        top = QHBoxLayout()
        title = QLabel("SYSTEM")
        title.setStyleSheet(
            f"color:#ffffff;font-family:'Goldplay',sans-serif;"
            f"font-size:22px;font-weight:bold;"
            f"letter-spacing:3px;{self._TRANSPARENT}"
        )
        top.addWidget(title)
        top.addStretch()
        self.refresh_btn = QPushButton("↻  Refresh All")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(self._refresh)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)
        root.addWidget(hsep())

        # Scroll area so content doesn't get clipped on smaller windows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_inner = QWidget()
        scroll_lay = QVBoxLayout(scroll_inner)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(0)

        # ── Left column: Video + Status ──
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(10)

        # ·· Video Input & Timing ··
        vid_grp = QGroupBox("VIDEO INPUT")
        vl = QVBoxLayout(vid_grp)
        vl.setSpacing(10)

        # Source: HDMI / Analog toggle buttons
        src_row = QHBoxLayout()
        src_lbl = QLabel("Source")
        src_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;min-width:60px;{self._TRANSPARENT}")
        src_row.addWidget(src_lbl)
        self._src_hdmi_btn = QPushButton("HDMI")
        self._src_hdmi_btn.setCheckable(True)
        self._src_hdmi_btn.setChecked(True)
        self._src_hdmi_btn.clicked.connect(lambda: self._set_source("hdmi"))
        self._src_analog_btn = QPushButton("ANALOG")
        self._src_analog_btn.setCheckable(True)
        self._src_analog_btn.clicked.connect(lambda: self._set_source("analog"))
        src_row.addWidget(self._src_hdmi_btn, stretch=1)
        src_row.addWidget(self._src_analog_btn, stretch=1)
        # Keep combo hidden for sync purposes
        self.src_combo = QComboBox()
        self.src_combo.addItem("Analog", "analog")
        self.src_combo.addItem("HDMI", "hdmi")
        self.src_combo.setVisible(False)
        vl.addLayout(src_row)

        # Analog connection type (shown when analog selected)
        self._analog_row = QHBoxLayout()
        analog_lbl = QLabel("Analog In")
        analog_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;min-width:60px;{self._TRANSPARENT}")
        self._analog_row.addWidget(analog_lbl)
        self._conn_cvbs_btn  = QPushButton("CVBS / COMPOSITE")
        self._conn_cvbs_btn.setCheckable(True)
        self._conn_cvbs_btn.setChecked(True)
        self._conn_comp_btn  = QPushButton("COMPONENT YPbPr")
        self._conn_comp_btn.setCheckable(True)
        for b in [self._conn_cvbs_btn, self._conn_comp_btn]:
            self._analog_row.addWidget(b, stretch=1)
        self._conn_cvbs_btn.clicked.connect(lambda: self._set_analog_conn("cvbs"))
        self._conn_comp_btn.clicked.connect(lambda: self._set_analog_conn("component"))
        self._analog_widget = QWidget()
        self._analog_widget.setLayout(self._analog_row)
        self._analog_widget.setVisible(False)
        vl.addWidget(self._analog_widget)

        # Timing selector
        timing_row = QHBoxLayout()
        timing_lbl = QLabel("Timing")
        timing_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;min-width:60px;{self._TRANSPARENT}")
        timing_row.addWidget(timing_lbl)

        self.timing_combo = QComboBox()
        TIMINGS = [
            ("NTSC (480i 59.94)",    "ntsc"),
            ("PAL (576i 50)",        "pal"),
            ("480p 59.94",           "480p"),
            ("576p 50",              "576p"),
            ("720p 50",              "720p50"),
            ("720p 59.94",           "720p5994"),
            ("720p 60",              "720p60"),
            ("1080i 50",             "1080i50"),
            ("1080i 59.94",          "1080i5994"),
            ("1080i 60",             "1080i60"),
            ("1080p 23.98",          "1080p2398"),
            ("1080p 24",             "1080p24"),
            ("1080p 25",             "1080p25"),
            ("1080p 29.97",          "1080p2997"),
            ("1080p 30",             "1080p30"),
        ]
        for label, val in TIMINGS:
            self.timing_combo.addItem(label, val)
        self.timing_combo.currentIndexChanged.connect(self._on_timing_changed)
        timing_row.addWidget(self.timing_combo, stretch=1)

        apply_timing_btn = QPushButton("APPLY")
        apply_timing_btn.setFixedWidth(80)
        apply_timing_btn.clicked.connect(self._apply_timing)
        timing_row.addWidget(apply_timing_btn)
        vl.addLayout(timing_row)

        timing_note = QLabel("Changing timing restarts video output")
        timing_note.setStyleSheet(f"color:{WARN};font-size:16px;{self._TRANSPARENT}")
        vl.addWidget(timing_note)

        ll.addWidget(vid_grp)

        # ·· Video Status ··
        status_grp = QGroupBox("VIDEO STATUS")
        sl = QVBoxLayout(status_grp)
        sl.setSpacing(4)

        self._status_fields = {}
        for label, key in [
            ("Source",          "source"),
            ("Timing",          "timing"),
            ("Frame Rate",      "framerate"),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;min-width:140px;{self._TRANSPARENT}")
            row.addWidget(lbl)
            sep = QLabel(":")
            sep.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;{self._TRANSPARENT}")
            sep.setFixedWidth(12)
            row.addWidget(sep)
            row.addSpacing(10)
            val = QLabel("\u2014")
            val.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:bold;{self._TRANSPARENT}")
            row.addWidget(val, stretch=1)
            sl.addLayout(row)
            self._status_fields[key] = val

        sl.addSpacing(40)
        self.refresh_status_btn = QPushButton("Refresh Status")
        self.refresh_status_btn.setEnabled(False)
        self.refresh_status_btn.clicked.connect(self._fetch_status)
        sl.addWidget(self.refresh_status_btn)

        ll.addWidget(status_grp)
        ll.addStretch()
        splitter.addWidget(left)

        # ── Right column: Firmware + BPM + MIDI ──
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(10)

        # ·· Firmware / Device Info ··
        fw_grp = QGroupBox("FIRMWARE")
        fl = QVBoxLayout(fw_grp)
        fl.setSpacing(6)

        self._fw_fields = {}
        for label, key in [
            ("Version",     "version"),
            ("Device",      "device"),
            ("Serial",      "serial"),
            ("Uptime",      "uptime"),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:18px;min-width:80px;{self._TRANSPARENT}")
            row.addWidget(lbl)
            val = QLabel("\u2014")
            val.setStyleSheet(f"color:{TEXT};font-size:19px;font-weight:bold;{self._TRANSPARENT}")
            row.addWidget(val, stretch=1)
            fl.addLayout(row)
            self._fw_fields[key] = val

        self.refresh_fw_btn = QPushButton("Refresh")
        self.refresh_fw_btn.setEnabled(False)
        self.refresh_fw_btn.clicked.connect(self._fetch_version)
        fl.addWidget(self.refresh_fw_btn)
        rl.addWidget(fw_grp)

        # ·· MIDI CC map ··
        midi_grp = QGroupBox("MIDI CC ASSIGNMENTS")
        ml = QVBoxLayout(midi_grp)
        self.midi_table = QTextEdit()
        self.midi_table.setReadOnly(True)
        self.midi_table.setMaximumHeight(180)
        self.midi_table.setStyleSheet(
            f"background:{SURFACE};color:{TEXT};font-size:18px;"
            f"border:1px solid {BORDER};border-radius:4px;"
        )
        ml.addWidget(self.midi_table)
        self.refresh_midi_btn = QPushButton("Refresh CC Map")
        self.refresh_midi_btn.setEnabled(False)
        self.refresh_midi_btn.clicked.connect(self._fetch_midi)
        ml.addWidget(self.refresh_midi_btn)
        rl.addWidget(midi_grp)

        # ·· Documentation ··
        docs_grp = QGroupBox("DOCUMENTATION")
        dl = QVBoxLayout(docs_grp)
        dl.setSpacing(8)

        self._doc_links = [
            ("Community", "https://community.lzxindustries.net/"),
            ("Firmware", "https://github.com/lzxindustries/videomancer-firmware"),
            ("Technical Manual", "https://docs.lzxindustries.net/docs/instruments/videomancer"),
        ]
        for label, url in self._doc_links:
            btn = QPushButton(f"📖  {label}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{background:{SURFACE2};border:1px solid {BORDER};"
                f"border-radius:4px;color:{ACCENT};font-size:13px;"
                f"font-weight:bold;padding:8px 12px;text-align:left;}}"
                f"QPushButton:hover{{background:{DIM};border-color:{ACCENT};color:#ffffff;}}"
            )
            _url = url
            btn.clicked.connect(lambda checked, u=_url: self._open_doc(u))
            dl.addWidget(btn)

        rl.addWidget(docs_grp)

        rl.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([400, 400])
        scroll_lay.addWidget(splitter, stretch=1)
        scroll.setWidget(scroll_inner)
        root.addWidget(scroll, stretch=1)

    def set_connected(self, v: bool):
        self._connected = v
        self.refresh_btn.setEnabled(v)
        self.refresh_status_btn.setEnabled(v)
        self.refresh_midi_btn.setEnabled(v)
        self.refresh_fw_btn.setEnabled(v)
        self.src_combo.setEnabled(v)
        self._src_hdmi_btn.setEnabled(v)
        self._src_analog_btn.setEnabled(v)
        self._conn_cvbs_btn.setEnabled(v)
        self._conn_comp_btn.setEnabled(v)
        self.timing_combo.setEnabled(v)
        if not v:
            for val in self._fw_fields.values():
                val.setText("\u2014")
            for val in self._status_fields.values():
                val.setText("\u2014")
                val.setStyleSheet(
                    f"color:{TEXT};font-size:18px;font-weight:bold;"
                    f"{self._TRANSPARENT}"
                )

    @staticmethod
    def _is_true(v) -> bool:
        """Handle bool, int, and string representations of true/false."""
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return v.lower() in ("true", "yes", "1", "locked")
        return bool(v)

    def apply_video_status(self, data: dict):
        src = data.get("source", "—")
        self._status_fields["source"].setText(src.upper())
        timing = data.get("timing", "—")
        self._status_fields["timing"].setText(timing)

        # Derive framerate from timing string
        output = data.get("output") or {}
        out_timing = output.get("timing", "—")
        fps = _timing_to_fps(out_timing or timing)
        self._status_fields["framerate"].setText(fps)
        self._status_fields["framerate"].setStyleSheet(
            f"color:#ffffff;font-size:19px;font-weight:bold;{self._TRANSPARENT}"
        )

        locked = self._is_true(data.get("locked", False))
        # Sync source toggle buttons
        self._src_hdmi_btn.setChecked(src == "hdmi")
        self._src_analog_btn.setChecked(src == "analog")
        self._analog_widget.setVisible(src == "analog")
        # Sync hidden combo
        idx = 1 if src == "hdmi" else 0
        self.src_combo.blockSignals(True)
        self.src_combo.setCurrentIndex(idx)
        self.src_combo.blockSignals(False)
        # Sync timing combo
        for i in range(self.timing_combo.count()):
            if self.timing_combo.itemData(i).lower() == out_timing.lower():
                self.timing_combo.blockSignals(True)
                self.timing_combo.setCurrentIndex(i)
                self.timing_combo.blockSignals(False)
                break

    def _on_timing_changed(self, idx):
        pass  # Only apply on button click — timing restarts video

    def _apply_timing(self):
        val = self.timing_combo.currentData()
        if val and self.on_send:
            self.on_send(f"video timing {val}")

    def apply_midi_cc(self, assignments: list):
        lines = []
        for i, a in enumerate(assignments):
            msb = a.get("msb", "?")
            lsb = a.get("lsb", "?")
            lines.append(f"P{i+1:02d}  MSB:{msb:3d}  LSB:{lsb:3d}")
        self.midi_table.setText("\n".join(lines))

    def _set_source(self, src: str):
        """Switch video input source and poll until locked."""
        # Update toggle button states
        self._src_hdmi_btn.setChecked(src == "hdmi")
        self._src_analog_btn.setChecked(src == "analog")
        self._analog_widget.setVisible(src == "analog")
        # Sync hidden combo
        self.src_combo.blockSignals(True)
        self.src_combo.setCurrentIndex(1 if src == "hdmi" else 0)
        self.src_combo.blockSignals(False)
        if self.on_send:
            self.on_send(f"video input {src}")
        # Poll video status repeatedly to catch lock (HDMI re-lock can be slow)
        for delay in [500, 1500, 3000, 5000, 8000, 12000]:
            QTimer.singleShot(delay, self._fetch_status)

    def _set_analog_conn(self, conn: str):
        """Set analog input connector type."""
        self._conn_cvbs_btn.setChecked(conn == "cvbs")
        self._conn_comp_btn.setChecked(conn == "component")
        if self.on_send:
            self.on_send(f"video input analog")

    def _on_src_changed(self, idx):
        src = self.src_combo.currentData()
        if self.on_send:
            self.on_send(f"video input {src}")

    def apply_firmware_info(self, version: str, device: str = "",
                            serial_num: str = "", uptime: str = ""):
        """Populate the firmware info fields."""
        self._fw_fields["version"].setText(version or "\u2014")
        self._fw_fields["device"].setText(device or "Videomancer")
        self._fw_fields["serial"].setText(serial_num or "\u2014")
        self._fw_fields["uptime"].setText(uptime or "\u2014")

    def _refresh(self):
        self._fetch_status()
        self._fetch_midi()
        self._fetch_version()

    def _fetch_status(self):
        if self.on_send and self._connected:
            self.on_send("video status")

    def _fetch_midi(self):
        if self.on_send and self._connected:
            self.on_send("modulation cc-map")

    def _fetch_version(self):
        if self.on_send and self._connected:
            self.on_send("version")

    def _open_doc(self, url: str):
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))


# ── State tab ──────────────────────────────────────────────────────────

class StateTab(QWidget):
    """
    Quick-recall preset grid + named list + export/import.
    Combines the old Presets and Snapshots tabs into one.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected  = False
        self._factory: List[dict] = []
        self._user: List[dict]    = []
        self._manager = SnapshotManager()

        self.on_apply_factory  = None   # (index)
        self.on_apply_user     = None   # (index)
        self.on_save_preset    = None   # (index, name)
        self.on_delete_preset  = None   # (index)
        self.on_capture        = None   # (label)
        self.on_restore        = None   # (data)
        self.on_refresh        = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("STATE")
        title.setStyleSheet(
            f"color:#ffffff;font-family:'Goldplay',sans-serif;"
            f"font-size:22px;font-weight:bold;"
            f"letter-spacing:3px;background:transparent;border:none;"
        )
        top.addWidget(title)
        top.addStretch()
        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setVisible(False)
        self.refresh_btn.clicked.connect(lambda: self.on_refresh and self.on_refresh())
        top.addWidget(self.refresh_btn)
        root.addLayout(top)
        root.addWidget(hsep())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(0)

        # ── Left: Quick recall grid + user presets ──
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(10)

        # User presets
        user_grp = QGroupBox("User Presets")
        ul = QVBoxLayout(user_grp)
        self.user_list = QListWidget()
        self.user_list.setMaximumHeight(160)
        ul.addWidget(self.user_list)

        user_btns = QHBoxLayout()
        apply_u = QPushButton("Apply")
        apply_u.clicked.connect(self._apply_user)
        user_btns.addWidget(apply_u)

        save_btn = QPushButton("Save Current")
        save_btn.clicked.connect(self._save_preset)
        user_btns.addWidget(save_btn)

        del_btn = QPushButton("Delete")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._delete_preset)
        user_btns.addWidget(del_btn)
        ul.addLayout(user_btns)

        self.flash_lbl = QLabel("")
        self.flash_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        ul.addWidget(self.flash_lbl)
        ll.addWidget(user_grp)
        ll.addStretch()
        splitter.addWidget(left)

        # ── Right: File snapshots ──
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(8)

        snap_grp = QGroupBox("File Snapshots")
        sl = QVBoxLayout(snap_grp)

        snap_top = QHBoxLayout()
        self.snap_label = QLineEdit()
        self.snap_label.setPlaceholderText("Snapshot label…")
        snap_top.addWidget(self.snap_label, stretch=1)
        save_snap = QPushButton("⬤  Save")
        save_snap.setObjectName("primary")
        save_snap.setEnabled(False)
        save_snap.clicked.connect(self._save_snapshot)
        self._save_snap_btn = save_snap
        snap_top.addWidget(save_snap)
        sl.addLayout(snap_top)

        self.snap_list = QListWidget()
        sl.addWidget(self.snap_list, stretch=1)

        snap_btns = QHBoxLayout()
        restore_btn = QPushButton("⬤  Restore")
        restore_btn.setObjectName("primary")
        restore_btn.setEnabled(False)
        restore_btn.clicked.connect(self._restore_snapshot)
        self._restore_btn = restore_btn
        snap_btns.addWidget(restore_btn)

        folder_btn = QPushButton("📁  Open Folder")
        folder_btn.clicked.connect(lambda: self._manager.open_folder())
        snap_btns.addWidget(folder_btn)

        del_snap = QPushButton("Delete")
        del_snap.setObjectName("danger")
        del_snap.clicked.connect(self._delete_snapshot)
        snap_btns.addWidget(del_snap)
        sl.addLayout(snap_btns)

        self.snap_status = QLabel("")
        self.snap_status.setStyleSheet(f"color:{WARN};font-size:11px;")
        sl.addWidget(self.snap_status)

        rl.addWidget(snap_grp, stretch=1)
        splitter.addWidget(right)
        splitter.setSizes([380, 380])
        root.addWidget(splitter, stretch=1)

        self._reload_snapshots()
        self.snap_list.currentItemChanged.connect(
            lambda i, _: self._restore_btn.setEnabled(
                self._connected and i is not None and
                i.data(Qt.ItemDataRole.UserRole) is not None
            )
        )

    def set_connected(self, v: bool):
        self._connected = v
        self.refresh_btn.setEnabled(v)
        self._save_snap_btn.setEnabled(v)

    def populate_presets(self, factory: list, user: list, flash_free: int = 0):
        self._factory = factory
        self._user    = user

        # User list
        self.user_list.clear()
        for i, p in enumerate(user):
            item = QListWidgetItem(p.get("n", f"User {i}"))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.user_list.addItem(item)

        if flash_free:
            self.flash_lbl.setText(f"Flash free: {flash_free // 1024} KB")

    def set_snapshot_status(self, text: str):
        self.snap_status.setText(text)
        if text:
            QTimer.singleShot(4000, lambda: self.snap_status.setText(""))

    def _reload_snapshots(self):
        snaps = self._manager.list_snapshots()
        self.snap_list.clear()
        for s in snaps:
            ts = ""
            try:
                dt = datetime.fromisoformat(s["timestamp"])
                ts = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            item = QListWidgetItem(f"{s['label']}  ·  {s['program']}  ·  {ts}")
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.snap_list.addItem(item)
        if not snaps:
            item = QListWidgetItem("No snapshots yet")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor(TEXT_DIM))
            self.snap_list.addItem(item)

    def _apply_factory(self, idx: int):
        if self.on_apply_factory:
            self.on_apply_factory(idx)

    def _apply_user(self):
        items = self.user_list.selectedItems()
        if items and self.on_apply_user:
            self.on_apply_user(items[0].data(Qt.ItemDataRole.UserRole))

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save Preset", "Name:", text="My Preset")
        if ok and name.strip() and self.on_save_preset:
            self.on_save_preset(len(self._user), name.strip())

    def _delete_preset(self):
        items = self.user_list.selectedItems()
        if not items:
            return
        idx  = items[0].data(Qt.ItemDataRole.UserRole)
        name = items[0].text()
        if QMessageBox.question(self, "Delete", f'Delete "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes and self.on_delete_preset:
            self.on_delete_preset(idx)

    def _save_snapshot(self):
        label = self.snap_label.text().strip() or \
                datetime.now().strftime("snapshot %Y-%m-%d %H:%M")
        if self.on_capture:
            self._save_snap_btn.setEnabled(False)
            self.snap_status.setText("Collecting device state…")
            self.on_capture(label)

    def populate_for_save(self, label, program, parameters, presets, settings, tss=None):
        self._manager.save(label, program, parameters, presets, settings, tss=tss)
        self._reload_snapshots()
        self._save_snap_btn.setEnabled(self._connected)
        self.set_snapshot_status(f"✓ Saved: {label}")

    def _restore_snapshot(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        if QMessageBox.question(self, "Restore",
            f'Restore "{s["label"]}" to device?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes and self.on_restore:
            self._restore_btn.setEnabled(False)
            self.on_restore(s["data"])

    def _delete_snapshot(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        if QMessageBox.question(self, "Delete",
            f'Delete snapshot "{s["label"]}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._manager.delete(s["path"])
            self._reload_snapshots()


# ── Main window ────────────────────────────────────────────────────────

class VideomancerApp(QMainWindow):

    def __init__(self, window_number: int = 1):
        super().__init__()
        self._window_number = window_number
        self._window_label = f"[Unit {window_number}]" if window_number > 0 else ""
        self._claimed_port: Optional[str] = None
        title = "VIDEOMANCER CONTROL"
        if self._window_label:
            title += f" {self._window_label}"
        self.setWindowTitle(title)
        self.resize(820, 1020)
        self.setMinimumSize(700, 860)
        self.setStyleSheet(STYLESHEET)

        self._worker: Optional[SerialWorker] = None
        self._active_program: Optional[str] = None
        self._pending_load: Optional[str] = None
        self._user_editing = False      # True while user is actively moving controls
        self._edit_cooldown = QTimer()  # delay before re-syncing from device
        self._edit_cooldown.setSingleShot(True)
        self._edit_cooldown.timeout.connect(self._on_edit_cooldown)
        # Bidirectional sync — poll device every 500ms
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(350)
        self._poll_timer.timeout.connect(self._poll_device)
        # snapshot collection state
        self._snap_label:    str  = ""
        self._snap_params:   list = []
        self._snap_presets:  dict = {}
        self._snap_settings: dict = {}
        self._snap_stage:    int  = 0   # 0=idle,1=params,2=presets,3=settings
        self._tss_readback_pending = False

        self._setup_ui()

        # Poof animation overlays
        self._poof = PoofOverlay(self)
        self._poof.hide()
        self._sparkle = SparkleRing(self)
        self._sparkle.hide()

        # Auto-connect — stagger by window number to avoid port race
        connect_delay = 100 + (self._window_number - 1) * 500
        QTimer.singleShot(connect_delay, self._try_auto_connect)
        # Hot-plug timer: scan for device every 3s when disconnected
        self._hotplug_timer = QTimer()
        self._hotplug_timer.setInterval(2000)
        self._hotplug_timer.timeout.connect(self._hotplug_scan)
        # Delay first scan — give auto-connect time to run first
        QTimer.singleShot(1500, self._hotplug_timer.start)

        # Check for app updates in background
        self._update_checker = _UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        root.addWidget(self._build_header())
        sep1 = QFrame()
        sep1.setFixedHeight(1)
        sep1.setStyleSheet(f"background:{ACCENT2};")
        root.addWidget(sep1)
        sep2 = QFrame()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background:{BORDER};")
        root.addWidget(sep2)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 14, 14, 8)
        bl.setSpacing(14)

        # conn_bar is in the header — created in _build_header()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)

        self.prog_tab    = ProgramsTab()
        self.param_tab   = ParametersTab()
        self.system_tab  = SystemTab()
        self.state_tab   = StateTab()
        self.snap_tab    = SnapshotsTab()   # keep for snapshot callbacks

        self.tabs.addTab(self.prog_tab,   "PROGRAMS")
        self.tabs.addTab(self.param_tab,  "CONTROL")
        self.tabs.addTab(self.system_tab, "SYSTEM")
        self.tabs.addTab(self.state_tab,  "STATE")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.conn_bar.data_refresh_btn.clicked.connect(self._on_tab_refresh)



        # Wire program tab
        self.prog_tab.on_load_program  = self.load_program
        self.prog_tab.load_more_btn.clicked.connect(self._load_more)


        # Wire motion/param tab
        self.param_tab.on_param_change = self._send_param
        self.param_tab.on_mod_change   = self._send_mod

        # Wire system tab
        self.system_tab.on_send = self._system_send

        # Wire state tab
        self.state_tab.on_apply_factory = lambda i: self._apply_preset(i, "factory")
        self.state_tab.on_apply_user    = lambda i: self._apply_preset(i, "user")
        self.state_tab.on_save_preset   = self._save_preset
        self.state_tab.on_delete_preset = self._delete_preset
        self.state_tab.on_refresh       = self._fetch_presets
        self.state_tab.on_capture       = self._snapshot_capture
        self.state_tab.on_restore       = self._snapshot_restore

        # Keep snap_tab wired for snapshot logic (invisible)
        self.snap_tab.on_capture = self._snapshot_capture
        self.snap_tab.on_restore = self._snapshot_restore

        self.conn_bar.data_refresh_btn.setVisible(False)

        self._header_prog_lbl.setVisible(False)
        bl.addWidget(self.tabs, stretch=1)

        # Console
        # Collapsible console
        self._console_visible = False
        console_header = QHBoxLayout()
        console_header.addStretch()
        console_lbl = QLabel("SERIAL CONSOLE")
        console_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;letter-spacing:2px;")
        console_header.addWidget(console_lbl)
        console_header.addSpacing(12)
        copy_btn = QPushButton("COPY")
        copy_btn.setFixedHeight(22)
        copy_btn.setFixedWidth(54)
        copy_btn.setStyleSheet(f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};border-radius:3px;color:{TEXT_DIM};font-size:10px;padding:0;}}QPushButton:hover{{color:{TEXT};}}")
        copy_btn.clicked.connect(lambda: self.console.text.selectAll() or self.console._copy_all())
        console_header.addWidget(copy_btn)
        clr_btn = QPushButton("CLEAR")
        clr_btn.setFixedHeight(22)
        clr_btn.setFixedWidth(54)
        clr_btn.setStyleSheet(f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};border-radius:3px;color:{TEXT_DIM};font-size:10px;padding:0;}}QPushButton:hover{{color:{TEXT};}}")
        clr_btn.clicked.connect(lambda: self.console.text.clear())
        console_header.addWidget(clr_btn)
        self._console_toggle_btn = QPushButton("▶  SHOW")
        self._console_toggle_btn.setFixedHeight(22)
        self._console_toggle_btn.setFixedWidth(80)
        self._console_toggle_btn.setStyleSheet(
            f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:3px;color:{TEXT_DIM};font-size:10px;padding:0;}}"
            f"QPushButton:hover{{color:{TEXT};}}"
        )
        self._console_toggle_btn.clicked.connect(self._toggle_console)
        console_header.addWidget(self._console_toggle_btn)
        console_header_w = QWidget()
        console_header_w.setLayout(console_header)
        bl.addWidget(console_header_w)

        self.console = ConsoleWidget()
        self.console.hide()
        bl.addWidget(self.console)

        root.addWidget(body, stretch=1)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._sb_prog = QLabel("Program: —")
        self._sb_vid  = QLabel("Video: —")
        self._sb_fw   = QLabel("FW: —")
        for w in [self._sb_prog, self._sb_vid, self._sb_fw]:
            w.setStyleSheet("color:#ffffff;font-size:12px;background:transparent;")
        self.status_bar.addPermanentWidget(self._sb_prog)
        sep1 = QLabel("  │  ")
        sep1.setStyleSheet(f"color:{BORDER};background:transparent;")
        self.status_bar.addPermanentWidget(sep1)
        self.status_bar.addPermanentWidget(self._sb_vid)
        sep2 = QLabel("  │  ")
        sep2.setStyleSheet(f"color:{BORDER};background:transparent;")
        self.status_bar.addPermanentWidget(sep2)
        self.status_bar.addPermanentWidget(self._sb_fw)
        self.status_bar.showMessage("Disconnected")

    def _build_header(self):
        w = QWidget()
        w.setStyleSheet(f"background:{BG};")
        w.setFixedHeight(38)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)

        # Title — left aligned
        logo_lbl = QLabel('VIDEOMANCER')
        logo_lbl.setStyleSheet(
            "background:transparent;border:none;color:#ffffff;"
            "font-family:'Goldplay',sans-serif;"
            "font-size:24px;font-weight:900;letter-spacing:2px;"
        )
        lay.addWidget(logo_lbl)

        lay.addStretch()

        # Active program label — in header, won't overlap tabs
        self._header_prog_lbl = QLabel("")
        self._header_prog_lbl.setStyleSheet(
            f"color:#ffffff;font-size:14px;font-weight:bold;letter-spacing:1px;"
            f"background:transparent;border:none;"
        )
        self._header_prog_lbl.setVisible(False)
        lay.addWidget(self._header_prog_lbl)

        lay.addStretch()

        # Dual Cast — toggle a second window
        self._dual_btn = QPushButton("Dual Cast")
        self._dual_btn.setCheckable(True)
        self._dual_btn.setFixedHeight(24)
        self._dual_btn.setStyleSheet(f"""
            QPushButton {{
                background:{SURFACE2}; border:1px solid {BORDER};
                border-radius:3px; color:{TEXT_DIM};
                font-size:10px; font-weight:bold; padding:2px 8px;
            }}
            QPushButton:checked {{
                background:#7c3aed; border:2px solid #ffffff;
                color:#ffffff;
            }}
        """)
        self._dual_btn.toggled.connect(self._toggle_dual_cast)
        lay.addWidget(self._dual_btn)
        self._dual_window = None

        # Monitor — floating capture card preview
        self._monitor_btn = QPushButton("Monitor")
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.setFixedHeight(24)
        self._monitor_btn.setStyleSheet(f"""
            QPushButton {{
                background:{SURFACE2}; border:1px solid {BORDER};
                border-radius:3px; color:{TEXT_DIM};
                font-size:10px; font-weight:bold; padding:2px 8px;
            }}
            QPushButton:checked {{
                background:#7c3aed; border:2px solid #ffffff;
                color:#ffffff;
            }}
        """)
        self._monitor_btn.toggled.connect(self._toggle_monitor)
        lay.addWidget(self._monitor_btn)
        self._monitor_window = None

        # Update available banner (hidden until check completes)
        self._update_btn = QPushButton("")
        self._update_btn.setFixedHeight(22)
        self._update_btn.setVisible(False)
        self._update_btn.setStyleSheet(f"""
            QPushButton {{
                background:#2d1f5e; border:1px solid {ACCENT};
                border-radius:3px; color:#a78bfa;
                font-size:9px; font-weight:bold; padding:1px 6px;
            }}
            QPushButton:hover {{
                background:#3d2f7e; color:#ffffff;
            }}
        """)
        self._update_btn.clicked.connect(self._open_update_url)
        lay.addWidget(self._update_btn)
        self._update_url = ""

        # Connection bar lives in header
        self.conn_bar = ConnectionBar()
        self.conn_bar.on_connect    = self._do_connect
        self.conn_bar.on_disconnect = self._do_disconnect
        lay.addWidget(self.conn_bar)

        # Placeholder so _header_prog attr exists
        self._header_prog = QLabel("")
        self._header_prog.setVisible(False)
        lay.addWidget(self._header_prog)

        return w

    # ------------------------------------------------------------------
    # Dual Cast
    # ------------------------------------------------------------------

    def _toggle_dual_cast(self, checked: bool):
        if checked:
            if self._dual_window is None or not self._dual_window.isVisible():
                num = len(_app_windows) + 1
                self._dual_window = _spawn_window(num)
                self._dual_window._parent_dual_btn = self._dual_btn
                self._dual_window._dual_btn.setVisible(False)
        else:
            if self._dual_window is not None and self._dual_window.isVisible():
                self._dual_window._parent_dual_btn = None
                self._dual_window.close()
            self._dual_window = None

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    def _toggle_monitor(self, checked: bool):
        if checked:
            if self._monitor_window is None:
                self._monitor_window = MonitorWindow()
                self._monitor_window.closed.connect(
                    lambda: self._monitor_btn.setChecked(False)
                )
            self._monitor_window.show()
            self._monitor_window.raise_()
        else:
            if self._monitor_window is not None:
                self._monitor_window.close()
                self._monitor_window = None

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    def _on_update_available(self, version: str, url: str):
        self._update_url = url
        self._update_btn.setText(f"v{version} available — click to download")
        self._update_btn.setVisible(True)

    def _open_update_url(self):
        if self._update_url:
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(self._update_url))

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _try_auto_connect(self):
        """Auto-connect to Videomancer if detected on startup."""
        if not self._worker:
            connected = self.conn_bar.try_auto_connect()
            if not connected:
                # Retry once after 800ms in case device is still booting
                QTimer.singleShot(800, lambda: self.conn_bar.try_auto_connect()
                                  if not self._worker else None)

    def _hotplug_scan(self):
        """Periodically scan for device when not connected (hot-plug support)."""
        if self._worker and self._worker.isRunning():
            return  # already connected
        self.conn_bar.refresh_ports()
        self.conn_bar.try_auto_connect()

    def _toggle_console(self):
        self._console_visible = not self._console_visible
        if self._console_visible:
            self.console.show()
            self._console_toggle_btn.setText("▼  HIDE")
        else:
            self.console.hide()
            self._console_toggle_btn.setText("▶  SHOW")

    def _do_connect(self, port: str):
        if self._worker and self._worker.isRunning():
            return
        # Claim port immediately to prevent other windows from grabbing it
        _claimed_ports.add(port)
        self._claimed_port = port
        self._worker = SerialWorker(self)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.response.connect(self._on_response)
        self._worker.error.connect(self._on_error)
        self._worker.programs_page.connect(self._on_programs_page)
        self._worker.status_update.connect(self._on_status_update)
        self.status_bar.showMessage(f"Connecting to {port}…")
        self._worker.connect_port(port)

    def _do_disconnect(self):
        if self._worker:
            self._worker.disconnect_port()

    @pyqtSlot(str)
    def _on_connected(self, port: str):
        # Port already claimed in _do_connect, just ensure it's set
        _claimed_ports.add(port)
        self._claimed_port = port
        self.conn_bar.set_connected(port)
        self.prog_tab.set_connected(True)
        self.param_tab.set_connected(True)
        self.system_tab.set_connected(True)
        self.state_tab.set_connected(True)
        self.snap_tab.set_connected(True)
        label = self._window_label
        self.setWindowTitle(f"VIDEOMANCER CONTROL {label} — {port}")
        self.status_bar.showMessage(f"Connected — {port}  (waiting for boot…)")
        self.console.append("ok", "connected", port)
        # Track connection time for local uptime display
        self._connected_at = time.monotonic()
        self._uptime_timer = QTimer()
        self._uptime_timer.setInterval(10000)  # update every 10s
        self._uptime_timer.timeout.connect(self._update_uptime)
        self._uptime_timer.start()
        # Start bidirectional sync polling
        self._poll_count = 0
        self._poll_timer.start()
        self.conn_bar.data_refresh_btn.setEnabled(True)

    @pyqtSlot()
    def _on_disconnected(self):
        # Release claimed port
        if hasattr(self, '_claimed_port') and self._claimed_port:
            _claimed_ports.discard(self._claimed_port)
            self._claimed_port = None
        self._poll_timer.stop()
        if hasattr(self, '_uptime_timer'):
            self._uptime_timer.stop()
        self.conn_bar.data_refresh_btn.setEnabled(False)
        self.conn_bar.set_disconnected()
        self.prog_tab.set_connected(False)
        self.param_tab.set_connected(False)
        self.system_tab.set_connected(False)
        self.state_tab.set_connected(False)
        self.snap_tab.set_connected(False)
        label = self._window_label
        self.setWindowTitle(f"VIDEOMANCER CONTROL {label}")
        self.status_bar.showMessage("Disconnected — waiting for device…")
        # Clear active program and switch to Programs tab (splash screen)
        self._active_program = None
        if hasattr(self, 'conn_bar') and hasattr(self.conn_bar, '_prog_lbl'):
            self._header_prog_lbl.setVisible(False)
        if hasattr(self, '_header_prog'):
            self._header_prog.setText("")
        self.tabs.setCurrentIndex(0)
        for w in [self._sb_prog, self._sb_vid, self._sb_fw]:
            w.setText(w.text().split(":")[0] + ": —")
        self.console.append("log", "", "Serial connection closed")
        # Clear worker so hot-plug timer can reconnect
        self._worker = None

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        self.status_bar.showMessage(f"Error: {msg}")
        self.console.append("error", "serial", msg)
        # Release claimed port on connection failure
        if "Could not open" in msg:
            if self._claimed_port:
                _claimed_ports.discard(self._claimed_port)
                self._claimed_port = None
            QMessageBox.warning(self, "Connection Error", msg)

    # ------------------------------------------------------------------
    # Response dispatcher
    # ------------------------------------------------------------------

    @pyqtSlot(str, str, str)
    def _on_response(self, prefix: str, key: str, payload: str):
        # Always log modulation for debugging
        if key == "modulation":
            self.console.append(prefix, key, payload[:80])
        # Suppress noisy poll responses from console
        elif key in ("transport",) and prefix == "ok" and self._poll_timer.isActive():
            pass
        else:
            self.console.append(prefix, key, payload)

        if prefix == "log":
            # Parse transport state from firmware log lines
            # e.g. "[24057.082] I: motion: playing BPM=20.00"
            #      "[24072.910] I: motion: stopped"
            lower = payload.lower()
            if "motion: playing" in lower:
                self.param_tab.set_transport_state("playing")
                self.status_bar.showMessage("Transport: playing", 2000)
            elif "motion: stopped" in lower:
                self.param_tab.set_transport_state("stopped")
                self.status_bar.showMessage("Transport: stopped", 2000)
            return

        if prefix != "ok":
            return

        if key == "version":
            self._sb_fw.setText(f"FW: {payload}")
            self.system_tab.apply_firmware_info(version=payload.strip())

        elif key == "program":
            if payload == "ok":
                # Could be load or preset apply
                if self._pending_load:
                    name = self._pending_load
                    self._pending_load = None
                    self._set_active_program(name)
                    self.prog_tab.set_loading_program(False)
                    self.status_bar.showMessage(f"Loaded: {name}", 4000)
                    self._trigger_poof()
                    # Fetch state + presets + TSS + info after load
                    QTimer.singleShot(500, lambda: self._worker.send("program info"))
                    QTimer.singleShot(1500, self._request_state)
                    QTimer.singleShot(1500, self._fetch_presets)
                    QTimer.singleShot(1800, self._fetch_tss_readback_auto)
                    QTimer.singleShot(2500, self._fetch_tss_readback)
                else:
                    # preset apply ok (TSS change)
                    self.status_bar.showMessage("Applied", 1000)
                    QTimer.singleShot(500, self._fetch_tss_readback_auto)
                    QTimer.singleShot(1000, self._fetch_tss_readback)

            else:
                # JSON payload — could be state or preset list
                try:
                    data = json.loads(payload)
                except Exception as exc:
                    self.console.append("error", "json", f"Bad program payload: {exc}")
                    return

                if "m" in data or "ch" in data:
                    # program state — RC11 uses "m", older docs said "ch"
                    m  = data.get("m",  data.get("ch", []))
                    sr = data.get("sr", [0]*12)
                    # Log raw keys so we can verify firmware sends TSS
                    self.console.append("ok", "program-state-keys",
                                        str(list(data.keys())))
                    # Only apply TSS if the response actually includes them
                    # (avoid resetting to 512 from responses that omit TSS)
                    has_tss = "t" in data or "sp" in data or "sl" in data
                    if has_tss:
                        t  = data.get("t",  [0]*12)
                        sp = data.get("sp", [0]*12)
                        sl = data.get("sl", [0]*12)
                        self.param_tab.apply_state(m, t, sp, sl, sr)
                    else:
                        # Apply only manual + operator, skip recently edited
                        now = __import__('time').monotonic()
                        for i in range(min(12, len(m))):
                            last_edit = self.param_tab._last_sent.get(f"edit_time_{i}", 0)
                            if now - last_edit < 3.0:
                                continue
                            card = self.param_tab.channels[i]
                            card.set_manual(m[i] if i < len(m) else 0)
                            card.set_operator(sr[i] if i < len(sr) else 0)
                    # snapshot stage 1 complete → fetch presets
                    if self._snap_stage == 1:
                        self._snap_params = m
                        self._snap_tss = {
                            "t":  data.get("t",  [0]*12),
                            "sp": data.get("sp", [0]*12),
                            "sl": data.get("sl", [0]*12),
                            "sr": sr,
                        }
                        self._snap_stage  = 2
                        self.state_tab.set_snapshot_status("Collecting presets…")
                        self._worker.send("program presets list")

                elif "n" in data and "t" in data and "sp" in data:
                    # Single preset readback — only apply if explicitly requested
                    # and not while user is actively editing
                    if getattr(self, "_tss_readback_pending", False) and not self._user_editing:
                        self._tss_readback_pending = False
                        m  = data.get("m",  [0]*12)
                        t  = data.get("t",  [0]*12)
                        sp = data.get("sp", [0]*12)
                        sl = data.get("sl", [0]*12)
                        sr = data.get("sr", [0]*12)
                        self.param_tab.apply_state(m, t, sp, sl, sr)

                elif "factory" in data or "user" in data:
                    factory    = data.get("factory", [])
                    user       = data.get("user", [])
                    flash_free = data.get("flash_free", 0)
                    self.state_tab.populate_presets(factory, user, flash_free)
                    # snapshot stage 2 complete → fetch settings
                    if self._snap_stage == 2:
                        self._snap_presets = {"factory": factory, "user": user}
                        self._snap_stage   = 3
                        self.state_tab.set_snapshot_status("Collecting settings…")
                        self._worker.send("settings export")

                elif "parameters" in data and "id" in data:
                    # program info response — apply parameter names to UI
                    params = data.get("parameters", [])
                    self.console.append("ok", "program-info", str(data))
                    self.param_tab.apply_param_labels(params)
                    # Show description in Programs tab if available
                    desc = data.get("description", data.get("desc", ""))
                    self.prog_tab.set_program_description(desc)

        elif key == "video":
            if payload == "ok":
                # Input switch confirmed — refresh status
                QTimer.singleShot(300, lambda: self._worker and
                                  self._worker.send("video status"))
            else:
                try:
                    data = json.loads(payload)
                    self.console.append("ok", "video-raw", str(data))
                    self.system_tab.apply_video_status(data)
                    # Update status bar
                    src    = data.get("source", "").upper()
                    timing = data.get("timing", "")
                    locked = "🔒" if data.get("locked") else "⚠"
                    self._sb_vid.setText(f"Video: {src} {timing} {locked}")
                except Exception as exc:
                    self.console.append("error", "json", f"Bad video payload: {exc}")
                    self._sb_vid.setText("Video: parse error")

        elif key == "modulation":
            try:
                data = json.loads(payload)
            except Exception as exc:
                self.console.append("error", "json", f"Bad modulation payload: {exc}")
                return
            if "modulators" in data:
                mods = data["modulators"]
                # Auto-switch to Motion tab when a physical knob is touched
                active = data.get("active", -1)
                if (active >= 0 and
                        hasattr(self, '_last_active_mod') and
                        active != self._last_active_mod and
                        not self._user_editing):
                    # Physical knob touched — switch to Motion tab
                    if self.tabs.currentIndex() != 1:
                        self.tabs.setCurrentIndex(1)
                self._last_active_mod = active

                self.param_tab.apply_modulation_status(mods)
            if "assignments" in data:
                self.system_tab.apply_midi_cc(data["assignments"])

        elif key == "transport":
            try:
                data = json.loads(payload)
                if "bpm_x100" in data:
                    bpm_x100 = data["bpm_x100"]
                    bpm = bpm_x100 / 100.0
                    self.param_tab.set_bpm(bpm, int(bpm_x100))
                if "state" in data:
                    self.param_tab.set_transport_state(data["state"])
                    self.status_bar.showMessage(
                        f"Transport: {data['state']}", 2000
                    )
            except Exception as exc:
                self.console.append("error", "json", f"Bad transport payload: {exc}")

        elif key == "settings":
            try:
                self._snap_settings = json.loads(payload)
            except Exception as exc:
                self.console.append("error", "json", f"Bad settings payload: {exc}")
                self._snap_settings = {}
            # snapshot stage 3 complete → write file
            if self._snap_stage == 3:
                self._snap_stage = 0
                tss = getattr(self, '_snap_tss', {})
                self.state_tab.populate_for_save(
                    self._snap_label,
                    self._active_program or "unknown",
                    self._snap_params,
                    self._snap_presets,
                    self._snap_settings,
                    tss=tss,
                )

    def _update_uptime(self):
        """Update firmware panel with local connection uptime as fallback."""
        if not hasattr(self, '_connected_at') or not self._worker:
            return
        elapsed = int(time.monotonic() - self._connected_at)
        hours, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        if hours > 0:
            text = f"{hours}h {mins}m"
        else:
            text = f"{mins}m {secs}s"
        self.system_tab.apply_firmware_info(
            version=self.system_tab._fw_fields["version"].text(),
            uptime=text,
        )

    @pyqtSlot(dict)
    def _on_status_update(self, data: dict):
        prog = data.get("current_program", "")
        vstd = data.get("video_standard", "")
        sd   = "  SD●" if data.get("sd_mounted") else ""
        if prog:
            self._set_active_program(prog)
        if vstd:
            self._sb_vid.setText(
                f"Video: {vstd.replace('_', ' ').upper()}{sd}"
            )
        # If firmware reports uptime, use it instead of local timer
        if "uptime" in data:
            self.system_tab._fw_fields["uptime"].setText(str(data["uptime"]))
        if "serial" in data:
            self.system_tab._fw_fields["serial"].setText(str(data["serial"]))
        if "device" in data:
            self.system_tab._fw_fields["device"].setText(str(data["device"]))

    # ------------------------------------------------------------------
    # Programs
    # ------------------------------------------------------------------

    def _fetch_programs(self):
        if not self._worker:
            return
        self.prog_tab.clear()
        self._worker.list_programs(0)

    def _load_more(self):
        if self._worker:
            # next offset is tracked in the worker via last page response
            pass  # load_more_btn is hidden when all loaded; auto-pages anyway

    @pyqtSlot(list, bool, int, int)
    def _on_programs_page(self, names, more, nxt, total):
        self.prog_tab.add_page(names, more, total)
        if more:
            self._worker.list_programs(nxt)
        else:
            self.status_bar.showMessage(f"{total} programs", 3000)

    def load_program(self, name: str):
        if not self._worker:
            QMessageBox.information(self, "Not Connected",
                                    "Connect to Videomancer first.")
            return
        self._pending_load = name
        self.param_tab._last_sent.clear()  # reset dedup on program change
        self.prog_tab.set_loading_program(True)
        self.status_bar.showMessage(f"Loading {name}…")
        self.console.append("cmd", f"program load {name}", "")
        self._worker.load_program(name)

    def _trigger_poof(self):
        """Fire the poof + sparkle animation centered on the RUNNING pill."""
        size = self.centralWidget().size()
        self._poof.resize(size)
        self._sparkle.resize(size)
        # Center on the RUNNING pill in the Programs tab
        pill = self.prog_tab.active_pill
        if pill.isVisible():
            pos = pill.mapTo(self.centralWidget(), pill.rect().center())
            cx, cy = pos.x(), pos.y()
        else:
            # Fallback: center on the program name label
            lbl = self.prog_tab.name_lbl
            pos = lbl.mapTo(self.centralWidget(), lbl.rect().center())
            cx, cy = pos.x(), pos.y()
        self._poof.trigger(cx, cy)
        self._sparkle.trigger(cx, cy)

    def _set_active_program(self, name: str):
        self._active_program = name
        self.prog_tab.set_active(name)
        self.param_tab.set_program(name)
        self.state_tab.set_snapshot_status("")
        self._sb_prog.setText(f"Program: {name}")
        # Show active program in window title
        if name:
            port = self.conn_bar.port_combo.currentText()
            self.setWindowTitle(f"VIDEOMANCER CONTROL — {name}  [{port}]")
        else:
            self.setWindowTitle("VIDEOMANCER CONTROL")
        if hasattr(self, '_header_prog'):
            self._header_prog.setText(f"ACTIVE PROGRAM:  {name.upper()}")
        if hasattr(self, 'conn_bar') and hasattr(self.conn_bar, '_prog_lbl'):
            if name:
                self._header_prog_lbl.setText(f"{name.upper()}")
                self._header_prog_lbl.setVisible(True)
            else:
                self._header_prog_lbl.setVisible(False)
        # Fetch parameter names for this program
        if self._worker:
            self._worker.send("program info")

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _on_edit_cooldown(self):
        """Called after user stops editing — safe to sync from device again."""
        self._user_editing = False

    def _poll_device(self):
        """Poll device state for bidirectional sync every 250ms.
        Skip modulation polling while user is editing to prioritize sends."""
        if not self._worker:
            return
        if not self._user_editing:
            self._worker.send("modulation status")
        if not hasattr(self, '_poll_count'):
            self._poll_count = 0
        self._poll_count += 1
        if self._poll_count % 4 == 0:
            self._worker.send("transport status")
        if self._poll_count % 3 == 0 and not self._user_editing:
            self._worker.send("program state")
        if self._poll_count % 20 == 0:
            self._worker.send("video status")

    def _request_state(self):
        if self._worker and not self._user_editing:
            self._worker.send("modulation status")
            self._worker.send("transport status")

    def _send_param(self, index: int, value: int):
        """Direct manual value set via modulation set command."""
        if not self._worker:
            return
        # Toggles (P7-P11) use short cooldown — instant click not a drag
        is_toggle = 7 <= (index + 1) <= 11
        self._user_editing = True
        self._edit_cooldown.start(300 if is_toggle else 700)
        cmd = f"modulation set {index} {value}"
        self.console.append("cmd", f"P{index+1} manual → {value}", "")
        self._worker.send(cmd)

    def _send_mod(self, index: int, field: str, value: int):
        """Send modulation field — operator via modulation source, TSS via preset."""
        if not self._worker:
            return
        self._user_editing = True
        self._edit_cooldown.start(600)
        if field == "sr":
            cmd = f"modulation source {index} {value}"
            self.console.append("cmd", f"P{index+1} operator → {value}", "")
            self._worker.send(cmd)
        else:
            # RC11: modulation set <ch> <val> <field> — confirmed working
            cmd = f"modulation set {index} {value} {field}"
            self.console.append("cmd", f"P{index+1} {field} → {value}", "")
            self._worker.send(cmd)

    def _fetch_tss_readback(self):
        """Read back preset slot 0 to sync TSS sliders from device."""
        if self._worker:
            self._tss_readback_pending = True
            self._worker.send("program presets get 0 user")

    def _fetch_tss_readback_auto(self):
        """Fetch full program state to sync TSS sliders from device."""
        if self._worker and not self._user_editing:
            self._worker.send("program state")

    def _send_transport(self, action: str):
        if not self._worker:
            return
        cmd_map = {
            "start": "transport start",
            "stop":  "transport stop",
            "tap":   "transport tap",
        }
        cmd = cmd_map.get(action)
        if cmd:
            self.console.append("cmd", cmd, "")
            if action == "tap":
                # Tap needs minimal latency — bypass queue
                self._worker.send_immediate(cmd)
            else:
                self._worker.send(cmd)

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _fetch_presets(self):
        if self._worker:
            self._worker.send("program presets list")

    def _apply_preset(self, index: int, type_str: str):
        if self._worker:
            self.param_tab._last_sent.clear()  # Reset dedup so all values re-sync
            self._user_editing = True
            self._edit_cooldown.start(1500)  # Block polling while preset applies
            cmd = f"program presets apply {index} {type_str}"
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)

    def _save_preset(self, index: int, name: str):
        if self._worker:
            vals  = [ch.get_manual() for ch in self.param_tab.channels]
            m_str = ",".join(str(v) for v in vals)
            t_vals, sp_vals, sl_vals, sr_vals = [], [], [], []
            for ch in self.param_tab.channels:
                tss = ch.get_tss()
                if tss:
                    t_vals.append(str(tss[0]))
                    sp_vals.append(str(tss[1]))
                    sl_vals.append(str(tss[2]))
                else:
                    t_vals.append("0")
                    sp_vals.append("0")
                    sl_vals.append("0")
                sr_vals.append(str(ch.get_operator()))
            t_str  = ",".join(t_vals)
            sp_str = ",".join(sp_vals)
            sl_str = ",".join(sl_vals)
            sr_str = ",".join(sr_vals)
            cmd = (f"program presets save {index} {name} "
                   f"m:{m_str} t:{t_str} sp:{sp_str} sl:{sl_str} sr:{sr_str}")
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)
            QTimer.singleShot(500, self._fetch_presets)

    def _delete_preset(self, index: int):
        if self._worker:
            cmd = f"program presets delete {index}"
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)

    def _rename_preset(self, index: int, name: str):
        if self._worker:
            cmd = f"program presets rename {index} {name}"
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)
            QTimer.singleShot(500, self._fetch_presets)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _snapshot_capture(self, label: str):
        """Begin a sequential data collection: state → presets → settings."""
        if not self._worker:
            return
        self._snap_label   = label
        self._snap_params  = []
        self._snap_presets = {}
        self._snap_settings = {}
        self._snap_stage   = 1
        self.state_tab.set_snapshot_status("Collecting device state…")
        self._worker.send("program state")

    def _snapshot_restore(self, data: dict):
        """Restore a snapshot to the device in sequence."""
        if not self._worker:
            return

        program    = data.get("program", "")
        parameters = data.get("parameters", [])
        presets    = data.get("presets", {})
        settings   = data.get("settings", {})

        self.state_tab.set_snapshot_status(f"Restoring: loading {program}…")

        def _abort_restore(msg="Restore aborted — device disconnected"):
            self.state_tab.set_snapshot_status(msg)
            self.state_tab._restore_btn.setEnabled(True)

        def step2():
            if not self._worker:
                return _abort_restore()
            if parameters:
                m_str  = ",".join(str(v) for v in parameters)
                t_list  = data.get("t",  [0]*12)
                sp_list = data.get("sp", [0]*12)
                sl_list = data.get("sl", [0]*12)
                sr_list = data.get("sr", [0]*12)
                t_str  = ",".join(str(v) for v in t_list)
                sp_str = ",".join(str(v) for v in sp_list)
                sl_str = ",".join(str(v) for v in sl_list)
                sr_str = ",".join(str(v) for v in sr_list)
                self._worker.send(
                    f"program presets save 0 live "
                    f"m:{m_str} t:{t_str} sp:{sp_str} sl:{sl_str} sr:{sr_str}"
                )
                self._worker.send("program presets apply 0 user")
                self.param_tab.apply_state(
                    parameters, t_list, sp_list, sl_list, sr_list
                )
            self.state_tab.set_snapshot_status("Restoring presets…")
            QTimer.singleShot(400, step3)

        def step3():
            if not self._worker:
                return _abort_restore()
            user = presets.get("user", [])
            for i, p in enumerate(user):
                name  = p.get("n", f"preset_{i}")
                m     = p.get("m", [])
                if m:
                    m_str = ",".join(str(v) for v in m)
                    self._worker.send(
                        f"program presets save {i} {name} m:{m_str}"
                    )
            self.state_tab.set_snapshot_status("Restoring settings…")
            QTimer.singleShot(600, step4)

        def step4():
            if not self._worker:
                return _abort_restore()
            if settings:
                self._worker.send(
                    f"settings import {json.dumps(settings, separators=(',', ':'))}"
                )
            QTimer.singleShot(400, step5)

        def step5():
            self.state_tab.set_snapshot_status("✓  Restore complete")
            self.state_tab._restore_btn.setEnabled(True)
            self.status_bar.showMessage("Snapshot restored", 4000)
            QTimer.singleShot(4000, lambda: self.state_tab.set_snapshot_status(""))
            if self._worker:
                self._fetch_presets()

        # Load program first, then chain the rest
        if program:
            self._pending_load = program
            self._worker.load_program(program)
            # After program loads (~2 s) the existing _on_response handler
            # fires → calls _request_state + _fetch_presets.
            # We override that flow for restore by using a one-shot timer.
            QTimer.singleShot(2200, step2)
        else:
            step2()

    def _system_send(self, cmd: str):
        """Send a command from the System tab."""
        if self._worker:
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)

    # ------------------------------------------------------------------
    # Tab change — lazy-load data
    # ------------------------------------------------------------------

    def _on_tab_changed(self, idx: int):
        if not self._worker:
            return
        if idx == 1:   # Motion
            self._request_state()
            QTimer.singleShot(300, self._fetch_tss_readback_auto)
        elif idx == 2: # System
            self._worker.send("video status")
            self._worker.send("modulation cc-map")
            self._worker.send("version")
        elif idx == 3: # State
            self._fetch_presets()
            self.state_tab._reload_snapshots()

    def _on_tab_refresh(self):
        """Refresh action for current tab."""
        idx = self.tabs.currentIndex()
        if not self._worker:
            return
        if idx == 0:   # Programs
            self._fetch_programs()
        elif idx == 1: # Motion
            self._request_state()
            if self._worker:
                self._worker.send("program info")
        elif idx == 2: # System
            self._worker.send("video status")
            self._worker.send("modulation cc-map")
            self._worker.send("version")
        elif idx == 3: # State
            self._fetch_presets()
            self.state_tab._reload_snapshots()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # Stop all timers first to prevent callbacks firing during teardown
        self._poll_timer.stop()
        self._edit_cooldown.stop()
        if hasattr(self, '_hotplug_timer'):
            self._hotplug_timer.stop()
        if hasattr(self, '_uptime_timer'):
            self._uptime_timer.stop()
        if hasattr(self, '_update_checker') and self._update_checker.isRunning():
            self._update_checker.quit()
        if self._monitor_window is not None:
            self._monitor_window.close()
            self._monitor_window = None
        if self._worker:
            self._worker.disconnect_port()
            # Non-blocking — let thread clean up on its own
            self._worker.quit()
        # Release claimed port and remove from global window list
        if self._claimed_port:
            _claimed_ports.discard(self._claimed_port)
            self._claimed_port = None
        if self in _app_windows:
            _app_windows.remove(self)
        # If this window was spawned by Dual Cast, uncheck the parent button
        btn = getattr(self, '_parent_dual_btn', None)
        if btn is not None:
            btn.setChecked(False)
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────

def _spawn_window(number: int) -> VideomancerApp:
    """Create and show a new VideomancerApp window."""
    w = VideomancerApp(window_number=number)
    _app_windows.append(w)
    w.show()
    return w


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Videomancer Control")
    app.setOrganizationName("LZX Industries")

    # Load custom fonts (works both from source and PyInstaller bundle)
    from PyQt6.QtGui import QFontDatabase
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    font_dir = base / "fonts"
    for font_file in ["goldplay-semibold.ttf", "ReliefSingleLine-Regular.ttf"]:
        fpath = font_dir / font_file
        if fpath.exists():
            QFontDatabase.addApplicationFont(str(fpath))

    # Set app icon from embedded splash image
    try:
        from PyQt6.QtGui import QPixmap, QIcon
        from PyQt6.QtCore import QByteArray
        raw = QByteArray.fromBase64(QByteArray(_SPLASH_IMG_B64.encode()))
        pm = QPixmap()
        pm.loadFromData(raw)
        pm = _make_transparent(pm)
        app.setWindowIcon(QIcon(pm))
    except Exception:
        pass

    # Detect how many Videomancer devices are already plugged in
    initial_ports = ConnectionBar.find_all_videomancer_ports()
    count = max(1, len(initial_ports))  # always open at least one window
    for i in range(count):
        _spawn_window(i + 1)

    # Global hot-plug watcher: spawn a new window when a new device appears
    def _global_hotplug():
        all_ports = ConnectionBar.find_all_videomancer_ports()
        unclaimed = [p for p in all_ports if p not in _claimed_ports]
        # Only spawn if there's an unclaimed device AND every existing
        # window already has a connection (avoids duplicate empty windows)
        if unclaimed:
            windows = list(_app_windows)  # copy to avoid mutation during iteration
            all_connected = all(
                w._worker and w._worker.isRunning() for w in windows
            )
            if all_connected:
                _spawn_window(len(windows) + 1)

    hotplug = QTimer()
    hotplug.setInterval(3000)
    hotplug.timeout.connect(_global_hotplug)
    hotplug.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
