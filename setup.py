"""
setup.py — py2app build configuration for Videomancer Control
Usage:
    pip3 install py2app
    python3 setup.py py2app
Output:
    dist/Videomancer Control.app
"""

from setuptools import setup

APP        = ['main.py']
DATA_FILES = [
    ('fonts', ['fonts/goldplay-semibold.ttf',
               'fonts/ReliefSingleLine-Regular.ttf']),
]

OPTIONS = {
    'argv_emulation': False,        # must be False for PyQt6
    'iconfile': 'icon.icns',        # optional — see BUILD.md
    'plist': {
        'CFBundleName':             'Videomancer Control',
        'CFBundleDisplayName':      'Videomancer Control',
        'CFBundleIdentifier':       'net.lzxindustries.videomancer-control',
        'CFBundleVersion':          '2.4.1',
        'CFBundleShortVersionString': '2.4.1',
        'NSHumanReadableCopyright': '© 2026 LZX Industries / Videowaste',
        'NSHighResolutionCapable':  True,

        # USB serial access — required for /dev/cu.usbmodem* ports
        'IOKitFrameworkIsUsedByApp': True,

        # macOS privacy strings
        'NSBluetoothAlwaysUsageDescription': '',  # not used — suppresses warning
    },
    'packages': [
        'PyQt6',
        'serial',
    ],
    'includes': [
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'serial_worker',
        'json',
        'pathlib',
        'datetime',
        're',
        'math',
        'time',
        'os',
        'sys',
    ],
    'excludes': [
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'IPython',
        'test',
        'unittest',
        'xmlrpc',
    ],
    # Bundle Qt frameworks inside the .app
    'frameworks': [],
    # Strip debug symbols for smaller bundle
    'strip': True,
    'optimize': 1,
}

setup(
    app=APP,
    name='Videomancer Control',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
