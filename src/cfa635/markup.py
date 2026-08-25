"""Server-side markup: widgets rendered into custom glyphs.

Page lines may contain tokens that expand at render time:

    {bar:0.65:12}    horizontal bar, 12 cells wide, 6 px/cell resolution
    {vbar:0.6}       one-cell vertical fill, 8 levels
    {spark:...}      comma-separated samples -> a row of vbar cells
    {chart:...:rows=2}  multi-row fill chart (row-span; widget owns cells)
    {spin}           one-cell spinner (bitmap-animated by the tick loop)
    {hr}             solid rule filling the remaining width (one glyph)
    {fill} {fill:.}  expands to consume leftover space (justify / leaders)
    {scroll}         line prefix: overflowing text marquees at ~2 Hz
    {blink:TEXT}     text alternates with blanks at 1 Hz
    {up} {down} {left} {right} {check} {cross} {bell} {lock} {unlock}

`{{` renders a literal `{`. Unknown or malformed tokens render literally —
page lines are content, not code, and never fail validation.

The module is pure: `parse_frame(lines, now)` returns Cells whose animation
state derives entirely from `now`. Cells either carry a CGROM character
code or a 6x8 bitmap wanting a CGRAM slot plus an ASCII fallback for when
the frame's 8-slot budget is exhausted. Slot allocation itself lives in the
renderer (it owns the device and the LRU cache).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cfa635 import charmap
from cfa635.driver import COLUMNS, ROWS

MAX_GLYPHS = 8  # CGRAM slots per frame

_TOKEN_RE = re.compile(r"\{([a-z]+)(?::([^{}]*))?\}")


@dataclass(frozen=True)
class Cell:
    """One display cell: a ROM character, or a custom glyph with fallback."""
    char: int | None = None
    glyph: tuple[int, ...] | None = None
    fallback: int = 0x20


def _ch(ch: str) -> Cell:
    return Cell(char=charmap.encode_char(ch))


BLANK = _ch(" ")


# --- bitmaps (8 rows of 6-bit values, MSB = leftmost pixel) ------------------

def _colfill(px: int) -> tuple[int, ...]:
    """px leftmost columns of all 8 rows filled (bar partials)."""
    value = ((1 << px) - 1) << (6 - px)
    return tuple([value] * 8)


def _rowfill(rows: int) -> tuple[int, ...]:
    """bottom `rows` rows filled (vbar/spark/chart partials)."""
    return tuple([0] * (8 - rows) + [0x3F] * rows)


FULL_BLOCK = tuple([0x3F] * 8)
RULE = (0, 0, 0, 0x3F, 0x3F, 0, 0, 0)

SPIN_FRAMES = (  # | / - \
    (0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C),
    (0x01, 0x03, 0x06, 0x0C, 0x18, 0x30, 0x20, 0x00),
    (0, 0, 0, 0x3F, 0x3F, 0, 0, 0),
    (0x20, 0x30, 0x18, 0x0C, 0x06, 0x03, 0x01, 0x00),
)
SPIN_FALLBACK = "|/-\\"

# Icon bitmaps use CGRAM slots today; any the CGROM already provides can be
# remapped to zero-cost ROM codes once transcribed from Figure 11 on real
# glass (see tests/smoke.md).
ICONS: dict[str, tuple[tuple[int, ...], str]] = {
    "up":     ((0x08, 0x1C, 0x3E, 0x2A, 0x08, 0x08, 0x08, 0x00), "^"),
    "down":   ((0x08, 0x08, 0x08, 0x2A, 0x3E, 0x1C, 0x08, 0x00), "v"),
    "left":   ((0x00, 0x08, 0x10, 0x3E, 0x3E, 0x10, 0x08, 0x00), "<"),
    "right":  ((0x00, 0x04, 0x02, 0x3E, 0x3E, 0x02, 0x04, 0x00), ">"),
    "check":  ((0x00, 0x01, 0x02, 0x04, 0x28, 0x10, 0x00, 0x00), "+"),
    "cross":  ((0x00, 0x22, 0x14, 0x08, 0x14, 0x22, 0x00, 0x00), "x"),
    "bell":   ((0x08, 0x1C, 0x1C, 0x1C, 0x3E, 0x00, 0x08, 0x00), "!"),
    "lock":   ((0x1C, 0x22, 0x22, 0x3E, 0x36, 0x36, 0x3E, 0x00), "#"),
    "unlock": ((0x1C, 0x20, 0x20, 0x3E, 0x36, 0x36, 0x3E, 0x00), "'"),
}

_RAMP = " ..::-=##"  # vertical-fill fallback by filled rows (0..8)


def _vcell(rows: int) -> Cell:
    rows = max(0, min(8, rows))
    if rows == 0:
        return BLANK
    bitmap = FULL_BLOCK if rows == 8 else _rowfill(rows)
    return Cell(glyph=bitmap, fallback=ord(_RAMP[rows]))


def _floats(text: str) -> list[float] | None:
    try:
        values = [float(v) for v in text.split(",") if v.strip() != ""]
    except ValueError:
        return None
    return values or None


# --- line parsing ------------------------------------------------------------

@dataclass
class _Fill:
    at: int              # index into the cell list
    char: str | None     # leader character; None = blank
    rule: bool = False


def parse_line(text: str, now: float) -> tuple[list[Cell], list[tuple]]:
    """-> (cells, spans). spans: (start_col, n_rows, values) for {chart:}."""
    if text.startswith("{scroll}"):
        return _scroll_line(text[len("{scroll}"):], now), []

    cells: list[Cell] = []
    fills: list[_Fill] = []
    spans: list[tuple] = []
    i = 0
    while i < len(text) and len(cells) <= COLUMNS + 8:
        if text.startswith("{{", i):
            cells.append(_ch("{"))
            i += 2
            continue
        match = _TOKEN_RE.match(text, i)
        if match is None:
            cells.append(_ch(text[i]))
            i += 1
            continue
        name, arg = match.group(1), match.group(2) or ""
        handled = _token(name, arg, now, cells, fills, spans)
        if handled:
            i = match.end()
        else:
            cells.append(_ch(text[i]))  # unknown token: render literally
            i += 1

    _expand_fills(cells, fills)
    cells = cells[:COLUMNS]
    cells += [BLANK] * (COLUMNS - len(cells))
    return cells, spans


def _token(name: str, arg: str, now: float, cells: list[Cell],
           fills: list[_Fill], spans: list[tuple]) -> bool:
    if name in ICONS:
        bitmap, fallback = ICONS[name]
        cells.append(Cell(glyph=bitmap, fallback=ord(fallback)))
        return True
    if name == "spin":
        phase = int(now * 4) % 4
        cells.append(Cell(glyph=SPIN_FRAMES[phase],
                          fallback=ord(SPIN_FALLBACK[phase])))
        return True
    if name == "hr":
        fills.append(_Fill(at=len(cells), char=None, rule=True))
        return True
    if name == "fill":
        if len(arg) > 1:
            return False
        fills.append(_Fill(at=len(cells), char=arg or None))
        return True
    if name == "blink":
        visible = (now % 1.0) < 0.5
        for ch in arg:
            cells.append(_ch(ch) if visible else BLANK)
        return True
    if name == "bar":
        parts = arg.split(":")
        if len(parts) != 2:
            return False
        try:
            frac, width = float(parts[0]), int(parts[1])
        except ValueError:
            return False
        if not 1 <= width <= COLUMNS:
            return False
        frac = max(0.0, min(1.0, frac))
        pixels = round(frac * width * 6)
        for c in range(width):
            px = max(0, min(6, pixels - c * 6))
            if px == 0:
                cells.append(Cell(char=0x20, fallback=0x20))
            elif px == 6:
                cells.append(Cell(glyph=FULL_BLOCK, fallback=ord("=")))
            else:
                cells.append(Cell(glyph=_colfill(px),
                                  fallback=ord("=") if px >= 3 else 0x20))
        return True
    if name == "vbar":
        try:
            frac = float(arg)
        except ValueError:
            return False
        cells.append(_vcell(round(max(0.0, min(1.0, frac)) * 8)))
        return True
    if name == "spark":
        values = _floats(arg)
        if values is None:
            return False
        for v in values[:COLUMNS]:
            cells.append(_vcell(round(max(0.0, min(1.0, v)) * 8)))
        return True
    if name == "chart":
        parts = arg.rsplit(":", 1)
        rows = 1
        if len(parts) == 2 and parts[1].startswith("rows="):
            try:
                rows = int(parts[1][5:])
            except ValueError:
                return False
            arg = parts[0]
        values = _floats(arg)
        if values is None or not 1 <= rows <= ROWS:
            return False
        values = values[:COLUMNS]
        spans.append((len(cells), rows, values))
        # the span's own-line cells are its top row; lower rows are filled
        # into the following lines by parse_frame
        for v in values:
            level = round(max(0.0, min(1.0, v)) * 8 * rows)
            cells.append(_vcell(level - 8 * (rows - 1)))
        return True
    return False


def _expand_fills(cells: list[Cell], fills: list[_Fill]) -> None:
    spare = COLUMNS - len(cells)
    if spare <= 0 or not fills:
        return
    share = spare // len(fills)
    for index, fill in enumerate(reversed(fills)):
        count = share if index < len(fills) - 1 else spare - share * (len(fills) - 1)
        if fill.rule:
            made = [Cell(glyph=RULE, fallback=ord("-"))] * count
        elif fill.char:
            made = [_ch(fill.char)] * count
        else:
            made = [BLANK] * count
        cells[fill.at:fill.at] = made


def _scroll_line(text: str, now: float) -> list[Cell]:
    if len(text) <= COLUMNS:
        cells = [_ch(c) for c in text]
        return cells + [BLANK] * (COLUMNS - len(cells))
    looped = text + "   "
    offset = int(now * 2) % len(looped)
    window = (looped + looped)[offset:offset + COLUMNS]
    return [_ch(c) for c in window]


def parse_frame(lines: list[str], now: float) -> list[list[Cell]]:
    """4 rows x 20 Cells; {chart:rows=N} spans overwrite the rows below."""
    padded = (list(lines) + [""] * ROWS)[:ROWS]
    rows: list[list[Cell]] = []
    pending: list[tuple[int, int, int, list[float]]] = []  # row, col, n, values
    for r, line in enumerate(padded):
        cells, spans = parse_line(line, now)
        rows.append(cells)
        for col, n_rows, values in spans:
            pending.append((r, col, n_rows, values))
    for r, col, n_rows, values in pending:
        for extra in range(1, n_rows):
            target = r + extra
            if target >= ROWS:
                break
            for k, v in enumerate(values):
                if col + k >= COLUMNS:
                    break
                level = round(max(0.0, min(1.0, v)) * 8 * n_rows)
                rows[target][col + k] = _vcell(level - 8 * (n_rows - 1 - extra))
    return rows


def frame_is_animated(lines: list[str]) -> bool:
    """True if this frame changes with time (spin/blink/scroll)."""
    return any("{spin}" in ln or "{blink:" in ln or ln.startswith("{scroll}")
               for ln in lines)
