"""Server configuration from CFA635_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _find_port() -> str:
    from cfa635.probe import find_port

    detected = find_port()
    return detected if detected else "/dev/cfa635"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Simulator mode: run against the in-process fake device (cfa635.sim)
    # instead of real hardware, and serve the visual simulator at /sim.
    sim: bool = field(default_factory=lambda: _env_flag("CFA635_SIM"))
    port: str = field(default_factory=lambda: (
        "sim" if _env_flag("CFA635_SIM")
        else os.environ.get("CFA635_PORT") or _find_port()))
    http_host: str = field(default_factory=lambda: os.environ.get("CFA635_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: int(os.environ.get("CFA635_HTTP_PORT", "8635")))
    rotation_secs: float = field(default_factory=lambda: float(os.environ.get("CFA635_ROTATION_SECS", "10")))
    nav_hold_secs: float = field(default_factory=lambda: float(os.environ.get("CFA635_NAV_HOLD_SECS", "30")))
    focus_hold_secs: float = field(default_factory=lambda: float(os.environ.get("CFA635_FOCUS_HOLD_SECS", "30")))
    nav_keys: tuple[str, ...] = field(default_factory=lambda: tuple(
        k.strip().lower() for k in os.environ.get("CFA635_NAV_KEYS", "up,down").split(",") if k.strip()
    ))
    # Defaults avoid backlight PWM (duty 1-99%): this unit's supply audibly
    # whines under PWM. 100 = solid on, 0 = solid off; both are silent.
    backlight: int = field(default_factory=lambda: int(os.environ.get("CFA635_BACKLIGHT", "100")))
    idle_backlight: int = field(default_factory=lambda: int(os.environ.get("CFA635_IDLE_BACKLIGHT", "0")))
    idle_dim_secs: float = field(default_factory=lambda: float(os.environ.get("CFA635_IDLE_DIM_SECS", "300")))
    contrast: int = field(default_factory=lambda: int(os.environ.get("CFA635_CONTRAST", "120")))
