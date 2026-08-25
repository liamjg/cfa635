"""Server-side markup: widgets rendered into custom glyphs.

Page lines may contain tokens that expand at render time:

    {bar:0.65:12}    horizontal bar, 12 cells wide, 6 px/cell resolution
    {vbar:0.6}       one-cell vertical fill, 8 levels
    {spark:...}      comma-separated samples -> a row of vbar cells
    {chart:...:rows=2}  multi-row fill chart (row-span; widget owns cells)
    {big:09:47}      3-row seven-segment digits (row-span; 3 cells per digit)
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


# --- big font ----------------------------------------------------------------
#
# Block digits on a 3-wide x 5-tall grid of 6 x 4 px blocks: 18 px wide, 20 px
# tall. A display cell is 6 x 8, exactly two block rows, so five block rows is
# two and a half cells — the digit ends halfway down its third row, and that
# leftover half-cell is what keeps the numerals off the date line beneath them.
#
# Folding block pairs into cells needs only three bitmaps (a full cell and its
# two halves), which leaves most of the 8-slot budget free — the colon spends
# one more on a square dot, placed in the first two rows so the pair straddles
# the digits' optical centre.

BLOCK_FULL = tuple([0x3F] * 8)
BLOCK_TOP = tuple([0x3F] * 4 + [0] * 4)
BLOCK_BOTTOM = tuple([0] * 4 + [0x3F] * 4)
COLON_DOT = tuple([0] * 4 + [0x1E] * 4)   # 4 px square, inset one column

# key -> bitmap. The keys draw the font legibly in source (see BIG_FONT).
BIG_CELLS: dict[str, tuple[int, ...] | None] = {
    " ": None,          # blank
    "#": BLOCK_FULL,    # both block rows
    "-": BLOCK_TOP,     # upper block row only
    "_": BLOCK_BOTTOM,  # lower block row only
}

# Three rows of three cells each. Derived from the 3x5 block grid above, so
# every glyph's last row is upper-half-only or empty: nothing reaches the
# bottom 4 px.
BIG_FONT: dict[str, tuple[str, str, str]] = {
    "0": ("#-#", "# #", "---"),
    "1": ("_# ", " # ", "---"),
    "2": ("--#", "#--", "---"),
    "3": ("--#", "--#", "---"),
    "4": ("# #", "--#", "  -"),
    "5": ("#--", "--#", "---"),
    "6": ("#--", "#-#", "---"),
    "7": ("--#", "  #", "  -"),
    "8": ("#-#", "#-#", "---"),
    "9": ("#-#", "--#", "---"),
}

BIG_ROWS = 3


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
    """-> (cells, spans). A span is (start_col, rows_below): pre-rendered
    cells for the rows a row-spanning widget ({chart:}, {big:}) also owns.
    A span covers only its own columns — text beside it survives."""
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

    inserted = _expand_fills(cells, fills)
    if inserted and spans:
        # fills shift everything after them right, row-spanning widgets included
        spans = [(col + sum(n for at, n in inserted if at <= col), below)
                 for col, below in spans]
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
        # the span's own-line cells are its top row; lower rows are filled
        # into the following lines by parse_frame
        spans.append((len(cells),
                      [_chart_row(values, rows, r) for r in range(1, rows)]))
        cells.extend(_chart_row(values, rows, 0))
        return True
    if name == "big":
        return _big(arg, cells, spans)
    return False


def _chart_row(values: list[float], n_rows: int, row: int) -> list[Cell]:
    """One row of a {chart:} span; row 0 is the top (the widget's own line)."""
    return [_vcell(round(max(0.0, min(1.0, v)) * 8 * n_rows)
                   - 8 * (n_rows - 1 - row))
            for v in values]


def _big(arg: str, cells: list[Cell], spans: list[tuple]) -> bool:
    """3-row seven-segment characters, 3 cells per digit.

    Unsupported characters (and ':') take a single column, which also keeps
    neighbouring digits' verticals from touching. The colon's own CGROM glyph
    lands on the middle row, dead centre of the 24 px character height, and
    costs no slot.
    """
    if not arg:
        return False
    rows: list[list[Cell]] = [[] for _ in range(BIG_ROWS)]
    previous_was_glyph = False
    for ch in arg:
        art = BIG_FONT.get(ch)
        if art is None:
            previous_was_glyph = False
            column: tuple[Cell, ...]
            if ch == ":":
                # square dots in the first two rows: their midpoint is the
                # digits' optical centre, which a CGROM colon cannot reach
                dot = Cell(glyph=COLON_DOT, fallback=0x20)
                column = (dot, Cell(glyph=COLON_DOT, fallback=ord(":")), BLANK)
            else:
                column = (BLANK, _ch(ch) if ch != " " else BLANK, BLANK)
            for row, cell in zip(rows, column):
                row.append(cell)
            continue
        if previous_was_glyph:
            for row in rows:  # neighbouring blocks would otherwise merge
                row.append(BLANK)
        previous_was_glyph = True
        # one cell of the top row carries the literal character as its
        # fallback, so an over-budget frame degrades to small text rather than
        # to noise. It has to be a cell that actually has a glyph: "1" and "4"
        # have nothing in the middle of their top row.
        literal = min((c for c, key in enumerate(art[0]) if key != " "),
                      key=lambda c: abs(c - 1), default=None)
        for r, spec in enumerate(art):
            for col, key in enumerate(spec):
                bitmap = BIG_CELLS[key]
                if bitmap is None:
                    rows[r].append(BLANK)
                else:
                    rows[r].append(Cell(
                        glyph=bitmap,
                        fallback=charmap.encode_char(ch)
                        if r == 0 and col == literal else 0x20))
    spans.append((len(cells), rows[1:]))
    cells.extend(rows[0])
    return True


def _expand_fills(cells: list[Cell], fills: list[_Fill]) -> list[tuple[int, int]]:
    """-> the (index, count) insertions made, so spans can be shifted."""
    spare = COLUMNS - len(cells)
    if spare <= 0 or not fills:
        return []
    share = spare // len(fills)
    inserted = []
    # last fill first: inserting at a later index leaves earlier ones valid
    for index, fill in enumerate(reversed(fills)):
        count = share if index < len(fills) - 1 else spare - share * (len(fills) - 1)
        if fill.rule:
            made = [Cell(glyph=RULE, fallback=ord("-"))] * count
        elif fill.char:
            made = [_ch(fill.char)] * count
        else:
            made = [BLANK] * count
        cells[fill.at:fill.at] = made
        inserted.append((fill.at, count))
    return inserted


def _scroll_line(text: str, now: float) -> list[Cell]:
    if len(text) <= COLUMNS:
        cells = [_ch(c) for c in text]
        return cells + [BLANK] * (COLUMNS - len(cells))
    looped = text + "   "
    offset = int(now * 2) % len(looped)
    window = (looped + looped)[offset:offset + COLUMNS]
    return [_ch(c) for c in window]


def parse_frame(lines: list[str], now: float) -> list[list[Cell]]:
    """4 rows x 20 Cells; row-spanning widgets overwrite the cells below."""
    padded = (list(lines) + [""] * ROWS)[:ROWS]
    rows: list[list[Cell]] = []
    pending: list[tuple[int, int, list[list[Cell]]]] = []  # row, col, below
    for r, line in enumerate(padded):
        cells, spans = parse_line(line, now)
        rows.append(cells)
        for col, below in spans:
            pending.append((r, col, below))
    for r, col, below in pending:
        for extra, span_cells in enumerate(below, start=1):
            target = r + extra
            if target >= ROWS:
                break
            for k, cell in enumerate(span_cells):
                if col + k >= COLUMNS:
                    break
                rows[target][col + k] = cell
    return rows


def frame_is_animated(lines: list[str]) -> bool:
    """True if this frame changes with time (spin/blink/scroll)."""
    return any("{spin}" in ln or "{blink:" in ln or ln.startswith("{scroll}")
               for ln in lines)
