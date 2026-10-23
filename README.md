# Sevcon Gen4 CANdapter Utility

Cross-platform Python/Tkinter CANopen diagnostics and settings utility for Sevcon Gen4 controllers connected using the **Ewert CANdapter** USB serial adapter.

> **Development prototype, not hardware validated.** Review the firmware's object dictionary / EDS before writing configuration. No guarantee of suitability for propulsion or safety-critical use.

## Features

- Select serial port, CAN bitrate and CANopen node ID
- Read CANopen identity, error register and **individual active Sevcon fault IDs**, and request **controller-provided text descriptions** (0x5300 / 0x5610)
- Read predefined battery protection thresholds, displaying volts alongside raw values
- Explicit write confirmation, read-before-write, and read-back verification
- Export collected diagnostics to CSV
- Communication log; supports manual SDO access

Active Sevcon fault descriptions are retrieved directly from the controller when supported by its firmware. Stored fault-log decoding and access unlocks are not implemented. The fault reader uses limited segmented SDO uploads for text descriptions (up to 4096 bytes). Fault selector SDO writes only change diagnostic selection, not fault storage or settings. Saving parameter changes across power cycles is not implemented. Controller-specific object mapping/scaling must be independently verified before applying writes.

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

## EDS object dictionary browser

Open the **EDS browser** tab and choose **Open EDS…** to load your controller's firmware-matched CANopen Electronic Data Sheet (`.eds`). Search the index or object name, select a parameter and choose **Read selected**.

The browser displays the EDS description, data type, access flags, default and declared limits. CANopen INTEGER8/16/32, UNSIGNED8/16/32, BOOLEAN and REAL32 values are decoded into numbers. Unsupported types (including strings, domain data and segmented transfers) are not written. EDS numeric types are **not** automatically converted into engineering units such as volts or amps: manufacturer-specific scaling requires independent verification. The existing Battery Settings tab continues to use its documented voltage conversions.

EDS entries marked writable may be edited only after a successful live read, explicit confirmation and identical read-back. Objects marked read-only cannot be edited. Actual Sevcon authentication, firmware limits, operational-state requirements and persistent saving are independent of EDS permission flags. **Back up controller configuration and keep propulsion disabled before writing settings.**

Tests: `python -m unittest discover -s tests -v`