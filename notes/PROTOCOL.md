# CFA635 packet protocol — integration reference

Everything here is verified against `docs/CFA635-TFE-KU_Data_Sheet_v2.1.pdf`
(hardware v1.1 / firmware v1.6, matching the attached unit) and, where noted,
against the hardware itself. Page references are to that PDF.

## Transport

115200 baud, 8 data bits, no parity, 1 stop bit, no flow control. The FTDI
bridge presents a virtual COM port; there is no USB-specific layer above the
serial stream. Command 33 can change the baud rate (only 19200 and 115200 are
valid) — avoid it, since a mismatch leaves the unit unreachable until it's
reset or found by rescanning both rates.

## Packet framing

```
<type><data_length><data 0..22 bytes><CRC lo><CRC hi>
```

The `type` byte is `TTcc cccc`:

| `TT` | Meaning |
|---|---|
| `00` | command, host → module |
| `01` | response, module → host |
| `10` | report, module → host, **unsolicited** |
| `11` | error, module → host |

The low 6 bits are the command code, echoed back in the response, so a
response to command 1 has type `0x41` and an error for it has type `0xC1`.

`data_length` is the payload length only — it excludes type, length, and CRC.
Maximum payload is 22 bytes, so a full packet is at most 26 bytes.

### CRC

CRC-16/CCITT, **reflected** polynomial `0x8408` (i.e. `0x1021` bit-reversed),
initial value `0xFFFF`, final one's complement, transmitted **LSB first**.
Computed over `type + data_length + data`. Bit-shift implementation:

```python
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF
```

The datasheet's Appendix B carries table-driven equivalents in C, PIC asm,
Visual Basic, Java, and Perl if a faster version is ever needed.

## The one real trap: interleaved reports

Key reporting is **enabled by default** on this unit (both masks read `0x3f`).
A `0x80` report packet can therefore land between your command and its
response. A naive "write command, read one packet" client will misread a
keypress as the answer and desynchronise.

Handle it by looping until a packet whose class is `01` *and* whose low 6 bits
match the command you sent arrives, queueing any reports seen along the way.
That's what `Cfa635.send()` does. The alternative — disabling reports with
command 23 and polling with command 24 — is simpler but loses key-release
edges between polls.

There is no packet-level sequence number or ACK beyond this, and no timeout
recovery in the protocol: if a packet is corrupted, the module simply doesn't
answer, and the host is expected to time out and retry.

## Command set

Supported on this unit unless marked otherwise. `→` shows the response payload.

| # | Command | Payload | → | Notes |
|---|---|---|---|---|
| 0 | Ping | 0-16 bytes | same bytes | best liveness check |
| 1 | Get Hardware & Firmware Version | — | 16 B ASCII | `CFA635:h1.1,v1.6` |
| 2 | Write User Flash Area | exactly 16 B | — | limited erase cycles |
| 3 | Read User Flash Area | — | 16 B | currently all `0xff` |
| 4 | Store Current State As Boot State | — | — | **writes EEPROM**, see below |
| 5 | Reboot / Reset | 3 B magic | — | also power-off/reset host via ATX |
| 6 | Clear LCD Screen | — | — | fills with spaces, homes cursor |
| 7, 8 | *deprecated* | | | use command 31 |
| 9 | Set LCD Special Character Data | 9 B | — | 8 user glyphs, indices 0-7 |
| 10 | Read 8 Bytes of LCD Memory | 1 B addr | 9 B | debug aid |
| 11 | Set LCD Cursor Position | 2 B col,row | — | col 0-19, row 0-3 |
| 12 | Set LCD Cursor Style | 1 B 0-4 | — | 0 = none |
| 13 | Set LCD Contrast | 1 B 0-255 | — | useful ~90-140, default 120 |
| 14 | Set LCD & Keypad Backlight | 1 or 2 B | — | 0-100%; 2 B = LCD, keypad |
| 16-21 | fan / temperature / DOW | | | **inert — no such hardware** |
| 22 | Send Command Directly to LCD Controller | 2 B | — | bypasses firmware; can wedge the display |
| 23 | Configure Key Reporting | 2 B masks | — | press mask, release mask |
| 24 | Read Keypad, Polled Mode | — | 3 B masks | pressed-now, pressed-since, released-since |
| 25-27 | fan control | | | inert |
| 28 | Set ATX Power Switch Functionality | 3 B | — | **pins not exposed on enclosure** |
| 29 | Enable/Disable/Reset Watchdog | 1 B | — | tied to ATX; not usable here |
| 30 | Read Reporting & Status | — | 15 B | layout in DEVICE.md |
| 31 | **Send Data to LCD** | 3-22 B | — | col, row, then 1-20 chars |
| 33 | Set Baud Rate | 1 B | — | don't; see above |
| 34 | Set GPO Pin | 2 B idx, duty | — | **the LED control** |
| 35 | Read GPIO Pin Levels | 1 B | 4 B | pins not exposed |

