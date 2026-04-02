"""
serial_worker.py
Async serial communication layer for the Videomancer GUI.
Runs in a QThread so the UI stays responsive.
"""

import json
import re
import time
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


class SerialWorker(QThread):
    connected     = pyqtSignal(str)
    disconnected  = pyqtSignal()
    response      = pyqtSignal(str, str, str)
    error         = pyqtSignal(str)
    programs_page = pyqtSignal(list, bool, int, int)
    status_update = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port      = None
        self._serial    = None
        self._running   = False
        self._mutex     = QMutex()
        self._cmd_queue = []
        self._buf       = b""   # partial line buffer

    def connect_port(self, port: str):
        self._port = port
        if not self.isRunning():
            self.start()

    def disconnect_port(self):
        self._running = False

    def send(self, command: str):
        self._mutex.lock()
        self._cmd_queue.append(command)
        self._mutex.unlock()

    def send_immediate(self, command: str):
        """Write directly to serial, bypassing the queue. Use for latency-critical commands."""
        if self._serial and self._running:
            self._write(command + "\n")

    def get_version(self):   self.send("version")
    def get_status(self):    self.send("status")
    def list_programs(self, offset: int = 0):
        self.send(f"programs list {offset}" if offset else "programs list")
    def load_program(self, name: str):
        self.send(f"program load {name}")

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self):
        try:
            import serial as pyserial
        except ImportError:
            self.error.emit("pyserial not installed – run: pip install pyserial")
            return

        try:
            # Non-blocking read — we poll in_waiting ourselves
            self._serial = pyserial.Serial(self._port, timeout=0)
        except Exception as exc:
            self.error.emit(f"Could not open {self._port}: {exc}")
            return

        self._running = True
        self.connected.emit(self._port)

        # Wait for device to be ready — poll for data instead of fixed sleep.
        # Most devices respond within 200-500ms; bail after 1.5s worst case.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                if self._serial.in_waiting > 0:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        self._serial.reset_input_buffer()
        self._buf = b""

        # Initial queries
        for cmd in ["version", "status", "programs list",
                    "modulation status", "transport status",
                    "video status"]:
            self._write(cmd + "\n")
            time.sleep(0.03)

        while self._running:
            # 1. Send all queued commands
            self._mutex.lock()
            cmds = list(self._cmd_queue)
            self._cmd_queue.clear()
            self._mutex.unlock()

            for cmd in cmds:
                self._write(cmd + "\n")

            # 2. Read all available bytes into buffer
            try:
                waiting = self._serial.in_waiting
                if waiting > 0:
                    self._buf += self._serial.read(waiting)
            except Exception:
                # Device was unplugged — exit cleanly
                self._running = False
                break

            # 3. Process all complete lines in buffer
            while b"\n" in self._buf:
                line_bytes, self._buf = self._buf.split(b"\n", 1)
                line = line_bytes.decode("ascii", errors="replace").strip()
                line = ANSI_RE.sub("", line).strip()
                if line:
                    self._dispatch(line)

            # 4. Short sleep to avoid burning CPU
            time.sleep(0.008)

        try:
            self._serial.close()
        except Exception:
            pass
        self.disconnected.emit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, text: str):
        try:
            self._serial.write(text.encode("ascii"))
        except Exception as exc:
            self.error.emit(f"Write error: {exc}")
            self._running = False  # unplug during write — stop loop

    def _dispatch(self, line: str):
        if line.startswith("@"):
            rest = line[1:]
            key, _, payload = rest.partition(":")
            self.response.emit("ok", key, payload)
            self._handle_ok(key, payload)
        elif line.startswith("!"):
            code, _, message = line[1:].partition(":")
            self.response.emit("error", code, message)
        else:
            self.response.emit("log", "", line)

    def _handle_ok(self, key: str, payload: str):
        if key == "status":
            try:
                data = json.loads(payload)
                self.status_update.emit(data)
            except Exception as exc:
                self.response.emit("error", "json", f"Bad status payload: {exc}")
        elif key == "programs":
            try:
                data = json.loads(payload)
                names = data.get("programs", [])
                more  = data.get("more", False)
                nxt   = data.get("next", 0)
                total = data.get("count", len(names))
                self.programs_page.emit(names, more, nxt, total)
            except Exception as exc:
                self.response.emit("error", "json", f"Bad programs payload: {exc}")
