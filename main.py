"""
main.py  —  Videomancer Control
Full companion app for the LZX Industries Videomancer.

Tabs:
  Programs   – browse & load FPGA programs
  Parameters – live 12-channel control (sliders + toggles)
  Presets    – factory & user preset management
  Snapshots  – save/restore full device state as local JSON files

Run:
    pip3 install PyQt6 pyserial python-osc
    python3 main.py
"""

APP_VERSION = "2.8.5"
GITHUB_REPO = "VIDEOWASTE/Videomancer-Control"

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
    QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
)
import math
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, pyqtSignal, QThread, QRectF, QPointF, QObject
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
    update_available = pyqtSignal(str, str)  # (new_version, release_html_url)

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
            if _UpdateDownloader._is_newer(remote_ver, local_ver):
                html_url = data.get("html_url", "")
                self.update_available.emit(remote_ver, html_url)
        except Exception:
            pass  # Network errors are non-fatal


LZX_CONNECT_URL = "https://lzxindustries.net/connect"
FIRMWARE_REPO = "lzxindustries/videomancer-firmware"       # GitHub mirror
# LZX publishes firmware and the official program library on its own
# Forgejo server first; the GitHub mirror lags (rc.55 there vs rc.62 on
# Forgejo, Sept 2026). LZX Connect reads Forgejo too.
LZX_FORGEJO_API = "https://git.lzxindustries.net/api/v1/repos"
LZX_FORGEJO_FIRMWARE = "lzx/videomancer-firmware"
FIRMWARE_RELEASES_URL = "https://git.lzxindustries.net/lzx/videomancer-firmware/releases"


def _fetch_releases(sources) -> list:
    """Release JSON from several endpoints, merged by tag — the first source
    listing a tag wins. Unreachable sources are skipped; raises only if every
    source fails."""
    from urllib.request import urlopen, Request
    merged, errors = {}, []
    for url in sources:
        try:
            req = Request(url, headers={"Accept": "application/json",
                                        "User-Agent": "VideomancerControl"})
            with urlopen(req, timeout=15) as resp:
                rows = json.loads(resp.read().decode())
            for r in rows if isinstance(rows, list) else []:
                tag = r.get("tag_name", "")
                if tag not in merged:
                    merged[tag] = r
                elif not _library_target_firmware(merged[tag].get("body") or "") and \
                        _library_target_firmware(r.get("body") or ""):
                    # keep the first source's assets, borrow the other's notes
                    # (e.g. "rebuilt for Videomancer 1.0.0-rc.61")
                    merged[tag] = dict(merged[tag], body=(merged[tag].get("body") or "")
                                       + "\n\n" + r["body"])
        except Exception as exc:
            errors.append(exc)
    if not merged and errors:
        raise errors[0]
    return list(merged.values())


def _fw_version_key(v: str):
    """Sort key for Videomancer firmware versions like '1.0.0-rc.55'.
    Pre-releases sort before the matching final release. None if unparseable."""
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)(?:-rc\.?(\d+))?", v or "")
    if not m:
        return None
    a, b, c, rc = m.groups()
    return (int(a), int(b), int(c), 0 if rc else 1, int(rc or 0))


class _FirmwareChecker(QThread):
    """Finds the newest Videomancer firmware LZX has published (their Forgejo
    server, falling back to the GitHub mirror). Firmware tags look like
    `videomancer/1.0.0-rc.62`; `connect/…` and `programs/…` are ignored."""
    latest_found = pyqtSignal(str)

    def run(self):
        try:
            releases = _fetch_releases([
                f"{LZX_FORGEJO_API}/{LZX_FORGEJO_FIRMWARE}/releases?limit=50",
                f"https://api.github.com/repos/{FIRMWARE_REPO}/releases?per_page=50",
            ])
            best = None
            for r in releases:
                tag = r.get("tag_name", "")
                if not tag.startswith("videomancer/") or r.get("draft"):
                    continue
                ver = tag.split("/", 1)[1]
                key = _fw_version_key(ver)
                if key and (best is None or key > best[0]):
                    best = (key, ver)
            if best:
                self.latest_found.emit(best[1])
        except Exception:
            pass  # offline is fine — the check is advisory


# ── Program library (official LZX + community) ────────────────────────
#
# LZX publishes SD program libraries as GitHub release zips that mirror the
# card's layout: programs/<vendor>/<name>.vmprog (+ programs/manifest.json
# for the official library). The app installs them over USB with `fs put`.

LIBRARY_SOURCES = [
    {"key": "official", "label": "OFFICIAL LZX",
     "repo": "lzxindustries/videomancer-firmware", "tag_prefix": "programs/",
     "forgejo": LZX_FORGEJO_FIRMWARE},
    {"key": "community", "label": "COMMUNITY",
     "repo": "lzxindustries/videomancer-community-programs", "tag_prefix": ""},
]
SD_PROGRAMS = "sd:/programs"
# How many SD programs the Videomancer loads at boot. rc.55 reported
# "93 programs read, 23 over limit"; extra files are skipped.
SD_PROGRAM_LIMIT = 70


_KNOWN_CACHE = {"key": None, "files": frozenset()}


def _known_library_files() -> frozenset:
    """Card-relative paths of every program in any library zip we have
    cached: 'vendor/file.vmprog', plus the bare file name for LZX's own
    (official programs end up loose on the card)."""
    import zipfile
    zips = sorted(_library_cache_dir().glob("*.zip"))
    key = tuple((z.name, z.stat().st_mtime) for z in zips)
    if key == _KNOWN_CACHE["key"]:
        return _KNOWN_CACHE["files"]
    out = set()
    for zp in zips:
        try:
            with zipfile.ZipFile(zp) as z:
                official = "program-library" in zp.name
                for n in z.namelist():
                    parts = n.split("/")
                    if len(parts) == 3 and parts[0] == "programs" and n.endswith(".vmprog"):
                        out.add(f"{parts[1]}/{parts[2]}")
                        if official or parts[1] == "lzx":
                            out.add(parts[2])
        except Exception:
            continue
    _KNOWN_CACHE.update(key=key, files=frozenset(out))
    return _KNOWN_CACHE["files"]


def _norm_prog_name(s: str) -> str:
    """'temporal_diff_v1.0.0' / 'Temporal Diff' → 'temporaldiff' for matching
    card file names against the device's display names."""
    s = re.sub(r"(?i)[_-]?v?\d+(\.\d+){1,2}$", "", s.rsplit("/", 1)[-1].replace(".vmprog", ""))
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _library_cache_dir() -> Path:
    from PyQt6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    d = Path(base or tempfile_dir()) / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def _vmprog_info(data: bytes) -> Optional[dict]:
    """Program metadata from a .vmprog: 64-byte 'VMPG' header, then a TOC of
    64-byte entries; entry type 1 is the program config (SDK vmprog-format.md)."""
    import struct
    try:
        if len(data) < 64 or data[:4] != b"VMPG":
            return None
        toc_off, _toc_bytes, toc_count = struct.unpack_from("<III", data, 20)
        for i in range(min(toc_count, 64)):
            etype, _flags, off, size = struct.unpack_from("<IIII", data, toc_off + i * 64)
            if etype != 1:
                continue
            cfg = data[off:off + size]
            text = lambda a, n: cfg[a:a + n].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
            major, minor, patch = struct.unpack_from("<HHH", cfg, 64)
            return {"program_id": text(0, 64), "version": f"{major}.{minor}.{patch}",
                    "name": text(86, 32), "author": text(118, 64),
                    "description": text(470, 128)}
    except Exception:
        pass
    return None


def _prog_version_key(v: str):
    """'1.0.2' → (1, 0, 2); None if unparseable."""
    m = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", str(v or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _library_target_firmware(notes: str) -> str:
    """'…rebuilt for Videomancer 1.0.0-rc.61…' → '1.0.0-rc.61' ('' if absent)."""
    m = re.search(r"Videomancer\W{0,4}(\d+\.\d+\.\d+(?:-rc\.?\d+)?)", notes or "")
    return m.group(1) if m else ""


class _LibraryIndexFetcher(QThread):
    """Lists published library releases for both sources (newest first)."""
    releases_ready = pyqtSignal(dict)     # source key → [release dicts]
    failed = pyqtSignal(str)

    def run(self):
        from urllib.request import urlopen, Request
        out = {}
        try:
            for src in LIBRARY_SOURCES:
                urls = [f"https://api.github.com/repos/{src['repo']}/releases?per_page=40"]
                if src.get("forgejo"):     # LZX's own server first (newer releases)
                    urls.insert(0, f"{LZX_FORGEJO_API}/{src['forgejo']}/releases?limit=50")
                rels = _fetch_releases(urls)
                rels.sort(key=lambda r: r.get("published_at") or "", reverse=True)
                rows = []
                for r in rels:
                    tag = r.get("tag_name", "")
                    if r.get("draft") or not tag.startswith(src["tag_prefix"]):
                        continue
                    if src["tag_prefix"] == "" and "/" in tag:
                        continue          # sdk/… etc. in the community repo
                    assets = r.get("assets") or []
                    zips = [a for a in assets if a["name"].endswith(".zip")
                            and ("program" in a["name"])]
                    if not zips:
                        continue
                    shas = [a for a in assets if a["name"].endswith(".sha256")]
                    rows.append({
                        "source": src["key"], "tag": tag,
                        "version": tag[len(src["tag_prefix"]):],
                        "date": (r.get("published_at") or "")[:10],
                        "prerelease": bool(r.get("prerelease")),
                        "zip_name": zips[0]["name"],
                        "zip_url": zips[0]["browser_download_url"],
                        "sha_url": shas[0]["browser_download_url"] if shas else "",
                        "target_fw": _library_target_firmware(r.get("body", "")),
                        "notes": (r.get("body") or "").strip(),
                    })
                out[src["key"]] = rows
            self.releases_ready.emit(out)
        except Exception as exc:
            self.failed.emit(f"Couldn't reach LZX's release servers: {exc}")


class _LibraryDownloader(QThread):
    """Downloads (or reuses a cached) library zip, verifies its SHA-256, and
    reads every program's metadata from inside the zip."""
    progress = pyqtSignal(int, int)
    done = pyqtSignal(dict, list)         # release (+ "zip_path", "manifest"), programs
    failed = pyqtSignal(str)

    def __init__(self, release: dict, parent=None):
        super().__init__(parent)
        self.release = dict(release)

    def run(self):
        import hashlib, zipfile
        from urllib.request import urlopen, Request
        rel = self.release
        try:
            expected = ""
            path = _library_cache_dir() / rel["zip_name"]
            if rel.get("sha_url"):
                req = Request(rel["sha_url"], headers={"User-Agent": "VideomancerControl"})
                try:
                    with urlopen(req, timeout=15) as resp:
                        sums = resp.read().decode()
                except OSError:
                    if not path.exists():
                        raise
                    # Offline: reuse the zip, checked against the digest we
                    # saved when it was first verified.
                    sums = _app_settings().value(f"library/sha/{rel['zip_name']}", "") or ""
                for line in sums.splitlines():
                    parts = line.split()
                    if parts and re.fullmatch(r"[0-9a-f]{64}", parts[0]) and \
                            (len(parts) == 1 or rel["zip_name"] in line):
                        expected = parts[0]
                        break
            if not (path.exists() and expected and
                    hashlib.sha256(path.read_bytes()).hexdigest() == expected):
                req = Request(rel["zip_url"], headers={"User-Agent": "VideomancerControl"})
                tmp = path.with_suffix(".part")
                with urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        self.progress.emit(got, total)
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected and digest != expected:
                path.unlink(missing_ok=True)
                self.failed.emit("Download didn't match LZX's published checksum — "
                                 "try again.")
                return
            rel["zip_path"] = str(path)
            rel["verified"] = bool(expected)
            if expected:
                _app_settings().setValue(f"library/sha/{rel['zip_name']}", expected)
            programs, manifest = [], None
            with zipfile.ZipFile(path) as z:
                if "programs/manifest.json" in z.namelist():
                    manifest = json.loads(z.read("programs/manifest.json").decode())
                by_file = {e.get("file"): e for e in (manifest or {}).get("programs", [])}
                for name in sorted(z.namelist()):
                    parts = name.split("/")
                    if not name.endswith(".vmprog") or len(parts) != 3 or parts[0] != "programs":
                        continue
                    data = z.read(name)
                    info = _vmprog_info(data) or {}
                    rel_file = f"{parts[1]}/{parts[2]}"
                    m = by_file.get(rel_file, {})
                    programs.append({
                        "file": rel_file, "zip_member": name, "size": len(data),
                        "sd_path": f"{SD_PROGRAMS}/{rel_file}",
                        "name": m.get("program_name") or info.get("name") or parts[2][:-7],
                        "author": m.get("author") or info.get("author", ""),
                        "version": m.get("program_version") or info.get("version", ""),
                        "description": m.get("description") or info.get("description", ""),
                        "categories": m.get("categories") or [],
                        "program_id": m.get("program_id") or info.get("program_id", ""),
                        "manifest_entry": m or None,
                    })
            rel["manifest"] = manifest
            self.done.emit(rel, programs)
        except Exception as exc:
            self.failed.emit(f"Library download failed: {exc}")


