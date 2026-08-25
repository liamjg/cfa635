"""The goose hook -> page mapping.

The payloads below are real: captured from goose 1.47.0 by wiring a logging
plugin into every event and running a session under each provider. The two
sequences differ, and that difference is the point of `test_delegating_...`.
"""

import json
import subprocess
import sys

import pytest

from cfa635.goose.status import (
    LED_ERROR,
    LED_IDLE,
    LED_WORKING,
    PAGE_ID,
    State,
    escape,
    render,
    update_for_event,
)

# --- recorded payloads (goose 1.47.0) -------------------------------------

SESSION_START = {"event": "SessionStart", "session_id": "20260825_2", "matcher_context": None}
PROMPT = {
    "event": "UserPromptSubmit",
    "session_id": "20260825_2",
    "matcher_context": "Run the shell command: echo PROBE2_OK",
    "message": "Run the shell command: echo PROBE2_OK",
}
PRE_TOOL = {
    "event": "PreToolUse",
    "session_id": "20260825_2",
    "matcher_context": "shell",
    "tool_name": "shell",
    "tool_input": {"command": "echo PROBE2_OK"},
    "working_dir": "/tmp",
}
POST_TOOL = dict(PRE_TOOL, event="PostToolUse")
POST_FAIL = dict(PRE_TOOL, event="PostToolUseFailure")
STOP = {
    "event": "Stop",
    "session_id": "20260825_2",
    "matcher_context": None,
    "last_assistant_message": "PROBE2_OK",
    "working_dir": "/tmp",
}
SESSION_END = {"event": "SessionEnd", "session_id": "20260825_2", "matcher_context": None}


def feed(payloads, state=None, start=1000.0, step=1.0):
    """Fold a sequence of events, returning (state, last update, all updates)."""
    state = state or State()
    updates = []
    for i, payload in enumerate(payloads):
        state, update = update_for_event(payload, state, start + i * step)
        updates.append(update)
    return state, updates[-1], updates


# --- the native-provider sequence -----------------------------------------


def test_native_provider_sequence_counts_tools():
    state, last, _ = feed([SESSION_START, PROMPT, PRE_TOOL, POST_TOOL, STOP])
    assert state.tools == 1
    assert state.errors == 0
    assert state.saw_tool_events is True
    assert state.working is False
    assert last.led == LED_IDLE


def test_tool_failure_counts_and_reddens_the_led():
    state, last, _ = feed([SESSION_START, PROMPT, PRE_TOOL, POST_FAIL])
    assert (state.tools, state.errors) == (1, 1)
    assert last.led == LED_ERROR
    # The error survives to the end of the turn.
    state, stop_update = update_for_event(STOP, state, 2000.0)
    assert stop_update.led == LED_ERROR


def test_running_tool_is_named_on_the_glass():
    state, last, _ = feed([SESSION_START, PROMPT, PRE_TOOL])
    assert last.led == LED_WORKING
    assert "shell echo PROBE2_OK" in last.lines[2]
    assert last.lines[0] == "goose{fill}{spin}"


# --- the delegating-provider sequence -------------------------------------


def test_delegating_provider_still_reads_as_working():
    """claude-acp runs tools inside the far-side agent, so goose emits no
    tool events at all. The page must not look stalled or empty."""
    state, last, _ = feed([SESSION_START, PROMPT])
    assert state.saw_tool_events is False
    assert last.led == LED_WORKING
    assert last.lines[0] == "goose{fill}{spin}"
    assert last.lines[2] == "working{fill:.}"
    # No tool counter is shown, because we have no honest number to put there.
    assert not last.lines[3].startswith("t")

    state, stop_update = update_for_event(STOP, state, 1100.0)
    assert stop_update.lines[0] == "goose{fill}{check}"
    assert stop_update.led == LED_IDLE


# --- session end ----------------------------------------------------------


def test_session_end_raises_an_alert_and_clears_the_page():
    state, last, _ = feed([SESSION_START, PROMPT, PRE_TOOL, POST_TOOL, STOP, SESSION_END])
    assert last.delete is True
    assert last.alert is not None
    assert last.alert["priority"] >= 100, "must preempt whatever is on the glass"
    assert last.alert["ttl"] == 20.0, "must clean itself up"
    assert "1 tools" in last.alert["lines"][3]
    # State is reset, ready for the next session in the same container.
    assert state.tools == 0 and state.prompt == ""


def test_session_end_marks_failure():
    state, last, _ = feed([SESSION_START, PROMPT, PRE_TOOL, POST_FAIL, STOP, SESSION_END])
    assert "{cross}" in last.alert["lines"][0]
    assert "1 failed" in last.alert["lines"][3]


# --- robustness -----------------------------------------------------------


def test_unknown_events_leave_the_display_alone():
    state, update = update_for_event({"event": "AfterFileEdit"}, State(started=1.0), 2.0)
    assert update.lines is None and update.led is None and update.delete is False


