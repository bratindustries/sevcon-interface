# Sevcon Gen4 CANdapter Utility

Cross-platform Python/Tkinter CANopen diagnostics and settings utility for Sevcon Gen4 controllers connected using the **Ewert CANdapter** USB serial adapter.

> **Development prototype, not hardware validated.** Review the firmware's object dictionary / EDS before writing configuration. No guarantee of suitability for propulsion or safety-critical use.

## Features

- Select serial port, CAN bitrate and CANopen node ID
- Read CANopen identity and error-register objects and Sevcon active-fault data
- Read predefined battery protection thresholds, displaying volts alongside raw values
- Explicit write confirmation, read-before-write, and read-back verification
- Export collected diagnostics to CSV
- Communication log; supports manual SDO access

Specific Sevcon fault descriptions and access unlocks are not implemented. Saving parameter changes across power cycles is not implemented. Controller-specific object mapping/scaling must be independently verified before applying writes.

## Run from source

Requires Python 3.10+ and Tkinter; `python3-tk` may be required on Linux.

```bash
python -m pip install -r requirements.txt
python sevcon_ui.py
```

Windows may use `py` instead of `python`.

## Package for your OS

Install `pyinstaller` in your build environment, then build on the target OS:

```bash
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name SevconGen4 sevcon_ui.py
```

On macOS, a normal `--windowed` build can produce an app bundle. PyInstaller cannot cross-compile between Windows, Linux and macOS.

## Safety

Start read-only, with the propeller disabled and motor prevented from starting. Verify the CAN wiring and termination. Do **not** reduce controller voltage protection below safe limits. Never assume a successful CANopen write has been stored nonvolatilely. No BMS is present on some installations; appropriate independent battery protection is essential.

## Status

Python syntax validated only; no live CANdapter/Sevcon testing completed. This repository is an experimental starting point.