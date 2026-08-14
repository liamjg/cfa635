"""A protocol-speaking stand-in for pyserial's Serial, for hardware-free tests.

Implements just the surface the driver touches (read/write/flush/in_waiting/
close/timeout) and models enough of a CFA635 to be useful: the 4x20 screen,
contrast/backlight, GPO duty cycles, and key-report masks. Key reports can be
injected either between exchanges (press_key) or *between a command and its
response* (press_key_mid_command) to exercise the interleaving path.
"""

from __future__ import annotations

from cfa635.driver import (
    COLUMNS,
    LED_GPO,
    MAX_DATA_LENGTH,
    ROWS,
    TYPE_ERROR,
    TYPE_REPORT,
    TYPE_RESPONSE,
    crc16,
)


def frame(ptype: int, data: bytes = b"") -> bytes:
    body = bytes([ptype, len(data)]) + data
    return body + crc16(body).to_bytes(2, "little")


def key_report(code: int) -> bytes:
    return frame(TYPE_REPORT | 0x00, bytes([code]))


class FakeSerial:
    def __init__(self):
        self.timeout = 0.5
        self.is_open = True
        self._rx = bytearray()  # bytes waiting to be read by the host
        self._parse = bytearray()  # partial command bytes from the host
        self.commands: list[tuple[int, bytes]] = []
        self.screen = [bytearray(b" " * COLUMNS) for _ in range(ROWS)]
        self.gpos: dict[int, int] = {}
        self.contrast = 120
        self.backlight = (100, 100)
        self.press_mask = 0x3F
        self.release_mask = 0x3F
        self._mid_command_keys: list[int] = []

    # --- pyserial surface ---------------------------------------------------

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, n: int) -> bytes:
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, data: bytes) -> int:
        self._parse += data
        while len(self._parse) >= 2:
            length = self._parse[1]
            total = 2 + length + 2
            if length > MAX_DATA_LENGTH or len(self._parse) < total:
                break
            pkt, self._parse = self._parse[:total], self._parse[total:]
            body, crc = bytes(pkt[:-2]), int.from_bytes(pkt[-2:], "little")
            assert crc == crc16(body), "host sent a bad CRC"
            self._handle(body[0] & 0x3F, body[2:])
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self.is_open = False

    # --- device model -------------------------------------------------------

    def _handle(self, command: int, data: bytes) -> None:
        self.commands.append((command, data))
        # Reports queued to arrive after the command but before its response.
        while self._mid_command_keys:
            self._rx += key_report(self._mid_command_keys.pop(0))

        payload = b""
        if command == 0:  # ping
            payload = data
        elif command == 1:  # version
            payload = b"CFA635:h1.1,v1.6"
        elif command == 3:  # user flash
            payload = b"\xff" * 16
        elif command == 6:  # clear
            self.screen = [bytearray(b" " * COLUMNS) for _ in range(ROWS)]
        elif command == 13:
            self.contrast = data[0]
        elif command == 14:
            if len(data) != 1:  # firmware v1.6 rejects the two-byte form
                self._rx += frame(TYPE_ERROR | command)
                return
            self.backlight = (data[0], data[0])
        elif command == 23:
            self.press_mask, self.release_mask = data[0], data[1]
        elif command == 24:  # keypad poll
            payload = b"\x00\x00\x00"
        elif command == 30:  # status blob
            payload = bytes(
                [0, 0, 0, 0, 0, self.press_mask, self.release_mask, 0, 0,
                 1, 1, 1, 1, self.contrast, self.backlight[0]]
            )
        elif command == 31:  # write text
            col, row, text = data[0], data[1], data[2:]
            assert col + len(text) <= COLUMNS and row < ROWS
            self.screen[row][col:col + len(text)] = text
        elif command == 34:  # GPO duty
            self.gpos[data[0]] = data[1]
        self._rx += frame(TYPE_RESPONSE | command, payload)

    # --- test hooks ---------------------------------------------------------

    def press_key(self, code: int) -> None:
        """Inject a key report as if it arrived between exchanges."""
        self._rx += key_report(code)

    def press_key_mid_command(self, code: int) -> None:
        """Inject a key report between the *next* command and its response."""
        self._mid_command_keys.append(code)

    def rows(self) -> list[str]:
        return [row.decode("latin-1") for row in self.screen]

    def led(self, n: int) -> tuple[int, int]:
        """(green, red) duty for LED n, defaulting unset GPOs to 0."""
        pins = LED_GPO[n]
        return self.gpos.get(pins["green"], 0), self.gpos.get(pins["red"], 0)
