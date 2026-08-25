"""Minimal CFA635 / XES635BK packet-protocol driver.

The CFA635 speaks a framed, CRC-checked packet protocol over a plain serial
port (an FTDI USB-serial bridge on the -KU variants). Every exchange is:

    host  -> module:  command packet
    module -> host:   response packet (same command code, type bits = 01)

The module may also emit *unsolicited* report packets (key presses, etc.)
at any time, so a reader must be prepared to skip reports while waiting for
the response it asked for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial

# --- Framing ---------------------------------------------------------------
#
#   +------+-------------+------------------+-----------+
#   | type | data_length | data (0..22 B)   | CRC (2 B) |
#   +------+-------------+------------------+-----------+
#     1 B       1 B                           LSB first
#
# type byte:  bits 7..6 = packet class, bits 5..0 = command code
MAX_DATA_LENGTH = 22

TYPE_COMMAND = 0x00  # host -> module
TYPE_RESPONSE = 0x40  # module -> host, answers a command
TYPE_REPORT = 0x80  # module -> host, unsolicited
TYPE_ERROR = 0xC0  # module -> host, command rejected

# Report codes arrive as (TYPE_REPORT | code).
REPORT_KEY_ACTIVITY = 0x80
REPORT_FAN_SPEED = 0x81  # not supported on the 635
REPORT_TEMPERATURE = 0x82  # not supported on the 635

# Geometry of the 20x4 character LCD.
COLUMNS = 20
ROWS = 4

# Keypad. Reports carry one of these in data[0]; releases are press + 6.
KEY_UP_PRESS = 1
KEY_DOWN_PRESS = 2
KEY_LEFT_PRESS = 3
KEY_RIGHT_PRESS = 4
KEY_ENTER_PRESS = 5
KEY_EXIT_PRESS = 6
KEY_UP_RELEASE = 7
KEY_DOWN_RELEASE = 8
KEY_LEFT_RELEASE = 9
KEY_RIGHT_RELEASE = 10
KEY_ENTER_RELEASE = 11
KEY_EXIT_RELEASE = 12

KEY_NAMES = {
    1: "UP_PRESS", 2: "DOWN_PRESS", 3: "LEFT_PRESS", 4: "RIGHT_PRESS",
    5: "ENTER_PRESS", 6: "EXIT_PRESS",
    7: "UP_RELEASE", 8: "DOWN_RELEASE", 9: "LEFT_RELEASE",
    10: "RIGHT_RELEASE", 11: "ENTER_RELEASE", 12: "EXIT_RELEASE",
}

# Bitmask positions used by command 23 (Configure Key Reporting) and by
# command 24's polled bitmasks. Bit 0 = UP ... bit 5 = EXIT.
KEY_MASK_UP = 0x01
KEY_MASK_DOWN = 0x02
KEY_MASK_LEFT = 0x04
KEY_MASK_RIGHT = 0x08
KEY_MASK_ENTER = 0x10
KEY_MASK_EXIT = 0x20
KEY_MASK_ALL = 0x3F

# The four bicolor indicator LEDs down the left edge are wired to GPO pins.
# LED 0 is the top one. Each die is driven independently, so mixing the two
# gives yellow/orange.
LED_GPO = {
    0: {"green": 11, "red": 12},
    1: {"green": 9, "red": 10},
    2: {"green": 7, "red": 8},
    3: {"green": 5, "red": 6},
}


def crc16(data: bytes) -> int:
    """CRC-16/CCITT as used by Crystalfontz: reflected poly 0x8408,
    init 0xFFFF, final complement. Transmitted LSB first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


@dataclass
class Packet:
    type: int
    data: bytes

    @property
    def command(self) -> int:
        return self.type & 0x3F

    @property
    def packet_class(self) -> int:
        return self.type & 0xC0

    def encode(self) -> bytes:
        if len(self.data) > MAX_DATA_LENGTH:
            raise ValueError(f"data too long: {len(self.data)} > {MAX_DATA_LENGTH}")
        body = bytes([self.type, len(self.data)]) + self.data
        return body + crc16(body).to_bytes(2, "little")

    def __repr__(self) -> str:
        names = {
            TYPE_COMMAND: "CMD",
            TYPE_RESPONSE: "RSP",
            TYPE_REPORT: "RPT",
            TYPE_ERROR: "ERR",
        }
        cls = names.get(self.packet_class, "???")
        return f"<{cls} cmd={self.command} data={self.data.hex(' ') or '-'}>"


class CrcError(Exception):
    pass


class CommandError(Exception):
    """Module answered with an error packet (type bits = 11)."""


