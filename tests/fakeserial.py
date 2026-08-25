"""Compatibility shim: the fake device now lives in the package (cfa635.sim)
so the simulator can use it too. Tests import from here unchanged."""

from cfa635.sim import FakeSerial, frame, key_report  # noqa: F401