class _UpdateDownloader(QThread):
    """Downloads + unzips the platform-specific release asset, strips quarantine,
    and emits the path of the extracted .app bundle."""
    from PyQt6.QtCore import pyqtSignal
    progress = pyqtSignal(int, int)    # (downloaded_bytes, total_bytes)
    finished_ok = pyqtSignal(str)      # (extracted_bundle_path)
    failed = pyqtSignal(str)           # (message)

    @staticmethod
    def asset_name_for_platform() -> Optional[str]:
        if sys.platform == "darwin":
            # Universal2 build (Apple Silicon + Intel) since 2.6.
            return "VideomancerControl_macOS.zip"
        if sys.platform.startswith("win"):
            return "VideomancerControl_Windows.zip"
        return None

    def run(self):
        try:
            import tempfile, zipfile, subprocess
            from urllib.request import urlopen, Request

            asset_name = self.asset_name_for_platform()
            if not asset_name:
                self.failed.emit("No installer available for this platform.")
                return

            # Fetch latest release metadata, find the matching asset URL
            api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = Request(api, headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": "VideomancerControl"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            assets = data.get("assets") or []
            dl_url = next(
                (a.get("browser_download_url") for a in assets
                 if a.get("name") == asset_name),
                None,
            )
            if not dl_url:
                self.failed.emit(f"Release missing asset {asset_name!r}.")
                return

            tmpdir = Path(tempfile.mkdtemp(prefix="vmctl-update-"))
            zip_path = tmpdir / asset_name

            req = Request(dl_url, headers={"User-Agent": "VideomancerControl"})
            with urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            with zipfile.ZipFile(zip_path) as z:
                z.extractall(tmpdir)

            new_bundle = next(tmpdir.rglob("*.app"), None)
            if new_bundle is None:
                self.failed.emit("Downloaded zip contains no .app bundle.")
                return

            # Strip quarantine xattr so Gatekeeper doesn't re-prompt on launch
            subprocess.run(["xattr", "-cr", str(new_bundle)], check=False)
            self.finished_ok.emit(str(new_bundle))
        except Exception as exc:
            self.failed.emit(str(exc))

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
_orphan_threads: set = set()   # network QThreads outliving a closed window
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
HILITE        = "#7c3aed"   # lit toggles / active buttons
HILITE_BORDER = "#a855f7"
TRACK_BG      = "#0d0b1e"   # empty fader track
BAR_BG        = "#1a1433"   # modulation bar background
GRIP          = "#9988cc"   # fader grip lines
SEL_BG        = "#2d1f5e"   # update banner
SEL_BG2       = "#3d2f7e"
SEL_TEXT      = "#a78bfa"
SPARKLE       = "#ff66ff"
LOGO          = "#ffffff"   # header wordmark
LOGO_GLOW     = ""          # glow colour behind the wordmark ("" = none)

# Selectable colour themes (System tab). "purple" is the original look;
# "amber" follows the orange/cream-on-black Videomancer shirt art; "neon" is
# cyberpunk green. Fills stay deep enough for white text; the bright neon is
# kept for borders, highlights and the glowing wordmark.
THEMES = {
    "purple": dict(BG="#0f0d1f", SURFACE="#1e1a38", SURFACE2="#2d2650", BORDER="#9955cc",
                   ACCENT="#ffffff", ACCENT2="#c040c0", DIM="#7733bb", TEXT="#ffffff",
                   TEXT_DIM="#d8cfee", ERROR="#ff4466", WARN="#e0d0ff",
                   HILITE="#7c3aed", HILITE_BORDER="#a855f7", TRACK_BG="#0d0b1e",
                   BAR_BG="#1a1433", GRIP="#9988cc", SEL_BG="#2d1f5e", SEL_BG2="#3d2f7e",
                   SEL_TEXT="#a78bfa", SPARKLE="#ff66ff", LOGO="#ffffff", LOGO_GLOW=""),
    "amber":  dict(BG="#14120e", SURFACE="#211d17", SURFACE2="#2f2a22", BORDER="#d9822b",
                   ACCENT="#fff4dc", ACCENT2="#f0922e", DIM="#a8581a", TEXT="#f7ecd2",
                   TEXT_DIM="#cdbb98", ERROR="#ff5a3c", WARN="#ffd9a0",
                   HILITE="#d97414", HILITE_BORDER="#ffb057", TRACK_BG="#0c0a07",
                   BAR_BG="#1b1813", GRIP="#c8a878", SEL_BG="#3a2a14", SEL_BG2="#4d3818",
                   SEL_TEXT="#ffb057", SPARKLE="#ffd27a", LOGO="#f3d9a6", LOGO_GLOW=""),
    "neon":   dict(BG="#050907", SURFACE="#0b1510", SURFACE2="#122119", BORDER="#22c96b",
                   ACCENT="#eafff2", ACCENT2="#19c25f", DIM="#0c6b36", TEXT="#e8fff0",
                   TEXT_DIM="#9ed8b4", ERROR="#ff3b6b", WARN="#d6ffb8",
                   HILITE="#0b8f47", HILITE_BORDER="#39ff88", TRACK_BG="#030604",
                   BAR_BG="#09120d", GRIP="#7fdca5", SEL_BG="#0d2a1b", SEL_BG2="#133b26",
                   SEL_TEXT="#5dff9e", SPARKLE="#b6ff3b", LOGO="#39ff88", LOGO_GLOW="#16ff66"),
}
THEME = "purple"


def _apply_theme(name: str):
    """Swap the module-level colour names (widgets read them when they're
    built, painted widgets on every paint) and rebuild the app stylesheet."""
    global THEME, STYLESHEET
    THEME = name if name in THEMES else "purple"
    globals().update(THEMES[THEME])
    STYLESHEET = _build_stylesheet()

PARAM_RANGE = 1023   # 0–1023, centre = 512

# After the user edits a channel, ignore device readback for that channel
# this long so an in-flight poll reply can't yank the control back. Short
# enough that hardware knob moves show up promptly afterwards.
EDIT_GUARD_S = 1.0


def _cmd_safe_name(name: str, fallback: str = "preset") -> str:
    """Make a preset name safe to embed in a space-delimited serial command.
    The firmware's quoting rules are undocumented, so collapse whitespace to
    underscores and drop anything that isn't printable ASCII."""
    name = re.sub(r"\s+", "_", str(name).strip())
    name = re.sub(r"[^\x21-\x7e]", "", name)
    return name or fallback

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


def _build_stylesheet() -> str:
    return f"""
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


STYLESHEET = _build_stylesheet()



# ── Shared helpers ─────────────────────────────────────────────────────

def pill(text, color=None):
    c = color or ACCENT2
    lbl = QLabel(text)
    if c == ACCENT2:
        # OFFLINE pill: solid highlight bg, white border + text
        lbl.setStyleSheet(f"""
            QLabel {{
                background: {HILITE}; border: 2px solid #ffffff;
                border-radius: 8px; color: #ffffff;
                padding: 1px 8px; font-size: 9px;
                letter-spacing: 1px; font-weight: bold;
            }}
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
        self.status_pill = pill("OFFLINE", ACCENT2)
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
            f"QPushButton{{background:{SURFACE2};border:1px solid {HILITE};"
            f"border-radius:5px;color:#ffffff;font-size:11px;font-weight:bold;padding:0 10px;}}"
            f"QPushButton:hover{{background:{HILITE};color:#ffffff;}}"
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
        self.status_pill.setStyleSheet(f"""
            QLabel {{
                background:{HILITE};border:2px solid #ffffff;
                border-radius:8px;color:#ffffff;
                padding:1px 8px;font-size:9px;letter-spacing:1px;font-weight:bold;
            }}
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
_SPLASH_NEON_IMG_B64 = """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JknLnk179KgoI/acDldLDRSSLVxqLynrVN5ix61GTmiutJI96MFHYUsTSUUUywooooAKKKKACjNFFABk0u40lFADg5FOWYr3qOigVkXYb94z1rWstekiI+Y/nXOZxTg5FQ4pmE6MJ7o9G0zxjJCR85H412eh/EuexnjmhuJIZk5WSNyrKfYjkV4Wlyy96tw6m6fxGuaeHjI8XEZTSrbo+xvDP7WfjLRkVF12S7jAIEd8izD8yN3H1r0PS/24dajIF5pWl3SAAfu/MiY+vO5h+lfAkGvyJj5j+dXU8TSqPvn864ZYCEuh8liOD8FWd3SX3W/I/QJv25ptrY8NWYJ6H7a5/wDZK5jXP22vE10hWzh03TeT80cJkbHp87Efjivic+KZSPvn86gl8SSsD85qVl8F0OWlwTgIO/s187v8z6F8YftCeJPFAddS168u4z/yyMu2P/vhcL2HavLtW8cvLkB+K88n1qR8/MTVGW+dz1rshhYx6H1mEyKhh0lCKS8jodS8RSTk/MawLm9aUnmqrylu9Rk5rtjBRPo6WHhTWiHM5NNooqzqCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigByvirEN20ZHNVaKCXFPc3rTWnix8xras/FcsePnP51xAYinrMR3rJ04s46mEp1N0enWvjiRAP3h/OtKHx/IB/rP1ryNbth3qRb5x3NYvDxZ5s8poy6Hrv8Awn5x94VFN4/kI4k/WvKPt7+ppGvnPc1P1aJmsnoroei3fjiRwf3h/OsS88VSSZ+c/nXItdMe9RtKT3rWNGKO2nl1KGyNi61p5c5Y1my3bSE5NVixNJWyikejClGGyHM5NNooqjYKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAozRRQAu40vmGm0UAO8w0hcmkooFYXcaSiigYUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFAH//Z"""
_SPLASH_AMBER_IMG_B64 = """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iiuoyCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigArrPh38V/GHwm1hdV8HeJdT8N34IJl065aISezqPlcezAiiik0nowPs34Wf8FhPid4XjitfGehaR42tlwDcoDYXZHclo8xk/8AAB9a+ovBH/BX/wCDevxoniHS/EnhO4Iy5ktUvIQfQNG24/8AfAoorllhaUuljZVZrqfQHgv9sr4QfEBIToviwzvLjbHLpl3G3P1ix39a9KtPHug39tHPBqAkikGVbyZBkfitFFeXVoxg7I7ITclqcz4n/aG+Hvg2N31jxEtmqEhj9iuXwQcH7sR9RXg3jb/gqj8BfB5mjg1TW/EF1H/y76dpLoT/AMCmMYooroo4aE1dmU6so7Hzj8Sf+C0F7LHLD4B+HkVqzA+XfeI7syke/kxbR+bmvjL4zftk/F348CSDxX4zvpNKcn/iUaeRaWePQxR4Df8AA91FFehChTh8KOVzlLdnihOaKKK3ICiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooA/9k="""


# The wizard in each theme's colours (amber/neon made by scripts/theme_character.py)
_CHARACTER_B64 = {"purple": _SPLASH_IMG_B64, "amber": _SPLASH_AMBER_IMG_B64,
                  "neon": _SPLASH_NEON_IMG_B64}
_SPLASH_PM_CACHE: dict = {}  # theme -> wizard pixmap with the background removed


def _character_pixmap(width: int = 0):
    """The Videomancer wizard in the current theme, on a transparent
    background; scaled to `width` px wide when given."""
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtCore import QByteArray
    key = THEME if THEME in _CHARACTER_B64 else "purple"
    pm = _SPLASH_PM_CACHE.get(key)
    if pm is None:
        pm = QPixmap()
        pm.loadFromData(QByteArray.fromBase64(QByteArray(_CHARACTER_B64[key].encode())))
        pm = _SPLASH_PM_CACHE[key] = _make_transparent(pm)
    if width:
        return pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    return pm


def _make_transparent(pm, threshold=30):
    """Replace near-black pixels using fast bytearray scan."""
    from PyQt6.QtGui import QImage, QPixmap
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
    return QPixmap.fromImage(img2)


class _SplashWidget(QWidget):
    """Disconnected state — shows Videomancer character artwork."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(16)

        # Character image
        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_lbl.setStyleSheet("background:transparent;border:none;")
        try:
            img_lbl.setPixmap(_character_pixmap(340))
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

from PyQt6.QtWidgets import QStyledItemDelegate
from PyQt6.QtCore import QEvent


class _StarDelegate(QStyledItemDelegate):
    """Program rows: name on the left, a favourite star at the right end —
    ☆ outline for normal programs, ★ filled for favourites."""
    STAR_W = 34

    def __init__(self, is_fav, parent=None):
        super().__init__(parent)
        self._is_fav = is_fav

    def paint(self, painter, option, index):
        super().paint(painter, option, index)          # row as usual (text, selection)
        fav = self._is_fav(index.data(Qt.ItemDataRole.UserRole))
        painter.save()
        f = QFont(option.font)
        if f.pixelSize() > 0:                 # list font is set in px by the stylesheet
            f.setPixelSize(round(f.pixelSize() * 1.3))
        elif f.pointSizeF() > 0:
            f.setPointSizeF(f.pointSizeF() * 1.3)
        painter.setFont(f)
        painter.setPen(QColor(ACCENT2 if fav else TEXT_DIM))
        star_rect = option.rect.adjusted(option.rect.width() - self.STAR_W, 0, 0, 0)
        painter.drawText(star_rect, int(Qt.AlignmentFlag.AlignCenter),
                         "\u2605" if fav else "\u2606")
        painter.restore()

    def editorEvent(self, event, model, option, index):
        """Click on the star toggles the favourite (and is swallowed so it
        doesn't select or load the program)."""
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                            QEvent.Type.MouseButtonDblClick):
            if event.position().x() >= option.rect.right() - self.STAR_W:
                if event.type() == QEvent.Type.MouseButtonRelease:
                    self.parent().on_star(index.data(Qt.ItemDataRole.UserRole))
                return True
        return super().editorEvent(event, model, option, index)


def _app_settings():
    from PyQt6.QtCore import QSettings
    # VMCTL_SETTINGS points tests (or a second profile) at a separate INI
    # file so they never touch the user's real preferences.
    alt = os.environ.get("VMCTL_SETTINGS")
    if alt:
        return QSettings(alt, QSettings.Format.IniFormat)
    return QSettings("VIDEOWASTE", "Videomancer Control")


class ProgramsTab(QWidget):
    RECENT_MAX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: List[str] = []
        self._active: Optional[str] = None
        self._connected = False
        st = _app_settings()
        self._favorites: List[str] = list(st.value("programs/favorites", [], type=list) or [])
        self._recents: List[str] = list(st.value("programs/recents", [], type=list) or [])
        self._view = "all"   # all | fav | recent

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

        # View switch: all / favourites / recently loaded
        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        self._view_btns = {}
        for key, label in (("all", "ALL"), ("fav", "★ FAVORITES"), ("recent", "RECENT")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:{SURFACE2};border:1px solid {BORDER};"
                f"border-radius:4px;color:{TEXT_DIM};font-size:10px;font-weight:bold;"
                f"padding:3px 6px;}}"
                f"QPushButton:checked{{background:{DIM};color:#ffffff;border-color:#ffffff;}}"
            )
            b.clicked.connect(lambda _c, k=key: self._set_view(k))
            view_row.addWidget(b, stretch=1)
            self._view_btns[key] = b
        ll.addLayout(view_row)

        self.count_lbl = QLabel("—")
        self.count_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:10px;")
        ll.addWidget(self.count_lbl)

        self.list_widget = QListWidget()
        self.list_widget.on_star = self._toggle_favorite
        self.list_widget.setItemDelegate(_StarDelegate(lambda n: n in self._favorites,
                                                       self.list_widget))
        self.list_widget.setMouseTracking(True)
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
            self._char_lbl.setPixmap(_character_pixmap(200))
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
        if name:
            self._recents = ([name] + [r for r in self._recents if r != name])[:self.RECENT_MAX]
            _app_settings().setValue("programs/recents", self._recents)
            if self._view == "recent":
                self._rebuild(self.search.text())
        self._highlight_active()
        if self._selected == name:
            self.active_pill.setVisible(True)
            self.load_btn.setText("⬤  RELOAD PROGRAM")
        elif not self._selected:
            # Nothing selected — show the active program in the panel
            self.name_lbl.setText(name)
            self.active_pill.setVisible(True)
            self.desc_lbl.setVisible(False)
            self.load_btn.setText("⬤  RELOAD PROGRAM")
            self.load_btn.setEnabled(self._connected)
        else:
            # Selected is a different program — right pane shows the selection,
            # so the RUNNING pill (which describes the selected program) must
            # be hidden, and the load button should offer to load, not reload.
            self.active_pill.setVisible(False)
            self.load_btn.setText("⬤  LOAD PROGRAM")

    def set_loading_program(self, loading: bool):
        self.loading_note.setVisible(loading)
        self.load_btn.setEnabled(not loading and self._connected)

    def _highlight_active(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            raw = item.data(Qt.ItemDataRole.UserRole)
            label = raw
            if raw == self._active:
                item.setText(f"▶  {label}")
                item.setForeground(QColor('#ffffff'))
            else:
                item.setText(label)
                item.setForeground(QColor(TEXT))

    def _filter(self, text):
        self._rebuild(text)

    def _set_view(self, key: str):
        self._view = key
        for k, b in self._view_btns.items():
            b.setChecked(k == key)
        self._rebuild(self.search.text())

    def _toggle_favorite(self, name: Optional[str] = None):
        name = name or self._selected or self._active
        if not name:
            return
        if name in self._favorites:
            self._favorites.remove(name)
        else:
            self._favorites.append(name)
        _app_settings().setValue("programs/favorites", self._favorites)
        if self._view == "fav":
            self._rebuild(self.search.text())      # list membership changed
        else:
            self.list_widget.viewport().update()   # just repaint the star

    def _rebuild(self, filt):
        self.list_widget.clear()
        fl = filt.lower().strip()
        if self._view == "fav":
            names = [n for n in self._all if n in self._favorites]
        elif self._view == "recent":
            names = [n for n in self._recents if n in self._all]
        else:
            names = self._all
        for name in names:
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

# NOTE: Firmware rc16 reshuffled operator IDs. Verified so far against rc16:
#   31 → Audio Input (confirmed: `modulation status` shows s=31 for channel
#        set to Audio Input via device menu, and output responds to audio).
# The rest of this list still reflects rc15 naming and may be mislabeled on
# rc16 until we map each ID. If a dropdown entry doesn't do what its label
# says on rc16, the ID→name pairing here is the thing to fix.
OPERATORS = [
    (0,  "Disabled"),
    (1,  "Free LFO"),
    (2,  "Sync LFO"),
    (3,  "CV Input"),
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
    (31, "Audio Input"),   # rc16
    (32, "Mouse"),
    (33, "Keyboard"),
    (34, "Gamepad"),
    (35, "Tablet"),
    (36, "Joystick"),
    (37, "Sensor"),
    (38, "MIDI Turing"),
]


# Visual style for the ModBar waveform per operator. Anything not listed here
# falls back to the smooth curve. Keeps each modulator type readable at a glance.
OPERATOR_WAVEFORM_STYLES = {
    # Audio-ish — symmetric-around-center fill
    31: "audio",   # Audio Input (rc16 ID)
    4:  "audio",   # Audio Input (rc15 legacy — harmless if unused)
    10: "audio",   # FFT Band
    21: "audio",   # Ring Mod
    # Stepped / gated — rectangular steps
    7:  "stepped", # Sample & Hold
    8:  "stepped", # Trigger Env
    9:  "stepped", # Step Seq
    15: "stepped", # Euclidean Rhythm
    18: "stepped", # Comparator
    23: "stepped", # Pulse Width
    24: "stepped", # Peak Hold
    29: "stepped", # Clock Div
    30: "stepped", # Prob Gate
    # Random / noisy — sharp straight lines, no smoothing
    5:  "jagged",  # Random
    12: "jagged",  # Turing Machine
    22: "jagged",  # Cellular
    27: "jagged",  # Perlin Noise
    38: "jagged",  # MIDI Turing
}


def _waveform_style_for(op_id: int) -> str:
    return OPERATOR_WAVEFORM_STYLES.get(op_id, "smooth")

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
        # Time-based LERP: glide from current display to target over poll interval
        self._lerp_start_frac = 0.0
        self._lerp_target_frac = 0.0
        self._lerp_start_time = 0.0
        self._lerp_duration = 0.36   # ~1 poll interval → continuous hand-off
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
            self._lerp_start_frac = new_frac
            self._lerp_target_frac = new_frac
        else:
            # Time-based LERP from current display to new target.
            # Each new poll hands off smoothly mid-animation.
            self._lerp_start_frac = self._display_frac
            self._lerp_target_frac = new_frac
            self._lerp_start_time = time.monotonic()
        if not self._smooth_timer.isActive():
            self._smooth_timer.start()
        self.update()
        self.valueChanged.emit(v)

    def _ensure_timer(self):
        if not self._smooth_timer.isActive():
            self._smooth_timer.start()

    def _smooth_step(self):
        """Constant-velocity LERP from last display position to new target.
        Each poll-driven setValue() starts a new LERP — motion is continuous
        across poll boundaries instead of exp-decay catch-up-then-stall."""
        if self._user_dragging:
            self._smooth_timer.stop()
            return
        elapsed = time.monotonic() - self._lerp_start_time
        t = elapsed / self._lerp_duration if self._lerp_duration > 0 else 1.0
        if t >= 1.0:
            self._display_frac = self._lerp_target_frac
            self._tss_glide = False
            self.update()
            self._smooth_timer.stop()
            return
        self._display_frac = self._lerp_start_frac + \
            (self._lerp_target_frac - self._lerp_start_frac) * t
        self.update()

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
        # Time-based LERP state
        self._lerp_start = 0.0
        self._lerp_target = 0.0
        self._lerp_start_time = 0.0
        self._lerp_duration = 0.36

    def setRange(self, mn, mx):
        self._min, self._max = mn, mx

    def setValue(self, v: int, animate: bool = True):
        v = max(self._min, min(self._max, int(v)))
        if v == self._value:
            return
        self._value = v
        if not animate or self._dragging:
            self._display_val = float(v)
            self._lerp_start = self._display_val
            self._lerp_target = self._display_val
        else:
            self._lerp_start = self._display_val
            self._lerp_target = float(v)
            self._lerp_start_time = time.monotonic()
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
        elapsed = time.monotonic() - self._lerp_start_time
        t = elapsed / self._lerp_duration if self._lerp_duration > 0 else 1.0
        if t >= 1.0:
            self._display_val = self._lerp_target
            self._velocity = 0.0
            self.update()
            self._timer.stop()
            return
        self._display_val = self._lerp_start + (self._lerp_target - self._lerp_start) * t
        self._velocity = 0.0
        self.update()

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
            p.setBrush(QColor(TRACK_BG))
            p.drawRoundedRect(QRectF(track_x - track_w * 0.5, 0.0, track_w, off_h), 4.0, 4.0)

        # Filled track below handle
        if fill_h > 0:
            grad = QLinearGradient(0.0, h, 0.0, h - fill_h)
            grad.setColorAt(0, QColor(DIM))
            grad.setColorAt(1, QColor(ACCENT2))
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(track_x - track_w * 0.5, h - fill_h, track_w, fill_h), 4.0, 4.0)

        # Handle body
        handle_color = ACCENT2 if self._dragging else DIM
        border_color = ACCENT2 if self._dragging else BORDER
        p.setBrush(QColor(handle_color))
        p.setPen(QPen(QColor(border_color), 2))
        p.drawRoundedRect(QRectF(hx, hy - half_h, handle_w, handle_h), 4.0, 4.0)

        # Grip lines — 3 horizontal notches across centre
        line_color = QColor("#ffffff" if self._dragging else GRIP)
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
        # Time-based LERP state
        self._lerp_start = 0.0
        self._lerp_target = 0.0
        self._lerp_start_time = 0.0
        self._lerp_duration = 0.36

    def setRange(self, mn, mx):
        self._min, self._max = mn, mx

    def setValue(self, v: int, animate: bool = True):
        v = max(self._min, min(self._max, int(v)))
        if v == self._value:
            return
        self._value = v
        if not animate or self._dragging:
            self._display_val = float(v)
            self._lerp_start = self._display_val
            self._lerp_target = self._display_val
        else:
            self._lerp_start = self._display_val
            self._lerp_target = float(v)
            self._lerp_start_time = time.monotonic()
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
        elapsed = time.monotonic() - self._lerp_start_time
        t = elapsed / self._lerp_duration if self._lerp_duration > 0 else 1.0
        if t >= 1.0:
            self._display_val = self._lerp_target
            self._velocity = 0.0
            self.update()
            self._timer.stop()
            return
        self._display_val = self._lerp_start + (self._lerp_target - self._lerp_start) * t
        self._velocity = 0.0
        self.update()

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
            p.setBrush(QColor(TRACK_BG))
            p.drawRoundedRect(off_start, track_y - track_h//2, off_w, track_h, 3, 3)

        # Filled track left of handle
        fill_w = max(0, hx - half_w - left_edge + half_w)
        if fill_w > 0:
            grad = QLinearGradient(left_edge - half_w, 0, left_edge - half_w + fill_w, 0)
            grad.setColorAt(0, QColor(DIM))
            grad.setColorAt(1, QColor(ACCENT2))
            p.setBrush(grad)
            p.drawRoundedRect(left_edge - half_w, track_y - track_h//2, fill_w, track_h, 3, 3)

        # Handle
        handle_color = ACCENT2 if self._dragging else DIM
        border_color = ACCENT2 if self._dragging else BORDER
        p.setBrush(QColor(handle_color))
        p.setPen(QPen(QColor(border_color), 2))
        p.drawRoundedRect(hx - half_w, hy, handle_w, handle_h, 4, 4)

        # Grip lines — 3 vertical notches
        line_color = QColor("#ffffff" if self._dragging else GRIP)
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
                ACCENT2, HILITE, HILITE_BORDER, BORDER,
                "#ffffff", WARN, SPARKLE,
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
        c = QColor(ACCENT2)
        c.setAlpha(alpha)
        p.setPen(QPen(c, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(self._cx, self._cy), radius, radius)

        # Inner glow
        c2 = QColor(HILITE)
        c2.setAlpha(alpha // 2)
        p.setPen(QPen(c2, 1))
        p.drawEllipse(QPointF(self._cx, self._cy), radius * 0.6, radius * 0.6)

        p.end()


class ModBar(QWidget):
    """Rolling waveform showing live LFO output with smooth interpolation.
    The visual style can be switched per operator type so audio/stepped/jagged
    modulators read distinctly at a glance."""

    # Render styles
    STYLE_SMOOTH  = "smooth"   # default — cubic bezier curve, bottom-referenced
    STYLE_AUDIO   = "audio"    # symmetric around center, denser fill (audio)
    STYLE_STEPPED = "stepped"  # rectangular steps (sequencer/gate-like)
    STYLE_JAGGED  = "jagged"   # straight sharp lines, no smoothing (random/noise)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._max = PARAM_RANGE
        self._active = False
        self._history = []
        self._history_len = 600  # longer time window — slower scroll
        self._display_val = 0.0  # smoothed current value
        self._target_val = 0.0
        self._style = self.STYLE_SMOOTH
        self.setFixedHeight(40)
        self.setMinimumWidth(10)

        # Smooth render timer — only active when animating
        self._render_timer = QTimer()
        self._render_timer.setInterval(16)
        self._render_timer.timeout.connect(self._interpolate)

    def setStyle(self, style: str):
        if style != self._style:
            self._style = style
            self.update()

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
        # Audio wants punchy response (see the signal move); other styles
        # look cleaner with more smoothing (LFOs, sequencers, etc.)
        factor = 0.40 if self._style == self.STYLE_AUDIO else 0.10
        if abs(diff) > 0.1:
            self._display_val += diff * factor
        elif abs(diff) > 0.01:
            self._display_val = self._target_val
        self._history.append(self._display_val)
        if len(self._history) > self._effective_history_len():
            self._history = self._history[-self._effective_history_len():]
        self.update()

    def _effective_history_len(self) -> int:
        # Scroll speed stays the same across all styles; detail comes from the
        # per-style interpolation factor above, not a shorter window.
        return self._history_len

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
        p.setBrush(QColor(BAR_BG))
        p.drawRoundedRect(0, 0, w, h, 3, 3)

        if not self._active or len(self._history) < 2:
            p.end()
            return

        from PyQt6.QtCore import QPointF
        n = len(self._history)
        hist_len = self._effective_history_len()
        step_x = w / max(1, hist_len - 1)
        start_x = w - (n - 1) * step_x

        fill_color = QColor(HILITE)
        fill_color.setAlpha(60)
        line_color = QColor(ACCENT2)

        if self._style == self.STYLE_AUDIO:
            # Symmetric around vertical center — looks like an audio waveform.
            # Excursion scales with distance from the mid-point of the range.
            center_y = h * 0.5
            max_excursion = h * 0.5 - 2
            top_pts, bot_pts = [], []
            for i, val in enumerate(self._history):
                x = start_x + i * step_x
                # Value 0 → no excursion; value at max → full top/bottom
                frac = (val - self._max * 0.5) / (self._max * 0.5)
                dy = max_excursion * frac
                top_pts.append((x, center_y - abs(dy)))
                bot_pts.append((x, center_y + abs(dy)))
            # Fill between top and bottom envelope
            fill = QPainterPath()
            fill.moveTo(QPointF(top_pts[0][0], top_pts[0][1]))
            for x, y in top_pts[1:]:
                fill.lineTo(QPointF(x, y))
            for x, y in reversed(bot_pts):
                fill.lineTo(QPointF(x, y))
            fill.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill_color)
            p.drawPath(fill)
            # Center reference line
            p.setPen(QPen(line_color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            top_path = QPainterPath()
            bot_path = QPainterPath()
            top_path.moveTo(QPointF(*top_pts[0]))
            bot_path.moveTo(QPointF(*bot_pts[0]))
            for x, y in top_pts[1:]:
                top_path.lineTo(QPointF(x, y))
            for x, y in bot_pts[1:]:
                bot_path.lineTo(QPointF(x, y))
            p.drawPath(top_path)
            p.drawPath(bot_path)
            p.end()
            return

        # Common point list (bottom-referenced)
        points = []
        for i, val in enumerate(self._history):
            x = start_x + i * step_x
            y = h - (val / self._max) * (h - 2)
            points.append((x, y))

        if self._style == self.STYLE_STEPPED:
            # Flat segments with vertical transitions — sequencer / gate look
            path = QPainterPath()
            path.moveTo(QPointF(points[0][0], points[0][1]))
            for i in range(1, len(points)):
                x0, y0 = points[i - 1]
                x1, y1 = points[i]
                path.lineTo(QPointF(x1, y0))  # hold
                path.lineTo(QPointF(x1, y1))  # step
            fill_path = QPainterPath(path)
            fill_path.lineTo(QPointF(points[-1][0], h))
            fill_path.lineTo(QPointF(points[0][0], h))
            fill_path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill_color)
            p.drawPath(fill_path)
            p.setPen(QPen(line_color, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.end()
            return

        if self._style == self.STYLE_JAGGED:
            # Straight sharp lines, no smoothing — emphasizes abrupt changes
            path = QPainterPath()
            path.moveTo(QPointF(points[0][0], points[0][1]))
            for x, y in points[1:]:
                path.lineTo(QPointF(x, y))
            fill_path = QPainterPath(path)
            fill_path.lineTo(QPointF(points[-1][0], h))
            fill_path.lineTo(QPointF(points[0][0], h))
            fill_path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill_color)
            p.drawPath(fill_path)
            p.setPen(QPen(line_color, 1.5, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.end()
            return

        # STYLE_SMOOTH (default) — cubic bezier through samples
        def _smooth_path(pts):
            path = QPainterPath()
            path.moveTo(QPointF(pts[0][0], pts[0][1]))
            if len(pts) == 2:
                path.lineTo(QPointF(pts[1][0], pts[1][1]))
                return path
            for i in range(1, len(pts)):
                x0, y0 = pts[i - 1]
                x1, y1 = pts[i]
                tension = step_x * 0.4
                path.cubicTo(
                    QPointF(x0 + tension, y0),
                    QPointF(x1 - tension, y1),
                    QPointF(x1, y1),
                )
            return path

        curve = _smooth_path(points)
        fill_path = QPainterPath(curve)
        fill_path.lineTo(QPointF(points[-1][0], h))
        fill_path.lineTo(QPointF(points[0][0], h))
        fill_path.closeSubpath()

        p.setBrush(fill_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(fill_path)

        p.setPen(QPen(line_color, 1.5, Qt.PenStyle.SolidLine,
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
        # Only fade "Unused" slots once a program has reported its parameters
        self._program_loaded = False
        self.on_value_label_refresh = None

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
                    background:{HILITE}; border:2px solid #ffffff;
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
        # Programs mark spare slots "Unused" (or omit them) — fade those so the
        # controls that matter stand out.
        unused = (not name) or name.strip().lower() == "unused"
        self._set_unused(unused and self._program_loaded)
        self._refresh_value_label()
        if name and name not in (f"P{self.index+1}", f"{self.index+1}", ""):
            self._num_lbl.setText(f"{self.index+1} -")
            self._param_name_lbl.setText(name.upper())
            self._param_name_lbl.setVisible(True)
        else:
            self._num_lbl.setText(f"{self.index+1}")
            self._param_name_lbl.setVisible(False)

    def _set_unused(self, unused: bool):
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        if unused:
            eff = QGraphicsOpacityEffect(self)
            eff.setOpacity(0.35)
            self.setGraphicsEffect(eff)
        else:
            self.setGraphicsEffect(None)
        self.setToolTip("Not used by this program" if unused else "")

    def _format_value(self, raw: int) -> str:
        """Format a raw 0-1023 value in the program's own units.

        `program info` reports each parameter's real range (e.g. Delay Depth
        0-2048, Diff Gain 0-200). 0-100 ranges read as a percentage; anything
        else shows the scaled value the program actually receives."""
        lo, hi = self._param_min, self._param_max
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) \
                or hi <= lo or (lo, hi) == (0, 100):
            return f"{round(raw / 10.23)}%"
        return f"{round(lo + (raw / PARAM_RANGE) * (hi - lo))}"

    def _refresh_value_label(self):
        """Re-render the value readout after the parameter range changes."""
        if self.val_lbl and (self.knob or self.slider):
            src = self.knob or self.slider
            self.val_lbl.setText(self._format_value(src.value()))
        if self.slider is not None and self.on_value_label_refresh:
            self.on_value_label_refresh(self.slider.value())

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
        if hasattr(self, "_out_bar"):
            self._out_bar.setStyle(_waveform_style_for(op_id))
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
        if hasattr(self, "_out_bar"):
            self._out_bar.setStyle(_waveform_style_for(op_id))
        self.op_combo.blockSignals(True)
        self.op_combo.setCurrentIndex(idx)
        self.op_combo.blockSignals(False)
        self._updating = False

    def set_tss(self, t: int, sp: int, sl: int, silent: bool = True):
        if not self._tss_sliders:
            return
        self._updating = silent
        try:
            for knob, val in zip(self._tss_sliders, (t, sp, sl)):
                if knob._user_dragging:
                    continue
                # Route through setValue so the time-based LERP is set up
                # correctly; `_updating` guards against the re-entry loop.
                knob.setValue(int(val), animate=True)
        finally:
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
        """Style the operator combo to indicate active LFO vs disabled, and
        lock Time/Space/Slope while the source is Disabled — the firmware
        ignores them then (verified on rc.55), so turning them did nothing."""
        active = op_id != 0
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        for knob in getattr(self, "_tss_sliders", []):
            if knob.isEnabled() == active:
                continue
            knob.setEnabled(active)
            if active:
                knob.setGraphicsEffect(None)
                knob.setToolTip("")
            else:
                eff = QGraphicsOpacityEffect(knob)
                eff.setOpacity(0.35)
                knob.setGraphicsEffect(eff)
                knob.setToolTip("Pick a modulation source to use Time / Space / Slope")
        # ModBar stays active — shows output regardless of operator
        if active:
            # Lit up — purple background, white text
            self.op_combo.setStyleSheet(
                f"QComboBox{{background:{HILITE};border:1px solid {HILITE_BORDER};"
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

        # Randomize the program's knobs; Undo steps back through randomizes
        self._history: List[tuple] = []
        self.random_btn = QPushButton("\U0001F3B2  RANDOMIZE")
        self.random_btn.setToolTip("Randomize knobs 1–6 and switches 7–10 (R).\n"
                                   "Dry/Wet, Bypass and unused controls are left alone.")
        self.undo_btn = QPushButton("\u21B6  UNDO")
        self.undo_btn.setToolTip("Restore the values from before the last randomize (\u2318Z)")
        for b in (self.random_btn, self.undo_btn):
            b.setFixedHeight(30)
            b.setEnabled(False)
            tl.addWidget(b)
        self.random_btn.clicked.connect(self.randomize)
        self.undo_btn.clicked.connect(self.undo)

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
            card12.on_value_label_refresh = (
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
        # TSS starts locked while the source is Disabled. Deferred: the P7–P11
        # switch cards are built further down this constructor.
        QTimer.singleShot(0, lambda: [c._update_tss_enabled(c.get_operator())
                                      for c in self.channels])
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
        self.reset_sync_caches()
        self._set_enabled(v)
        self.refresh_btn.setEnabled(v)
        self.start_btn.setEnabled(v)
        self.random_btn.setEnabled(v)
        self.undo_btn.setEnabled(v and bool(self._history))
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
                card._program_loaded = False
                card._set_unused(False)
                if hasattr(card, '_param_name_lbl'):
                    card._param_name_lbl.setVisible(False)
                if hasattr(card, '_tss_sliders') and card._tss_sliders:
                    card.set_tss(0, 0, 0, silent=True)

    def set_program(self, name: str):
        self.prog_lbl.setText(name or "No program loaded")
        self._history.clear()          # undo steps belong to one program
        self.undo_btn.setEnabled(False)

    def apply_param_labels(self, params: list):
        """Update channel card labels from program info parameter names."""
        for i, card in enumerate(self.channels):
            card._program_loaded = bool(params)
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

    def _capture(self) -> tuple:
        return ([c.get_manual() for c in self.channels],
                [c.get_operator() for c in self.channels])

    def randomize(self):
        import random
        self._history = (self._history + [self._capture()])[-20:]
        for i, card in enumerate(self.channels):
            name = card._param_name_lbl.text().lower()
            if card.graphicsEffect() is not None or i >= 10 or "bypass" in name:
                continue   # unused slot, Bypass, or the Dry/Wet fader
            v = random.choice((0, PARAM_RANGE)) if card._is_toggle else random.randint(0, PARAM_RANGE)
            card.set_manual(v, silent=True)
            self._manual_changed(i, v)
        self.undo_btn.setEnabled(True)

    def undo(self):
        if not self._history:
            return
        m, sr = self._history.pop()
        for i, card in enumerate(self.channels):
            card.set_manual(m[i], silent=True)
            self._manual_changed(i, m[i])
            if sr[i] != card.get_operator():
                card.set_operator(sr[i])
                self._mod_changed(i, "sr", sr[i])
        self.undo_btn.setEnabled(bool(self._history))

    def reset_sync_caches(self):
        """Forget what we last sent / last saw — call on connect, disconnect
        and program change so stale values can't suppress sends or updates."""
        self._last_sent.clear()
        self._prev_mod = [None] * 12

    def _recently_edited(self, i: int, now: float) -> bool:
        return now - self._last_sent.get(f"edit_time_{i}", 0) < EDIT_GUARD_S

    def _note_device_value(self, i: int, m=None, sr=None, t=None, sp=None, sl=None):
        """Record a value the device reported, so the send-dedup compares
        against the device's real state rather than our last send."""
        for key, val in (("m", m), ("sr", sr), ("t", t), ("sp", sp), ("sl", sl)):
            if val is not None:
                self._last_sent[f"{key}{i}"] = val

    def set_tss_panel(self, ch: int, t: int, sp: int, sl: int):
        """Update per-card TSS sliders from device state."""
        if ch < len(self.channels):
            self.channels[ch].set_tss(t, sp, sl, silent=True)
            self._note_device_value(ch, t=t, sp=sp, sl=sl)

    def apply_state(self, m: list, t: list, sp: list, sl: list, sr: list):
        """Apply full state including TSS panel — skip recently edited channels."""
        now = __import__('time').monotonic()
        for i in range(min(12, len(m))):
            if self._recently_edited(i, now):
                continue
            card = self.channels[i]
            card.set_manual(m[i] if i < len(m) else 0)
            card.set_operator(sr[i] if i < len(sr) else 0)
            self._note_device_value(i, m=m[i], sr=sr[i] if i < len(sr) else 0)
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
            recently_edited = self._recently_edited(i, now)
            m = mod.get("m", 0)
            if not recently_edited:
                card.set_manual(m)
                s = mod.get("s", 0)
                if s != card.get_operator():
                    card.set_operator(s)
                self._note_device_value(i, m=m, sr=s)
            else:
                # Re-check next poll: this reply may predate our edit
                self._prev_mod[i] = None
            # Always update output bar — shows live modulation even during edits
            card.set_output(mod.get("o", m))

            # Update TSS from fast poll if present
            if not recently_edited and ("t" in mod or "sp" in mod or "sl" in mod):
                t_val  = mod.get("t", 0)
                sp_val = mod.get("sp", 0)
                sl_val = mod.get("sl", 0)
                card.set_tss(t_val, sp_val, sl_val, silent=True)

    def set_transport_state(self, state: str):
        """Update play/stop button styling based on transport state."""
        playing = state.lower() in ("playing", "running", "started")
        self.transport_playing = playing
        if playing:
            self.start_btn.setStyleSheet(
                f"QPushButton{{background:{HILITE};border:2px solid {HILITE_BORDER};"
                f"color:#ffffff;font-weight:bold;border-radius:4px;padding:7px 18px;}}"
            )
            self.stop_btn.setStyleSheet("")
        else:
            self.stop_btn.setStyleSheet(
                f"QPushButton{{background:{HILITE};border:2px solid {HILITE_BORDER};"
                f"color:#ffffff;font-weight:bold;border-radius:4px;padding:7px 18px;}}"
            )
            self.start_btn.setStyleSheet("")
            self.start_btn.setObjectName("primary")
            self.start_btn.style().polish(self.start_btn)

    def flash_tap(self):
        """Briefly light up the TAP button — color only, no bounce."""
        self.tap_btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT2};border:2px solid #ffffff;"
            f"border-radius:4px;color:#ffffff;font-weight:bold;padding:7px 18px;}}"
            f"QPushButton:pressed{{background:{ACCENT2};border:2px solid #ffffff;color:#ffffff;}}"
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
        if last == value and not self.channels[index]._is_toggle:
            return
        self._last_sent[f"m{index}"] = value
        self._last_sent[f"edit_time_{index}"] = __import__('time').monotonic()
        if self.on_param_change:
            self.on_param_change(index, value)

    def _mod_changed(self, index: int, field: str, value: int):
        """Direct send — no timer, deduplicate."""
        key = f"{field}{index}"
        last = self._last_sent.get(key)
        if last == value and field != "sr":
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
        if hasattr(mw, "_queue_cmd") and mw._worker:
            mw._queue_cmd("bpm", f"transport bpm {val}")

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
        # Cap history — an unbounded QTextEdit slows every append over a
        # long session.
        self.text.document().setMaximumBlockCount(2000)
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
        try:
            from PyQt6.QtWidgets import QApplication
            # Copy last 500 lines to avoid huge clipboard dumps
            lines = self.text.toPlainText().splitlines()
            text = "\n".join(lines[-500:])
            QApplication.clipboard().setText(text)
            # Flash "COPIED" on the button
            if hasattr(self, '_copy_btn_ref') and self._copy_btn_ref:
                self._copy_btn_ref.setText("COPIED")
                QTimer.singleShot(1500, lambda: self._copy_btn_ref.setText("COPY"))
        except Exception:
            pass

    def _fmt(self, color: str):
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        return f

    def append(self, prefix: str, key: str, payload: str):
        # Skip high-frequency modulation logs to prevent event loop flooding
        if key == "modulation" and prefix == "ok":
            return
        # Harmless: firmware returns this after a program load when the
        # program has no user presets yet. Not actionable, clutters the console.
        if prefix == "error" and key == "1543503875":
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
        # Don't mkdir here — that eagerly touches ~/Documents at launch
        # and triggers macOS TCC prompt before the user has done anything.
        # The folder is created lazily on first save.
        self.folder = folder or (
            Path.home() / "Documents" / "VideomancerSnapshots"
        )

    def _ensure_folder(self):
        self.folder.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def save(self, label: str, program: str, parameters: List[int],
             presets: dict, settings: dict, tss: dict = None,
             path: Optional[Path] = None) -> Path:
        """Write a snapshot file and return its path.

        If `path` is given, write there. Otherwise, auto-name in the default
        snapshots folder.
        """
        ts   = datetime.now()
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
        if path is None:
            slug = re.sub(r"[^\w\-]", "_", label.strip())[:40] or "snapshot"
            fname = f"{ts.strftime('%Y%m%d_%H%M%S')}_{slug}.json"
            self._ensure_folder()
            path = self.folder / fname
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def default_filename(self, label: str) -> str:
        ts = datetime.now()
        slug = re.sub(r"[^\w\-]", "_", label.strip())[:40] or "snapshot"
        return f"{ts.strftime('%Y%m%d_%H%M%S')}_{slug}.json"

    def list_snapshots(self) -> List[dict]:
        """Return metadata for all snapshots, newest first."""
        # Don't touch ~/Documents if the folder doesn't exist yet —
        # that would trigger macOS TCC prompt unnecessarily.
        if not self.folder.exists():
            return []
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
        self._ensure_folder()
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))


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


class _VMConfirmDialog(QDialog):
    """Dark-themed confirmation dialog with Videomancer character as icon."""

    def __init__(self, title: str, message: str, parent=None, buttons="yes_cancel",
                 input_default: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.input: Optional[QLineEdit] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(14)

        body = QHBoxLayout()
        body.setSpacing(16)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        icon_lbl.setStyleSheet("background:transparent;border:none;")
        try:
            icon_lbl.setPixmap(_character_pixmap(96))
        except Exception:
            pass
        body.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(8)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            "color:#ffffff;font-size:18px;font-weight:bold;"
            "letter-spacing:1px;background:transparent;border:none;"
        )
        text_col.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(
            f"color:{TEXT};font-size:13px;background:transparent;border:none;"
        )
        text_col.addWidget(msg_lbl)

        if input_default is not None:
            self.input = QLineEdit(input_default)
            self.input.selectAll()
            text_col.addWidget(self.input)

        text_col.addStretch(1)

        body.addLayout(text_col, stretch=1)
        root.addLayout(body, stretch=1)

        if buttons == "ok":
            btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, parent=self)
            btn_box.accepted.connect(self.accept)
        elif buttons == "ok_cancel":
            btn_box = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                parent=self,
            )
            btn_box.accepted.connect(self.accept)
            btn_box.rejected.connect(self.reject)
        else:
            btn_box = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel,
                parent=self,
            )
            btn_box.accepted.connect(self.accept)
            btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box, alignment=Qt.AlignmentFlag.AlignRight)

    @classmethod
    def ask(cls, parent, title: str, message: str) -> bool:
        return cls(title, message, parent=parent).exec() == QDialog.DialogCode.Accepted

    @classmethod
    def notify(cls, parent, title: str, message: str) -> None:
        cls(title, message, parent=parent, buttons="ok").exec()

    @classmethod
    def ask_text(cls, parent, title: str, message: str, default: str = "") -> Optional[str]:
        dlg = cls(title, message, parent=parent, buttons="ok_cancel", input_default=default)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.input is not None:
            return dlg.input.text()
        return None


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
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(10)

        LBL = f"color:{TEXT_DIM};font-size:14px;{self._TRANSPARENT}"
        VAL = f"color:{TEXT};font-size:15px;font-weight:bold;{self._TRANSPARENT}"

        def field_rows(layout, rows, store, min_w=78):
            """Label/value rows in a tight 2-column grid; values elide rather
            than push the column wider."""
            g = QGridLayout()
            g.setHorizontalSpacing(10)
            g.setVerticalSpacing(4)
            g.setColumnStretch(1, 1)
            for r, (label, key) in enumerate(rows):
                lbl = QLabel(label)
                lbl.setStyleSheet(LBL)
                lbl.setMinimumWidth(min_w)
                val = QLabel("\u2014")
                val.setStyleSheet(VAL)
                val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                g.addWidget(lbl, r, 0)
                g.addWidget(val, r, 1)
                store[key] = val
            layout.addLayout(g)

        top_row = QHBoxLayout()
        theme_lbl = QLabel("Theme")
        theme_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:13px;{self._TRANSPARENT}")
        top_row.setSpacing(6)
        top_row.addWidget(theme_lbl)
        top_row.addSpacing(6)
        self.on_theme = None           # (name) — set by the main window
        self._theme_btns = {}
        for key, label in (("purple", "PURPLE"), ("amber", "AMBER"), ("neon", "NEON")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(key == THEME)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            sw = THEMES[key]
            b.setStyleSheet(
                f"QPushButton{{background:{SURFACE2};border:1px solid {BORDER};border-radius:4px;"
                f"color:{TEXT_DIM};font-size:11px;font-weight:bold;padding:5px 12px;}}"
                f"QPushButton:checked{{background:{sw['HILITE']};border-color:{sw['ACCENT']};"
                f"color:#ffffff;}}")
            b.clicked.connect(lambda _c, k=key: self._pick_theme(k))
            top_row.addWidget(b)
            self._theme_btns[key] = b
        top_row.addStretch(1)
        # The tab refreshes itself when opened; the button is kept (hidden)
        # only because other code toggles its enabled state.
        self.refresh_btn = QPushButton("\u21bb  Refresh", self)
        self.refresh_btn.setVisible(False)
        self.refresh_btn.clicked.connect(self._refresh)
        # (added to the FIRMWARE box below — saves a row so the tab fits)

        # Two equal columns; every section visible at the default window size
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # ·· VIDEO: input select, live status, timing ··
        vid_grp = QGroupBox("VIDEO")
        vl = QVBoxLayout(vid_grp)
        vl.setSpacing(8)

        src_row = QHBoxLayout()
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

        self._status_fields = {}
        field_rows(vl, [("Input", "source"), ("Timing", "timing"),
                        ("Frame rate", "framerate")], self._status_fields)

        # Signal: lock / ext sync / HDMI out. (The firmware has no CVBS vs
        # component select, so this only reports `video status`.)
        self._analog_row = QHBoxLayout()
        analog_lbl = QLabel("Signal")
        analog_lbl.setStyleSheet(LBL)
        analog_lbl.setMinimumWidth(78)
        self._analog_row.addWidget(analog_lbl)
        self._signal_lbl = QLabel("\u2014")
        self._signal_lbl.setStyleSheet(VAL)
        self._signal_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._analog_row.addWidget(self._signal_lbl, stretch=1)
        self._analog_widget = QWidget()
        self._analog_widget.setStyleSheet(self._TRANSPARENT)
        self._analog_row.setContentsMargins(0, 0, 0, 0)
        self._analog_widget.setLayout(self._analog_row)
        vl.addWidget(self._analog_widget)

        timing_row = QHBoxLayout()
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
        self.timing_combo.setToolTip("Output timing. Applying restarts video output.")
        timing_row.addWidget(self.timing_combo, stretch=1)
        apply_timing_btn = QPushButton("SET OUTPUT")
        apply_timing_btn.setToolTip("Apply the selected output timing (restarts video output)")
        apply_timing_btn.clicked.connect(self._apply_timing)
        timing_row.addWidget(apply_timing_btn)
        vl.addLayout(timing_row)
        grid.addWidget(vid_grp, 0, 0)

        # ·· FIRMWARE ··
        fw_grp = QGroupBox("FIRMWARE")
        fl = QVBoxLayout(fw_grp)
        fl.setSpacing(8)
        self._fw_fields = {}
        field_rows(fl, [("Device", "version"), ("Latest", "latest"),
                        ("App", "app"), ("Uptime", "uptime")], self._fw_fields)
        # App row is static — show the running version always
        self._fw_fields["app"].setText(f"v{APP_VERSION}")

        # LZX links (filled in below): Connect, Firmware Releases, Manual, Forum
        self._fw_links = QGridLayout()
        self._fw_links.setHorizontalSpacing(6)
        self._fw_links.setVerticalSpacing(6)
        fl.addSpacing(4)
        fl.addLayout(self._fw_links)
        fl.addStretch()
        self._device_fw = ""
        self._latest_fw = ""
        grid.addWidget(fw_grp, 0, 1)

        # ·· STORAGE (SD card as USB mass storage) ··
        storage_grp = QGroupBox("SD CARD AS USB DRIVE")
        stg = QVBoxLayout(storage_grp)
        stg.setSpacing(6)
        storage_note = QLabel(
            "Mount the SD card on this computer to sideload programs or back "
            "up snapshots. Program loading pauses until you eject it."
        )
        storage_note.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;{self._TRANSPARENT}")
        storage_note.setWordWrap(True)
        stg.addWidget(storage_note)
        self.msd_btn = QPushButton("MOUNT AS USB DRIVE")
        self.msd_btn.setEnabled(False)
        self.msd_btn.clicked.connect(self._on_msd_click)
        stg.addWidget(self.msd_btn)

        self._msd_state = "idle"  # idle | waiting | mounted
        # Failsafe: optimistically promote waiting → mounted after 15 s
        # if no `{"active":true}` arrives (USB re-enum can delay replies).
        self._msd_wait_timer = QTimer(self)
        self._msd_wait_timer.setSingleShot(True)
        self._msd_wait_timer.setInterval(15000)
        self._msd_wait_timer.timeout.connect(self._msd_wait_timeout)
        # Promotion delay: once the device says `{"active":true}`, keep
        # showing MOUNTING… for a few more seconds to line up with when
        # the drive actually appears in Finder on the host side.
        self._msd_promote_timer = QTimer(self)
        self._msd_promote_timer.setSingleShot(True)
        self._msd_promote_timer.setInterval(4000)
        self._msd_promote_timer.timeout.connect(self._promote_msd_if_waiting)
        # Polls `msd status` every 2 s so the UI tracks true device state
        # from `{"active":true/false}`.
        self._msd_poll_timer = QTimer(self)
        self._msd_poll_timer.setInterval(2000)
        self._msd_poll_timer.timeout.connect(self._msd_poll_status)
        grid.addWidget(storage_grp, 1, 0)

        # ·· MIDI CC map: all 12 at once, 4 columns x 3 rows ··
        midi_grp = QGroupBox("MIDI CC  (MSB / LSB)")
        mg = QGridLayout(midi_grp)
        mg.setHorizontalSpacing(16)
        mg.setVerticalSpacing(4)
        self._midi_cells = []
        for i in range(12):
            cell = QLabel(f"P{i+1}  \u2014")
            cell.setStyleSheet(f"color:{TEXT};font-size:14px;{self._TRANSPARENT}")
            mg.addWidget(cell, i % 3, i // 3)
            self._midi_cells.append(cell)
        grid.addWidget(midi_grp, 2, 0, 1, 2)

        # ·· OSC remote ··
        osc_grp = QGroupBox("OSC REMOTE")
        og = QVBoxLayout(osc_grp)
        og.setSpacing(6)
        orow = QHBoxLayout()
        self.on_osc = None                   # (enabled, port) — set by the app
        self.osc_btn = QPushButton("OFF")
        self.osc_btn.setCheckable(True)
        self.osc_btn.setFixedWidth(64)
        self.osc_btn.clicked.connect(self._osc_changed)
        orow.addWidget(self.osc_btn)
        plbl = QLabel("Port")
        plbl.setStyleSheet(LBL)
        orow.addWidget(plbl)
        from PyQt6.QtWidgets import QSpinBox
        self.osc_port = QSpinBox()
        self.osc_port.setRange(1024, 65535)
        self.osc_port.setValue(_app_settings().value("osc/port", 9000, type=int))
        self.osc_port.setFixedWidth(90)
        self.osc_port.editingFinished.connect(
            lambda: self.osc_btn.isChecked() and self.osc_port.value() != _OSC["port"]
            and self._osc_changed())
        orow.addWidget(self.osc_port)
        self.osc_status = QLabel("Off")
        self.osc_status.setStyleSheet(VAL)
        self.osc_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        orow.addWidget(self.osc_status, stretch=1)
        og.addLayout(orow)
        self.osc_help = QLabel(
            "/videomancer/bpm \u00b7 /play \u00b7 /stop \u00b7 /tap \u00b7 /param/1\u201312 "
            "\u00b7 /program \u00b7 /randomize \u2026   (hover for all)   "
            "\u26a0 anyone on your network can send while on")
        self.osc_help.setToolTip(OSC_HELP)
        self.osc_help.setWordWrap(False)
        self.osc_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.osc_help.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;{self._TRANSPARENT}")
        og.addWidget(self.osc_help)
        grid.addWidget(osc_grp, 3, 0, 1, 2)

        root.addLayout(grid)

        # ·· LZX links (in the Firmware box) + Application settings ··
        res_grp = QGroupBox("APPLICATION SETTINGS")
        rg = QGridLayout(res_grp)
        rg.setHorizontalSpacing(8)
        rg.setVerticalSpacing(6)
        self.on_open_library = None      # set by the main window
        self._doc_links = [
            ("LZX Connect", "Update firmware and install program libraries",
             LZX_CONNECT_URL),
            ("Videomancer Manual", "Official guide and serial command reference",
             "https://lzxindustries.net/instruments/videomancer/manual"),
            ("Firmware Releases", "Version history and release notes",
             FIRMWARE_RELEASES_URL),
            ("Community Forum", "Help desk, patches and discussion",
             "https://community.lzxindustries.net/"),
            ("App Releases", "Videomancer Control downloads and changelog",
             f"https://github.com/{GITHUB_REPO}/releases"),
        ]
        self._pill_css = (
            f"QPushButton{{background:{SURFACE2};border:1px solid {BORDER};"
            f"border-radius:12px;color:#ffffff;font-size:12px;font-weight:bold;"
            f"padding:5px 12px;}}"
            f"QPushButton:hover{{background:{DIM};border-color:#ffffff;}}")

        def pill(title, blurb, url):
            btn = QPushButton(title + ("  \u2197" if url.startswith("http") else "  \u2192"))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(blurb + ("" if url == "library:" else f"\n{url}"))
            btn.setStyleSheet(self._pill_css)
            if url == "library:":
                btn.clicked.connect(lambda _c: self.on_open_library and self.on_open_library())
            else:
                btn.clicked.connect(lambda _c, u=url: self._open_doc(u))
            return btn

        # Firmware links live with the firmware info …
        firmware_titles = ("LZX Connect", "Firmware Releases",
                           "Videomancer Manual", "Community Forum")
        self._connect_pill = None
        i = 0
        for title in firmware_titles:
            _t, blurb, url = next(d for d in self._doc_links if d[0] == title)
            btn = pill(title, blurb, url)
            if title == "LZX Connect":
                self._connect_pill = btn
            self._fw_links.addWidget(btn, i // 2, i % 2, Qt.AlignmentFlag.AlignLeft)
            i += 1
        self._fw_links.setColumnStretch(2, 1)
        # Application settings: theme + app releases, beside the SD card box
        rg.addLayout(top_row, 0, 0, 1, 3)
        _t, blurb, url = next(d for d in self._doc_links if d[0] == "App Releases")
        rg.addWidget(pill("App Releases", blurb, url), 1, 0, Qt.AlignmentFlag.AlignLeft)
        rg.setColumnStretch(2, 1)
        grid.addWidget(res_grp, 1, 1)

        root.addStretch(1)

    def set_connected(self, v: bool):
        self._connected = v
        self.refresh_btn.setEnabled(v)
        if not v:
            self._msd_state = "idle"
            self._msd_wait_timer.stop()
            self._msd_promote_timer.stop()
            self._msd_poll_timer.stop()
            self.msd_btn.setText("MOUNT AS USB DRIVE")
        self.msd_btn.setEnabled(v and self._msd_state == "idle")
        self.src_combo.setEnabled(v)
        self._src_hdmi_btn.setEnabled(v)
        self._src_analog_btn.setEnabled(v)
        self.timing_combo.setEnabled(v)
        if not v:
            self._device_fw = ""
            for key, val in self._fw_fields.items():
                # "app" and "latest" don't depend on the device
                if key in ("app", "latest"):
                    continue
                val.setText("\u2014")
            for val in self._status_fields.values():
                val.setText("\u2014")
            for cell in self._midi_cells:
                cell.setText(cell.text().split("  ")[0] + "  \u2014")

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

        locked = self._is_true(data.get("locked", False))
        # Per-input detail (rc.5x firmware): lock for the active input,
        # external sync, and whether an HDMI display is attached.
        per_input = data.get(src) if isinstance(data.get(src), dict) else {}
        in_locked = self._is_true(per_input.get("locked", locked))
        parts = ["LOCKED" if in_locked else "NO SIGNAL"]
        if self._is_true(data.get("external_vsync", False)):
            parts.append("EXT SYNC")
        if "hdmi_connected" in output:
            parts.append("HDMI OUT " + ("ON" if self._is_true(output["hdmi_connected"]) else "OFF"))
        self._signal_lbl.setText("  \u00b7  ".join(parts))
        self._signal_lbl.setStyleSheet(
            f"color:{TEXT if in_locked else ERROR};font-size:15px;font-weight:bold;{self._TRANSPARENT}")
        # Sync source toggle buttons
        self._src_hdmi_btn.setChecked(src == "hdmi")
        self._src_analog_btn.setChecked(src == "analog")
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
        for i, cell in enumerate(self._midi_cells):
            a = assignments[i] if i < len(assignments) and isinstance(assignments[i], dict) else {}
            msb, lsb = a.get("msb"), a.get("lsb")
            msb = str(msb) if isinstance(msb, int) else "-"
            lsb = str(lsb) if isinstance(lsb, int) else "-"
            cell.setText(f"P{i+1}  {msb} / {lsb}")

    def _set_source(self, src: str):
        """Switch video input source and poll until locked."""
        # Update toggle button states
        self._src_hdmi_btn.setChecked(src == "hdmi")
        self._src_analog_btn.setChecked(src == "analog")
        # Sync hidden combo
        self.src_combo.blockSignals(True)
        self.src_combo.setCurrentIndex(1 if src == "hdmi" else 0)
        self.src_combo.blockSignals(False)
        if self.on_send:
            self.on_send(f"video input {src}")
        # Poll video status repeatedly to catch lock (HDMI re-lock can be slow)
        for delay in [500, 1500, 3000, 5000, 8000, 12000]:
            QTimer.singleShot(delay, self._fetch_status)

    def _on_src_changed(self, idx):
        src = self.src_combo.currentData()
        if self.on_send:
            self.on_send(f"video input {src}")

    def apply_firmware_info(self, version: str, uptime: str = ""):
        """Populate the firmware info fields. App row is static (set at init)."""
        self._fw_fields["version"].setText(version or "\u2014")
        self._fw_fields["uptime"].setText(uptime or "\u2014")
        if version:
            self._device_fw = version
        self._refresh_fw_compare()

    def _osc_changed(self):
        on = self.osc_btn.isChecked()
        if self.on_osc:
            self.on_osc(on, self.osc_port.value())

    def set_osc_state(self, listening: bool, port: int, error: str, last: str):
        self.osc_btn.blockSignals(True)
        self.osc_btn.setChecked(listening)
        self.osc_btn.blockSignals(False)
        self.osc_btn.setText("ON" if listening else "OFF")
        if error:
            self.osc_status.setText(f"\u26a0 {error}")
            self.osc_status.setStyleSheet(f"color:{ERROR};font-size:14px;font-weight:bold;"
                                          f"{self._TRANSPARENT}")
            return
        self.osc_status.setStyleSheet(f"color:{TEXT};font-size:14px;font-weight:bold;"
                                      f"{self._TRANSPARENT}")
        if listening:
            self.osc_port.setValue(port)
            self.osc_status.setText(f"Listening on {_OSC['ip'] or _local_ip()}:{port}"
                                    + (f"   \u00b7   last: {last}" if last else ""))
        else:
            self.osc_status.setText("Off \u2014 turn on to control the app over OSC")

    def _pick_theme(self, key: str):
        for k, b in self._theme_btns.items():
            b.setChecked(k == key)
        if key != THEME and self.on_theme:
            self.on_theme(key)

    def set_latest_firmware(self, latest: str):
        self._latest_fw = latest
        self._fw_fields["latest"].setText(latest)
        self._refresh_fw_compare()

    def firmware_outdated(self) -> bool:
        dev, new = _fw_version_key(self._device_fw), _fw_version_key(self._latest_fw)
        return bool(dev and new and new > dev)

    def _refresh_fw_compare(self):
        outdated = self.firmware_outdated()
        # The LZX Connect pill's tooltip says when newer firmware is available
        if getattr(self, "_connect_pill", None) is not None:
            self._connect_pill.setToolTip(
                (f"Firmware {self._latest_fw} is available — update it with LZX Connect\n"
                 if outdated else "Update firmware and install program libraries\n")
                + LZX_CONNECT_URL)
        colour = WARN if outdated else TEXT
        self._fw_fields["latest"].setStyleSheet(
            f"color:{colour};font-size:15px;font-weight:bold;{self._TRANSPARENT}")
        if self._latest_fw and self._device_fw and not outdated:
            self._fw_fields["latest"].setText(f"{self._latest_fw}  \u2714")
            self._fw_fields["latest"].setToolTip("Your Videomancer is on the newest firmware")

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

    def _on_msd_click(self):
        if self._msd_state == "idle":
            self._start_mount()

    def _start_mount(self):
        if not _VMConfirmDialog.ask(
            self,
            "Mount SD Card",
            "Mount the Videomancer's SD card as a USB drive?\n\n"
            "Program loading and snapshots will be unavailable until the "
            "card is ejected in Finder. Allow up to 15 seconds for the "
            "drive to appear.",
        ):
            return
        if not self.on_send:
            return
        self._msd_state = "waiting"
        self.msd_btn.setEnabled(False)
        self.msd_btn.setText("MOUNTING…")
        self.on_send("msd enter")
        self._msd_wait_timer.start()
        self._msd_poll_timer.start()

    def _msd_wait_timeout(self):
        """No `{"active":true}` within 15 s — mount failed, revert."""
        if self._msd_state == "waiting":
            self._set_msd_idle()
            _VMConfirmDialog.notify(
                self,
                "Mount Failed",
                "The Videomancer did not enter USB mass storage mode within "
                "15 seconds.\n\nLoad a program on the device first, then try "
                "again. If that doesn't help, power-cycle the device "
                "(unplug USB, wait 5 s, replug).",
            )

    def _promote_msd_if_waiting(self):
        if self._msd_state == "waiting":
            self._set_msd_mounted()

    def _set_msd_mounted(self):
        self._msd_wait_timer.stop()
        self._msd_promote_timer.stop()
        self._msd_state = "mounted"
        # Button is passive while mounted — only way out is an eject in Finder
        self.msd_btn.setEnabled(False)
        self.msd_btn.setText("MOUNTED  —  EJECT IN FINDER TO RETURN")
        if not self._msd_poll_timer.isActive():
            self._msd_poll_timer.start()

    def _set_msd_idle(self):
        self._msd_wait_timer.stop()
        self._msd_promote_timer.stop()
        self._msd_poll_timer.stop()
        self._msd_state = "idle"
        self.msd_btn.setEnabled(self._connected)
        self.msd_btn.setText("MOUNT AS USB DRIVE")

    def _msd_poll_status(self):
        if self.on_send and self._connected and self._msd_state in (
            "waiting", "mounted",
        ):
            self.on_send("msd status")

    def apply_msd_response(self, prefix: str, payload: str):
        """Handle @msd: / !msd: responses from the device.

        The device emits:
          - `{"status":"entering"}` / `{"status":"exiting"}` — command acks, no state change
          - `{"active":true}` / `{"active":false}` — authoritative status from `msd status`

        Only `active` drives state transitions. Command acks are ignored so the
        button doesn't flip instantly on the echo of `msd enter`.
        """
        if prefix == "error":
            self._set_msd_idle()
            return

        lower = (payload or "").strip().lower()
        try:
            active = json.loads(payload).get("active")
            lower = '"active":true' if active is True else '"active":false' if active is False else lower
        except Exception:
            pass

        if '"active":true' in lower:
            if self._msd_state == "waiting":
                # Delay promotion so the UI waits for the drive to actually
                # appear in Finder (OS enumeration lags firmware by a few s).
                if not self._msd_promote_timer.isActive():
                    self._msd_promote_timer.start()
            return

        if '"active":false' in lower:
            # Only treat as eject if we were actually mounted. While "waiting"
            # the device is still transitioning — polls often return false for
            # several seconds before flipping to true, and treating them as an
            # exit would cancel our own mount.
            if self._msd_state == "mounted":
                self._set_msd_idle()
            return
        # Any other payload (e.g. `{"status":"entering"}`) is just an ack — ignore.

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
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(10)

        # Kept for callers; the tab header (title + refresh) was dropped to
        # give the lists the full height.
        self.refresh_btn = QPushButton("↻  Refresh", self)
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setVisible(False)
        self.refresh_btn.clicked.connect(lambda: self.on_refresh and self.on_refresh())

        _note_css = (f"color:{TEXT_DIM};font-size:12px;"
                     f"background:transparent;border:none;")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(0)

        # ── Left: Quick recall grid + user presets ──
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(10)

        # User states (LZX's term for what we internally call user presets)
        user_grp = QGroupBox("STATES ON THE DEVICE")
        ul = QVBoxLayout(user_grp)
        user_note = QLabel("Saved control settings for the program that's "
                           "running, stored on the Videomancer.")
        user_note.setWordWrap(True)
        user_note.setStyleSheet(_note_css)
        ul.addWidget(user_note)
        self.user_list = QListWidget()
        ul.addWidget(self.user_list, stretch=1)

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
        self.flash_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:14px;"
            f"background:transparent;border:none;"
        )
        ul.addWidget(self.flash_lbl)
        ll.addWidget(user_grp, stretch=1)

        splitter.addWidget(left)

        # ── Right: File snapshots ──
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(8)

        snap_grp = QGroupBox("SNAPSHOTS ON THIS COMPUTER")
        sl = QVBoxLayout(snap_grp)
        snap_note = QLabel("Files with the program, its device states and "
                           "global settings — for backups or sharing a session.")
        snap_note.setWordWrap(True)
        snap_note.setStyleSheet(_note_css)
        sl.addWidget(snap_note)

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
        # Two-line entries wrap instead of scrolling sideways
        self.snap_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.snap_list.setWordWrap(True)
        self.snap_list.setTextElideMode(Qt.TextElideMode.ElideRight)
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
        self.snap_status.setStyleSheet(
            f"color:{WARN};font-size:11px;"
            f"background:transparent;border:none;"
        )
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
            item = QListWidgetItem(f"{s['label']}\n{s['program']}  ·  {ts}")
            item.setToolTip(f"{s['label']}\n{s['program']} — saved {ts}")
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
        name = _VMConfirmDialog.ask_text(
            self,
            "Save State",
            "Name this state. The firmware CLI is space-separated, so spaces "
            "in the name will be replaced with underscores.",
            default="My_State",
        )
        if name is None:
            return
        # Firmware splits the command on whitespace — a space in the name would
        # be parsed as the start of the m:/t:/... payload and corrupt the save.
        sanitized = name.strip().replace(" ", "_")
        if sanitized and self.on_save_preset:
            self.on_save_preset(len(self._user), sanitized)

    def _delete_preset(self):
        items = self.user_list.selectedItems()
        if not items:
            return
        idx  = items[0].data(Qt.ItemDataRole.UserRole)
        name = items[0].text()
        if _VMConfirmDialog.ask(
            self, "Delete State", f'Delete "{name}"?'
        ) and self.on_delete_preset:
            self.on_delete_preset(idx)

    def _save_snapshot(self):
        label = self.snap_label.text().strip() or \
                datetime.now().strftime("snapshot %Y-%m-%d %H:%M")
        if self.on_capture:
            self._save_snap_btn.setEnabled(False)
            self.snap_status.setText("Collecting device state…")
            self.on_capture(label)

    def populate_for_save(self, label, program, parameters, presets, settings, tss=None):
        # Ensure default folder exists so Save As opens somewhere sensible.
        self._manager._ensure_folder()
        default_path = self._manager.folder / self._manager.default_filename(label)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Snapshot As",
            str(default_path),
            "Videomancer Snapshot (*.json);;All Files (*)",
        )
        self._save_snap_btn.setEnabled(self._connected)
        if not path:
            self.set_snapshot_status("Save cancelled")
            return
        saved = self._manager.save(
            label, program, parameters, presets, settings, tss=tss, path=Path(path),
        )
        self._reload_snapshots()
        self.set_snapshot_status(f"✓ Saved: {saved.name}")

    def _restore_snapshot(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        prog = s.get("program") or s.get("data", {}).get("program") or "—"
        msg = (
            f'Restore "{s["label"]}"?\n\n'
            f'This will load program "{prog}" and overwrite the device\'s '
            f'current parameters, user states, and settings.'
        )
        if _VMConfirmDialog.ask(self, "Restore Snapshot", msg) and self.on_restore:
            self._restore_btn.setEnabled(False)
            self.on_restore(s["data"])

    def _delete_snapshot(self):
        items = self.snap_list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.ItemDataRole.UserRole)
        if not s:
            return
        if _VMConfirmDialog.ask(
            self, "Delete Snapshot", f'Delete snapshot "{s["label"]}"?'
        ):
            self._manager.delete(s["path"])
            self._reload_snapshots()


# ── Main window ────────────────────────────────────────────────────────

# ── Program Library tab ───────────────────────────────────────────────

class LibraryTab(QWidget):
    """Browse LZX's official and community program libraries, see what's on
    the Videomancer's SD card, and install / remove programs over USB."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_refresh = None        # ()
        self.on_open_release = None   # (release)
        self.on_install = None        # (release, [programs])
        self.on_remove = None         # ([programs])
        self.on_add_file = None       # (path)
        self._releases = {}           # source → [release]
        self._source = "official"
        self._release = None
        self._programs = []
        self._device_files = None     # {"vendor/file.vmprog": size} or None if unknown
        self._device_names = set()    # program names in the device's boot index
        self._device_versions = {}    # "vendor/file.vmprog" → version from the card manifest
        self._device_fw = ""
        self._connected = False
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)
        note_css = f"color:{TEXT_DIM};font-size:12px;background:transparent;border:none;"

        # Source + version row
        top = QHBoxLayout()
        top.setSpacing(6)
        self._src_btns = {}
        for src in LIBRARY_SOURCES + [{"key": "card", "label": "MY PROGRAMS"}]:
            b = QPushButton(src["label"])
            b.setCheckable(True)
            b.setChecked(src["key"] == self._source)
            b.setStyleSheet(
                f"QPushButton{{background:{SURFACE2};border:1px solid {BORDER};border-radius:4px;"
                f"color:{TEXT_DIM};font-size:12px;font-weight:bold;padding:6px 12px;}}"
                f"QPushButton:checked{{background:{DIM};color:#ffffff;border-color:#ffffff;}}")
            b.clicked.connect(lambda _c, k=src["key"]: self._set_source(k))
            top.addWidget(b)
            self._src_btns[src["key"]] = b
        top.addSpacing(10)
        ver_lbl = QLabel("Version")
        ver_lbl.setStyleSheet(note_css)
        self.version_lbl = ver_lbl
        top.addWidget(ver_lbl)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumWidth(230)
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)
        top.addWidget(self.version_combo, stretch=1)
        self.refresh_btn = QPushButton("\u21bb  Refresh")
        self.refresh_btn.clicked.connect(lambda: self.on_refresh and self.on_refresh())
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        # Compatibility / info banner
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        self.capacity = QLabel("")
        self.capacity.setWordWrap(True)
        self.capacity.setVisible(False)
        root.addWidget(self.capacity)

        # Program list
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Program", "Author", "Version", "On your Videomancer"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setStyleSheet(
            f"QTreeWidget{{background:{SURFACE};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:13px;}}"
            f"QTreeWidget::item{{padding:4px 2px;}}"
            f"QTreeWidget::item:selected{{background:{DIM};color:#ffffff;}}"
            f"QHeaderView::section{{background:{SURFACE2};color:{TEXT_DIM};border:none;"
            f"padding:4px 6px;font-size:11px;font-weight:bold;}}")
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.itemChanged.connect(lambda *_: self._update_buttons())
        root.addWidget(self.tree, stretch=1)

        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setMinimumHeight(34)
        self.desc.setStyleSheet(f"color:{TEXT};font-size:12px;font-style:italic;"
                                f"background:transparent;border:none;")
        root.addWidget(self.desc)

        # Actions
        act = QHBoxLayout()
        act.setSpacing(6)
        self.sel_all_btn = QPushButton("Select not installed")
        self.sel_all_btn.clicked.connect(self._select_missing)
        self.update_all_btn = QPushButton("\u21bb  Update all")
        self.update_all_btn.setToolTip("Install this library's version of every program "
                                       "whose copy on the card is older or a different build")
        self.update_all_btn.clicked.connect(self._update_all)
        self.sel_none_btn = QPushButton("Clear")
        self.sel_none_btn.clicked.connect(lambda: self._check_all(False))
        self.add_file_btn = QPushButton("Add .vmprog file\u2026")
        self.add_file_btn.clicked.connect(self._pick_file)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(self._remove_checked)
        self.install_btn = QPushButton("\u2B07  INSTALL")
        self.install_btn.setObjectName("primary")
        self.install_btn.clicked.connect(self._install_checked)
        for b in (self.sel_all_btn, self.sel_none_btn, self.add_file_btn, self.update_all_btn):
            act.addWidget(b)
        act.addStretch(1)
        act.addWidget(self.remove_btn)
        act.addWidget(self.install_btn)
        root.addLayout(act)

        from PyQt6.QtWidgets import QProgressBar
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        self.status = QLabel("New programs appear after you power-cycle the Videomancer.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(note_css)
        root.addWidget(self.status)
        self._update_buttons()

    # ── state from the controller ──
    def set_connected(self, v: bool):
        self._connected = v
        if not v:
            self._device_files = None
            self._device_names = set()
        self._render()

    def set_device_firmware(self, fw: str):
        self._device_fw = fw or ""
        self._render_banner()

    def set_device_names(self, names):
        self._device_names = set(names or [])
        if self._programs:
            self._render()

    def set_device_programs(self, files, names, versions=None, card_names=None):
        self._device_files = dict(files) if files is not None else None
        self._device_names = set(names or [])
        self._device_versions = dict(versions or {})   # file → version (card manifest)
        self._card_names = dict(card_names or {})      # file → display name (card manifest)
        if self._source == "card":
            self._programs = self._card_programs()
        self._render()

    # ── ON THIS CARD view ──
    def _loaded_norm(self) -> set:
        return {_norm_prog_name(n) for n in self._device_names}

    def _card_programs(self) -> list:
        """Programs on the SD card that aren't part of any LZX library
        (the user's own builds, hand-copied files, unknown programs)."""
        out = []
        known = set(_known_library_files())
        # the card's manifest.json is the official library's catalogue
        known |= {k for k in self._card_names if k.endswith(".vmprog")}
        for rel, size in sorted((self._device_files or {}).items()):
            folder, _, base = rel.rpartition("/")
            if rel in known:
                continue
            stem = re.sub(r"(?i)[_-]?v?\d+(\.\d+){1,2}$", "", base[:-7])
            name = self._card_names.get(rel) or self._card_names.get(base) or \
                stem.replace("_", " ").title()
            out.append({"file": rel, "size": size, "sd_path": f"{SD_PROGRAMS}/{rel}",
                        "name": name, "author": folder or "(loose)",
                        "version": self._device_versions.get(rel, "") or "",
                        "description": f"{SD_PROGRAMS}/{rel}  \u00b7  {size / 1024:.0f} KB",
                        "categories": [], "program_id": "", "manifest_entry": None,
                        "on_card": True})
        return out

    def _card_loaded(self, p) -> bool:
        loaded = self._loaded_norm()
        return bool(loaded) and (_norm_prog_name(p["name"]) in loaded
                                 or _norm_prog_name(p["file"]) in loaded)

    def _render_capacity(self):
        n = len(self._device_files or {})
        if self._device_files is None or not n:
            self.capacity.setVisible(False)
            return
        over = n - SD_PROGRAM_LIMIT
        if over > 0:
            self.capacity.setText(
                f"\u26a0  SD card: {n} programs \u2014 the Videomancer loads {SD_PROGRAM_LIMIT}, "
                f"so {over} {'is' if over == 1 else 'are'} skipped at boot. "
                f"Remove {over} to load them all.")
            self.capacity.setStyleSheet(
                f"color:#ffffff;background:#5a1f3a;border:1px solid {ERROR};border-radius:6px;"
                f"padding:6px 8px;font-size:12px;")
        else:
            self.capacity.setText(f"SD card: {n} of {SD_PROGRAM_LIMIT} program slots used.")
            self.capacity.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;"
                                        f"background:transparent;border:none;")
        self.capacity.setVisible(True)

    def set_releases(self, releases: dict):
        self._releases = releases
        if self._source == "card":
            return          # stay on the card view; library lists fill in quietly
        self._fill_versions()

    def set_programs(self, release: dict, programs: list):
        self._release = release
        self._programs = programs
        self._render()

    def set_busy(self, busy: bool, msg: str = ""):
        self._busy = busy
        self.progress.setVisible(busy)
        if msg:
            self.status.setText(msg)
        for w in (self.version_combo, self.refresh_btn, *self._src_btns.values()):
            w.setEnabled(not busy)
        self._update_buttons()

    def set_progress(self, done: int, total: int, msg: str = ""):
        self.progress.setVisible(True)
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        if msg:
            self.status.setText(msg)

    # ── UI internals ──
    def _set_source(self, key: str):
        self._source = key
        for k, b in self._src_btns.items():
            b.setChecked(k == key)
        card = key == "card"
        self.version_combo.setVisible(not card)
        self.version_lbl.setVisible(not card)
        if card:
            self._release = None
            self._programs = self._card_programs()
            self._render()
            self.status.setText("Programs on your card that aren't from an LZX library \u2014 "
                                "your own builds and anything added by hand.")
        else:
            self._fill_versions()

    def _fill_versions(self):
        rows = self._releases.get(self._source, [])
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for r in rows:
            label = r["version"] + ("  (pre-release)" if r["prerelease"] else "")
            if r["target_fw"]:
                label += f"  \u00b7  for firmware {r['target_fw']}"
            self.version_combo.addItem(label, r)
        # Default: newest release this Videomancer's firmware can run
        pick = 0
        dev = _fw_version_key(self._device_fw)
        if dev:
            for i, r in enumerate(rows):
                tgt = _fw_version_key(r["target_fw"])
                if not r["prerelease"] and (tgt is None or tgt <= dev):
                    pick = i
                    break
        self.version_combo.setCurrentIndex(pick if rows else -1)
        self.version_combo.blockSignals(False)
        self._on_version_changed()

    def _on_version_changed(self, *_):
        rel = self.version_combo.currentData()
        self._programs = []
        self._render()
        if rel and self.on_open_release:
            self.on_open_release(rel)

    def card_path(self, p) -> Optional[str]:
        """Where this library program sits on the card, relative to
        sd:/programs: its author folder if present, else loose in programs/
        (LZX Connect and Finder copies both leave programs there)."""
        if not self._device_files:
            return None
        if p["file"] in self._device_files:
            return p["file"]
        if p.get("on_card"):
            return None
        vendor, _, base = p["file"].rpartition("/")
        # Only LZX's own programs are matched by bare file name: the official
        # library ends up loose in programs/, while a loose file that shares a
        # community program's name is usually someone else's program (e.g. a
        # user's own tetris.vmprog vs homegrownvhs/tetris.vmprog).
        if vendor in ("", "lzx") and base in self._device_files:
            return base
        return None

    def _kind(self, p) -> str:
        """missing | installed | restart | update | newer | different | unknown"""
        if self._device_files is None:
            return "unknown"
        if p.get("on_card"):
            return "loaded" if self._card_loaded(p) else "notloaded"
        cp = self.card_path(p)
        size = self._device_files.get(cp) if cp else None
        if size is None:
            return "missing"
        if size == p["size"]:
            loaded = self._loaded_norm()
            if loaded and _norm_prog_name(p["name"]) not in loaded:
                return "restart"
            return "installed"
        card_ver = self._device_versions.get(cp) or self._device_versions.get(p["file"])
        lib, card = _prog_version_key(p["version"]), _prog_version_key(card_ver)
        if lib and card and lib > card:
            return "update"
        if lib and card and lib < card:
            return "newer"
        return "different"

    def _status_for(self, p) -> tuple:
        """(text, colour) for one library program vs the SD card."""
        cp = self.card_path(p)
        card_ver = (self._device_versions.get(cp) if cp else None) or self._device_versions.get(p["file"], "")
        return {
            "unknown":   ("\u2014", TEXT_DIM),
            "missing":   ("Not installed", TEXT_DIM),
            "installed": ("Installed", "#7ee787"),
            "restart":   (("Not loaded \u00b7 over the card limit"
                          if len(self._device_files or {}) > SD_PROGRAM_LIMIT
                          else "Installed \u00b7 restart to load"), WARN),
            "loaded":    ("Loaded", "#7ee787"),
            "notloaded": (("Not loaded \u00b7 over the card limit"
                           if len(self._device_files or {}) > SD_PROGRAM_LIMIT
                           else "Not loaded"), ERROR),
            "update":    (f"Update available (card has {card_ver})", WARN),
            "newer":     (f"Card has newer {card_ver}", "#8ab4ff"),
            "different": ("Different build", WARN),
        }[self._kind(p)]

    def _render(self):
        self.tree.blockSignals(True)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for p in self._programs:
            st, colour = self._status_for(p)
            it = QTreeWidgetItem([p["name"], p["author"], p["version"], st])
            it.setData(0, Qt.ItemDataRole.UserRole, p)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Unchecked)
            it.setForeground(3, QColor(colour))
            tip = p["description"] + (f"\nCategories: {', '.join(p['categories'])}" if p["categories"] else "")
            for c in range(4):
                it.setToolTip(c, tip)
            self.tree.addTopLevelItem(it)
        self.tree.setSortingEnabled(True)
        self.tree.blockSignals(False)
        card = self._source == "card"
        self.tree.setHeaderLabels(["Program", "Location" if card else "Author", "Version",
                                   "On your Videomancer"])
        self._render_banner()
        self._render_capacity()
        self._update_buttons()

    def _render_banner(self):
        rel = self._release
        dev, tgt = _fw_version_key(self._device_fw), _fw_version_key((rel or {}).get("target_fw", ""))
        if rel and dev and tgt and tgt > dev:
            self.banner.setText(
                f"\u26a0  This library was built for firmware {rel['target_fw']}; your Videomancer "
                f"runs {self._device_fw}. Programs built for newer firmware may not load. "
                f"Update the firmware with LZX Connect first, or pick an older library version.")
            self.banner.setStyleSheet(
                f"color:#ffffff;background:#5a1f3a;border:1px solid {ERROR};border-radius:6px;"
                f"padding:6px 8px;font-size:12px;")
            self.banner.setVisible(True)
        elif rel and not rel.get("verified", True):
            self.banner.setText("This release has no published checksum, so the download can't be verified.")
            self.banner.setStyleSheet(f"color:{WARN};font-size:12px;background:transparent;border:none;")
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _on_current(self, item, _prev):
        p = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not p:
            self.desc.setText("")
            return
        cats = f"   [{', '.join(p['categories'])}]" if p["categories"] else ""
        self.desc.setText(f"{p['description']}{cats}")

    def _checked(self) -> list:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.CheckState.Checked:
                out.append(it.data(0, Qt.ItemDataRole.UserRole))
        return out

    def _check_all(self, on: bool, only_missing: bool = False):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            p = it.data(0, Qt.ItemDataRole.UserRole)
            want = on and (not only_missing or self._kind(p) == "missing")
            it.setCheckState(0, Qt.CheckState.Checked if want else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _select_missing(self):
        self._check_all(True, only_missing=True)

    def _updatable(self) -> list:
        return [p for p in self._programs if self._kind(p) in ("update", "different")]

    def _update_all(self):
        todo = self._updatable()
        if not todo:
            return
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            on = it.data(0, Qt.ItemDataRole.UserRole) in todo
            it.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._install_checked()

    def _update_buttons(self):
        checked = self._checked() if hasattr(self, "tree") else []
        can_device = self._connected and not self._busy and self._device_files is not None
        self.install_btn.setEnabled(can_device and bool(checked))
        self.install_btn.setText(f"\u2B07  INSTALL {len(checked)}" if checked else "\u2B07  INSTALL")
        on_card = [p for p in checked if self.card_path(p)]
        self.remove_btn.setEnabled(can_device and bool(on_card))
        n_upd = len(self._updatable()) if self._programs else 0
        self.update_all_btn.setEnabled(can_device and n_upd > 0)
        self.update_all_btn.setText(f"\u21bb  Update all ({n_upd})" if n_upd else "\u21bb  Update all")
        self.add_file_btn.setEnabled(can_device)
        for b in (self.sel_all_btn, self.sel_none_btn):
            b.setEnabled(bool(self._programs) and not self._busy)
        card = self._source == "card"
        for b in (self.install_btn, self.update_all_btn, self.sel_all_btn):
            b.setVisible(not card)

    def _install_checked(self):
        progs = self._checked()
        if not progs or not self.on_install:
            return
        rel = self._release or {}
        dev, tgt = _fw_version_key(self._device_fw), _fw_version_key(rel.get("target_fw", ""))
        if dev and tgt and tgt > dev:
            if not _VMConfirmDialog.ask(
                self, "Newer firmware required?",
                f"This library was built for firmware <b>{rel['target_fw']}</b> and your "
                f"Videomancer runs <b>{self._device_fw}</b>.<br><br>Programs built for newer "
                f"firmware may fail to load — the device keeps the previous program.<br><br>"
                f"Install anyway?"):
                return
        older = [p for p in progs if self._kind(p) == "newer"]
        if older and not _VMConfirmDialog.ask(
                self, "Replace with an older version?",
                "The card already has a <b>newer</b> version of:<br><br>"
                + "<br>".join(f"{p['name']} — card {self._device_versions.get(self.card_path(p) or p['file'], '?')}, "
                              f"this library {p['version']}" for p in older[:8])
                + ("<br>\u2026" if len(older) > 8 else "")
                + "<br><br>Installing replaces them with the older version. Continue?"):
            return
        new = [p for p in progs if not self.card_path(p)]
        after = len(self._device_files or {}) + len(new)
        if new and after > SD_PROGRAM_LIMIT and not _VMConfirmDialog.ask(
                self, "Over the program limit",
                f"The Videomancer loads at most <b>{SD_PROGRAM_LIMIT}</b> programs from the SD "
                f"card. After this install the card would hold <b>{after}</b>, so "
                f"<b>{after - SD_PROGRAM_LIMIT}</b> won't load.<br><br>Remove programs first "
                f"(any library view or MY PROGRAMS), or install anyway?"):
            return
        total_kb = sum(p["size"] for p in progs) // 1024
        if not _VMConfirmDialog.ask(
            self, "Install Programs",
            f"Copy <b>{len(progs)}</b> program{'s' if len(progs) != 1 else ''} "
            f"({total_kb:,} KB) to the Videomancer's SD card?<br><br>"
            f"Controls pause during the copy (about {max(1, total_kb // 150)} s). "
            f"Power-cycle the Videomancer afterwards to load them."):
            return
        self.on_install(rel, progs)

    def _remove_checked(self):
        progs = [p for p in self._checked() if self.card_path(p)]
        if not progs or not self.on_remove:
            return
        names = ", ".join(p["name"] for p in progs[:6]) + (" \u2026" if len(progs) > 6 else "")
        if not _VMConfirmDialog.ask(
            self, "Remove Programs",
            f"Delete {len(progs)} program{'s' if len(progs) != 1 else ''} from the SD card?"
            f"<br><br>{names}<br><br>You can reinstall them from the library later."):
            return
        self.on_remove(progs)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Install a Videomancer program", str(Path.home()),
            "Videomancer programs (*.vmprog)")
        if path and self.on_add_file:
            self.on_add_file(path)


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
        self.setMinimumSize(640, 560)
        self.setStyleSheet(STYLESHEET)

        self._worker: Optional[SerialWorker] = None
        self._active_program: Optional[str] = None
        self._pending_load: Optional[str] = None
        self._suppress_poof = False   # skip load animation during snapshot restore
        # Monotonic timestamp: while time.monotonic() < this, status polls that
        # disagree with _active_program are ignored so a slow device switch
        # can't revert the UI after a manual snapshot restore / load.
        self._active_program_lock_until: float = 0.0
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
        self._poll_inflight = {}        # poll cmd → monotonic send time
        self._pending_cmds = {}         # control key → latest command
        self._cmd_flush = QTimer(self)  # ~30 Hz coalesced send
        self._cmd_flush.setSingleShot(True)
        self._cmd_flush.setInterval(33)
        self._cmd_flush.timeout.connect(self._flush_cmds)
        self._taps = []
        self._user_disconnected = False
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(10000)
        self._load_watchdog.timeout.connect(
            lambda: self._abort_pending_load(
                f"Load failed ({self._pending_load_error})" if self._pending_load_error
                else "Load timed out — device didn't confirm"))
        self._pending_load_error = ""
        self._last_load_request = ("", 0.0)   # (program, monotonic) we asked for
        # Program library: one `fs` command in flight at a time; polling
        # pauses while the library owns the link (installs, card scans).
        self._library_busy = False
        self._fs_queue = []            # [(command, callback)]
        self._fs_current = None
        self._fs_watchdog = QTimer(self)
        self._fs_watchdog.setSingleShot(True)
        self._fs_watchdog.timeout.connect(lambda: self._fs_reply(None))
        self._lib_releases = {}
        self._lib_threads = set()

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
        self._fw_checker = _FirmwareChecker()
        self._fw_checker.latest_found.connect(self._on_latest_firmware)
        self._fw_checker.start()
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
        self.library_tab = LibraryTab()
        self.snap_tab    = SnapshotsTab()   # keep for snapshot callbacks

        # Each tab scrolls when the window is smaller than its layout, so the
        # app fits 768-px-tall laptop screens (Control alone needs ~690 px).
        def _scrollable(w):
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            # Vertical only: the window's minimum width (below) always fits
            # the widest tab, so nothing is ever cut off sideways.
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setStyleSheet("QScrollArea{background:transparent;border:none;}")
            sa.setWidget(w)
            return sa
        self.tabs.addTab(_scrollable(self.prog_tab),   "PROGRAMS")
        self.tabs.addTab(_scrollable(self.param_tab),  "CONTROL")
        self.tabs.addTab(_scrollable(self.system_tab), "SYSTEM")
        self.tabs.addTab(_scrollable(self.state_tab),  "STATE")
        self.tabs.addTab(self.library_tab,               "LIBRARY")
        widest = max(t.minimumSizeHint().width() for t in
                     (self.prog_tab, self.param_tab, self.system_tab, self.state_tab,
                      self.library_tab))
        self.setMinimumWidth(widest + 100)  # tab frame, margins, vertical scrollbar
        # Default size: wide enough for every tab, no taller than the screen
        scr = QApplication.primaryScreen()
        avail_h = scr.availableGeometry().height() - 40 if scr else 1020
        self.resize(max(self.width(), self.minimumWidth()), min(self.height(), avail_h))
        self._install_shortcuts()
        self.library_tab.on_refresh = lambda: self._lib_refresh(force=True)
        self.system_tab.on_open_library = lambda: self.tabs.setCurrentIndex(4)
        self.system_tab.on_theme = lambda k: QTimer.singleShot(0, lambda: _switch_theme(k))
        self.library_tab.on_open_release = self._lib_open_release
        self.library_tab.on_install = self._lib_install
        self.library_tab.on_remove = self._lib_remove
        self.library_tab.on_add_file = self._lib_add_file
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

        # Collapsible console — its COPY / CONSOLE buttons live in the status
        # bar (added below) instead of a footer row, so tabs keep the height.
        self._console_visible = False
        small = (f"QPushButton{{background:{SURFACE};border:1px solid {BORDER};"
                 f"border-radius:3px;color:{TEXT_DIM};font-size:10px;padding:0 6px;}}"
                 f"QPushButton:hover{{color:{TEXT};}}")
        copy_btn = QPushButton("COPY")
        copy_btn.setFixedHeight(18)
        copy_btn.setToolTip("Copy the serial console to the clipboard")
        copy_btn.setStyleSheet(small)
        self._copy_btn = copy_btn
        copy_btn.clicked.connect(lambda: self.console._copy_all())
        self._console_toggle_btn = QPushButton("\u25b6  CONSOLE")
        self._console_toggle_btn.setFixedHeight(18)
        self._console_toggle_btn.setToolTip("Show / hide the serial console")
        self._console_toggle_btn.setStyleSheet(small)
        self._console_toggle_btn.clicked.connect(self._toggle_console)

        self.console = ConsoleWidget()
        self.console._copy_btn_ref = copy_btn
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
        self.status_bar.addPermanentWidget(self._copy_btn)
        self.status_bar.addPermanentWidget(self._console_toggle_btn)
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
            f"background:transparent;border:none;color:{LOGO};"
            "font-family:'Goldplay',sans-serif;"
            "font-size:24px;font-weight:900;letter-spacing:2px;"
        )
        if LOGO_GLOW:
            from PyQt6.QtWidgets import QGraphicsDropShadowEffect
            glow = QGraphicsDropShadowEffect(logo_lbl)
            glow.setOffset(0, 0)
            glow.setBlurRadius(18)
            glow.setColor(QColor(LOGO_GLOW))
            logo_lbl.setGraphicsEffect(glow)
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
                background:{HILITE}; border:2px solid #ffffff;
                color:#ffffff;
            }}
        """)
        self._dual_btn.toggled.connect(self._toggle_dual_cast)
        lay.addWidget(self._dual_btn)
        self._dual_window = None

        # Monitor — hidden for now
        self._monitor_window = None

        # Update available banner (hidden until check completes)
        self._update_btn = QPushButton("")
        self._update_btn.setFixedHeight(22)
        self._update_btn.setVisible(False)
        self._update_btn.setStyleSheet(f"""
            QPushButton {{
                background:{SEL_BG}; border:1px solid {ACCENT};
                border-radius:3px; color:{SEL_TEXT};
                font-size:9px; font-weight:bold; padding:1px 6px;
            }}
            QPushButton:hover {{
                background:{SEL_BG2}; color:#ffffff;
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
        self._update_version = version
        self._update_btn.setText(f"v{version} available — click to install")
        self._update_btn.setVisible(True)

    def _open_update_url(self):
        """Entry point when the header update button is clicked."""
        if not self._update_url:
            return
        self._start_update_flow()

    def _start_update_flow(self):
        """Confirm → download-with-progress → restart prompt → install."""
        version = getattr(self, "_update_version", "")
        if not _VMConfirmDialog.ask(
            self,
            "Update Available",
            f"Version <b>{version}</b> is available.<br><br>"
            f"Download and install it now? The app will restart when the "
            f"update is ready.",
        ):
            return

        if sys.platform != "darwin":
            # In-place install is macOS-only; hand Windows users the download.
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(self._update_url or
                                          f"https://github.com/{GITHUB_REPO}/releases/latest"))
            return

        # Refuse if we can't locate our own .app (running from source etc.)
        exe = Path(sys.executable).resolve()
        current_bundle = next(
            (p for p in exe.parents if p.suffix == ".app"), None,
        )
        if current_bundle is None:
            _VMConfirmDialog.notify(
                self, "Running from Source",
                "Auto-install only works when launched from a built .app "
                "bundle. Use the Releases link in the System tab's "
                "Documentation section to download manually.",
            )
            return
        if _is_translocated(current_bundle) or \
                not os.access(current_bundle.parent, os.W_OK):
            _VMConfirmDialog.notify(
                self, "Move to Applications First",
                "macOS is running this copy from a read-only location, so it "
                "can't be updated in place. Drag Videomancer Control into your "
                "Applications folder, relaunch it, then install the update.",
            )
            return

        from PyQt6.QtWidgets import QProgressDialog
        self._update_progress = QProgressDialog(
            f"Downloading v{version}…", "Cancel", 0, 100, self,
        )
        self._update_progress.setWindowTitle("Updating")
        self._update_progress.setMinimumDuration(0)
        self._update_progress.setAutoClose(False)
        self._update_progress.setAutoReset(False)

        self._update_downloader = _UpdateDownloader()
        self._update_current_bundle = current_bundle

        def _on_progress(done, total):
            if total > 0:
                self._update_progress.setMaximum(total)
                self._update_progress.setValue(done)
            else:
                # Unknown total — indeterminate bar
                self._update_progress.setMaximum(0)

        def _on_done(path):
            self._update_progress.close()
            self._prompt_restart_and_install(Path(path))

        def _on_failed(msg):
            self._update_progress.close()
            _VMConfirmDialog.notify(
                self, "Update Failed",
                f"Could not complete the update.\n\n{msg}\n\n"
                f"You can download manually from the Releases link in the "
                f"System tab.",
            )

        self._update_downloader.progress.connect(_on_progress)
        self._update_downloader.finished_ok.connect(_on_done)
        self._update_downloader.failed.connect(_on_failed)
        self._update_progress.canceled.connect(self._update_downloader.terminate)
        self._update_downloader.start()

    def _prompt_restart_and_install(self, new_bundle: Path):
        if not _VMConfirmDialog.ask(
            self,
            "Update Ready",
            f"Download complete. Restart Videomancer Control now to install "
            f"<b>v{getattr(self, '_update_version', '')}</b>?",
        ):
            return
        try:
            self._perform_install_and_restart(new_bundle)
        except Exception as exc:
            _VMConfirmDialog.notify(
                self, "Install Failed",
                f"The install step didn't complete.\n\n{exc}",
            )

    def _perform_install_and_restart(self, new_bundle: Path):
        """Write a small helper script that waits for this process to exit,
        swaps the bundle, and relaunches. Then quit.

        The new bundle is placed in the OLD bundle's parent directory but
        keeps its OWN filename — so upgrades from a legacy bundle name (e.g.
        `VideomancerControl.app` → `Videomancer Control.app`) install with the
        new name rather than being renamed back to the old one.
        """
        import tempfile, os, subprocess, stat
        old_bundle = self._update_current_bundle
        # Install the new bundle alongside the old one, preserving the new name
        install_path = old_bundle.parent / new_bundle.name
        pid = os.getpid()
        script = f"""#!/bin/bash
set -e
PARENT_PID={pid}
OLD_APP={str(old_bundle)!r}
NEW_APP={str(new_bundle)!r}
INSTALL_PATH={str(install_path)!r}
# Wait for parent to exit (cap at 30 s)
for i in $(seq 1 60); do
    if ! kill -0 "$PARENT_PID" 2>/dev/null; then break; fi
    sleep 0.5
done
sleep 0.5
# Move old aside to Trash (keeps a recovery copy)
if [ -d "$OLD_APP" ]; then
    TS=$(date +%s)
    /bin/mv "$OLD_APP" "$HOME/.Trash/$(basename "$OLD_APP").$TS.bak" 2>/dev/null || /bin/rm -rf "$OLD_APP"
fi
# If something exists at the install path (e.g. same-name bundle), trash it too
if [ -d "$INSTALL_PATH" ] && [ "$INSTALL_PATH" != "$OLD_APP" ]; then
    TS=$(date +%s)
    /bin/mv "$INSTALL_PATH" "$HOME/.Trash/$(basename "$INSTALL_PATH").$TS.bak" 2>/dev/null || /bin/rm -rf "$INSTALL_PATH"
fi
/bin/mv "$NEW_APP" "$INSTALL_PATH"
/usr/bin/xattr -cr "$INSTALL_PATH" 2>/dev/null || true
/usr/bin/open "$INSTALL_PATH"
"""
        tmpdir = Path(tempfile.mkdtemp(prefix="vmctl-install-"))
        script_path = tmpdir / "install.sh"
        script_path.write_text(script)
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC |
                          stat.S_IXGRP | stat.S_IXOTH)
        subprocess.Popen([str(script_path)], start_new_session=True)
        # Quit so the helper can replace our bundle
        QApplication.quit()

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
        try:
            if self._worker and self._worker.isRunning():
                return  # already connected
            if self._user_disconnected:
                return
            # Skip if a reconnect attempt is pending (avoid double-connect)
            if getattr(self, '_reconnect_attempt', 0) > 0:
                return
            self.conn_bar.refresh_ports()
            self.conn_bar.try_auto_connect()
        except Exception:
            pass

    def _toggle_console(self):
        self._console_visible = not self._console_visible
        if self._console_visible:
            self.console.show()
            self._console_toggle_btn.setText("\u25bc  CONSOLE")
        else:
            self.console.hide()
            self._console_toggle_btn.setText("\u25b6  CONSOLE")

    def _do_connect(self, port: str):
        if self._worker and self._worker.isRunning():
            return
        self._user_disconnected = False
        # Claim port immediately to prevent other windows from grabbing it
        _claimed_ports.add(port)
        self._claimed_port = port
        self._worker = SerialWorker(self)
        self._wire_worker(self._worker)
        self.status_bar.showMessage(f"Connecting to {port}…")
        self._worker.connect_port(port)

    def _worker_links(self, w):
        return [(w.connected, self._on_connected), (w.disconnected, self._on_disconnected),
                (w.response, self._on_response), (w.put_progress, self._lib_put_progress),
                (w.put_finished, self._lib_put_finished), (w.error, self._on_error),
                (w.programs_page, self._on_programs_page),
                (w.status_update, self._on_status_update)]

    def _wire_worker(self, w):
        for sig, slot in self._worker_links(w):
            sig.connect(slot)

    def release_worker(self):
        """Detach the live serial connection (for a theme rebuild) without
        closing the port. Returns (worker, port) or None."""
        w, port = self._worker, self._claimed_port
        if w is None or not w.isRunning():
            return None
        for sig, slot in self._worker_links(w):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        self._poll_timer.stop()
        self._worker = None
        self._claimed_port = None          # stays in _claimed_ports for the new window
        return w, port

    def adopt_worker(self, w, port):
        """Take over a running connection from a window being rebuilt."""
        w.setParent(self)
        self._worker = w
        self._wire_worker(w)
        self._on_connected(port)
        self.status_bar.showMessage(f"Connected — {port}", 3000)
        for cmd in ("version", "status", "video status", "modulation cc-map",
                    "transport status", "program info"):
            w.send(cmd)
        self._fetch_programs()

    def _do_disconnect(self):
        # Manual disconnect: stay disconnected until the user clicks Connect
        self._user_disconnected = True
        if self._worker:
            self._worker.disconnect_port()

    @pyqtSlot(str)
    def _on_connected(self, port: str):
        self._reconnect_attempt = 0  # reset backoff on success
        # Port already claimed in _do_connect, just ensure it's set
        _claimed_ports.add(port)
        self._claimed_port = port
        self.conn_bar.set_connected(port)
        self.library_tab.set_connected(True)
        self._lib_scan_on_boot = True    # scan the card once the program list is in
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
        job = getattr(self, "_lib_job", None)
        if job is not None:
            self._lib_job = None
            done = len(job["ok"])
            left = [p["name"] for p in job["programs"]]
            QTimer.singleShot(0, lambda: _VMConfirmDialog.notify(
                self, "Videomancer restarted during install",
                f"{done} program(s) were copied before the Videomancer disconnected."
                + (f"\n\nNot copied: {', '.join(left[:6])}{' …' if len(left) > 6 else ''}"
                   if left else "")
                + "\n\nNothing more was written. When it reconnects, the Library tab "
                  "shows what's on the card — install the rest from there."))
        self._fs_queue.clear()
        self._fs_watchdog.stop()
        if self._fs_current is not None:
            _cmd, cb = self._fs_current
            self._fs_current = None
            cb(None)
        self._library_busy = False
        self.library_tab.set_connected(False)
        self.library_tab.set_busy(False)
        # Remember the port for auto-reconnect
        last_port = getattr(self, '_claimed_port', None)
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
        self.status_bar.showMessage(
            "Disconnected" if self._user_disconnected else "Disconnected — reconnecting…")
        self._abort_pending_load("Disconnected during load")
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
        # Auto-reconnect: try the same port after a delay
        # Use increasing backoff to avoid rapid reconnect loops
        attempt = getattr(self, '_reconnect_attempt', 0) + 1
        self._reconnect_attempt = attempt
        delay = min(3000 + 2000 * attempt, 15000)  # 5s, 7s, 9s, ... 15s max
        if last_port:
            QTimer.singleShot(delay, lambda: self._try_reconnect(last_port))

    def _try_reconnect(self, port: str):
        """Attempt to reconnect to the last known port."""
        if self._worker and self._worker.isRunning():
            return  # already reconnected via hotplug
        if self._user_disconnected:
            return
        # Check if port still exists without opening it
        from pathlib import Path
        if not Path(port).exists():
            self.status_bar.showMessage("Disconnected — waiting for device…")
            return
        self.status_bar.showMessage(f"Reconnecting to {port}…")
        self._do_connect(port)

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

        # MSD responses need both ok and error prefixes handled.
        if key == "msd":
            self.system_tab.apply_msd_response(prefix, payload)
            return

        if prefix == "error" and self._fs_current is not None:
            # While the library runs, `fs` commands are the only traffic, so an
            # error answers the command in flight.
            self._fs_reply({"error": f"{key}: {payload}".strip(": ")})
            return

        if prefix == "error" and self._pending_load:
            # Errors carry a numeric code, not the command that caused them, so
            # we can't tell a rejected load from a poll that failed while the
            # FPGA reconfigures. Remember it; the load watchdog reports it if
            # the load never confirms.
            self._pending_load_error = f"{key}: {payload}".strip(": ")

        if prefix != "ok":
            return

        if key == "version":
            self._sb_fw.setText(f"FW: {payload}")
            self.system_tab.apply_firmware_info(version=payload.strip())
            self.library_tab.set_device_firmware(payload.strip())
            self._maybe_announce_firmware()

        elif key == "fs" and self._fs_current is not None and '"put"' not in payload:
            try:
                self._fs_reply(json.loads(payload))
            except Exception:
                self._fs_reply({"raw": payload})

        elif key == "program":
            if payload == "ok":
                # Could be load or preset apply
                if self._pending_load:
                    name = self._pending_load
                    self._pending_load = None
                    self._load_watchdog.stop()
                    self._set_active_program(name, force=True)
                    self.prog_tab.set_loading_program(False)
                    self.status_bar.showMessage(f"Loaded: {name}", 4000)
                    self._trigger_poof()
                    # Fetch state + presets + TSS + info after load
                    QTimer.singleShot(500, lambda: self._worker and self._worker.send("program info"))
                    QTimer.singleShot(1500, self._request_state)
                    QTimer.singleShot(1500, self._fetch_presets)
                    QTimer.singleShot(1800, self._fetch_tss_readback_auto)
                else:
                    # preset apply ok (TSS change)
                    self.status_bar.showMessage("Applied", 1000)
                    QTimer.singleShot(500, self._fetch_tss_readback_auto)

            else:
                # JSON payload — could be state or preset list
                try:
                    data = json.loads(payload)
                except Exception as exc:
                    self.console.append("error", "json", f"Bad program payload: {exc}")
                    return

                if ("m" in data or "ch" in data) and "n" not in data:
                    self._poll_inflight.pop("program state", None)
                    # program state — RC11 uses "m", older docs said "ch"
                    m  = data.get("m",  data.get("ch", []))
                    sr = data.get("sr", [0]*12)
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
                            if self.param_tab._recently_edited(i, now):
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
                    running = data.get("name", "")
                    if running and self._active_program and running != self._active_program:
                        # `program info` is a direct query, so it wins over our
                        # guess. Two ways to get here: the device answered
                        # `program load X` with @program:ok but X failed and the
                        # old program kept running (seen with Bleach on rc.55),
                        # or the program was changed on the hardware itself.
                        wanted = self._active_program
                        req_name, req_time = self._last_load_request
                        if req_name == wanted and time.monotonic() - req_time < 20.0:
                            self.console.append("error", "load",
                                f"Device is running {running!r}, not {wanted!r}")
                            self.status_bar.showMessage(
                                f"\u26a0 Couldn't load {wanted} — the Videomancer is still "
                                f"running {running}. The program file may be damaged or "
                                f"incompatible; try reinstalling it with LZX Connect.", 12000)
                        self._last_load_request = ("", 0.0)
                        self._set_active_program(running, force=True)
                        return   # force=True re-requests info for the real program
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
            if payload.strip() == "ok":
                return  # ack for `modulation set/source`
            try:
                data = json.loads(payload)
            except Exception as exc:
                self.console.append("error", "json", f"Bad modulation payload: {exc}")
                return
            if "modulators" in data:
                self._poll_inflight.pop("modulation status", None)
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
        try:
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
        except Exception:
            pass

    @pyqtSlot(dict)
    def _on_status_update(self, data: dict):
        prog = data.get("current_program", "")
        vstd = data.get("video_standard", "")
        sd   = "  SD●" if data.get("sd_mounted") else ""
        if prog:
            # Drop stale status that disagrees with a manual set (snapshot
            # restore, Load button) while the lock window is still active.
            locked = (
                time.monotonic() < self._active_program_lock_until
                and prog != self._active_program
            )
            pending_mismatch = self._pending_load and prog != self._pending_load
            if locked or pending_mismatch:
                reason = "locked" if locked else "pending-mismatch"
                self.console.append("log", "",
                    f"[status] ignore current_program={prog!r} active={self._active_program!r} ({reason})")
            else:
                # If this status confirms our pending load, clear the flag so
                # future unrelated status updates don't keep being blocked.
                if self._pending_load and prog == self._pending_load:
                    self._pending_load = None
                if prog == self._active_program:
                    self._active_program_lock_until = 0.0   # device confirmed
                self._set_active_program(prog)
        if vstd:
            self._sb_vid.setText(
                f"Video: {vstd.replace('_', ' ').upper()}{sd}"
            )
        # If firmware reports uptime, use it instead of local timer
        if "uptime" in data:
            self.system_tab._fw_fields["uptime"].setText(str(data["uptime"]))

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
            # The device's program list is what "loaded" means for the
            # Library tab; refresh it every time the list is re-read.
            self.library_tab.set_device_names(self.prog_tab._all)
            if getattr(self, "_lib_scan_on_boot", False):
                # Fresh connection (app start or the Videomancer rebooted):
                # read the SD card so the Library tab is current.
                self._lib_scan_on_boot = False
                QTimer.singleShot(1500, lambda: self._worker and not self._library_busy
                                  and self._lib_scan_device())

    def load_program(self, name: str):
        if not self._worker:
            QMessageBox.information(self, "Not Connected",
                                    "Connect to Videomancer first.")
            return
        self._pending_load = name
        self.param_tab.reset_sync_caches()  # reset dedup on program change
        self._poll_inflight.clear()
        self.prog_tab.set_loading_program(True)
        self.status_bar.showMessage(f"Loading {name}…")
        self.console.append("cmd", f"program load {name}", "")
        self._pending_load_error = ""
        self._last_load_request = (name, time.monotonic())
        self._worker.load_program(name)
        self._load_watchdog.start()

    def _abort_pending_load(self, msg: str):
        if not self._pending_load:
            return
        self._pending_load = None
        self._load_watchdog.stop()
        self.prog_tab.set_loading_program(False)
        self.status_bar.showMessage(msg, 6000)
        self.console.append("error", "load", msg)

    def _trigger_poof(self):
        """Fire the poof + sparkle animation centered on the RUNNING pill."""
        if self._suppress_poof:
            return
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

    def _set_active_program(self, name: str, force: bool = False):
        if name == self._active_program and not force:
            # Routine status poll confirming what we already show — don't
            # re-fetch program info, rebuild lists or clear status text.
            return
        # Lock window: keep stale status polls from reverting this change for
        # up to 15 s. The first poll that confirms `name` clears it (see
        # _on_status_update), so program changes made on the hardware show up
        # on the next poll instead of being ignored.
        self._active_program_lock_until = time.monotonic() + 15.0
        self.console.append("log", "", f"[active] {self._active_program!r} → {name!r}")
        self._active_program = name
        self.prog_tab.set_active(name)
        self.param_tab.set_program(name)
        self.state_tab.set_snapshot_status("")
        self._sb_prog.setText(f"Program: {name}")
        # Show active program in window title
        if name:
            port = self.conn_bar.port_combo.currentText()
            self.setWindowTitle(f"VIDEOMANCER CONTROL {self._window_label} — {name}  [{port}]")
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
        try:
            if not self._worker or self._library_busy:
                return
            # Keep polling modulation status while the user edits: readback
            # is filtered per channel, so other knobs (and hardware moves)
            # stay live.
            self._poll_once("modulation status")
            if not hasattr(self, '_poll_count'):
                self._poll_count = 0
            self._poll_count += 1
            if self._poll_count % 4 == 0:
                self._worker.send("transport status")
            if not self._user_editing:
                # Poll every tick so TSS knobs (carried in `program state`)
                # update at the same cadence as the main knobs in
                # `modulation status` — keeps their glide visually matched.
                self._poll_once("program state")
            if self._poll_count % 20 == 0:
                self._worker.send("video status")
            if self._poll_count % 8 == 0:
                self._worker.send("status")
        except Exception:
            pass

    def _poll_once(self, cmd: str):
        """Send a poll only if the previous one has been answered (or has
        been outstanding > 1 s), so a slow device doesn't get a backlog of
        queries in front of the user's own commands."""
        now = time.monotonic()
        sent = self._poll_inflight.get(cmd)
        if sent is not None and now - sent < 1.0:
            return
        self._poll_inflight[cmd] = now
        self._worker.send(cmd)

    def _queue_cmd(self, key: str, cmd: str):
        """Coalesce rapid-fire commands (knob drags, BPM fader) so only the
        latest value per control goes out, ~30 times a second. Dragging a
        knob used to send one command per pixel of movement."""
        self._pending_cmds[key] = cmd
        if not self._cmd_flush.isActive():
            self._cmd_flush.start()

    def _flush_cmds(self):
        if not self._worker:
            self._pending_cmds.clear()
            return
        cmds, self._pending_cmds = self._pending_cmds, {}
        for cmd in cmds.values():
            self._worker.send(cmd)

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
        if is_toggle:
            self.console.append("cmd", f"P{index+1} manual → {value}", "")
            self._worker.send(cmd)
        elif self._pending_cmds.get(f"m{index}", "").count(" ") > 3:
            # A Time/Space/Slope change for this channel is still queued —
            # keep sending the full form so it isn't dropped.
            self._queue_cmd(f"m{index}", self._full_mod_set(index))
        else:
            self._queue_cmd(f"m{index}", cmd)

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
            # Time/Space/Slope are positional after the manual value:
            #   modulation set <ch> <manual> <time> <space> <slope>
            # (The old `modulation set <ch> <val> <t|sp|sl>` form was RC11-era;
            # current firmware reads the field name as a bad positional arg,
            # so TSS never changed and the knob snapped back on the next poll.)
            self._queue_cmd(f"m{index}", self._full_mod_set(index))

    def _full_mod_set(self, index: int) -> str:
        """`modulation set` with manual + time/space/slope from the card."""
        card = self.param_tab.channels[index]
        t, sp, sl = card.get_tss()
        return f"modulation set {index} {card.get_manual()} {t} {sp} {sl}"

    def _fetch_tss_readback_auto(self):
        """Fetch full program state to sync TSS sliders from device."""
        if self._worker and not self._user_editing:
            self._worker.send("program state")

    def _send_transport(self, action: str):
        if not self._worker:
            return
        cmd_map = {
            # Firmware (rc.5x `help`, LZX serial guide) has play/stop/bpm —
            # there is no `transport start` or `transport tap`.
            "start": "transport play",
            "stop":  "transport stop",
        }
        if action == "tap":
            bpm_x100 = self._tap_tempo()
            if bpm_x100:
                self.param_tab.set_bpm(bpm_x100 / 100.0, bpm_x100)
                self._worker.send(f"transport bpm {bpm_x100}")
                self.console.append("cmd", f"tap → {bpm_x100/100:.2f} BPM", "")
            return
        cmd = cmd_map.get(action)
        if cmd:
            self.console.append("cmd", cmd, "")
            self._worker.send(cmd)

    def _tap_tempo(self) -> Optional[int]:
        """Average the last few tap intervals into BPM×100. Taps more than
        2 s apart start a new count. Returns None until two taps."""
        now = time.monotonic()
        taps = self._taps
        if taps and now - taps[-1] > 2.0:
            taps = []
        taps = (taps + [now])[-5:]
        self._taps = taps
        if len(taps) < 2:
            return None
        avg = (taps[-1] - taps[0]) / (len(taps) - 1)
        return max(2000, min(30000, round(6000 / avg)))

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
            cmd = (f"program presets save {index} {_cmd_safe_name(name)} "
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
            cmd = f"program presets rename {index} {_cmd_safe_name(name)}"
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

        self.console.append("log", "",
            f"[restore] snapshot program={program!r} current_active={self._active_program!r}")

        self.state_tab.set_snapshot_status(f"Restoring: loading {program}…")

        def _abort_restore(msg="Restore aborted — device disconnected"):
            self._suppress_poof = False
            self.state_tab.set_snapshot_status(msg)
            self.state_tab._restore_btn.setEnabled(True)

        def step2():
            """Write the live values straight to the running program.
            (This used to save them into user preset slot 0 as "live" and
            apply it, silently overwriting the user's first saved State.)"""
            if not self._worker:
                return _abort_restore()
            if parameters:
                n = min(12, len(parameters))
                t_list  = list(data.get("t",  [512] * 12))
                sp_list = list(data.get("sp", [512] * 12))
                sl_list = list(data.get("sl", [512] * 12))
                sr_list = list(data.get("sr", [0] * 12))
                now = time.monotonic()
                for i in range(n):
                    card = self.param_tab.channels[i]
                    m = int(parameters[i])
                    if card._is_toggle:
                        m = PARAM_RANGE if m > 0 else 0   # device reports 0/1
                    self._worker.send(f"modulation source {i} {int(sr_list[i])}")
                    tss = [int(v[i]) if i < len(v) else 512 for v in (t_list, sp_list, sl_list)]
                    self._worker.send(f"modulation set {i} {m} {tss[0]} {tss[1]} {tss[2]}")
                    # keep in-flight polls from snapping the UI back
                    self.param_tab._last_sent[f"edit_time_{i}"] = now
                self.param_tab.apply_state(parameters, t_list, sp_list, sl_list, sr_list)
            self.state_tab.set_snapshot_status("Restoring presets…")
            QTimer.singleShot(400, step3)

        def step3():
            if not self._worker:
                return _abort_restore()
            for i, p in enumerate(presets.get("user", [])):
                name = _cmd_safe_name(p.get("n", f"preset_{i}"), f"preset_{i}")
                fields = [f"{k}:{','.join(str(v) for v in p[k])}"
                          for k in ("m", "t", "sp", "sl", "sr") if p.get(k)]
                if fields:
                    self._worker.send(f"program presets save {i} {name} {' '.join(fields)}")
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
            self._suppress_poof = False

        def wait_for_load(deadline):
            """Chain on the device confirming the load instead of a fixed
            delay, so values never land in the previous program."""
            if not self._worker:
                return _abort_restore()
            if self._pending_load is None and self._active_program == program:
                QTimer.singleShot(300, step2)   # let the FPGA settle
            elif self._pending_load is None or time.monotonic() > deadline:
                _abort_restore(f"Restore aborted — {program} didn't load")
            else:
                QTimer.singleShot(100, lambda: wait_for_load(deadline))

        if program and program != self._active_program:
            self._suppress_poof = True   # no spell animation during restore
            self.load_program(program)
            wait_for_load(time.monotonic() + 12.0)
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

    def _install_shortcuts(self):
        """Keyboard control. Single-key shortcuts are ignored while typing in
        a text field (program filter, preset names)."""
        from PyQt6.QtGui import QShortcut, QKeySequence

        def guarded(fn):
            def run():
                from PyQt6.QtWidgets import QAbstractItemView, QAbstractSpinBox
                if isinstance(QApplication.focusWidget(),
                              (QLineEdit, QTextEdit, QAbstractItemView, QAbstractSpinBox)):
                    return
                fn()
            return run

        def toggle_play():
            if self._worker:
                playing = getattr(self.param_tab, "transport_playing", False)
                self._send_transport("stop" if playing else "start")

        bindings = [
            ("Space", guarded(toggle_play)),
            ("T", guarded(lambda: self._worker and self.param_tab._transport("tap"))),
            ("Ctrl+R", self._on_tab_refresh),
            ("R", guarded(lambda: self._worker and self.param_tab.randomize())),
            ("Ctrl+Z", guarded(lambda: self._worker and self.param_tab.undo())),
            ("Ctrl+F", lambda: (self.tabs.setCurrentIndex(0),
                                self.prog_tab.search.setFocus(),
                                self.prog_tab.search.selectAll())),
        ]
        for i in range(self.tabs.count()):
            bindings.append((f"Ctrl+{i+1}", lambda i=i: self.tabs.setCurrentIndex(i)))
        self._shortcuts = []
        for keys, fn in bindings:
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            self._shortcuts.append(sc)

    # ------------------------------------------------------------------
    # Program library
    # ------------------------------------------------------------------

    def _fs(self, cmd: str, cb, timeout_ms: int = 10000):
        """Queue one `fs …` command; cb(reply_dict_or_None) runs on its reply."""
        self._fs_queue.append((cmd, cb, timeout_ms))
        self._fs_next()

    def _fs_next(self):
        if self._fs_current is not None or not self._fs_queue or not self._worker:
            return
        cmd, cb, timeout_ms = self._fs_queue.pop(0)
        self._fs_current = (cmd, cb)
        self._worker.send(cmd)
        self._fs_watchdog.start(timeout_ms)

    def _fs_reply(self, data):
        if self._fs_current is None:
            return
        self._fs_watchdog.stop()
        _cmd, cb = self._fs_current
        self._fs_current = None
        try:
            cb(data)
        finally:
            self._fs_next()

    def _lib_set_busy(self, busy: bool, msg: str = ""):
        self._library_busy = busy
        self.library_tab.set_busy(busy, msg)
        if busy:
            self._pending_cmds.clear()

    def _lib_refresh(self, force: bool = False):
        """Show cached releases instantly, refresh them from GitHub in the
        background, and rescan the card unless it was scanned very recently."""
        if not self._lib_releases:
            try:
                cached = json.loads(_app_settings().value("library/releases", "") or "{}")
            except Exception:
                cached = {}
            if cached:
                self._lib_on_releases(cached, from_cache=True)
            else:
                self.library_tab.status.setText("Loading library releases from GitHub\u2026")
        stale = time.monotonic() - getattr(self, "_lib_fetched_at", -1e9) > 600
        if force or stale:
            t = _LibraryIndexFetcher()
            t.releases_ready.connect(self._lib_on_releases)
            t.failed.connect(lambda m: None if self._lib_releases
                             else self.library_tab.status.setText(m))
            self._lib_track(t)
            t.start()
        if force or time.monotonic() - getattr(self, "_lib_scanned_at", -1e9) > 60:
            self._lib_scan_device()

    def _lib_track(self, t):
        self._lib_threads.add(t)
        t.finished.connect(lambda t=t: self._lib_threads.discard(t))

    def _lib_prefetch(self, releases: dict):
        """Quietly cache the newest release of each library so MY PROGRAMS
        can tell library programs from the user's own."""
        for rows in releases.values():
            rel = next((r for r in rows if not r["prerelease"]), None)
            if not rel or (_library_cache_dir() / rel["zip_name"]).exists():
                continue
            t = _LibraryDownloader(rel)
            t.done.connect(lambda *_: self.library_tab._source == "card"
                           and self.library_tab._set_source("card"))
            self._lib_track(t)
            t.start()

    def _lib_on_releases(self, releases: dict, from_cache: bool = False):
        if not from_cache:
            self._lib_fetched_at = time.monotonic()
            _app_settings().setValue("library/releases", json.dumps(releases))
            if releases == self._lib_releases:
                return            # nothing new — keep the current view
        self._lib_releases = releases
        self.library_tab.set_device_firmware(self.system_tab._device_fw)
        self.library_tab.set_releases(releases)
        self._lib_prefetch(releases)

    def _lib_open_release(self, rel: dict):
        self.library_tab.status.setText(f"Downloading library {rel['version']}\u2026")
        t = _LibraryDownloader(rel)
        t.progress.connect(lambda d, n: self.library_tab.set_progress(d, n or d))
        t.done.connect(self._lib_on_release_ready)
        t.failed.connect(lambda m: (self.library_tab.status.setText(m),
                                    self.library_tab.progress.setVisible(False)))
        self._lib_track(t)
        t.start()

    def _lib_on_release_ready(self, rel: dict, programs: list):
        if self.library_tab.version_combo.currentData() and \
                self.library_tab.version_combo.currentData().get("tag") != rel.get("tag"):
            return   # user picked another version meanwhile
        self._lib_release = rel
        self.library_tab.progress.setVisible(False)
        self.library_tab.set_programs(rel, programs)
        self.library_tab.status.setText(
            f"{len(programs)} programs in {rel['version']}"
            + ("  \u00b7  checksum verified" if rel.get("verified") else "")
            + ".  New programs appear after you power-cycle the Videomancer.")

    def _lib_scan_device(self, then=None):
        """List sd:/programs/<vendor>/*.vmprog with sizes."""
        if not self._worker:
            self.library_tab.set_device_programs(None, [])
            return
        self._lib_set_busy(True, "Reading the SD card\u2026")
        files = {}

        def entries_of(d):
            if isinstance(d, dict):
                return d.get("entries") or [], bool(d.get("more")), d.get("next")
            return (d if isinstance(d, list) else []), False, None

        def is_dir(e):
            t = str(e.get("type", "")).lower()
            return bool(e.get("dir") or e.get("is_dir") or t in ("dir", "directory", "d"))

        def listdir(path, done, offset=0, acc=None):
            acc = {} if acc is None else acc
            cmd = f"fs ls {path}" + (f" {offset}" if offset else "")

            def got(d):
                if d is None or (isinstance(d, dict) and "error" in d):
                    return done(acc, d)
                page, more, nxt = entries_of(d)
                for e in page:
                    if isinstance(e, dict) and e.get("name"):
                        acc.setdefault(e["name"], e)   # last entry repeats — dedupe
                if more and isinstance(nxt, int) and nxt > offset:
                    listdir(path, done, nxt, acc)
                else:
                    done(acc, None)
            self._fs(cmd, got)

        vendors = []

        def is_prog(n):
            return n.endswith(".vmprog") and not n.startswith("._")   # ._ = Finder metadata

        def root_done(acc, err):
            if err is not None:
                return finish(err)
            for n, e in acc.items():
                if is_prog(n) and not is_dir(e):
                    files[n] = int(e.get("size", -1))          # loose in programs/
            vendors.extend(n for n, e in acc.items() if is_dir(e) and not n.startswith("."))
            next_vendor()

        def next_vendor():
            if not vendors:
                return finish(None)
            v = vendors.pop(0)

            def vdone(acc, err):
                for n, e in acc.items():
                    if is_prog(n) and not is_dir(e):
                        files[f"{v}/{n}"] = int(e.get("size", -1))
                next_vendor()
            listdir(f"{SD_PROGRAMS}/{v}", vdone)

        def finish(err):
            if err is not None and "error" in (err or {}):
                self._lib_set_busy(False)
                self.library_tab.set_device_programs({}, list(self.prog_tab._all))
                self.library_tab.status.setText(
                    "No program folder on the SD card yet." if "not" in str(err).lower()
                    else f"Couldn't read the SD card ({err.get('error')}).")
                self._lib_scanned_at = time.monotonic()
                if then:
                    then()
                return

            def got_manifest(man, missing):
                rows = [e for e in ((man or {}).get("programs") or []) if isinstance(e, dict)]
                versions = {e.get("file"): e.get("program_version", "") for e in rows}
                names = {}
                for e in rows:                 # by manifest path and by bare file name
                    f = str(e.get("file", ""))
                    names[f] = names[f.rsplit("/", 1)[-1]] = e.get("program_name", "")
                self._lib_set_busy(False)
                self._lib_scanned_at = time.monotonic()
                self.library_tab.set_device_programs(files, list(self.prog_tab._all), versions, names)
                self.library_tab.status.setText(
                    f"{len(files)} programs on the SD card.  "
                    "New programs appear after you power-cycle the Videomancer.")
                if then:
                    then()
            self._lib_read_manifest(got_manifest)

        listdir(SD_PROGRAMS, root_done)

    # -- install / remove -------------------------------------------------

    def _lib_install(self, rel: dict, programs: list, file_blobs: Optional[dict] = None):
        """mkdir vendor folders → fs put each file → fs stat to confirm size
        → merge the SD manifest → rescan."""
        import zipfile
        if not self._worker:
            return
        blobs = dict(file_blobs or {})
        if not file_blobs:
            try:
                with zipfile.ZipFile(rel["zip_path"]) as z:
                    for p in programs:
                        blobs[p["file"]] = z.read(p["zip_member"])
            except Exception as exc:
                self.library_tab.status.setText(f"Couldn't read the library zip: {exc}")
                return
        total = sum(len(b) for b in blobs.values())
        replaced = sum(max(0, (self.library_tab._device_files or {}).get(
                           self.library_tab.card_path(p) or "", 0)) for p in programs)
        need = total - replaced + 64 * 1024          # + manifest and headroom
        self._lib_set_busy(True, "Checking free space on the SD card\u2026")

        def got_info(d):
            free = d.get("free") if isinstance(d, dict) else None
            if isinstance(free, (int, float)) and need > free:
                self._lib_set_busy(False)
                _VMConfirmDialog.notify(
                    self, "Not enough space",
                    f"These programs need about {need / 1e6:.1f} MB but the SD card has "
                    f"{free / 1e6:.1f} MB free. Remove some programs first.")
                return
            self._lib_begin_install(rel, programs, blobs, total)
        self._fs("fs info", got_info)

    def _lib_targets(self, programs, blobs):
        """Decide each program's card path. Never creates folders: on rc.55
        `fs mkdir` of a new folder resets the Videomancer (verified), so a
        program goes into its author folder only if that already exists,
        otherwise loose in sd:/programs/. Returns (placed, clashes)."""
        on_card = self.library_tab._device_files or {}
        folders = {k.split("/")[0] for k in on_card if "/" in k}
        placed, clashes, new_blobs = [], [], {}
        for p in programs:
            vendor, base = (p["file"].split("/", 1) + [""])[:2] if "/" in p["file"] else ("", p["file"])
            rel = p["file"] if vendor in folders else base
            existing = self.library_tab.card_path(p)
            if existing:
                rel = existing                         # update in place
            elif rel == base and base in on_card and vendor not in ("", "lzx"):
                clashes.append(p)                      # a different program owns this name
                continue
            q = dict(p, card_file=rel, sd_path=f"{SD_PROGRAMS}/{rel}")
            placed.append(q)
            new_blobs[rel] = blobs[p["file"]]
        return placed, clashes, new_blobs

    def _lib_begin_install(self, rel, programs, blobs, total):
        programs, clashes, blobs = self._lib_targets(programs, blobs)
        if clashes:
            _VMConfirmDialog.notify(
                self, "Name already in use",
                "Skipping — the card already has a different program with the same "
                "file name:\n\n" + "\n".join(f"{p['name']} ({p['file'].split('/')[-1]})"
                                            for p in clashes))
        if not programs:
            self._lib_set_busy(False)
            return
        total = sum(len(b) for b in blobs.values())
        self._lib_set_busy(True, f"Installing {len(programs)} program(s)\u2026")
        self._lib_job = {"programs": list(programs), "blobs": blobs, "done_bytes": 0,
                         "total": total, "ok": [], "failed": [], "rel": rel}
        self._lib_put_next()

    def _lib_put_next(self):
        job = getattr(self, "_lib_job", None)
        if job is None:
            return
        if not job["programs"]:
            # The card's manifest.json is LZX Connect's catalogue; the firmware
            # finds programs without it (verified on rc.55), so it's left as
            # is — one less large write that a reset could corrupt.
            return self._lib_finish(job, "")
        p = job["programs"][0]
        self.library_tab.set_progress(job["done_bytes"], job["total"],
                                      f"Copying {p['name']}\u2026")
        self._worker.put_file(p["sd_path"], job["blobs"][p["card_file"]])

    def _lib_put_progress(self, path: str, sent: int, total: int):
        job = getattr(self, "_lib_job", None)
        if job:
            self.library_tab.set_progress(job["done_bytes"] + sent, job["total"])

    def _lib_put_finished(self, path: str, ok: bool, msg: str):
        job = getattr(self, "_lib_job", None)
        if job is None:
            return
        p = job["programs"].pop(0)
        job["done_bytes"] += p["size"]

        def verified(d):
            size = (d or {}).get("size") if isinstance(d, dict) else None
            if ok and size is not None and int(size) == p["size"]:
                job["ok"].append(p)
            else:
                job["failed"].append((p, msg if not ok else f"size on card {size}, expected {p['size']}"))
            self._lib_put_next()
        if ok:
            self._fs(f"fs stat {p['sd_path']}", verified)
        else:
            verified(None)

    def _lib_read_manifest(self, done):
        """Read sd:/programs/manifest.json via chunked base64 `fs read`."""
        import base64
        path = f"{SD_PROGRAMS}/manifest.json"

        def got_stat(d):
            if isinstance(d, dict) and "error" in d and "not found" in str(d["error"]).lower():
                return done(None, missing=True)
            if not isinstance(d, dict) or "size" not in d:
                return done(None, missing=False)     # couldn't read ≠ doesn't exist
            size, buf = int(d["size"]), bytearray()

            def read_more(_=None):
                if len(buf) >= size:
                    try:
                        return done(json.loads(buf.decode("utf-8")), missing=False)
                    except Exception:
                        return done(None, missing=False)
                n = min(180, size - len(buf))      # 256-byte base64 cap per reply

                def got(r):
                    data = base64.b64decode((r or {}).get("data", "")) if isinstance(r, dict) else b""
                    if not data:
                        return done(None, missing=False)
                    buf.extend(data)
                    read_more()
                self._fs(f"fs read {path} {len(buf)} {n}", got)
            read_more()
        self._fs(f"fs stat {path}", got_stat)

    def _lib_finish(self, job, note: str):
        self._lib_job = None
        ok, failed = job["ok"], job["failed"]
        removed = job.get("removed", [])

        def after_scan():
            if removed:
                msg = f"Removed {len(removed)} program(s)."
            else:
                msg = f"Installed {len(ok)} program(s)."
            if failed:
                msg += f"  {len(failed)} failed: " + ", ".join(p["name"] for p, _ in failed[:4])
            if note:
                msg += f"  ({note})"
            msg += "  Power-cycle the Videomancer to load the changes."
            self.library_tab.status.setText(msg)
            self.library_tab.progress.setVisible(False)
            if ok or removed:
                _VMConfirmDialog.notify(
                    self, "Restart your Videomancer",
                    msg + "\n\nThe Videomancer reads its program list when it starts, so "
                    "turn it off and on again. The app reconnects automatically.")
        self._lib_set_busy(False)
        self._lib_scan_device(then=after_scan)

    def _lib_remove(self, programs: list):
        if not self._worker:
            return
        self._lib_set_busy(True, f"Removing {len(programs)} program(s)\u2026")
        job = {"programs": [], "ok": [], "failed": [], "removed": [], "total": 1, "rel": None}
        pending = list(programs)

        def next_rm(_=None):
            if not pending:
                return self._lib_finish(job, "")
            p = pending.pop(0)

            def got(d):
                if isinstance(d, dict) and "error" in d:
                    job["failed"].append((p, d["error"]))
                else:
                    job["removed"].append(p)
                next_rm()
            cp = self.library_tab.card_path(p) or p["file"]
            p = dict(p, card_file=cp)
            self._fs(f"fs rm {SD_PROGRAMS}/{cp}", got)
        next_rm()

    def _lib_add_file(self, path: str):
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            return _VMConfirmDialog.notify(self, "Can't read file", str(exc))
        info = _vmprog_info(data)
        if not info:
            return _VMConfirmDialog.notify(
                self, "Not a Videomancer program",
                f"{Path(path).name} isn't a valid .vmprog file.")
        fname = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(path).name)
        prog = {"file": fname, "size": len(data),
                "sd_path": f"{SD_PROGRAMS}/{fname}", "name": info["name"] or Path(path).stem,
                "author": info["author"], "version": info["version"],
                "description": info["description"], "categories": [],
                "program_id": info["program_id"], "manifest_entry": None}
        if not _VMConfirmDialog.ask(
                self, "Install Program",
                f"Install <b>{prog['name']}</b> {prog['version']} by {prog['author'] or 'unknown'} "
                f"to <code>{prog['sd_path']}</code>?"
                + ("<br><br><b>This replaces the file with the same name on the card.</b>"
                   if fname in (self.library_tab._device_files or {}) else "")):
            return
        self._lib_install({}, [prog], file_blobs={prog["file"]: data})

    def _on_osc(self, addr: str, args: list):
        """Route one OSC message. Trigger addresses ignore a 0 argument (a
        TouchOSC-style button sends 1 on press and 0 on release)."""
        a = addr.rstrip("/").lower()
        if not a.startswith(OSC_PREFIX) or not self._worker:
            return
        a = a[len(OSC_PREFIX):]
        val = args[0] if args else None
        num = float(val) if isinstance(val, (int, float)) else None
        if num is not None and not math.isfinite(num):
            return                                  # NaN / inf from a broken patch
        released = num is not None and num == 0
        pt = self.param_tab
        if a == "/bpm" and num is not None:
            bpm = max(20.0, min(300.0, num))
            x100 = round(bpm * 100)
            # Update the display quietly (set_bpm flashes TAP for device-side
            # tempo changes, which would strobe during an OSC tempo sweep).
            pt.bpm_display.setText(f"{bpm:.2f}")
            pt.bpm_slider.blockSignals(True)
            pt.bpm_slider.setValue(x100)
            pt.bpm_slider.blockSignals(False)
            self._queue_cmd("bpm", f"transport bpm {x100}")
        elif a in ("/play", "/start") and not released:
            self._send_transport("start")
        elif a == "/stop" and not released:
            self._send_transport("stop")
        elif a == "/tap" and not released:
            pt._transport("tap")
        elif a.startswith("/param/") and num is not None:
            try:
                n = int(a.split("/")[2])
            except (IndexError, ValueError):
                return
            if 1 <= n <= 12:
                card = pt.channels[n - 1]
                f = max(0.0, min(1.0, num))
                v = (PARAM_RANGE if f >= 0.5 else 0) if card._is_toggle else round(f * PARAM_RANGE)
                card.set_manual(v, silent=True)
                pt._manual_changed(n - 1, v)
        elif a == "/program" and isinstance(val, str):
            match = next((p for p in self.prog_tab._all if p.casefold() == val.strip().casefold()), None)
            if match and match != self._active_program:
                self.load_program(match)
        elif a in ("/program/next", "/program/prev") and not released:
            names = self.prog_tab._all
            if names:
                # Step from a load still in flight, so quick presses advance.
                cur = self._pending_load or self._active_program
                i = names.index(cur) if cur in names else -1
                i = (i + (1 if a.endswith("next") else -1)) % len(names)
                self.load_program(names[i])
        elif a == "/randomize" and not released:
            pt.randomize()
        elif a == "/undo" and not released:
            pt.undo()

    def _on_latest_firmware(self, latest: str):
        self.system_tab.set_latest_firmware(latest)
        self._maybe_announce_firmware()

    def _maybe_announce_firmware(self):
        """One status-bar nudge per session when the device firmware is behind."""
        if self.system_tab.firmware_outdated() and not getattr(self, "_fw_announced", False):
            self._fw_announced = True
            self.status_bar.showMessage(
                f"Firmware {self.system_tab._latest_fw} is available — "
                f"update it with LZX Connect (System tab)", 10000)

    def _on_tab_changed(self, idx: int):
        if idx == 4 and not self._worker and not self._lib_releases:
            self._lib_refresh()          # browse the library even when offline
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
        elif idx == 4 and not self._library_busy:  # Library
            self._lib_refresh()

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
        self._load_watchdog.stop()
        self._cmd_flush.stop()
        for t in (getattr(self, '_update_checker', None), getattr(self, '_fw_checker', None)):
            if t is not None and t.isRunning() and not t.wait(1500):
                # Still inside urlopen (10 s timeout). Keep a reference so the
                # QThread isn't destroyed while running — Qt aborts on that.
                t.setParent(None)
                _orphan_threads.add(t)
                t.finished.connect(lambda t=t: _orphan_threads.discard(t))
        if self._monitor_window is not None:
            self._monitor_window.close()
            self._monitor_window = None
        if self._worker:
            self._worker.disconnect_port()
            # run() has no event loop, so quit() is a no-op — wait for the
            # loop to see _running=False and close the port.
            self._worker.wait(1500)
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

# ── OSC remote ─────────────────────────────────────────────────────────
#
# Anything that sends OSC (TouchOSC, Max, TouchDesigner, Resolume, Ableton
# via Max for Live …) can drive the app. Messages arrive on a background
# thread and are handed to the GUI thread through a Qt signal.

OSC_PREFIX = "/videomancer"
OSC_HELP = ("/videomancer/bpm 120  \u00b7  /play  \u00b7  /stop  \u00b7  /tap  \u00b7  "
            "/param/1\u201312 0.0\u20131.0  \u00b7  /program \"Name\"  \u00b7  /program/next  "
            "\u00b7  /program/prev  \u00b7  /randomize  \u00b7  /undo\n"
            "While OSC is on, any device on your network can send these \u2014 "
            "turn it off on shared networks.")


class _OscBridge(QObject):
    message = pyqtSignal(str, list)


_OSC = {"server": None, "bridge": None, "port": 0, "error": "", "last": "", "ip": "",
        "ui_timer": None}


def _local_ip() -> str:
    """This computer's LAN address (no packets are sent)."""
    import socket
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _osc_start(port: int) -> str:
    """Start (or restart) the OSC listener on UDP `port`. '' or an error."""
    _osc_stop()
    try:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
    except ImportError:
        _OSC["error"] = "python-osc isn't installed"
        return _OSC["error"]
    import threading
    if _OSC["bridge"] is None:
        _OSC["bridge"] = _OscBridge()
        _OSC["bridge"].message.connect(_osc_dispatch)
    d = Dispatcher()
    d.set_default_handler(lambda addr, *args: _OSC["bridge"].message.emit(addr, list(args)))
    try:
        srv = ThreadingOSCUDPServer(("0.0.0.0", int(port)), d)
    except OSError as exc:
        _OSC["error"] = f"port {port} unavailable ({exc.strerror or exc})"
        return _OSC["error"]
    threading.Thread(target=srv.serve_forever, daemon=True, name="osc").start()
    _OSC.update(server=srv, port=int(port), error="", ip=_local_ip())
    return ""


def _osc_stop():
    srv = _OSC.get("server")
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    _OSC.update(server=None, port=0)


def _osc_dispatch(addr: str, args: list):
    _OSC["last"] = f"{addr} {' '.join(str(a) for a in args)}".strip()
    target = next((w for w in _app_windows if w._worker), None) or \
        (_app_windows[0] if _app_windows else None)
    if target is not None:
        target._on_osc(addr, args)
    # Faders can send 60+ messages/s — refresh the status line at most 5×/s.
    if _OSC["ui_timer"] is None:
        t = QTimer()
        t.setSingleShot(True)
        t.setInterval(200)
        t.timeout.connect(_osc_refresh_ui)
        _OSC["ui_timer"] = t
    if not _OSC["ui_timer"].isActive():
        _OSC["ui_timer"].start()


def _osc_refresh_ui():
    for w in _app_windows:
        w.system_tab.set_osc_state(bool(_OSC["server"]), _OSC["port"],
                                   _OSC["error"], _OSC["last"])


def _osc_configure(enabled: bool, port: int):
    """From the System tab: remember the choice and start / stop listening."""
    st = _app_settings()
    st.setValue("osc/enabled", bool(enabled))
    st.setValue("osc/port", int(port))
    if enabled:
        _osc_start(port)
    else:
        _osc_stop()
        _OSC["error"] = ""
    _osc_refresh_ui()


def _switch_theme(name: str):
    """Remember the theme, then rebuild every window in it (widgets read the
    colour names when they're built). The live serial connection is handed
    from the old window to the new one, so the Videomancer stays connected."""
    _app_settings().setValue("appearance/theme", name)
    _apply_theme(name)
    _set_app_icon()
    for old in list(_app_windows):
        number, geo = old._window_number, old.geometry()
        live = old.release_worker()        # keep the Videomancer connected
        old.close()
        new = _spawn_window(number)
        if live:
            new.adopt_worker(*live)
        new.setGeometry(geo)
        new.tabs.setCurrentIndex(2)


def _set_app_icon():
    """Dock/taskbar icon: the wizard in the current theme's colours."""
    try:
        from PyQt6.QtGui import QIcon
        QApplication.instance().setWindowIcon(QIcon(_character_pixmap()))
    except Exception:
        pass


def _spawn_window(number: int) -> VideomancerApp:
    """Create and show a new VideomancerApp window."""
    w = VideomancerApp(window_number=number)
    _app_windows.append(w)
    w.system_tab.on_osc = _osc_configure
    w.system_tab.set_osc_state(bool(_OSC["server"]), _OSC["port"], _OSC["error"], _OSC["last"])
    w.show()
    return w


def _global_exception_hook(exc_type, exc_value, exc_tb):
    """Prevent PyQt6/Python 3.14 from calling abort() on unhandled exceptions.
    Without this, any Python exception in a Qt slot triggers SIGABRT."""
    import traceback
    traceback.print_exception(exc_type, exc_value, exc_tb)

def _is_translocated(bundle: Path) -> bool:
    """True when macOS Gatekeeper is running this bundle from a read-only
    AppTranslocation mount (quarantined app launched from where it was
    downloaded). In-place updates can't work from there."""
    return "/AppTranslocation/" in str(bundle)


def _offer_move_to_applications():
    """If launched from Downloads or Desktop, offer to move the .app bundle
    into /Applications so menu/dock/Spotlight behave the way users expect.
    Silently skipped when already installed, not running as a bundle, or on
    non-macOS platforms."""
    if sys.platform != "darwin":
        return
    exe = Path(sys.executable).resolve()
    bundle = next((p for p in exe.parents if p.suffix == ".app"), None)
    if bundle is None:
        return  # running from source, not a .app bundle
    parent = str(bundle.parent.resolve())
    home = str(Path.home().resolve())
    # Only nag if the bundle is in a transient-looking spot. A quarantined app
    # opened from Downloads runs from a read-only AppTranslocation copy, so its
    # parent is a random /private/var/folders path rather than ~/Downloads.
    if not _is_translocated(bundle) and \
            parent not in (f"{home}/Downloads", f"{home}/Desktop"):
        return
    target = Path("/Applications") / bundle.name
    if target.exists():
        return  # don't clobber an existing install
    if not _VMConfirmDialog.ask(
        None,
        "Move to Applications?",
        f"Keep <b>{bundle.name}</b> in your Applications folder for easy "
        f"launch and automatic updates? A copy will be made — you can trash "
        f"the one in {bundle.parent.name} afterward.",
    ):
        return
    try:
        import shutil, subprocess
        shutil.copytree(str(bundle), str(target), symlinks=True)
        # Strip Gatekeeper quarantine so it opens cleanly next launch
        subprocess.run(["xattr", "-cr", str(target)], check=False)
        # Launch the copy and quit this instance
        subprocess.Popen(["open", str(target)])
        sys.exit(0)
    except Exception as exc:
        _VMConfirmDialog.notify(
            None, "Move Failed",
            f"Could not move the app automatically.\n\n{exc}\n\n"
            f"You can drag it from {bundle.parent.name} to Applications "
            f"manually.",
        )


def main():
    # Install global exception hook BEFORE creating QApplication
    sys.excepthook = _global_exception_hook

    app = QApplication(sys.argv)
    app.setApplicationName("Videomancer Control")
    # Display name drives the Dock title, menu bar, and Cmd-Tab label even
    # when the bundle on disk is `VideomancerControl.app` (no space).
    app.setApplicationDisplayName("Videomancer Control")
    app.setOrganizationName("LZX Industries")
    _apply_theme(_app_settings().value("appearance/theme", "purple") or "purple")

    # Offer to move into /Applications if running from Downloads/Desktop.
    # Must run after QApplication is created (we use a Qt dialog).
    _offer_move_to_applications()

    # Load custom fonts (works both from source and PyInstaller bundle)
    from PyQt6.QtGui import QFontDatabase
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    font_dir = base / "fonts"
    for font_file in ["goldplay-semibold.ttf", "ReliefSingleLine-Regular.ttf"]:
        fpath = font_dir / font_file
        if fpath.exists():
            QFontDatabase.addApplicationFont(str(fpath))

    _set_app_icon()                      # the wizard, in the theme's colours

    # Detect how many Videomancer devices are already plugged in
    initial_ports = ConnectionBar.find_all_videomancer_ports()
    count = max(1, len(initial_ports))  # always open at least one window
    for i in range(count):
        _spawn_window(i + 1)

    app.aboutToQuit.connect(_osc_stop)
    # OSC remote: resume listening if it was on last time
    st = _app_settings()
    if st.value("osc/enabled", False, type=bool):
        _osc_start(st.value("osc/port", 9000, type=int))
        _osc_refresh_ui()

    # Global hot-plug watcher: spawn a new window when a new device appears
    def _global_hotplug():
        try:
            all_ports = ConnectionBar.find_all_videomancer_ports()
            unclaimed = [p for p in all_ports if p not in _claimed_ports]
            if unclaimed:
                windows = list(_app_windows)
                all_connected = all(
                    w._worker and w._worker.isRunning() for w in windows
                )
                if all_connected:
                    _spawn_window(len(windows) + 1)
        except Exception:
            pass

    hotplug = QTimer()
    hotplug.setInterval(3000)
    hotplug.timeout.connect(_global_hotplug)
    hotplug.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
