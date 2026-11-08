"""PreToolUse command hook for the sandboxed ``claude -p`` run.

Claude Code runs this script before every tool call (registered through
``--settings`` by agent_runner.py) and passes the call as JSON on stdin. It
keeps file tools inside the task workspace (``$AGENT_GUARD_ROOT``) and nudges
the agent away from ``git push``; anything else is left to the tool allowlist.

Fails closed: if the input can't be read or the root isn't set, the call is denied.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT_ENV = "AGENT_GUARD_ROOT"
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit"})
# A nudge, not a boundary: the sandbox has no remote and no credential to push with.
BLOCKED_BASH = re.compile(r"\bgit\b[^\n;&|]*\b(?:push|remote\s+set-url)\b")


def deny(reason: str) -> dict[str, Any]:
    """Hook output that refuses the tool call."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(data: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return the hook output for one tool call (``{}`` = no objection)."""
    name, args = data.get("tool_name"), data.get("tool_input") or {}
    if name in FILE_TOOLS:
        raw = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if raw and not (root / raw).resolve().is_relative_to(root):
            return deny("file access outside the task workspace is not allowed")
    elif name == "Bash" and BLOCKED_BASH.search(str(args.get("command", ""))):
        return deny("pushing is done by the worker after you finish; just commit")
    return {}


def main() -> None:
    """Read the tool call from stdin and print the decision."""
    root = os.environ.get(ROOT_ENV)
    try:
        data = json.load(sys.stdin)
        if not root or not isinstance(data, dict):
            raise ValueError("no workspace root or malformed input")
        out = decide(data, Path(root).resolve())
    except ValueError:
        out = deny("tool guard could not validate this call")
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
