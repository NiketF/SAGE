"""Action-policy abstraction for drive-aware conversational safeguards."""

from __future__ import annotations

import ntpath
import re

_WRITE_ACTIONS = {
    "delete",
    "remove",
    "erase",
    "modify",
    "edit",
    "move",
    "rename",
    "replace",
    "overwrite",
    "cleanup",
}


def _drive_of(path: str | None) -> str:
    if not path:
        return ""
    drive, _ = ntpath.splitdrive(path)
    return drive.upper().rstrip("\\")


def policy_for(path: str | None, action: str) -> dict[str, object]:
    """Return a deterministic, extensible policy decision for this path/action."""
    action_key = re.sub(r"\s+", "_", action.strip().casefold())
    drive = _drive_of(path)
    write_intent = any(word in action_key for word in _WRITE_ACTIONS)
    tier = "c_strict_read_only" if drive == "C:" else "default_read_only"
    reason = (
        "C drive uses stricter read-only policy in conversational mode."
        if drive == "C:"
        else "Conversational mode is read-only for file-changing actions."
    )
    return {
        "drive": drive or "unknown",
        "tier": tier,
        "read_only": bool(write_intent),
        "action": action_key,
        "reason": reason,
    }
