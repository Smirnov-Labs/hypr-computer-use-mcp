"""i3 control (option 2): workspace / layout / launch mutation over i3 IPC.

Kept separate from i3tree.py (read + focus) so the *mutating* surface stays
small and auditable. Everything routes through one i3ipc `Connection.command()`
— no shell, so no injection — except `launch`, which is i3 `exec` and therefore
runs its argument through `sh -c` (documented on the tool).
"""
from __future__ import annotations

import re

from . import i3tree

# i3 verbs too disruptive to expose through the raw escape hatch. `exit` quits
# i3 entirely (ends your X session). `launch` is the sanctioned way to `exec`.
_DENY = {"exit"}

_LAYOUTS = {"splith", "splitv", "tabbed", "stacking", "default", "toggle split"}

_CRITERIA = re.compile(r"^\s*\[[^\]]*\]\s*")


class I3CommandError(RuntimeError):
    """An i3 IPC command was rejected or failed."""


def _run(command: str) -> list:
    conn = i3tree._conn()
    replies = conn.command(command)
    failed = [r for r in replies if not r.success]
    if not replies or failed:
        why = "; ".join((getattr(r, "error", None) or "?") for r in failed) or "empty reply"
        raise I3CommandError(f"i3 command {command!r} failed: {why}")
    return replies


def _verbs(command: str) -> list[str]:
    """First verb of each ;/,-chained segment, ignoring a leading [criteria]."""
    out = []
    for seg in re.split(r"[;,]", command):
        seg = _CRITERIA.sub("", seg).strip()
        if seg:
            out.append(seg.split(None, 1)[0].lower())
    return out


def _ws_target(workspace: str) -> str:
    ws = str(workspace).strip()
    if ws.isdigit():
        return f"number {ws}"
    return '"{}"'.format(ws.replace('"', r"\""))


def run_i3(command: str) -> str:
    """Run a raw i3 command over IPC (the escape hatch). `exit` is blocked."""
    if any(v in _DENY for v in _verbs(command)):
        raise I3CommandError(
            f"command blocked: one of {sorted(_DENY)} would disrupt the session"
        )
    _run(command)
    return f"ran: {command}"


def goto_workspace(workspace: str) -> str:
    """Switch the focused output to a workspace (creates it if absent)."""
    _run(f"workspace {_ws_target(workspace)}")
    return f"switched to workspace {workspace}"


def move_window_to_workspace(con_id: int, workspace: str) -> str:
    """Move a window to a workspace (without switching your view)."""
    if i3tree.find_window(con_id) is None:
        raise ValueError(f"no window with con id {con_id}; call list_windows first")
    _run(f"[con_id={int(con_id)}] move to workspace {_ws_target(workspace)}")
    # i3 reports success even if a criteria matched nothing, so report where the
    # window actually ended up rather than trusting the reply.
    w = i3tree.find_window(con_id)
    return f"moved con {con_id} -> workspace {w['workspace'] if w else '?'}"


def launch(command: str) -> str:
    """Launch an app via i3 `exec` (runs `command` through sh -c)."""
    _run(f"exec {command}")
    return f"launched: {command}"


def set_floating(con_id: int, enable: bool = True) -> str:
    """Toggle a window's floating state."""
    if i3tree.find_window(con_id) is None:
        raise ValueError(f"no window with con id {con_id}; call list_windows first")
    _run(f"[con_id={int(con_id)}] floating {'enable' if enable else 'disable'}")
    return f"floating {'enabled' if enable else 'disabled'} for con {con_id}"


def set_layout(con_id: int, layout: str) -> str:
    """Set the layout of a window's container (splith/splitv/tabbed/stacking/...)."""
    if layout not in _LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; use one of {sorted(_LAYOUTS)}")
    if i3tree.find_window(con_id) is None:
        raise ValueError(f"no window with con id {con_id}; call list_windows first")
    _run(f"[con_id={int(con_id)}] layout {layout}")
    return f"layout {layout} for con {con_id}"