def test_joining_mid_session_does_not_crash():
    # Plugin installed while goose was already running: no SessionStart seen.
    state, update = update_for_event(PROMPT, State(), 500.0)
    assert update.lines is not None
    assert state.started == 500.0


def test_markup_in_a_prompt_is_neutralised():
    """`{{` is the server's literal-brace escape; a bare `}` needs none."""
    assert escape("{a}") == "{{a}"

    hostile = dict(PROMPT, message="render {bar:1:20} and {spin} now")
    _, update = update_for_event(hostile, State(started=1.0), 2.0)

    # Feed the result through the real parser and confirm no widget appeared:
    # every cell must be plain text, never a custom-glyph slot.
    from cfa635.markup import parse_frame

    frame = parse_frame(update.lines, now=0.0)
    assert all(cell.glyph is None for cell in frame[1]), \
        "a prompt must not be able to draw widgets"
    # The braces survive as literal text (as the CGROM's brace lookalikes).
    row = "".join(chr(cell.char or 0x20) for cell in frame[1])
    assert "bar:1:20" in row

    # A control: the same tokens written by *us* do still render as widgets.
    assert any(cell.glyph is not None for cell in parse_frame(["{bar:1:20}"], now=0.0)[0])


def test_lines_are_always_four_rows():
    for payloads in ([SESSION_START], [SESSION_START, PROMPT], [SESSION_START, PROMPT, PRE_TOOL]):
        _, last, _ = feed(payloads)
        assert len(last.lines) == 4


def test_elapsed_is_formatted_compactly():
    state = State(started=0.0, session_id="s")
    assert render(state, 45.0)[3].endswith("45s")
    assert render(state, 125.0)[3].endswith("2m05s")
    assert render(state, 7300.0)[3].endswith("2h01m")


# --- the script as goose actually invokes it ------------------------------


def test_script_is_silent_and_exits_zero_with_no_display(tmp_path):
    """goose parses hook stdout for {"decision": ...} and treats failure as
    grounds to deny the tool call. With the display unreachable this must
    still be a silent, successful no-op."""
    proc = subprocess.run(
        [sys.executable, "-m", "cfa635.goose.status"],
        input=json.dumps(PRE_TOOL),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "XDG_CACHE_HOME": str(tmp_path),
            "CFA635_URL": "http://127.0.0.1:1",  # reserved: never listening
            "PYTHONPATH": "src",
        },
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_script_publishes_to_a_live_server(live_server, tmp_path):
    for payload in (SESSION_START, PROMPT, PRE_TOOL):
        proc = subprocess.run(
            [sys.executable, "-m", "cfa635.goose.status"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "XDG_CACHE_HOME": str(tmp_path),
                "CFA635_URL": live_server.url,
                "PYTHONPATH": "src",
            },
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr

    assert live_server.wait_for(lambda: live_server.contains("goose"))
    assert live_server.wait_for(lambda: live_server.contains("shell echo"))
    assert live_server.wait_for(lambda: live_server.fake.led(1)[0] > 0)


def test_breaker_stops_calling_a_dead_display(tmp_path, monkeypatch):
    from cfa635.goose import status

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("CFA635_URL", "http://127.0.0.1:1")

    calls = []
    real_apply = status.apply

    def counting_apply(update, state, now):
        calls.append(now)
        real_apply(update, state, now)

    monkeypatch.setattr(status, "apply", counting_apply)

    for _ in range(6):
        monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(PRE_TOOL)))
        assert status.main() == 0

    # Three failures trip the breaker; the rest are skipped without a socket.
    assert len(calls) == status.BREAKER_THRESHOLD


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


# --- url resolution -------------------------------------------------------


def test_url_comes_from_env_then_file_then_default(tmp_path, monkeypatch):
    """goose does not pass CFA635_URL through to hooks, so the installer
    records it on disk; the env var still wins when it is present."""
    from cfa635.goose.status import resolve_url

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CFA635_URL", raising=False)
    assert resolve_url() is None  # falls through to the client's own default

    conf = tmp_path / "cfa635"
    conf.mkdir(parents=True)
    (conf / "url").write_text("http://192.168.99.20:8635\n")
    assert resolve_url() == "http://192.168.99.20:8635"

    monkeypatch.setenv("CFA635_URL", "http://10.0.0.1:8635")
    assert resolve_url() == "http://10.0.0.1:8635"


def test_bulky_tool_arguments_do_not_marquee_a_document():
    """goose's todo_write carries a whole checklist in `content`; the glass
    should say what ran, not scroll the document across four rows."""
    from cfa635.goose.status import _tool_brief

    bulky = {
        "tool_name": "todo__todo_write",
        "tool_input": {"content": "- [x] sleep 6\n- [x] echo hi\n- [ ] more"},
    }
    assert _tool_brief(bulky) == "todo__todo_write"

    # A short, single-line argument is still worth showing.
    assert _tool_brief({"tool_name": "t", "tool_input": {"thing": "brief"}}) == "t brief"
    # And a recognised key always wins.
    assert _tool_brief({"tool_name": "shell", "tool_input": {"command": "ls -l"}}) == "shell ls -l"
