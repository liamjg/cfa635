"""The server's own on-device UI: page switcher + settings.

Opened with EXIT from browse mode; the highest input layer while open
(every key is consumed here, none reach page owners or global nav). Pure
logic over an injected clock — rendering returns plain 4x20 lines, and
key handling returns effect tuples for the server to apply:

    ("select", index)                 switcher: ENTER on a row
    ("close",)                        shell should close
    ("adjust", "backlight"|"contrast", -1 | +1)

The switcher lists pages with badges (P pinned, ! alert, * visible) and a
trailing "Settings" row. Settings adjusts backlight/contrast live; values
persist to the device's user flash when the shell closes.
"""

from __future__ import annotations

import time

COLS = 20
VISIBLE_ROWS = 3  # list rows under the title


def _pad(text: str, width: int = COLS) -> str:
    return text[:width].ljust(width)


def _title(left: str, right: str) -> str:
    gap = COLS - len(left) - len(right)
    return _pad(left + " " * max(1, gap) + right)


def switcher_lines(entries: list[tuple[str, str]], cursor: int) -> list[str]:
    """entries: (label, badge) including the trailing Settings row."""
    total = len(entries)
    start = min(max(0, cursor - 1), max(0, total - VISIBLE_ROWS))
    lines = [_title("PAGES", f"{cursor + 1}/{total}")]
    for i in range(start, min(start + VISIBLE_ROWS, total)):
        label, badge = entries[i]
        marker = ">" if i == cursor else " "
        body = f"{marker} {label}"
        if badge:
            gap = COLS - len(body) - len(badge)
            body = body[:COLS - len(badge) - 1] + " " * max(1, gap) + badge
        lines.append(_pad(body))
    while len(lines) < 4:
        lines.append(_pad(""))
    return lines


def settings_lines(backlight: int, contrast: int, row: int) -> list[str]:
    def item(idx: int, label: str, value: int) -> str:
        marker = ">" if row == idx else " "
        return _title(f"{marker} {label}", str(value))

    return [
        _pad("SETTINGS"),
        "{hr}",  # markup: solid rule, one glyph slot
        item(0, "Backlight", backlight),
        item(1, "Contrast", contrast),
    ]


class Shell:
    def __init__(self, *, clock=time.monotonic, timeout: float = 30.0):
        self.clock = clock
        self.timeout = timeout
        self.mode: str = "closed"  # closed | switcher | settings
        self.cursor = 0
        self.settings_row = 0
        self.dirty = False  # settings changed since open -> persist on close
        self._last = 0.0

    @property
    def is_open(self) -> bool:
        return self.mode != "closed"

    def open(self) -> None:
        self.mode = "switcher"
        self.cursor = 0
        self.dirty = False
        self._touch()

    def close(self) -> None:
        self.mode = "closed"

    def enter_settings(self) -> None:
        self.mode = "settings"
        self.settings_row = 0
        self._touch()

    def expired(self) -> bool:
        return self.is_open and self.clock() - self._last > self.timeout

    def _touch(self) -> None:
        self._last = self.clock()

    def on_key(self, key: str, n_items: int) -> tuple | None:
        """Handle a key press; returns an effect for the server, or None."""
        self._touch()
        if self.mode == "switcher":
            if n_items == 0:
                if key == "exit":
                    return ("close",)
                return None
            self.cursor = min(self.cursor, n_items - 1)
            if key == "up":
                self.cursor = (self.cursor - 1) % n_items
            elif key == "down":
                self.cursor = (self.cursor + 1) % n_items
            elif key == "enter":
                return ("select", self.cursor)
            elif key == "exit":
                return ("close",)
        elif self.mode == "settings":
            if key in ("up", "down"):
                self.settings_row = 1 - self.settings_row
            elif key in ("left", "right"):
                field = "backlight" if self.settings_row == 0 else "contrast"
                return ("adjust", field, -1 if key == "left" else +1)
            elif key == "exit":
                self.mode = "switcher"
        return None


# --- user-flash persistence for the shell's settings -------------------------
#
# 16-byte record in the device's scratch flash: survives daemon restarts and
# host swaps without any filesystem state.

FLASH_MAGIC = b"CF35"
FLASH_VERSION = 1


def encode_settings(backlight: int, contrast: int) -> bytes:
    body = FLASH_MAGIC + bytes([FLASH_VERSION, backlight & 0xFF, contrast & 0xFF])
    checksum = sum(body) & 0xFF
    return (body + bytes([checksum])).ljust(16, b"\xff")


def decode_settings(record: bytes) -> tuple[int, int] | None:
    """(backlight, contrast) from a flash record, or None if invalid."""
    if len(record) < 8 or record[:4] != FLASH_MAGIC or record[4] != FLASH_VERSION:
        return None
    if record[7] != sum(record[:7]) & 0xFF:
        return None
    backlight, contrast = record[5], record[6]
    if backlight > 100:
        return None
    return backlight, contrast
