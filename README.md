# Videomancer Control
### Desktop companion app for LZX Industries Videomancer

---

## Download

Go to the [**Releases**](https://github.com/VIDEOWASTE/VIDEOMANCER-Control-Interface/releases) page and download the latest version for your platform:

| Platform | Download | Requirements |
|----------|----------|-------------|
| **macOS** | `VideomancerControl_macOS.zip` | macOS 12+ |
| **Windows** | `VideomancerControl_Windows.zip` | Windows 10+ |

### macOS Install
1. Download `VideomancerControl_macOS.zip` from [Releases](https://github.com/VIDEOWASTE/VIDEOMANCER-Control-Interface/releases)
2. Unzip the file
3. Drag **Videomancer Control.app** to your Applications folder
4. On first launch, macOS will block it (unsigned app). Fix:
   - Go to **System Settings > Privacy & Security**
   - Scroll down and click **"Open Anyway"**
   - Or run in Terminal: `xattr -cr "/Applications/Videomancer Control.app"`

### Windows Install
1. Download `VideomancerControl_Windows.zip` from [Releases](https://github.com/VIDEOWASTE/VIDEOMANCER-Control-Interface/releases)
2. Unzip the file
3. Run **VideomancerControl.exe**
4. Windows Defender may show a warning — click **"More info"** then **"Run anyway"**

---

## Getting Started

1. Plug in your Videomancer via USB
2. Launch the app — it will auto-detect and connect
3. Browse and load programs from the **Programs** tab
4. Adjust parameters in real-time on the **Motion** tab
5. Manage presets and snapshots on the **State** tab

The app auto-connects whenever you plug in your Videomancer. No manual port configuration needed.

---

## Features

- **Program Browser** — search, browse, and load FPGA programs
- **12-Channel Parameter Control** — custom knobs, faders, and toggles with real-time sync
- **LFO Modulation** — assign modulators (Free LFO, Sync LFO, Random, Envelope, Step Seq, and 30+ more) with live waveform visualization
- **Time/Space/Slope Controls** — per-channel TSS adjustment for each modulator
- **Transport** — tap tempo, BPM control, play/stop with hardware sync
- **Presets** — factory and user preset management
- **Snapshots** — save/restore full device state as local JSON files
- **System Settings** — video input/output, timing, MIDI CC mapping, firmware info
- **Auto-Connect** — hot-plug detection, automatic device discovery
- **Cross-Platform** — macOS and Windows

---

## Running from Source

If you prefer to run from source instead of using the pre-built app:

### Prerequisites
- Python 3.10+ ([download from python.org](https://www.python.org/downloads/))
- On macOS: you may need Xcode Command Line Tools (`xcode-select --install`)

### Install & Run
```bash
pip install PyQt6 pyserial
python main.py
```

Works on macOS, Windows, and Linux.

---

## Building from Source

### macOS (.app)
```bash
chmod +x BUILD.sh
./BUILD.sh
```
Produces `dist/Videomancer Control.app`

### Windows (.exe)
```cmd
BUILD_WIN.bat
```
Produces `dist\VideomancerControl.exe`

---

## Files
| File | Purpose |
|------|---------|
| `main.py` | Main application |
| `serial_worker.py` | USB serial communication thread |
| `setup.py` | py2app build config (macOS) |
| `BUILD.sh` | macOS build script |
| `BUILD_WIN.bat` | Windows build script |

---

## Troubleshooting

**App won't open on macOS**
- System Settings > Privacy & Security > Open Anyway
- Or: `xattr -cr "Videomancer Control.app"`

**App won't open on Windows**
- Windows Defender SmartScreen > More info > Run anyway

**Device not detected**
- Make sure Videomancer is connected via USB
- Try unplugging and replugging
- Check that no other app is using the serial port

**Controls feel unresponsive**
- The app polls the device at ~100ms intervals for real-time sync
- If lag persists, close other apps using the serial port

---

*Built with PyQt6 + pyserial*
