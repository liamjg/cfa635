"""The {big:} seven-segment font: geometry, glyph budget, degradation."""

from cfa635 import markup
from cfa635.markup import MAX_GLYPHS, BIG_FONT, parse_frame, parse_line

# Reverse of markup.BIG_CELLS, for readable failures: renders a parsed frame
# back into the source art so a wrong segment shows up as a diff, not as hex.
_ART = {bitmap: key for key, bitmap in markup.BIG_CELLS.items()
        if bitmap is not None}


def art(frame, col=0, width=3, rows=3):
    out = []
    for row in frame[:rows]:
        line = ""
        for cell in row[col:col + width]:
            if cell.glyph is not None:
                line += _ART[cell.glyph]
            elif cell.char in (None, 0x20):
                line += " "
            else:
                line += chr(cell.char)
        out.append(line)
    return tuple(out)


def bitmaps(frame):
    seen = []
    for row in frame:
        for cell in row:
            if cell.glyph is not None and cell.glyph not in seen:
                seen.append(cell.glyph)
    return seen


def test_every_digit_renders_its_own_shape():
    for digit, expected in BIG_FONT.items():
        frame = parse_frame(["{big:" + digit + "}", "", "", ""], 0.0)
        assert art(frame) == expected, digit


def test_whole_alphabet_fits_the_glyph_budget():
    # the point of the 3-row geometry: all ten digits share exactly 8 bitmaps,
    # so no time of day can ever exhaust CGRAM and degrade mid-frame
    every = bitmaps(parse_frame(["{big:" + "".join(BIG_FONT) + "}"], 0.0))
    assert len(every) == MAX_GLYPHS

    for hour in range(24):
        for minute in (0, 7, 23, 38, 47, 59):
            frame = parse_frame([f"{{big:{hour:02d}:{minute:02d}}}"], 0.0)
            assert len(bitmaps(frame)) <= MAX_GLYPHS, (hour, minute)


def test_clock_geometry_and_free_colon():
    frame = parse_frame(["{big:09:47}", "", "", ""], 0.0)
    # 4 digits at 3 cells + 2 separators + 1 colon column
    assert art(frame, width=20) == (
        "[-] [-] | ! --]     ",
        "| ! --]:--]   !     ",
        "L_J __J   !   !     ",
    )
    # the colon is a CGROM character on the middle row: no slot spent, and it
    # lands dead centre of the 24 px character height
    assert frame[1][7].glyph is None
    assert frame[1][7].char == ord(":")
    assert frame[0][7].char == ord(" ")


def test_span_leaves_the_rest_of_the_rows_alone():
    # this is what puts the small seconds beside the clock
    frame = parse_frame(["{big:09:47}", " " * 17 + "23", "", "Mon 24 Aug"], 0.0)
    assert "".join(chr(c.char) for c in frame[1][15:]) == "  23 "
    assert "".join(chr(c.char) for c in frame[3][:10]) == "Mon 24 Aug"


def test_fill_shifts_the_span_with_the_cells():
    # {fill} inserts cells; the rows below have to move with them
    frame = parse_frame(["{fill}{big:8}", "", "", ""], 0.0)
    assert art(frame, col=17) == ("[-]", "[-]", "L_J")


def test_over_budget_degrades_to_small_text():
    # the digits' own alphabet fills all 8 slots, so anything sharing the frame
    # loses. Every digit keeps one cell carrying its literal, including "1" and
    # "4", whose top row has nothing in the middle to hang it on.
    cells, _ = parse_line("{big:09:47}", 0.0)
    assert "".join(chr(c.fallback) for c in cells[:15]) == " 0   9  4    7 "
    cells, _ = parse_line("{big:11:14}", 0.0)
    assert "".join(chr(c.fallback) for c in cells[:15]) == "  1   1   1 4  "


def test_unsupported_characters_render_small_and_centred():
    # a character with no big form takes a single column and renders normally
    # on the middle row, i.e. vertically centred against the digits. That is
    # what makes "{big:9 AM}"-style suffixes work.
    cells, spans = parse_line("{big:9 x}", 0.0)
    assert cells[3].char == ord(" ")                 # the space
    assert cells[4].char == ord(" ")                 # top row above the "x"
    assert spans[0][1][0][4].char == ord("x")        # middle row carries it
    assert spans[0][1][1][4].char == ord(" ")        # bottom row stays clear


def test_empty_and_malformed_render_literally():
    cells, spans = parse_line("{big:}", 0.0)
    assert spans == []
    assert "".join(chr(c.char) for c in cells).startswith("(big:)")
