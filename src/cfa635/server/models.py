"""Request/response schemas for the REST API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from cfa635.driver import ROWS


class LedSpec(BaseModel):
    green: int = Field(0, ge=0, le=100)
    red: int = Field(0, ge=0, le=100)


class PagePut(BaseModel):
    lines: list[str] = Field(default_factory=list, max_length=ROWS)
    name: str | None = None
    priority: int = Field(50, ge=0, le=255)
    ttl: float | None = Field(None, gt=0)
    leds: dict[int, LedSpec] | None = None


class PagePatch(BaseModel):
    lines: list[str] | dict[int, str] | None = Field(None)
    name: str | None = None
    priority: int | None = Field(None, ge=0, le=255)
    ttl: float | None = Field(None, gt=0)
    leds: dict[int, LedSpec] | None = None


class PageOut(BaseModel):
    id: str
    name: str
    lines: list[str]
    priority: int
    ttl: float | None
    expires_in: float | None
    leds: dict[int, LedSpec] | None
    visible: bool


class ActivateBody(BaseModel):
    hold: float | None = Field(None, gt=0)


class BacklightPut(BaseModel):
    lcd: int = Field(..., ge=0, le=100)
    keypad: int | None = Field(None, ge=0, le=100)


class ContrastPut(BaseModel):
    value: int = Field(..., ge=0, le=255)
