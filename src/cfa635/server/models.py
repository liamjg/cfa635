"""Request/response schemas for the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cfa635.driver import ROWS


class LedSpec(BaseModel):
    green: int = Field(0, ge=0, le=100)
    red: int = Field(0, ge=0, le=100)
    # Animated server-side by the tick loop: no client PUT-loops, and an
    # alarm keeps blinking even if the client that raised it dies.
    # hz <= 2 because animation is sampled by the 4 Hz tick (Nyquist).
    mode: Literal["solid", "blink", "pulse"] = "solid"
    hz: float = Field(1.0, gt=0, le=2)


class CursorSpec(BaseModel):
    """Hardware cursor: the zero-cost selection affordance for focused UIs."""
    row: int = Field(..., ge=0, le=3)
    col: int = Field(..., ge=0, le=19)
    style: Literal["none", "block", "underscore",
                   "block_underscore", "invert"] = "block"


class PagePut(BaseModel):
    lines: list[str] = Field(default_factory=list, max_length=ROWS)
    name: str | None = None
    priority: int = Field(50, ge=0, le=255)
    ttl: float | None = Field(None, gt=0)
    duration: float | None = Field(None, gt=0)
    leds: dict[int, LedSpec] | None = None
    interactive: bool = False
    cursor: CursorSpec | None = None


class PagePatch(BaseModel):
    lines: list[str] | dict[int, str] | None = Field(None)
    name: str | None = None
    priority: int | None = Field(None, ge=0, le=255)
    ttl: float | None = Field(None, gt=0)
    duration: float | None = Field(None, gt=0)
    leds: dict[int, LedSpec] | None = None
    interactive: bool | None = None
    cursor: CursorSpec | None = None  # style "none" turns the cursor off


class PageOut(BaseModel):
    id: str
    name: str
    lines: list[str]
    owner: str
    priority: int
    ttl: float | None
    expires_in: float | None
    duration: float | None
    leds: dict[int, LedSpec] | None
    interactive: bool
    cursor: CursorSpec | None
    visible: bool
    focused: bool


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)


class ClientRegistered(BaseModel):
    """Returned once, at registration — the only time the token is shown."""
    id: str
    token: str
    name: str


class ClientOut(BaseModel):
    id: str
    name: str
    last_seen_ago: float
    pages: list[str]


class ActivateBody(BaseModel):
    hold: float | None = Field(None, gt=0)
