from cfa635 import charmap


def test_plain_ascii_passthrough():
    assert charmap.encode_line("Hello, World! 123", width=17) == b"Hello, World! 123"


def test_divergent_ascii_is_substituted_never_raw():
    # These ASCII codes render as accented characters on this ROM, so the
    # encoder must not emit them for their ASCII meaning.
    for ch in "$@[]{}\\^_`|~":
        code = charmap.encode_char(ch)
        assert code != ord(ch) or ch == "$", ch
    # $ maps to 0x24 deliberately: the ROM glyph there is ¤, the closest match.
    assert charmap.encode_char("$") == 0x24


def test_rom_unicode_characters():
    assert charmap.encode_char("ü") == 0x7E
    assert charmap.encode_char("Ä") == 0x5B
    assert charmap.encode_char("°") == 0x80
    assert charmap.encode_char("§") == 0x5F


def test_unknown_becomes_question_mark():
    assert charmap.encode_char("€") == ord("?")
    assert charmap.encode_char("\n") == ord("?")


def test_padding_and_truncation():
    assert charmap.encode_line("hi") == b"hi" + b" " * 18
    assert len(charmap.encode_line("x" * 30)) == 20


def test_never_emits_cgram_slots():
    # Codes 0-7 are custom-glyph slots; no text input may reach them.
    for code in range(0x110000 // 4096):  # spot-check a spread of the BMP
        ch = chr(code * 4096 % 0x110000)
        try:
            encoded = charmap.encode_char(ch)
        except ValueError:
            continue
        assert encoded > 7
