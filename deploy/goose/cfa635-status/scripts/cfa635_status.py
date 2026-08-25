#!/usr/bin/env python3
"""goose hook entry point: one lifecycle event -> one page update.

Kept deliberately thin. All the logic lives in :mod:`cfa635.goose.status` so
it can be unit-tested without goose, a network, or a display.

This must never write to stdout and never exit non-zero: goose parses hook
stdout for ``{"decision": ...}`` and treats a failing hook as grounds to deny
the tool call. A status display does not get a vote on what the agent does.
"""

import os
import sys

try:
    from cfa635.goose.status import main
except ModuleNotFoundError:
    # Not installed: fall back to the copy install.sh vendored beside us.
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "vendor")
    )
    try:
        from cfa635.goose.status import main
    except Exception:
        sys.exit(0)  # no display library available; stay out of the way
except Exception:
    sys.exit(0)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
