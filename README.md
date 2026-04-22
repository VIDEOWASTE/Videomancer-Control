# Videomancer Control
### Desktop companion app for LZX Industries Videomancer

---

## Download

Grab the latest release from the [**Releases**](https://github.com/VIDEOWASTE/Videomancer-Control/releases) page.

| Platform | Download | Requirements |
|----------|----------|-------------|
| **macOS (Apple Silicon)** | `VideomancerControl_macOS.zip` | M1 / M2 / M3 / M4, macOS 12+ |
| **macOS (Intel)** | `VideomancerControl_macOS_Intel.zip` | Intel-based Mac, macOS 12+ |
| **Windows** | `VideomancerControl_Windows.zip` | Windows 10+ |

Both macOS builds are **signed and notarized by Apple**, so they launch with no security prompts. After the first launch, the app's built-in updater will keep you on the latest version automatically — it picks the right binary for your Mac's architecture.

### macOS Install
1. Download the zip matching your Mac from [Releases](https://github.com/VIDEOWASTE/Videomancer-Control/releases) (Apple Silicon or Intel).
2. Unzip — you'll get **Videomancer Control.app**.
3. First launch from Downloads/Desktop will offer to move the app into `/Applications/` for you. Accept, or drag it there manually.
4. Double-click to run.

*Not sure which Mac you have?* Apple menu → About This Mac → "Chip" says "Apple M…" (Apple Silicon) or "Intel…" (Intel).

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

- **Program Browser** — search, browse, and load FPGA programs by name
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
- **Cross-Platform** — native Apple Silicon, native Intel Mac, and Windows

---

## Running from Source

Works on macOS, Windows, and Linux.

### Prerequisites
- Python 3.10+ ([python.org](https://www.python.org/downloads/))
- On macOS: Xcode Command Line Tools (`xcode-select --install`) if prompted

### Install & Run
```bash
pip install PyQt6 pyserial
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
| `.github/workflows/build-release.yml` | CI: builds Apple Silicon + Intel + Windows, signs, notarizes, publishes release |
| `CLAUDE.md` | Onboarding doc for AI agents or new contributors working in this repo |

---

## Troubleshooting

**App won't open on macOS** — the release is notarized, so this shouldn't happen. If it does, try `xattr -cr "/Applications/Videomancer Control.app"` to clear any leftover quarantine attribute.

**"Needs to be updated" / crashes on Intel Mac** — make sure you downloaded `VideomancerControl_macOS_Intel.zip` (not the Apple Silicon version).

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
