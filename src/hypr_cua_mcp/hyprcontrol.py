"""Hyprland control (mutations) via `hyprctl dispatch`.

This Hyprland is configured in Lua (hyprland.lua), so `hyprctl dispatch X` is
parsed as Lua and fails. Dispatchers must be invoked as
``hyprctl eval 'hl.dispatch(hl.dsp.<path>({...}))'`` — hl.dsp.* builds a
dispatcher closure and hl.dispatch runs it. Window targets use the address
selector "address:0x..." (the same form `hyprctl clients` reports).

Kept separate from hyprtree (read) so the mutating surface stays small and
auditable — including focus_window, which is a dispatch here.
"""
from __future__ import annotations

import re
import subprocess
import time

from . import hyprtree

CMD_TIMEOUT = 5
_DENY = ("exit",)  # dispatchers too disruptive to expose through the raw escape hatch


class HyprCommandError(RuntimeError):
    """A hyprctl dispatch was rejected or failed."""


def _eval(lua: str) -> str:
    """Run `hyprctl eval <lua>`; return stdout, raising on failure or `error:`."""
    try:
        out = subprocess.run(
            ["hyprctl", "eval", lua],
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
    except FileNotFoundError:
        raise HyprCommandError("hyprctl not found; is Hyprland running?")
    except subprocess.TimeoutExpired as exc:
        raise HyprCommandError(f"hyprctl eval timed out after {CMD_TIMEOUT}s") from exc
    res = (out.stdout or "").strip()
    if out.returncode != 0 or res.startswith("error:"):
        raise HyprCommandError(f"hyprctl eval failed: {res or out.stderr.strip()}")
    return res


def _dispatch(dsp_expr: str) -> str:
    """hl.dispatch(<dsp_expr>) — e.g. dsp_expr = 'hl.dsp.focus({ workspace = 5 })'."""
    return _eval(f"hl.dispatch({dsp_expr})")


def _cursorpos() -> tuple[int, int] | None:
    """Current global pointer (x, y) via `hyprctl cursorpos`, or None if unreadable.

    Read directly (a query, not a dispatch) so it works mid-focus; any failure
    returns None so a focus op degrades to "focus without restoring the cursor"
    rather than erroring.
    """
    try:
        out = subprocess.run(
            ["hyprctl", "cursorpos"],
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    x, _, y = out.stdout.strip().partition(",")
    try:
        return int(x.strip()), int(y.strip())
    except ValueError:
        return None


def _focus_keeping_cursor(focus_expr: str) -> None:
    """Run a focus dispatch, then restore the pointer to where it was.

    Hyprland warps the cursor to the newly-focused window/output on every focus
    (`cursor:no_warps` defaults false), so a background agent's focus_window /
    goto_workspace would otherwise yank the user's REAL, shared cursor — the
    "screenshot jumped my mouse to 0,0" symptom (the warp lands on a monitor's
    origin when the focused workspace has no window to center on).

    We warp it back with `hl.dsp.cursor.move` — a SYNTHETIC warp that, unlike a
    real uinput motion, does NOT re-trigger follow_mouse (verified on the live
    compositor), so keyboard focus still lands on and stays on the target. If the
    pointer can't be read we focus without restoring rather than fail.
    """
    before = _cursorpos()
    _dispatch(focus_expr)
    if before is not None:
        _dispatch(f"hl.dsp.cursor.move({{ x = {before[0]}, y = {before[1]} }})")


def _lua_str(s: str) -> str:
    """Quote a Python str as a Lua string literal (double-quoted, escaped)."""
    body = (
        str(s).replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r")
    )
    return '"' + body + '"'


def _ws_field(workspace) -> str:
    """Lua value for a workspace: ASCII-numeric -> bare number, else quoted string."""
    ws = str(workspace).strip()
    return ws if (ws.isascii() and ws.isdigit()) else _lua_str(ws)


def _addr(con_id: str) -> str:
    return _lua_str("address:" + str(con_id))


def goto_workspace(workspace) -> str:
    """Switch the focused monitor to a workspace (created if absent).

    Restores the pointer afterwards so switching workspaces from a background agent
    doesn't warp the user's shared cursor (see _focus_keeping_cursor).
    """
    _focus_keeping_cursor(f"hl.dsp.focus({{ workspace = {_ws_field(workspace)} }})")
    return f"switched to workspace {workspace}"


def focus_window(con_id: str) -> bool:
    """Focus a window by address, then VERIFY focus landed — the guard that keeps
    keystrokes from going to the wrong window. Returns False if the window is gone
    or focus didn't land.

    CAVEAT: under Hyprland `follow_mouse=1` (the default) keyboard focus tracks the
    pointer, so a dispatch-focus can be reverted by the cursor's position. The
    reliable way to focus a window for typing is to CLICK it via screenshot
    coordinates (the natural computer-use flow) — prefer click-then-type over
    type_text(con_id=...) when this returns False.
    """
    if hyprtree.find_window(con_id) is None:
        return False
    # Focus, then restore the cursor (the synthetic warp doesn't disturb the focus
    # we just set — see _focus_keeping_cursor) BEFORE verifying, so the verdict
    # reflects the final state the user is left in.
    _focus_keeping_cursor(f"hl.dsp.focus({{ window = {_addr(con_id)} }})")
    for _ in range(3):  # allow the focus event to settle before verifying
        if hyprtree.get_focused_address() == con_id:
            return True
        time.sleep(0.05)
    return False


def move_window_to_workspace(con_id: str, workspace) -> str:
    """Move a window (by address) to a workspace without following it."""
    if hyprtree.find_window(con_id) is None:
        raise ValueError(f"no window with address {con_id!r}; call list_windows first")
    _dispatch(
        f"hl.dsp.window.move({{ workspace = {_ws_field(workspace)}, follow = false, "
        f"window = {_addr(con_id)} }})"
    )
    # Report where it actually landed rather than trusting the dispatch.
    w = hyprtree.find_window(con_id)
    return f"moved {con_id} -> workspace {w['workspace'] if w else '?'}"


def set_floating(con_id: str, enable: bool = True) -> str:
    """Enable/disable floating for a window (by address). Reads the current state
    and only toggles when it differs, so the outcome is deterministic regardless
    of which toggle-action verbs the dispatcher accepts."""
    w = hyprtree.find_window(con_id)
    if w is None:
        raise ValueError(f"no window with address {con_id!r}; call list_windows first")
    if bool(w.get("floating")) == bool(enable):
        return f"floating already {'on' if enable else 'off'} for {con_id}"
    _dispatch(f"hl.dsp.window.float({{ action = \"toggle\", window = {_addr(con_id)} }})")
    return f"floating {'enabled' if enable else 'disabled'} for {con_id}"


def launch(command: str) -> str:
    """Launch an app via Hyprland's exec_cmd dispatcher."""
    _dispatch(f"hl.dsp.exec_cmd({_lua_str(command)})")
    return f"launched: {command}"


def layout_message(message: str) -> str:
    """Send a message to the active layout engine (dwindle/master), e.g.
    'togglesplit', 'swapsplit', 'orientationnext'. (Hyprland has no per-window
    splith/tabbed/stacking like i3 — this is the closest equivalent.)"""
    _dispatch(f"hl.dsp.layout({_lua_str(message)})")
    return f"layout message: {message}"


def run_dispatch(dsp_expr: str) -> str:
    """Escape hatch: run hl.dispatch(<dsp_expr>) with the given Lua dispatcher
    expression (e.g. 'hl.dsp.window.fullscreen({})'). Denied verbs (`exit`, which
    ends the whole Hyprland session) are blocked in any Lua spelling."""
    # Lua is whitespace-insensitive and `t.x` == `t["x"]`, so we can't reliably
    # substring-match the source (`hl.dsp .exit()`, `hl.dsp["exit"]` are the same
    # call). Strip whitespace + quotes and reject any denied verb as a member name.
    norm = re.sub(r"[\s'\"]+", "", dsp_expr).lower()
    if any(d in norm for d in _DENY):
        raise HyprCommandError(f"command blocked: {sorted(_DENY)} would disrupt the session")
    _dispatch(dsp_expr)
    return f"dispatched: {dsp_expr}"
