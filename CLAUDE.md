# CLAUDE.md — Agent onboarding

Context for agents working in `/Users/nathanielcoleman/Downloads/files`. Claude Code auto-loads this file; other agents should read it on entry.

The end-user-facing doc is `README.md` — keep user install / usage instructions there, not here.

---

## Two unrelated projects share this directory

1. **Videomancer Control** — everything at the root. Python 3.10+/PyQt6 desktop companion app for LZX Industries' Videomancer hardware. Ships as a signed+notarized macOS `.app` and Windows `.exe`. Current version: **2.7** (see `APP_VERSION` in `main.py`).
2. **FairWaste/** — self-contained Swift/Metal iOS+macOS project (camera-input real-time frame store). No code dependency on Videomancer; they just share a parent folder.

---

## Videomancer — key files

| File | Purpose |
|------|---------|
| `main.py` (~7.1k lines) | Entire GUI. Visible tabs: PROGRAMS, CONTROL (12-channel), SYSTEM, STATE (device presets + local snapshots). `PresetsTab`/`SnapshotsTab` are hidden legacy classes kept for callbacks; `MonitorWindow`/DeckLink capture is unreachable dead code. Bump `APP_VERSION` here on release. |
| `serial_worker.py` | `SerialWorker(QThread)` — owns the pyserial connection, queues commands, parses responses, emits Qt signals (`connected`, `response`, `status_update`, `programs_page`, …). |
| `setup.py` | py2app config for the macOS build. Also carries `CFBundleVersion` / `CFBundleShortVersionString` — keep in sync with `APP_VERSION`. |
| `Videomancer Control.spec`, `VideomancerControl.spec` | PyInstaller specs (macOS + Windows). |
| `BUILD.sh` / `BUILD_WIN.bat` | Local build scripts (PyInstaller-based). |
| `entitlements.plist` | Hardened-runtime entitlements for Apple notarization (USB + network client). |
| `.github/workflows/build-release.yml` | CI: builds a universal2 macOS app (DMG + updater zips) and Windows, code-signs (Apple), notarizes, publishes a GitHub release on tag push. |
| `scripts/fuse_qt_universal.py` | lipo-merges the other-arch PyQt6-Qt6 wheel into the installed one. PyQt6-Qt6 ships per-arch wheels (everything else is universal2), which is why a plain `--target-architecture universal2` build crashes on Intel. Run before PyInstaller. |
| `fonts/` | Embedded fonts (`goldplay-semibold.ttf`, `ReliefSingleLine-Regular.ttf`). Referenced by `setup.py` DATA_FILES and loaded at runtime in `main.py`. |
| `icon.icns`, `icon.ico`, `icon.iconset/` | App icons (macOS, Windows, source). |

## Videomancer — device protocol

Serial over USB (`/dev/cu.usbmodem*` on macOS). Host sends newline-terminated ASCII commands; device responds line-by-line:

- `@key:payload` — success (payload is often JSON)
- `!code:message` — error
- any other line — free-form log

Common commands: `version`, `status`, `programs list [offset]`, `program load <name>`, `modulation status`, `transport status`, `video status`. `SerialWorker._dispatch` is the canonical parser.

Protocol gotchas (verified on firmware 1.0.0-rc.55):
- The reply key is the command's first word, so `program state`/`info`/`presets list` all come back as `@program:` and are told apart by JSON keys. Error keys are numeric codes (`0x58000000 | n`), so an error can't be tied to the command that caused it.
- Transport is `transport play|stop|bpm <x100>` — there is no `start` or `tap` (tap tempo is computed in the app).
- `program state` reports toggles as 0/1; the app sends 1023 for ON.
- Time/Space/Slope are positional: `modulation set <ch> <manual> <time> <space> <slope>`. The old `modulation set <ch> <val> <t|sp|sl>` form (RC11) does not work on current firmware.
- SD card (rc.55): `fs mkdir` of a NEW folder resets the device — the Library installer never creates folders (existing author folder, else loose in `sd:/programs/`). The firmware finds programs without `sd:/programs/manifest.json`, so the app only reads it. At most **70** SD programs load at boot (`SD_PROGRAM_LIMIT`); Finder `._` files don't count. `fs put` owns the link until its last byte — `SerialWorker.put_file` runs uploads exclusively.
- Knob drags go through `_queue_cmd` (~30 Hz coalescing); polls through `_poll_once` (one in flight); device readback for a channel is ignored for `EDIT_GUARD_S` after a local edit.

## Videomancer — release flow

1. Bump `APP_VERSION` in `main.py:16` and `CFBundleVersion` / `CFBundleShortVersionString` in `setup.py`.
2. Commit, tag (`vX.Y.Z`), push tag.
3. `.github/workflows/build-release.yml` does macOS + Windows builds, Apple signing, notarization, and creates the GitHub release.

## Run Videomancer from source

```bash
pip install PyQt6 pyserial
python main.py
```

---

## FairWaste — quick notes

- Xcode project is generated from `FairWaste/project.yml` via **XcodeGen** — there is no `.xcodeproj` committed by convention (run `xcodegen` inside `FairWaste/` before opening).
- Source lives in `FairWaste/FairWaste/` (Swift, Metal shaders, Info.plist, entitlements).
- Targets iOS 16+ and macOS 13+.

---

## Ignore / do not edit

- `main_backup_v1.3.py`, `main_backup_v1.4.py`, `main_backup_v2.0.py`, `main_backup_v2.0.1.py`, `main_backup_v2.1.py` and the matching `serial_worker_backup_v*.py` — frozen snapshots of prior versions. `git log` is authoritative; don't diff against these unless you have a reason.
- `build/`, `dist/` — build outputs. Never hand-edit or commit.
- `.venv/` — local virtualenv.
- `VideomancerControl.zip` (~93 MB) — checked-in release artifact. Leave it alone.
- `__pycache__/`, `.DS_Store` — OS / interpreter junk.

---

## Conventions for agents

- Changes to user-visible install/usage instructions → `README.md`.
- Changes to agent context (new files, new subsystems, protocol changes) → this file.
- Don't introduce new backup-by-copy files (`main_backup_vX.Y.py` style). Use git.
- `main.py` is large but monolithic by design — don't split it up without an explicit ask.
