# Device identification

Probed 2026-08-13 on `/dev/ttyUSB0`.

## Raw probe output

```
Port: /dev/ttyUSB0

USB identity (from sysfs):
  vid          0403
  pid          fc0d
  manufacturer Crystalfontz
  product      Crystalfontz CFA635-USB LCD
  serial       CF124841
  usb_version  2.00

Probing packet protocol...
  115200 baud: ping OK

Device responds to the CFA packet protocol.
  Version string   'CFA635:h1.1,v1.6'
  Model            CFA635
  Hardware rev     1.1
  User flash       ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff
  Status blob      (15 B) 00 00 00 00 00 3f 3f 00 00 01 01 01 01 78 64
  Keypad           {'currently_pressed': 0, 'pressed_since_last_poll': 0,
                    'released_since_last_poll': 0}
```

The unit answered a Ping (command 0) with a byte-exact echo and a correct
CRC on the first attempt at 115200 baud, so the framing, CRC polynomial, and
baud rate in `src/cfa635.py` are all confirmed against real hardware rather
than inferred from the datasheet.

## Status blob decoded

`00 00 00 00 00 3f 3f 00 00 01 01 01 01 78 64` against command 30's layout:

| Byte | Value | Meaning |
|---|---|---|
| 0 | `00` | fan 1-4 reporting — no fan hardware on the 635 |
| 1-4 | `00` | temperature reporting — no DOW sensors on the 635 |
| 5 | `3f` | key **press** reporting enabled, all 6 keys |
| 6 | `3f` | key **release** reporting enabled, all 6 keys |
| 7 | `00` | ATX power switch functionality off |
| 8 | `00` | watchdog disabled |
| 9-12 | `01` | fan RPM glitch delays — inert on this model |
| 13 | `78` | contrast = 120 (factory default) |
| 14 | `64` | backlight = 100% |

Both key report masks being `0x3f` matters for integration: **the module is
already emitting unsolicited key reports**. Any read loop must tolerate a
`0x80` report packet arriving in the middle of a command/response exchange.
`Cfa635.send()` handles this by stashing reports in `.reports` and continuing
to wait for the response it asked for.

User flash reading all `0xff` means the 16-byte scratch area has never been
written — it's free for use.

## Which datasheet is authoritative

Four documents are in `docs/`. They disagree, so the ordering matters:

1. **`CFA635-TFE-KU_Data_Sheet_v2.1.pdf`** — masthead reads *Hardware v1.1 /
   Firmware v1.6*, an exact match for the probed `h1.1,v1.6`. **Use this one.**
2. `XES635BK-TFE-KU_Data_Sheet_v2.0.pdf` — the enclosure product. Correct for
   mechanical dimensions, cable, and part-number decoding; its command
   reference is a restricted subset (see below).
3. `XES635BK-xxx-KU.pdf` — 2019 revision covering the TFK/TML/YYK variants at
   hardware v1.5 / firmware u2.6. Newer than this unit; useful only as a
   cross-check on how the protocol evolved.
4. `CFA_635_1_0.pdf` — the original hardware v1.0 / firmware v1.0 datasheet.
   Historical; predates this unit.

Plain-text extractions of all four are in `docs/text/` for grepping.

## Datasheet discrepancies found while verifying

These are real conflicts between the docs and the hardware. They cost time if
rediscovered later:

- **Version string prefix.** The XES635BK datasheet documents the reply as
  `"XES635BK:hX.X,yY.Y"`. The actual device returns `CFA635:h1.1,v1.6` — model
  prefix `CFA635`, not `XES635BK`, and firmware prefix `v`, not `y` (the
  CFA635 datasheet in turn shows `u`). The enclosed product runs stock
  bare-module firmware and identifies as the module. Parse this defensively:
  split on `:`, then treat the `h`-prefixed field as hardware and whatever
  else appears as firmware.
- **Hardware revision.** The XES635BK datasheet masthead says hardware v3.1;
  the module reports h1.1. Those number different things — v3.1 is the
  enclosed-product revision, h1.1 is the LCD module inside it. Don't try to
  reconcile them.
- **Command availability.** The XES635BK datasheet marks commands 16-21, 25-29
  and 35 "Not Supported", while the CFA635 datasheet documents 28 (ATX power
  switch), 29 (watchdog) and 35 (read GPIO) in full. Since this unit runs
  CFA635 firmware, those commands will likely answer — but the ATX and GPIO
  pins are not broken out through the steel enclosure, so **treat them as
  unavailable regardless of what the firmware accepts**. Command 34 is
  genuinely useful, but only for GPO indices 5-12, which drive the LEDs.
- **Fan and temperature commands** (16-21, 25-27) are inert on all 635
  variants; that hardware exists on the CFA633, from which the 635 protocol
  is derived. Ignore them.
