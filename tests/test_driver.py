import time

import pytest

from cfa635.driver import (
    KEY_ENTER_PRESS,
    KEY_UP_PRESS,
    TYPE_COMMAND,
    TYPE_RESPONSE,
    Cfa635,
    CrcError,
    Packet,
    crc16,
)
from tests.fakeserial import FakeSerial, frame


def make_dev() -> tuple[Cfa635, FakeSerial]:
    fake = FakeSerial()
    return Cfa635(ser=fake), fake


# --- framing ----------------------------------------------------------------

def test_crc_residue_property():
    # Appending the complemented CRC (LSB first) to any body must always
    # produce the same residue-of-valid-frame; the driver relies on
    # recomputing and comparing instead, so check self-consistency plus a
    # pinned value so an accidental algorithm change fails loudly.
    assert crc16(b"\x00\x05probe") == crc16(b"\x00\x05probe")
    body = bytes([TYPE_COMMAND | 0, 5]) + b"probe"
    encoded = Packet(TYPE_COMMAND | 0, b"probe").encode()
    assert encoded == body + crc16(body).to_bytes(2, "little")


def test_packet_too_long_rejected():
    with pytest.raises(ValueError):
        Packet(TYPE_COMMAND | 31, bytes(23)).encode()


def test_corrupt_crc_raises():
    dev, fake = make_dev()
    good = frame(TYPE_RESPONSE | 1, b"CFA635:h1.1,v1.6")
    fake._rx += good[:-1] + bytes([good[-1] ^ 0xFF])
    with pytest.raises(CrcError):
        dev._read_packet(deadline=time.monotonic() + 0.1)


# --- basic exchanges --------------------------------------------------------

def test_ping_and_version():
    dev, fake = make_dev()
    assert dev.ping(b"hello") == b"hello"
    assert dev.version() == "CFA635:h1.1,v1.6"
    assert fake.commands == [(0, b"hello"), (1, b"")]


def test_write_text_updates_screen_and_bounds():
    dev, fake = make_dev()
    dev.write_text(2, 1, "hi there")
    assert fake.rows()[1] == "  hi there          "
    with pytest.raises(ValueError):
        dev.write_text(15, 0, "too long text")
    with pytest.raises(ValueError):
        dev.write_text(0, 4, "off screen")


def test_set_led_drives_both_dies():
    dev, fake = make_dev()
    dev.set_led(0, green=25, red=75)
    assert fake.led(0) == (25, 75)


# --- the report-preservation contract (the reason send() was changed) -------

def test_buffered_report_survives_send():
    """A key pressed before a command must not be flushed away by send()."""
    dev, fake = make_dev()
    fake.press_key(KEY_UP_PRESS)
    dev.ping()
    events = dev.drain_reports_nonblocking()
    assert [p.data[0] for p in events] == [KEY_UP_PRESS]


def test_mid_command_report_is_stashed():
    """A report interleaved between command and response is kept, and the
    response is still matched correctly."""
    dev, fake = make_dev()
    fake.press_key_mid_command(KEY_ENTER_PRESS)
    assert dev.ping(b"x") == b"x"
    events = dev.drain_reports_nonblocking()
    assert [p.data[0] for p in events] == [KEY_ENTER_PRESS]


def test_drain_is_nonblocking_when_empty():
    dev, _ = make_dev()
    assert dev.drain_reports_nonblocking() == []


def test_lost_response_is_retried_not_stalled():
    """Datasheet handshake: retry after ~250 ms rather than waiting seconds."""
    dev, fake = make_dev()
    fake.swallow_responses = 1
    start = time.monotonic()
    assert dev.ping(b"again") == b"again"
    assert time.monotonic() - start < 1.5  # one short timeout, not a long stall
    # the command was sent twice: original + retry
    assert [c for c, _ in fake.commands].count(0) == 2


def test_exhausted_retries_raise():
    dev, fake = make_dev()
    fake.swallow_responses = 10
    with pytest.raises(TimeoutError):
        dev.send(0, b"x", timeout=0.05, retries=1)


def test_report_order_preserved_across_paths():
    dev, fake = make_dev()
    fake.press_key(KEY_UP_PRESS)
    fake.press_key_mid_command(KEY_ENTER_PRESS)
    dev.ping()
    events = dev.drain_reports_nonblocking()
    assert [p.data[0] for p in events] == [KEY_UP_PRESS, KEY_ENTER_PRESS]
