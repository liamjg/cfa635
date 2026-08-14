"""Crystalfontz CFA635 driver and LAN API server.

The driver lives in :mod:`cfa635.driver`; the most useful names are
re-exported here so `from cfa635 import Cfa635` keeps working.
"""

from cfa635.driver import (
    COLUMNS,
    KEY_MASK_ALL,
    KEY_NAMES,
    LED_GPO,
    MAX_DATA_LENGTH,
    ROWS,
    TYPE_COMMAND,
    TYPE_ERROR,
    TYPE_REPORT,
    TYPE_RESPONSE,
    Cfa635,
    CommandError,
    CrcError,
    Packet,
    crc16,
)

__all__ = [
    "COLUMNS",
    "KEY_MASK_ALL",
    "KEY_NAMES",
    "LED_GPO",
    "MAX_DATA_LENGTH",
    "ROWS",
    "TYPE_COMMAND",
    "TYPE_ERROR",
    "TYPE_REPORT",
    "TYPE_RESPONSE",
    "Cfa635",
    "CommandError",
    "CrcError",
    "Packet",
    "crc16",
]
