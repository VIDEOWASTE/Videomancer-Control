# Videomancer Control
### Desktop companion app for LZX Industries Videomancer
**LZX Industries x Videowaste x Claude**

---

## Running from source
```bash
pip install PyQt6 pyserial
python main.py
```
Works on macOS, Windows, and Linux.

---

## Building the macOS .app

### Prerequisites
- macOS 12+
- Python 3.10+ (from python.org — not the Apple system Python)
- Xcode Command Line Tools: `xcode-select --install`

### One-command build
```bash
chmod +x BUILD.sh
./BUILD.sh
```
Produces `dist/Videomancer Control.app`

### First launch (Gatekeeper)
macOS blocks unsigned apps. Fix with:
```bash
xattr -cr "dist/Videomancer Control.app"
open "dist/Videomancer Control.app"
```
Or: System Settings > Privacy & Security > Open Anyway

### Distribute
```bash
zip -r VideomancerControl_macOS.zip "dist/Videomancer Control.app"
```

---

## Building the Windows .exe

### Prerequisites
- Windows 10+
- Python 3.10+ (from python.org — check "Add to PATH" during install)

### One-command build
```cmd
BUILD_WIN.bat
```
Produces `dist\VideomancerControl.exe`

### Distribute
Zip the exe:
```cmd
powershell Compress-Archive -Path "dist\VideomancerControl.exe" -DestinationPath VideomancerControl_Windows.zip
```

---

## Files
| File | Purpose |
|------|---------|
| `main.py` | Main application |
| `serial_worker.py` | USB serial thread |
| `setup.py` | py2app build config (macOS) |
| `BUILD.sh` | macOS build script |
| `BUILD_WIN.bat` | Windows build script |