class Cfa635:
    """Blocking driver. Use as a context manager."""

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200,
                 timeout: float = 0.5, *, ser=None):
        self.ser = ser if ser is not None else serial.Serial(port, baudrate, timeout=timeout)
        self.reports: list[Packet] = []

    def __enter__(self) -> "Cfa635":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.ser.close()

    # --- transport ---------------------------------------------------------

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n:
            if time.monotonic() > deadline:
                raise TimeoutError(f"wanted {n} bytes, got {len(buf)}")
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return buf

    def _read_packet(self, deadline: float) -> Packet:
        header = self._read_exact(2, deadline)
        ptype, length = header
        if length > MAX_DATA_LENGTH:
            raise CrcError(f"implausible data_length {length}")
        data = self._read_exact(length, deadline) if length else b""
        crc_bytes = self._read_exact(2, deadline)
        expected = int.from_bytes(crc_bytes, "little")
        actual = crc16(header + data)
        if expected != actual:
            raise CrcError(f"CRC {expected:#06x} != computed {actual:#06x}")
        return Packet(ptype, data)

    def _drain_buffered(self) -> None:
        """Consume every complete packet already sitting in the OS buffer.

        Reports are stashed in .reports; anything else (a stale response from
        a timed-out command) is dropped. Never blocks longer than it takes to
        finish a packet whose first byte has already arrived.
        """
        while self.ser.in_waiting:
            try:
                pkt = self._read_packet(time.monotonic() + 0.05)
            except (TimeoutError, CrcError):
                break
            if pkt.packet_class == TYPE_REPORT:
                self.reports.append(pkt)

    def send(self, command: int, data: bytes = b"",
             timeout: float = 0.35, retries: int = 2) -> bytes:
        """Send a command and return the response payload.

        The datasheet guarantees a response within 250 ms and prescribes
        retry-then-error ("About Handshaking"); the default timeout adds
        headroom for OS serial latency, and a lost exchange is retried
        rather than stalling. A CommandError (explicit rejection) is not
        retried. Unsolicited reports received while waiting are stashed in
        .reports rather than being mistaken for the answer.
        """
        for attempt in range(retries + 1):
            self._drain_buffered()
            self.ser.write(Packet(TYPE_COMMAND | command, data).encode())
            self.ser.flush()

            deadline = time.monotonic() + timeout
            try:
                while True:
                    pkt = self._read_packet(deadline)
                    if pkt.packet_class == TYPE_REPORT:
                        self.reports.append(pkt)
                        continue
                    if pkt.packet_class == TYPE_ERROR and pkt.command == command:
                        raise CommandError(
                            f"module rejected command {command}: {pkt}")
                    if pkt.packet_class == TYPE_RESPONSE and pkt.command == command:
                        return pkt.data
                    # Response to something else (stale) -- keep waiting.
            except (TimeoutError, CrcError):
                if attempt == retries:
                    raise
        raise AssertionError("unreachable")

    # --- commands (read-only subset; safe to run against a live unit) ------

    def ping(self, payload: bytes = b"probe") -> bytes:
        """Command 0. Module echoes the payload back verbatim."""
        return self.send(0, payload)

    def version(self) -> str:
        """Command 1. e.g. 'CFA635:h1.4,u1.9' -> hardware h1.4, firmware u1.9."""
        return self.send(1).decode("ascii", "replace")

    def read_user_flash(self) -> bytes:
        """Command 3. 16 bytes of host-usable non-volatile scratch space."""
        return self.send(3)

    def store_boot_state(self) -> None:
        """Command 4. Save the current display contents, special characters,
        cursor, contrast, backlight, key masks and LED states as the
        power-on state. One-time deploy nicety (e.g. a boot screen); flash
        has limited write cycles, so never call this routinely."""
        self.send(4)

    def write_user_flash(self, data: bytes) -> None:
        """Command 2. Write up to 16 bytes of non-volatile scratch space.
        Flash has limited write cycles -- write on settled changes, not
        per keystroke."""
        if len(data) > 16:
            raise ValueError("user flash is 16 bytes")
        self.send(2, data)

    def read_status(self) -> bytes:
        """Command 30. 15-byte reporting & status blob. Bytes 0-4 are fan and
        temperature reporting masks (always 0 on the 635 -- no such hardware);
        [5]/[6] are key press/release report masks, [7] ATX, [8] watchdog
        counter, [9-12] fan glitch delays, [13] contrast, [14] backlight."""
        return self.send(30)

    def read_keypad(self) -> dict[str, int]:
        """Command 24. Polled keypad state, independent of key reporting.
        All three values are KEY_MASK_* bitmaps. The 'since last poll' fields
        are cleared by the act of reading them."""
        data = self.send(24)
        return {
            "currently_pressed": data[0],
            "pressed_since_last_poll": data[1],
            "released_since_last_poll": data[2],
        }

    # --- commands that change device state --------------------------------

    def clear(self) -> None:
        """Command 6. Fills the screen with spaces and homes the cursor."""
        self.send(6)

    def write_text(self, col: int, row: int, text: str | bytes) -> None:
        """Command 31. Place up to 20 characters at (col, row).

        Bytes are CGROM codes, not ASCII -- they agree for most of the
        printable range but diverge for some symbols. See the CGROM chart in
        the datasheet before assuming a character maps straight through.
        """
        if not 0 <= col < COLUMNS or not 0 <= row < ROWS:
            raise ValueError(f"({col}, {row}) is off-screen")
        payload = text.encode("ascii", "replace") if isinstance(text, str) else text
        if col + len(payload) > COLUMNS:
            raise ValueError(f"text runs {col + len(payload) - COLUMNS} chars past the right edge")
        self.send(31, bytes([col, row]) + payload)

    def set_special_char(self, index: int, bitmap: bytes) -> None:
        """Command 9. Program CGRAM special character `index` (0-7) with an
        8-row bitmap; each row is a 6-bit value, MSB = leftmost pixel. The
        glyph displays at data codes 0x00-0x07, and redefining it instantly
        repaints every on-screen cell that references it."""
        if not 0 <= index <= 7:
            raise ValueError("special character index must be 0-7")
        if len(bitmap) != 8 or any(b > 0x3F for b in bitmap):
            raise ValueError("bitmap must be 8 rows of 6-bit values")
        self.send(9, bytes([index]) + bytes(bitmap))

    def set_cursor_position(self, col: int, row: int) -> None:
        """Command 11. Move the hardware cursor (visible per cursor style)."""
        if not 0 <= col < COLUMNS or not 0 <= row < ROWS:
            raise ValueError(f"({col}, {row}) is off-screen")
        self.send(11, bytes([col, row]))

    def set_cursor_style(self, style: int) -> None:
        """Command 12. 0=none 1=blinking block 2=underscore
        3=block+underscore 4=inverting blinking block."""
        if not 0 <= style <= 4:
            raise ValueError("cursor style must be 0-4")
        self.send(12, bytes([style]))

    def set_contrast(self, value: int) -> None:
        """Command 13. 0-255; the useful window is roughly 90-140 and the
        factory default is 120."""
        if not 0 <= value <= 255:
            raise ValueError("contrast must be 0-255")
        self.send(13, bytes([value]))

    def set_backlight(self, lcd_percent: int, keypad_percent: int | None = None) -> None:
        """Command 14. LCD and keypad backlights, 0-100%. Sending one byte
        sets both together; two bytes control them separately.

        Firmware v1.6 (this unit) rejects the two-byte form with an error
        packet -- only pass keypad_percent on firmware known to accept it."""
        for pct in (lcd_percent, keypad_percent):
            if pct is not None and not 0 <= pct <= 100:
                raise ValueError("backlight must be 0-100")
        payload = bytes([lcd_percent])
        if keypad_percent is not None:
            payload += bytes([keypad_percent])
        self.send(14, payload)

    def set_led(self, led: int, green: int = 0, red: int = 0) -> None:
        """Command 34, applied to both dies of one bicolor LED.

        led is 0 (top) to 3 (bottom); green/red are 0-100 duty cycle. The
        module PWMs at ~100 Hz, so intermediate values mix to yellow/orange.
        """
        if led not in LED_GPO:
            raise ValueError("led must be 0-3")
        for pct in (green, red):
            if not 0 <= pct <= 100:
                raise ValueError("brightness must be 0-100")
        self.send(34, bytes([LED_GPO[led]["green"], green]))
        self.send(34, bytes([LED_GPO[led]["red"], red]))

    def configure_key_reporting(self, press_mask: int = KEY_MASK_ALL,
                                release_mask: int = KEY_MASK_ALL) -> None:
        """Command 23. Choose which keys emit unsolicited 0x80 reports.
        Both masks default to all six keys, which is the factory setting."""
        self.send(23, bytes([press_mask & 0x3F, release_mask & 0x3F]))

    # --- unsolicited reports ----------------------------------------------

    def poll_reports(self, timeout: float = 0.1) -> list[Packet]:
        """Drain any report packets the module has sent since the last call.

        Reports also accumulate in .reports as a side effect of send().
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.ser.in_waiting:
                time.sleep(0.005)
                continue
            try:
                pkt = self._read_packet(deadline)
            except (TimeoutError, CrcError):
                break
            if pkt.packet_class == TYPE_REPORT:
                self.reports.append(pkt)
        drained, self.reports = self.reports, []
        return drained

    def drain_reports_nonblocking(self) -> list[Packet]:
        """Like poll_reports() but returns as soon as the OS buffer is empty.

        Suitable for a tight worker loop that provides its own pacing.
        """
        self._drain_buffered()
        drained, self.reports = self.reports, []
        return drained

    def key_events(self, timeout: float = 0.1) -> list[str]:
        """poll_reports() filtered down to named key events."""
        return [
            KEY_NAMES.get(pkt.data[0], f"UNKNOWN_{pkt.data[0]}")
            for pkt in self.poll_reports(timeout)
            if pkt.command == 0x00 and pkt.data  # 0x80 & 0x3F == 0
        ]
