#!/usr/bin/env python3
"""Identify a Crystalfontz CFA635-family module on the serial bus.

Read-only: sends only Ping (0), Get Version (1), Read User Flash (3),
Read Reporting & Status (30) and Read Keypad (24). Nothing is written to
the display, to flash, or to the boot state.

Usage:
    uv run cfa635-probe [--port /dev/ttyUSB0]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import serial

from cfa635.driver import Cfa635, CommandError, CrcError

# The CFA635 only ever ships with these two rates; 115200 is the -KU default.
BAUD_CANDIDATES = (115200, 19200)


def find_port() -> str | None:
    by_id = sorted(glob.glob("/dev/serial/by-id/*Crystalfontz*"))
    if by_id:
        return os.path.realpath(by_id[0])
    ttys = sorted(glob.glob("/dev/ttyUSB*"))
    return ttys[0] if ttys else None


def usb_identity(port: str) -> dict[str, str]:
    """Walk sysfs up from the tty to the USB device node for VID/PID/serial."""
    node = f"/sys/class/tty/{os.path.basename(port)}/device"
    out: dict[str, str] = {}
    for _ in range(6):
        node = os.path.dirname(os.path.realpath(node))
        if os.path.exists(os.path.join(node, "idVendor")):
            for key, fname in (
                ("vid", "idVendor"), ("pid", "idProduct"),
                ("manufacturer", "manufacturer"), ("product", "product"),
                ("serial", "serial"), ("usb_version", "version"),
            ):
                try:
                    with open(os.path.join(node, fname)) as fh:
                        out[key] = fh.read().strip()
                except OSError:
                    pass
            break
    return out


def decode_status(data: bytes) -> dict[str, str]:
    """Command 30's 15-byte blob. Firmware versions may return more or
    fewer bytes, so decode defensively."""
    fields: dict[str, str] = {}
    if len(data) < 15:
        return {"note": f"short blob ({len(data)} B), not decoded"}
    fields["key presses"] = f"mask 0x{data[5]:02x}"
    fields["key releases"] = f"mask 0x{data[6]:02x}"
    fields["ATX switch"] = f"0x{data[7]:02x}"
    fields["watchdog"] = "disabled" if data[8] == 0 else f"counter {data[8]}"
    fields["contrast"] = str(data[13])
    fields["backlight"] = f"{data[14]}%"
    return fields


def probe_at(port: str, baud: int) -> dict[str, object] | None:
    try:
        with Cfa635(port, baud, timeout=0.3) as dev:
            echoed = dev.ping(b"CFAPROBE")
            if echoed != b"CFAPROBE":
                return None
            result: dict[str, object] = {"baud": baud, "version": dev.version()}
            for label, fn in (
                ("user_flash", dev.read_user_flash),
                ("status", dev.read_status),
                ("keypad", dev.read_keypad),
            ):
                try:
                    result[label] = fn()
                except (CommandError, CrcError, TimeoutError) as exc:
                    result[label] = f"<unavailable: {exc}>"
            return result
    except (TimeoutError, CrcError, CommandError):
        return None
    except serial.SerialException as exc:
        print(f"  cannot open {port} at {baud}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial device (default: autodetect)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No candidate serial port found.", file=sys.stderr)
        return 1

    print(f"Port: {port}")
    ident = usb_identity(port)
    if ident:
        print("\nUSB identity (from sysfs):")
        for key in ("vid", "pid", "manufacturer", "product", "serial", "usb_version"):
            if key in ident:
                print(f"  {key:12} {ident[key]}")

    print("\nProbing packet protocol...")
    for baud in BAUD_CANDIDATES:
        info = probe_at(port, baud)
        if info is None:
            print(f"  {baud:>6} baud: no valid response")
            continue

        print(f"  {baud:>6} baud: ping OK\n")
        version = str(info["version"])
        print("Device responds to the CFA packet protocol.")
        print(f"  Version string   {version!r}")
        # Format is 'MODEL:hHW,<p>FW' e.g. 'CFA635:h1.1,v1.6'. The firmware
        # prefix varies by model/vintage (u, v, y all appear in the wild and
        # in the datasheets), so treat anything that isn't 'h' as firmware.
        if ":" in version:
            model, _, rev = version.partition(":")
            print(f"  Model            {model}")
            for part in rev.split(","):
                part = part.strip()
                if part[:1] == "h":
                    print(f"  Hardware rev     {part[1:]}")
                elif part:
                    print(f"  Firmware rev     {part[1:]} (prefix {part[0]!r})")

        flash = info["user_flash"]
        if isinstance(flash, bytes):
            print(f"  User flash       {flash.hex(' ')}")
        else:
            print(f"  User flash       {flash}")

        status = info["status"]
        if isinstance(status, bytes):
            print(f"  Status blob      ({len(status)} B) {status.hex(' ')}")
            for label, value in decode_status(status).items():
                print(f"    {label:14} {value}")
        else:
            print(f"  Status blob      {status}")

        print(f"  Keypad           {info['keypad']}")
        return 0

    print("\nNo response at any supported baud rate.", file=sys.stderr)
    print("The port opened fine, so this is likely the wrong device, a unit in "
          "a wedged state, or non-default firmware.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
