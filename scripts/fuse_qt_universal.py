#!/usr/bin/env python3
"""
fuse_qt_universal.py — make the installed PyQt6-Qt6 package universal2.

PyQt6, PyQt6-sip and pyserial ship universal2 wheels, but PyQt6-Qt6 (the Qt
frameworks and plugins) ships separate arm64 and x86_64 wheels. That is why
v2.4.2's `--target-architecture universal2` build crashed on Intel Macs: the
launcher was universal but Qt was arm64-only.

This script downloads the *other* architecture's PyQt6-Qt6 wheel (same
version as the one installed) and lipo-merges every Mach-O file into the
installed copy, in place. Afterwards PyInstaller's universal2 target can
collect a genuinely universal Qt.

Run it with the same (universal2 python.org) interpreter PyInstaller uses:
    python scripts/fuse_qt_universal.py
"""

import platform
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path

MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little-endian thin)
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
}


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in MACHO_MAGICS
    except OSError:
        return False


def archs(path: Path) -> set:
    out = subprocess.run(["lipo", "-archs", str(path)],
                         capture_output=True, text=True)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def main() -> int:
    if sys.platform != "darwin":
        print("fuse_qt_universal: macOS only")
        return 1

    version = metadata.version("PyQt6-Qt6")
    dist = metadata.distribution("PyQt6-Qt6")
    site = Path(dist.locate_file(""))
    here = platform.machine()                  # arch of the installed wheel
    other = "x86_64" if here == "arm64" else "arm64"
    plat = "macosx_10_14_x86_64" if other == "x86_64" else "macosx_11_0_arm64"
    print(f"PyQt6-Qt6 {version} installed ({here}); fetching {other} wheel")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "--quiet",
             "--no-deps", "--only-binary=:all:", "--platform", plat,
             "--dest", str(tmp), f"PyQt6-Qt6=={version}"],
            check=True,
        )
        wheel = next(tmp.glob("*.whl"))
        other_root = tmp / "other"
        with zipfile.ZipFile(wheel) as z:
            z.extractall(other_root)

        fused = skipped = 0
        missing = []
        for src in other_root.rglob("*"):
            if src.is_symlink() or not src.is_file() or not is_macho(src):
                continue
            rel = src.relative_to(other_root)
            dst = site / rel
            if not dst.exists():
                missing.append(str(rel))
                continue
            dst_real = dst.resolve()
            have = archs(dst_real)
            if {"x86_64", "arm64"} <= have:
                skipped += 1
                continue
            out = dst_real.with_name(dst_real.name + ".universal")
            subprocess.run(["lipo", "-create", str(dst_real), str(src),
                            "-output", str(out)], check=True)
            out.replace(dst_real)
            fused += 1

    print(f"fused {fused} Mach-O files ({skipped} already universal)")
    if missing:
        print(f"WARNING: {len(missing)} files only in the {other} wheel:")
        for m in missing[:20]:
            print("   ", m)

    # Verify: every Mach-O under PyQt6/Qt6 must now carry both slices
    bad = [p for p in (site / "PyQt6" / "Qt6").rglob("*")
           if p.is_file() and not p.is_symlink() and is_macho(p)
           and not {"x86_64", "arm64"} <= archs(p)]
    if bad:
        print(f"ERROR: {len(bad)} Qt binaries are still single-arch, e.g. {bad[0]}")
        return 1
    print("PyQt6-Qt6 is now universal2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
