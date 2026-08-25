"""goose lifecycle hooks -> a status page on the display.

goose runs this script once per lifecycle event, handing it a JSON payload on
stdin (see `deploy/goose/cfa635-status/`). It maps that event onto one page
update and exits.

Two things about the host make the shape of this module:

**It runs in goose's hot path.** Every tool call blocks on this process, so a
display that is switched off, unplugged, or slow must cost goose as close to
nothing as possible: short timeouts, a circuit breaker, and `exit 0` no matter
what. goose parses hook stdout for `{"decision": ...}` and treats a hook
failure as grounds to deny the tool call ("denied by plugin hook"), so this
script prints nothing to stdout and never exits non-zero. A status display
must never be able to veto the agent.

**Tool events are not guaranteed.** Which events fire depends on the provider:
a native provider (`anthropic`) emits the full sequence including
PreToolUse/PostToolUse, but a delegating one (`claude-acp`) runs tools inside
the far-side agent, so goose only sees SessionStart, UserPromptSubmit, Stop
and SessionEnd. The page therefore treats tool detail as an enrichment and
still reads correctly without it.

The mapping is a pure function of (payload, state) so it can be tested without
a display, a network, or goose.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PAGE_ID = "goose"
ALERT_ID = "goose-alert"
LED = 1  # LED 0 is the server's own indicator and refuses writes

# The page outlives a couple of quiet events but not a dead agent: if goose is
# killed mid-session, the page evaporates instead of lying about being busy.
PAGE_TTL = 90.0

WIDTH = 20

# LED colours as (green, red, mode) — the display animates blink server-side,
# so it keeps blinking even if this process never runs again.
LED_IDLE = (100, 0, "solid")
LED_WORKING = (100, 100, "blink")
LED_ERROR = (0, 100, "solid")


def escape(text: str) -> str:
    """Neutralise markup in text we did not write.

    Prompts and shell commands routinely contain braces; without this a
    prompt mentioning `{bar:1:20}` would render as a widget. Mirrors the
    escaping the server's own shell applies to client-supplied page names.
    """
    return text.replace("{", "{{")


def _one_line(text: str, limit: int = 96) -> str:
    """Flatten to a single line and bound it: the marquee scrolls a long
    string forever, but there is no point carrying an essay across the wire."""
    flat = " ".join(str(text).split())
    return flat[:limit]


@dataclass
class State:
    """What we know about the session so far, persisted between invocations."""

    session_id: str = ""
    started: float = 0.0
    prompt: str = ""
    tool: str = ""
    tools: int = 0
    errors: int = 0
    working: bool = False
    saw_tool_events: bool = False
    # circuit breaker
    failures: int = 0
    disabled_until: float = 0.0

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self)))
        except OSError:
            pass


@dataclass
class Update:
    """What this event should do to the display."""

    lines: list[str] | None = None
    led: tuple[int, int, str] | None = None
    alert: dict[str, Any] | None = None
    delete: bool = False


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _tool_brief(payload: dict[str, Any]) -> str:
    """A short label for the running tool: its name plus the most telling
    argument, which for the shell tool is the command itself."""
    name = str(payload.get("tool_name") or "tool")
    args = payload.get("tool_input")
    detail = ""
    if isinstance(args, dict):
        for key in ("command", "path", "query", "url", "pattern"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                detail = value
                break
        else:
            # No argument we recognise. Fall back to the first short scalar —
            # some tools (todo_write) carry an entire document in there, and
            # marqueeing a checklist across the glass is just noise.
            detail = next(
                (v for v in args.values()
                 if isinstance(v, str) and v.strip() and len(v) <= 40 and "\n" not in v),
                "",
            )
    return _one_line(f"{name} {detail}".strip())


def render(state: State, now: float) -> list[str]:
    """Four rows. Animation tokens are re-rendered server-side at ~4 Hz, so a
    spinning page costs no further traffic between events."""
    head = "goose{fill}" + ("{spin}" if state.working else "{check}")

    prompt = f"{{scroll}}{escape(state.prompt)}" if state.prompt else ""

    if state.tool:
        activity = "{scroll}" + escape(state.tool)
    elif state.working:
        # No tool visibility (a delegating provider, or nothing has run yet):
        # say so plainly rather than leaving a blank row that reads as stalled.
        activity = "working{fill:.}"
    else:
        activity = ""

    counts = ""
    if state.saw_tool_events:
        counts = f"t{state.tools}"
        if state.errors:
            counts += f" {{cross}}{state.errors}"
    footer = counts + "{fill}" + _elapsed(now - state.started)

    return [head, prompt, activity, footer]


def update_for_event(
    payload: dict[str, Any], state: State, now: float
) -> tuple[State, Update]:
    """Fold one hook event into the state and say what the display should do.

    Pure: no clock, no network, no filesystem.
    """
    event = str(payload.get("event") or "")

    if event == "SessionStart":
        state = State(
            session_id=str(payload.get("session_id") or ""),
            started=now,
            # Carry the breaker across the session boundary.
            failures=state.failures,
            disabled_until=state.disabled_until,
        )
        return state, Update(lines=render(state, now), led=LED_IDLE)

    if not state.started:
        # We joined mid-session (plugin installed while goose was running).
        state.started = now
        state.session_id = str(payload.get("session_id") or state.session_id)

    if event == "UserPromptSubmit":
        state.prompt = _one_line(payload.get("message") or payload.get("matcher_context") or "")
        state.working = True
        state.tool = ""
        return state, Update(lines=render(state, now), led=LED_WORKING)

    if event == "PreToolUse":
        state.saw_tool_events = True
        state.working = True
        state.tool = _tool_brief(payload)
        return state, Update(lines=render(state, now), led=LED_WORKING)

    if event == "PostToolUse":
        state.saw_tool_events = True
        state.tools += 1
        state.working = True
        return state, Update(lines=render(state, now), led=LED_WORKING)

    if event == "PostToolUseFailure":
        state.saw_tool_events = True
        state.tools += 1
        state.errors += 1
        state.working = True
        return state, Update(lines=render(state, now), led=LED_ERROR)

    if event == "Stop":
        state.working = False
        state.tool = ""
        led = LED_ERROR if state.errors else LED_IDLE
        return state, Update(lines=render(state, now), led=led)

    if event == "SessionEnd":
        summary = _elapsed(now - state.started)
        if state.saw_tool_events:
            summary += f", {state.tools} tools"
            if state.errors:
                summary += f", {state.errors} failed"
        alert = {
            "lines": [
                "goose{fill}" + ("{cross}" if state.errors else "{check}"),
                "{hr}",
                "session finished",
                escape(summary),
            ],
            # >= 100 preempts whatever is on the glass, then TTLs itself away.
            "priority": 200,
            "ttl": 20.0,
            "leds": {LED: {"green": 0 if state.errors else 100,
                           "red": 100 if state.errors else 0,
                           "mode": "blink", "hz": 1.0}},
        }
        return State(failures=state.failures, disabled_until=state.disabled_until), \
            Update(alert=alert, delete=True, led=LED_IDLE)

    # An event we did not wire, or one goose added later: leave the glass alone.
    return state, Update()


# --------------------------------------------------------------------------
# host side: state file, circuit breaker, and the one HTTP call
# --------------------------------------------------------------------------

BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 60.0
HTTP_TIMEOUT = 0.4


def _state_path(session_id: str) -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id or "unknown")[:64]
    return Path(root) / "cfa635" / f"goose-{slug}.json"


def resolve_url() -> str | None:
    """Where the display is.

    goose runs hooks with its own environment, which will not have CFA635_URL
    in it unless the user exported it before starting goose. So fall back to a
    file the installer writes, and only then to the client's own default.
    """
    from_env = os.environ.get("CFA635_URL")
    if from_env:
        return from_env
    root = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    try:
        return (Path(root) / "cfa635" / "url").read_text().strip() or None
    except OSError:
        return None


def apply(update: Update, state: State, now: float) -> None:
    """Push one update. Any failure trips the breaker rather than propagating."""
    from cfa635.client import Cfa635Client, Cfa635Error

    client = Cfa635Client(resolve_url(), name="goose", timeout=HTTP_TIMEOUT)
    try:
        if update.delete:
            try:
                client.delete_page(PAGE_ID)
            except Cfa635Error:
                pass  # never published, or already gone
        if update.alert is not None:
            client.put_page(ALERT_ID, **update.alert)
        if update.lines is not None:
            client.put_page(PAGE_ID, update.lines, name="goose",
                            priority=40, ttl=PAGE_TTL)
        if update.led is not None:
            green, red, mode = update.led
            try:
                client.set_led(LED, green=green, red=red, mode=mode)
            except Cfa635Error:
                pass  # another client owns it; the page still tells the story
        state.failures = 0
        state.disabled_until = 0.0
    except Exception:
        state.failures += 1
        if state.failures >= BREAKER_THRESHOLD:
            state.disabled_until = now + BREAKER_COOLDOWN


def main() -> int:
    """Always returns 0. See the module docstring."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        now = time.time()
        path = _state_path(str(payload.get("session_id") or ""))
        state = State.load(path)

        state, update = update_for_event(payload, state, now)

        if now >= state.disabled_until:
            apply(update, state, now)
        state.save(path)
    except Exception:
        pass  # a status display is never a reason to interrupt the agent
    return 0


if __name__ == "__main__":
    sys.exit(main())