### Command 31 — writing text

`data[0]` = column 0-19, `data[1]` = row 0-3, `data[2..21]` = 1 to 20
characters. Text does not wrap; writing past column 19 is rejected. This is
the only sanctioned way to put text on screen — commands 7 and 8 are
deprecated leftovers.

Bytes are **CGROM codes, not ASCII.** They coincide across most of the
printable ASCII range, but not everywhere — check the CGROM chart (p. 55 of
the CFA635 datasheet) before assuming a symbol maps straight through. Eight
user-defined glyphs at indices 0-7 are available via command 9, which is how
you'd build bar graphs or custom icons.

### Command 34 — the four bicolor LEDs

The LEDs down the left edge are wired to GPO pins, each die addressed
separately. `data[0]` is the GPO index, `data[1]` is 0 (off), 1-99 (PWM duty
at ~100 Hz), or 100 (full on).

| LED | Position | Green die | Red die |
|---|---|---|---|
| 0 | top | GPO 11 | GPO 12 |
| 1 | | GPO 9 | GPO 10 |
| 2 | | GPO 7 | GPO 8 |
| 3 | bottom | GPO 5 | GPO 6 |

Indices 0-4 are reserved — don't write to them. Driving both dies of one LED
mixes to yellow/orange.

### Command 23/24 — the keypad

Six keys. Report codes arrive in a `0x80` packet's `data[0]`:

| Code | Key | | Code | Key |
|---|---|---|---|---|
| 1 | UP press | | 7 | UP release |
| 2 | DOWN press | | 8 | DOWN release |
| 3 | LEFT press | | 9 | LEFT release |
| 4 | RIGHT press | | 10 | RIGHT release |
| 5 | ENTER press | | 11 | ENTER release |
| 6 | EXIT press | | 12 | EXIT release |

Release code = press code + 6. Command 23's two mask bytes and command 24's
three result bytes use a different encoding — a bitmask where bit 0 = UP
through bit 5 = EXIT. Don't confuse the report codes with the mask bits.

The keypad is fully decoded, so any simultaneous combination is detectable.
Command 24's "since last poll" fields are cleared by reading them.

## Persistence — command 4

Contrast, backlight, LED/GPO state, cursor style, key reporting config, and
the current screen contents can all be frozen as the power-on state with
command 4. That's how you'd replace the factory welcome screen.

It writes to EEPROM, which has a finite erase-cycle budget. Call it once when
provisioning, never in a loop, and never in a retry path.

## Notes for whatever we build on this

- **Refresh cost.** A full 20x4 repaint is four command-31 packets (~26 bytes
  each) at 115200 baud — trivially fast. But repainting unchanged rows causes
  visible flicker on character LCDs, so diff against a shadow buffer and only
  send rows that changed.
- **The FTDI latency timer** is 16 ms (`/sys/bus/usb-serial/devices/ttyUSB0/latency_timer`).
  That's the floor on round-trip latency for small packets. Lowering it to
  1 ms is possible and helps if key-press response ever feels sluggish.
- **On startup**, don't assume screen state — the module retains whatever its
  boot state holds. Send command 6 first.
- **Don't leave the backlight at 100% indefinitely** if the unit runs
  unattended; LED backlights have a rated half-life (see the datasheet's
  reliability section).
- The module is powered from USB and the cable is captive, so a hard reset
  means unplugging it. Command 5 is the soft equivalent.
