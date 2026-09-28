#!/bin/bash
# BUILD.sh — builds Videomancer Control.app
# Run from the directory containing main.py and serial_worker.py
# ─────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "  ╔════════════════════════════════════╗"
echo "  ║   VIDEOMANCER CONTROL — BUILD      ║"
echo "  ╚════════════════════════════════════╝"
echo ""

# ── 1. Check dependencies ─────────────────────────────────────────
echo "→ Checking dependencies..."

if ! python3 -c "import PyQt6" 2>/dev/null; then
    echo "  Installing PyQt6..."
    pip3 install PyQt6
fi

if ! python3 -c "import serial" 2>/dev/null; then
    echo "  Installing pyserial..."
    pip3 install pyserial
fi

if ! python3 -c "import pythonosc" 2>/dev/null; then
    echo "  Installing python-osc..."
    pip3 install python-osc
fi

if ! python3 -c "import py2app" 2>/dev/null; then
    echo "  Installing py2app..."
    pip3 install py2app
fi

echo "  ✓ Dependencies OK"

# ── 2. Clean previous build ───────────────────────────────────────
echo "→ Cleaning previous build..."
rm -rf build dist
echo "  ✓ Clean"

# ── 3. Generate icon (if no icon.icns provided) ───────────────────
if [ ! -f "icon.icns" ]; then
    echo "→ Generating placeholder icon..."
    mkdir -p icon.iconset
    # Create a simple dark square icon using sips
    python3 - << 'PYEOF'
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import Qt
import sys, os

app = QApplication(sys.argv)
for size in [16, 32, 64, 128, 256, 512]:
    pm = QPixmap(size, size)
    pm.fill(QColor('#0a0a0a'))
    p = QPainter(pm)
    p.setPen(QColor('#ffffff'))
    f = QFont('Courier New', max(4, size // 8))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, 'VM')
    p.end()
    pm.save(f'icon.iconset/icon_{size}x{size}.png')
    pm.save(f'icon.iconset/icon_{size}x{size}@2x.png')
PYEOF
    iconutil -c icns icon.iconset -o icon.icns 2>/dev/null || echo "  (iconutil not available — skipping custom icon)"
    rm -rf icon.iconset
fi

# ── 4. Build .app ─────────────────────────────────────────────────
echo "→ Building .app bundle..."
python3 setup.py py2app 2>&1 | grep -v "^running\|^creating\|^copying\|^stripping\|^byte" || true

# ── 5. Verify ─────────────────────────────────────────────────────
APP="dist/Videomancer Control.app"
if [ -d "$APP" ]; then
    SIZE=$(du -sh "$APP" | cut -f1)
    echo ""
    echo "  ✓ Built: $APP ($SIZE)"
    echo ""
    echo "  ┌─────────────────────────────────────────┐"
    echo "  │  To run:  open \"dist/Videomancer Control.app\""
    echo "  │  To share: zip -r VideomancerControl.zip \"dist/Videomancer Control.app\""
    echo "  └─────────────────────────────────────────┘"
    echo ""
else
    echo ""
    echo "  ✗ Build failed — check output above"
    exit 1
fi

# ── 6. Optional: ad-hoc code sign so Gatekeeper doesn't block it ──
echo "→ Ad-hoc signing (allows running without notarization)..."
codesign --force --deep --sign - "$APP" 2>/dev/null && echo "  ✓ Signed" || echo "  (skipped — codesign not available)"

echo ""
echo "  Done! Drag 'Videomancer Control.app' to your Applications folder."
echo ""
