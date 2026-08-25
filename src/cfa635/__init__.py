"""Crystalfontz CFA635 driver and LAN API server.

The driver lives in :mod:`cfa635.driver`; the most useful names are
re-exported here so `from cfa635 import Cfa635` keeps working.

The re-export is lazy (PEP 562): importing the driver pulls in pyserial, and
the network-side modules — :mod:`cfa635.client`, :mod:`cfa635.markup` — have
no business requiring a serial library to talk HTTP. Touching any name below
imports the driver on demand, so `from cfa635 import Cfa635` is unchanged.
"""

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


def __getattr__(name: str):
    if name in __all__:
        from cfa635 import driver

        return getattr(driver, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
