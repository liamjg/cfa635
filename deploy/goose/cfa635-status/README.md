# cfa635-status — a goose status plugin

Puts the running goose session on the CFA635: what it is doing, how long it
has been doing it, how many tools it has run, and an LED you can read across
the room.

## Install

On the machine (or container) that runs goose:

    CFA635_URL=http://192.168.99.20:8635 deploy/goose/install.sh

The installer copies the plugin to `~/.agents/plugins/cfa635-status/`, vendors
the two stdlib-only modules the hook needs (no pyserial, no venv — just
`python3`), records the display URL in `~/.config/cfa635/url`, and checks the
hook is silent and exits 0. Restart any running goose session to pick it up.

`GOOSE_PLUGIN_DIR` overrides the install location.

## What you see

    goose              ⠋     spinner while working, ✓ when idle
    Run these shell commands  the prompt, marqueeing
    shell sleep 6            the tool currently running
    t11              36s     tools run, elapsed

LED 1 is green when idle, blinking amber while working, red after a failed
tool. At the end of a session an alert page preempts the glass for 20 s with a
summary, then everything clears.

The spinner and the marquee are re-rendered by the server at ~4 Hz, so a busy
page costs no HTTP traffic between events.

## Which events actually fire

Confirmed against goose 1.47.0. **What you get depends on the provider:**

| Event | `anthropic` (native) | `claude-acp` (delegating) |
|---|---|---|
| SessionStart | yes | yes |
| UserPromptSubmit | yes | yes |
| PreToolUse / PostToolUse | yes | **no** |
| Before/AfterShellExecution | yes | **no** |
| PostToolUseFailure | yes | **no** |
| Stop | yes | yes |
| SessionEnd | yes | yes |

Under a delegating provider the tools run inside the far-side agent, so goose
never sees them. The page handles this: it shows `working....` and drops the
tool counter rather than displaying a zero that would be a lie.

Recorded payload shapes:

    SessionStart      {event, session_id, matcher_context}
    UserPromptSubmit  {event, session_id, matcher_context, message}
    PreToolUse        {event, session_id, matcher_context, tool_name,
                       tool_input, working_dir}
    PostToolUse       (same as PreToolUse)
    Stop              {event, session_id, matcher_context,
                       last_assistant_message, working_dir}
    SessionEnd        {event, session_id, matcher_context}

`Before/AfterShellExecution` duplicate `Pre/PostToolUse` for shell calls and
are deliberately **not** wired: each hook is a subprocess in goose's hot path.

## The rules this plugin plays by

- **It never exits non-zero and never writes to stdout.** goose parses hook
  stdout for `{"decision": ...}` and treats a failing hook as grounds to deny
  the tool call ("denied by plugin hook"). A status display does not get a
  vote on what the agent does.
- **It never makes goose wait.** HTTP timeout is 0.4 s, and three consecutive
  failures trip a circuit breaker that skips the network entirely for 60 s. An
  unplugged display costs goose nothing.
- **It escapes what it did not write.** Prompts and shell commands routinely
  contain braces; without escaping, a prompt mentioning `{bar:1:20}` would
  draw a widget.
- **It uses LED 1.** LED 0 is the server's own indicator and refuses writes.

## Uninstall

    rm -rf ~/.agents/plugins/cfa635-status ~/.config/cfa635/url

Pages carry a TTL, so anything left on the glass clears itself within 90 s.
