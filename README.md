# Videomancer Control
### Desktop companion app for LZX Industries Videomancer

---

## Download

Grab the latest release from the [**Releases**](https://github.com/VIDEOWASTE/Videomancer-Control/releases) page.

| Platform | Download | Requirements |
|----------|----------|-------------|
| **macOS (Apple Silicon + Intel)** | `VideomancerControl_macOS.dmg` | Any Mac, macOS 12+ |
| **Windows** | `VideomancerControl_Windows.zip` | Windows 10+ |

The macOS app is a single **universal** build — it runs natively on both Apple Silicon and Intel Macs — and is **signed and notarized by Apple**, so it launches with no security prompts. After the first launch, the app's built-in updater keeps you on the latest version automatically.

### macOS Install
1. Download `VideomancerControl_macOS.dmg` from [Releases](https://github.com/VIDEOWASTE/Videomancer-Control/releases).
2. Open the DMG and drag **Videomancer Control** onto the **Applications** folder.
3. Launch it from Applications. (If you run it straight from Downloads instead, it will offer to move itself — updates need it in Applications.)

### Windows Install
1. Download `VideomancerControl_Windows.zip` from [Releases](https://github.com/VIDEOWASTE/Videomancer-Control/releases).
2. Unzip the file.
3. Run **Videomancer Control.exe**.
4. Windows Defender SmartScreen may show a warning — click **More info** → **Run anyway**.

---

## Getting Started

1. Plug your Videomancer into your computer via USB.
2. Launch the app — it auto-detects and connects.
3. Browse and load programs from the **Programs** tab.
4. Shape parameters in real-time on the **Motion** tab.
5. Save / restore full device state on the **State** tab.
6. Monitor device info, mount the SD card as a drive, or manage video routing from the **System** tab.

The app auto-connects whenever you plug in your Videomancer. No manual port configuration.

---

## Features

- **Program Browser** — search, browse, and load FPGA programs by name; a ☆ at the end of each row marks favorites, plus a recently-loaded list
- **Program Library** — browse LZX's official and community libraries, see what's on your SD card, and install / update / remove programs over USB (checksum-verified, firmware-compatibility and free-space checks). **MY PROGRAMS** lists your own programs on the card. Shows the 70-program load limit.
- **Themes** — Videomancer Purple or Amber (System tab)
- **OSC Remote** — control the app from TouchOSC, Max, TouchDesigner, Resolume or Ableton (via Max for Live): turn it on in System → OSC Remote (UDP port 9000 by default) and send `/videomancer/bpm 120`, `/videomancer/play`, `/stop`, `/tap`, `/param/1`–`/param/12` (0.0–1.0), `/program "Name"`, `/program/next`, `/program/prev`, `/randomize`, `/undo`
- **Firmware Check** — compares your Videomancer's firmware with LZX's newest release and links straight to LZX Connect when an update is out
- **Device Health** — CPU, memory, FPGA state and SD card space on the System tab
- **Keyboard Shortcuts** — Space play/stop · T tap tempo · ⌘1–4 switch tabs · ⌘F find a program · ⌘R refresh
- **12-Channel Parameter Control** — custom knobs, faders, and toggles with live bidirectional sync
- **LFO Modulation** — assign modulators (Free LFO, Sync LFO, Audio Input, Step Seq, Random, Envelope, and 30+ more) with per-operator waveform visualization (smooth, audio-style, stepped, or jagged)
- **Time / Space / Slope** — per-channel TSS knobs that glide in sync with the main parameters
- **Transport** — tap tempo, BPM control, play / stop with hardware sync
- **States (device presets)** — save and recall parameter snapshots on the device itself
- **Snapshots** — save / restore full device state as local JSON files you can back up or share
- **SD Card as USB Drive** — mount the Videomancer's SD storage on your computer with a single click (System tab → Storage)
- **System Settings** — video input / output, timing, MIDI CC mapping, firmware version
- **Auto-Connect** — hot-plug detection spins up a new window per connected Videomancer
- **In-App Updater** — detects new releases, downloads and installs in place, relaunches
- **Cross-Platform** — one universal Mac app (Apple Silicon + Intel) and Windows
- **LZX Connect link** — jump to LZX's official firmware updater from the System tab

---

## Running from Source

Works on macOS, Windows, and Linux.

### Prerequisites
- Python 3.10+ ([python.org](https://www.python.org/downloads/))
- On macOS: Xcode Command Line Tools (`xcode-select --install`) if prompted

### Install & Run
```bash
pip install PyQt6 pyserial python-osc
python main.py
```

---

## Building from Source

### macOS (.app)
```bash
chmod +x BUILD.sh
./BUILD.sh
```
Produces `dist/Videomancer Control.app` for your machine's native architecture.

### Windows (.exe)
```cmd
BUILD_WIN.bat
```
Produces `dist\Videomancer Control.exe`.

Official releases are built through GitHub Actions — see `.github/workflows/build-release.yml` for the signed / notarized pipeline.

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | The full application (GUI + serial protocol + updater) |
| `serial_worker.py` | Background thread for USB serial communication |
| `BUILD.sh` | macOS local build script (PyInstaller + py2app) |
| `BUILD_WIN.bat` | Windows local build script (PyInstaller) |
| `entitlements.plist` | Hardened-runtime entitlements for macOS signing |
| `setup.py` | py2app configuration for macOS bundling |
| `scripts/fuse_qt_universal.py` | Merges the x86_64 and arm64 Qt wheels so PyInstaller can build a true universal2 app |
| `.github/workflows/build-release.yml` | CI: builds the universal macOS DMG + Windows, signs, notarizes, publishes release |
| `CLAUDE.md` | Onboarding doc for AI agents or new contributors working in this repo |

---

## Troubleshooting

**App won't open on macOS** — the release is notarized, so this shouldn't happen. If it does, try `xattr -cr "/Applications/Videomancer Control.app"` to clear any leftover quarantine attribute.

**Updating Videomancer firmware** — use LZX's [LZX Connect](https://lzxindustries.net/connect) app (also linked from the System tab).

**App won't open on Windows** — SmartScreen warning: **More info** → **Run anyway**.

**Device not detected**
- Check the USB cable is a data cable (not charge-only).
- Make sure no other app is holding the serial port (`screen`, Arduino IDE, etc.).
- Unplug and replug the Videomancer; the app will auto-detect.

**Controls feel unresponsive**
- The app polls the device every 350 ms and smooths between samples. Fast-moving modulators are expected to glide rather than snap.
- If there's real lag, close other apps that might be using the serial port.

---

*Built with PyQt6 + pyserial. Signed and notarized through GitHub Actions.*
