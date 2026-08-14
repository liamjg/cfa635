"""Unicode/ASCII -> CFA635 CGROM translation.

The CGROM (datasheet v2.1, Figure 11, p. 52) mostly agrees with ASCII over
0x20-0x7E, but this ROM family replaces a handful of codes with accented
Latin characters and symbols:

    0x24 '$' -> ¤     0x5B '[' -> Ä     0x60 '`' -> ¿     0x7B '{' -> ä
    0x40 '@' -> ¡     0x5C '\\' -> Ö                      0x7C '|' -> ö
                      0x5D ']' -> Ñ                       0x7D '}' -> ñ
                      0x5E '^' -> Ü                       0x7E '~' -> ü
                      0x5F '_' -> §                       0x7F     -> à

Codes 0x00-0x07 are the CGRAM custom-glyph slots (command 9) and are never
emitted by this mapping. Unknown characters render as '?'.
"""

from __future__ import annotations

from cfa635.driver import COLUMNS

# ASCII codes whose CGROM glyph is NOT the ASCII character, mapped to the
# closest glyph the ROM does have.
_ASCII_SUBSTITUTES = {
    "$": 0x24,  # ROM glyph is ¤ (generic currency) — closest match for $
    "@": ord("a"),
    "[": ord("("),
    "]": ord(")"),
    "{": ord("("),
    "}": ord(")"),
    "\\": ord("/"),
    "^": ord("'"),
    "_": ord("-"),
    "`": ord("'"),
    "|": 0x40,  # ROM glyph ¡ reads as a broken vertical bar
    "~": ord("-"),
}

# Unicode characters the ROM genuinely has.
_UNICODE_TO_CGROM = {
    "¤": 0x24,
    "¡": 0x40,
    "Ä": 0x5B,
    "Ö": 0x5C,
    "Ñ": 0x5D,
    "Ü": 0x5E,
    "§": 0x5F,
    "¿": 0x60,
    "ä": 0x7B,
    "ö": 0x7C,
    "ñ": 0x7D,
    "ü": 0x7E,
    "à": 0x7F,
    "°": 0x80,  # superscript-0; the 0x80-0x89 block is superscript digits
    "¹": 0x81,
    "²": 0x82,
    "³": 0x83,
}

UNKNOWN = ord("?")


def encode_char(ch: str) -> int:
    if ch in _ASCII_SUBSTITUTES:
        return _ASCII_SUBSTITUTES[ch]
    if ch in _UNICODE_TO_CGROM:
        return _UNICODE_TO_CGROM[ch]
    code = ord(ch)
    if 0x20 <= code <= 0x7E:
        return code
    return UNKNOWN


def encode_line(text: str, width: int = COLUMNS) -> bytes:
    """Encode one display row: translated, truncated, space-padded to width."""
    return bytes(encode_char(ch) for ch in text[:width]).ljust(width, b" ")
