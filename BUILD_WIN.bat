@echo off
REM ── Videomancer Control — Windows Build Script ──
REM Produces: dist\VideomancerControl.exe

echo === Videomancer Control Windows Build ===
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Install from python.org
    pause
    exit /b 1
)

REM Install dependencies
echo Installing dependencies...
pip install PyQt6 pyserial python-osc pyinstaller

REM Build
echo.
echo Building .exe...
pyinstaller --onefile --windowed ^
    --name "VideomancerControl" ^
    --icon icon.iconset\icon_256x256.png ^
    --add-data "VM_Logo.png;." ^
    --add-data "VM.png;." ^
    --hidden-import serial.tools.list_ports ^
    --hidden-import pythonosc.osc_server ^
    main.py

echo.
if exist "dist\VideomancerControl.exe" (
    echo SUCCESS: dist\VideomancerControl.exe
    echo.
    echo To run: dist\VideomancerControl.exe
) else (
    echo BUILD FAILED — check output above for errors
)
pause
