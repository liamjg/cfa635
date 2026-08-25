"""Markup widgets, CGRAM slot allocation, span-diff writes, cursor."""

from cfa635 import markup
from cfa635.markup import FULL_BLOCK, Cell, parse_frame, parse_line
from tests.test_api import auth, make_client, wait_for


# --- pure markup parsing -----------------------------------------------------


def cells_text(cells):
    return "".join(chr(c.char) if c.char is not None else "\x00" for c in cells)


def test_plain_text_passthrough():
    cells, spans = parse_line("hello world", 0.0)
    assert cells_text(cells) == "hello world".ljust(20)
    assert spans == []


def test_bar_slots_and_fallbacks():
    cells, _ = parse_line("{bar:0.5:10}", 0.0)
    glyphs = [c.glyph for c in cells[:10]]
    assert glyphs[:5] == [FULL_BLOCK] * 5      # 30 of 60 px -> 5 full cells
    assert all(g is None for g in glyphs[5:])  # rest empty
    # 0.575 * 10 cells * 6 px = 34.5 -> rounds to 34: 5 full + a 4-px partial
    cells, _ = parse_line("{bar:0.575:10}", 0.0)
    partial = cells[5].glyph
    assert partial is not None and partial != FULL_BLOCK
    assert partial[0] == 0b111100  # 4 leftmost pixels of the 6


def test_fill_right_justifies_and_dot_leaders():
    cells, _ = parse_line("CPU{fill}42%", 0.0)
    assert cells_text(cells) == "CPU" + " " * 14 + "42%"
    cells, _ = parse_line("T{fill:.}9", 0.0)
    assert cells_text(cells) == "T" + "." * 18 + "9"


def test_hr_is_one_bitmap_across_width():
    cells, _ = parse_line("ab{hr}", 0.0)
    rules = {c.glyph for c in cells[2:]}
    assert len(rules) == 1  # one distinct bitmap regardless of width


def test_spark_and_vbar_share_row_fill_family():
    cells, _ = parse_line("{vbar:0.5}{spark:0.5,1.0}", 0.0)
    assert cells[0].glyph == cells[1].glyph  # same level -> same bitmap
    assert cells[2].glyph == FULL_BLOCK


def test_chart_spans_rows_and_owns_covered_cells():
    frame = parse_frame(["{chart:0.0,0.5,1.0:rows=2}", "OVERWRITTEN", "", ""],
                        0.0)
    # sample 1.0: full in both rows; 0.5: empty top, full bottom; 0.0: blank
    assert frame[0][2].glyph == FULL_BLOCK
    assert frame[1][2].glyph == FULL_BLOCK
    assert frame[0][1].glyph is None
    assert frame[1][1].glyph == FULL_BLOCK
    assert frame[1][0].glyph is None
    # covered cells lost their text; beyond the span the text survives
    assert frame[1][3].char == ord("R")


def test_blink_and_spin_derive_from_clock():
    on, _ = parse_line("{blink:HI}", 0.2)
    off, _ = parse_line("{blink:HI}", 0.7)
    assert cells_text(on).startswith("HI")
    assert cells_text(off).startswith("  ")
    a, _ = parse_line("{spin}", 0.0)
    b, _ = parse_line("{spin}", 0.25)
    assert a[0].glyph != b[0].glyph


def test_scroll_marquee_advances():
    text = "{scroll}" + "abcdefghijklmnopqrstuvwxyz"
    frame0 = cells_text(parse_line(text, 0.0)[0])
    frame1 = cells_text(parse_line(text, 0.5)[0])
    assert frame0 == "abcdefghijklmnopqrst"
    assert frame1 == "bcdefghijklmnopqrstu"


def test_escapes_and_unknown_tokens_render_literally():
    # literal braces reach the glass as the CGROM's closest glyphs (parens);
    # the point is that nothing errors and nothing vanishes
    cells, _ = parse_line("{{x}} {nope} {bar:zz:5}", 0.0)
    assert cells_text(cells).startswith("(x)) (nope) (bar:zz:"[:20])


# --- end-to-end: glyph programming, budget, span-diff, cursor ---------------


def test_bar_programs_glyphs_and_renders_codes():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/meter", json={"lines": ["{bar:0.575:10}"]}, headers=h)
        # full block + 4px partial programmed into CGRAM
        assert wait_for(lambda: list(FULL_BLOCK) in fake.cgram)
        assert wait_for(lambda: [0b111100] * 8 in fake.cgram)
        # screen row references slot codes 0-7 for the first 6 cells
        assert wait_for(lambda: all(b < 8 for b in fake.screen[0][:6]))


def test_over_budget_falls_back_to_ascii():
    client, fake = make_client()
    with client:
        h = auth(client)
        # 5 distinct bar partials + full + 7 distinct spark levels + icons
        # cannot fit in 8 slots; later widgets degrade to ASCII, no error
        client.put("/pages/busy", json={"lines": [
            "{bar:0.99:14}",
            "{spark:0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.9}",
            "{up}{down}{check}{cross}{bell}{lock}",
        ]}, headers=h)
        assert wait_for(lambda: bytes(fake.screen[0]).rstrip() != b"")
        def ascii_fallback_present():
            rows = fake.rows()
            return any(ch in rows[1] + rows[2] for ch in ".:-=#^v+x!")
        assert wait_for(ascii_fallback_present)
        assert client.get("/pages/busy").status_code == 200


def test_span_diff_writes_only_changed_cells():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/p", json={"lines": ["abcdefghij"]}, headers=h)
        assert wait_for(lambda: "abcdefghij" in fake.rows()[0])
        fake.commands.clear()
        client.patch("/pages/p", json={"lines": ["abcXefghij"]}, headers=h)
        assert wait_for(lambda: "abcXefghij" in fake.rows()[0])
        writes = [(c, d) for c, d in fake.commands if c == 31]
        assert any(d[0] == 3 and d[2:] == b"X" for c, d in writes), writes


def test_cursor_field_drives_hardware_cursor():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/pin-entry", json={
            "lines": ["PIN: ____"],
            "cursor": {"row": 0, "col": 5, "style": "invert"},
        }, headers=h)
        assert wait_for(lambda: fake.cursor == (5, 0))
        assert wait_for(lambda: fake.cursor_style == 4)
        # style none turns it off
        client.patch("/pages/pin-entry",
                     json={"cursor": {"row": 0, "col": 0, "style": "none"}},
                     headers=h)
        assert wait_for(lambda: fake.cursor_style == 0)


def test_cursor_cleared_when_page_not_visible():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/edit", json={
            "lines": ["x"],
            "cursor": {"row": 1, "col": 2, "style": "block"},
        }, headers=h)
        assert wait_for(lambda: fake.cursor_style == 1)
        client.put("/pages/alarm", json={"lines": ["!"], "priority": 200},
                   headers=h)
        assert wait_for(lambda: fake.cursor_style == 0)
