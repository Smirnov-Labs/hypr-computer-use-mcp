"""i3-aware computer-use MCP server (X11).

Tools let a model SEE the desktop (per-output / per-window screenshots),
UNDERSTAND it (i3 tree), and DRIVE it (click / type / key / scroll).

Two safety contracts enforced here:
- Pointer tools take the `frame_id` of the screenshot they were reasoned against,
  and the server rejects a stale/absent one instead of silently mapping against
  "whatever screenshot happened last" (races) or raw global pixels (no screenshot).
- Keyboard tools with a `con_id` VERIFY focus actually landed before sending any
  keystroke — a stale con_id fails loudly rather than typing into the wrong window.

Scope: read + focus + input + i3 control (workspaces / layout / launch, in
i3control.py; the `run_i3` escape hatch blocks `exit`).
"""
from __future__ import annotations

import json
import os
import sys

from mcp.server.mcpserver import MCPServer, Image

from . import capture, clipboard, coords, i3control, i3tree, inputs

mcp = MCPServer("hypr-cua", version="0.1.0")


def _shot_response(shot: capture.Shot):
    """Store the mapping under a fresh id and return [image, caption]."""
    frame_id = coords.frames.set_last(shot.frame)
    meta = shot.frame.as_dict()
    meta["frame_id"] = frame_id
    caption = (
        f"Screenshot of {meta['captured']} — "
        f"{meta['screenshot_size']['w']}x{meta['screenshot_size']['h']}px. "
        f"frame_id={frame_id!r}. Give click/move/scroll/drag coordinates in THIS "
        "image's pixel space (top-left = 0,0) and pass this frame_id so the server "
        "maps against the screenshot you actually saw.\n" + json.dumps(meta)
    )
    return [Image(data=shot.png, format="png"), caption]


# --- perception -------------------------------------------------------------

@mcp.tool()
def list_windows() -> str:
    """List all i3-managed windows: con id, class, title, workspace, output,
    global geometry, focus/visibility. Use the `id` for focus/screenshot tools."""
    return json.dumps(i3tree.list_windows(), indent=2)


@mcp.tool()
def get_outputs() -> str:
    """List active outputs (monitors) with their global geometry and workspace."""
    return json.dumps(i3tree.get_outputs(), indent=2)


@mcp.tool(structured_output=False)
def screenshot(output: str | None = None):
    """Screenshot one output (monitor); defaults to the primary output.

    Pass an output name from get_outputs (e.g. "DP-0") to target another monitor.
    Returns the image plus a frame_id — pass that frame_id to click/move/etc.
    """
    outs = i3tree.get_outputs()
    if output:
        target = next((o for o in outs if o["name"] == output), None)
        if target is None:
            raise ValueError(
                f"no such output {output!r}; available: {[o['name'] for o in outs]}"
            )
    else:
        target = i3tree.primary_output(outs)
    if target is None:
        raise RuntimeError("no active outputs found")
    r = target["rect"]
    shot = capture.capture_region(r["x"], r["y"], r["w"], r["h"], f"output {target['name']}")
    return _shot_response(shot)


@mcp.tool(structured_output=False)
def screenshot_window(con_id: int):
    """Full-resolution screenshot cropped to a single window (by i3 con id).

    NOTE: this crops the root grab by the window's *geometry*; it does not verify
    the window is the topmost content there. For a window in a tabbed/stacking
    container that isn't the active child, the pixels shown may be a sibling. A
    warning is added to the caption when the window isn't focused.
    """
    w = i3tree.find_window(con_id)
    if w is None:
        raise ValueError(f"no window with con id {con_id}; call list_windows first")
    if not w["visible"]:
        raise ValueError(
            f"window {con_id} ({w['wm_class']}) is on non-visible workspace "
            f"{w['workspace']!r}; focus_window it first, or screenshot its output"
        )
    r = w["rect"]
    label = f"window {con_id} ({w['wm_class'] or '?'}: {w['title'] or ''})".strip()
    if not w["focused"]:
        label += " [not focused — if it's in a tabbed/stacked container these pixels may be a sibling window]"
    shot = capture.capture_region(r["x"], r["y"], r["w"], r["h"], label)
    return _shot_response(shot)


# --- pointer (coordinates are in the named screenshot's pixel space) ---------

@mcp.tool()
def click(x: float, y: float, button: str = "left", frame_id: str | None = None) -> str:
    """Click at (x,y) in the pixels of screenshot `frame_id` (default: the last
    screenshot). button: left|middle|right. Errors if no screenshot exists yet or
    the frame_id is stale."""
    frame = coords.frames.resolve(frame_id)
    gx, gy = frame.to_global(x, y)
    inputs.reset_idle()
    inputs.click(gx, gy, button)
    return f"{button} click at shot({x},{y}) -> screen({gx},{gy}) [frame {frame_id or 'last'}: {frame.label}]"


@mcp.tool()
def double_click(x: float, y: float, button: str = "left", frame_id: str | None = None) -> str:
    """Double-click at (x,y) in screenshot `frame_id`'s pixel space."""
    frame = coords.frames.resolve(frame_id)
    gx, gy = frame.to_global(x, y)
    inputs.reset_idle()
    inputs.click(gx, gy, button, count=2)
    return f"double {button} click at screen({gx},{gy})"


@mcp.tool()
def move_mouse(x: float, y: float, frame_id: str | None = None) -> str:
    """Move the pointer to (x,y) in screenshot `frame_id`'s pixel space."""
    frame = coords.frames.resolve(frame_id)
    gx, gy = frame.to_global(x, y)
    inputs.reset_idle()
    inputs.move(gx, gy)
    return f"moved to screen({gx},{gy})"


@mcp.tool()
def drag(x1: float, y1: float, x2: float, y2: float, button: str = "left",
         frame_id: str | None = None) -> str:
    """Drag from (x1,y1) to (x2,y2), coords in screenshot `frame_id`'s pixel space."""
    frame = coords.frames.resolve(frame_id)
    gx1, gy1 = frame.to_global(x1, y1)
    gx2, gy2 = frame.to_global(x2, y2)
    inputs.reset_idle()
    inputs.drag(gx1, gy1, gx2, gy2, button)
    return f"dragged screen({gx1},{gy1})->({gx2},{gy2})"


@mcp.tool()
def scroll(x: float, y: float, direction: str = "down", amount: int = 3,
           frame_id: str | None = None) -> str:
    """Scroll at (x,y) (screenshot `frame_id` pixel space). direction: up|down."""
    frame = coords.frames.resolve(frame_id)
    gx, gy = frame.to_global(x, y)
    inputs.reset_idle()
    inputs.scroll(gx, gy, direction, amount)
    return f"scrolled {direction} x{amount} at screen({gx},{gy})"


# --- keyboard + focus -------------------------------------------------------

def _focus_or_raise(con_id: int) -> None:
    """Focus con_id, or raise — so keystrokes never go to an unverified window."""
    if i3tree.find_window(con_id) is None:
        raise ValueError(f"no window with con id {con_id}; call list_windows first")
    if not i3tree.focus_window(con_id):
        raise RuntimeError(
            f"could not focus con {con_id} (window gone, or criteria matched "
            "nothing); refusing to send input to the wrong window"
        )


@mcp.tool()
def focus_window(con_id: int) -> str:
    """Give i3 keyboard focus to a window (by con id). Raises if focus can't land."""
    _focus_or_raise(con_id)
    return f"focused con {con_id}"


@mcp.tool()
def type_text(text: str, con_id: int | None = None) -> str:
    """Type text into the focused window. If con_id is given, focus it first and
    fail (no keystrokes sent) if that focus can't be verified.

    NOTE: embedded newlines are Enter keypresses (xdotool behavior), so multi-line
    text submits line-by-line in single-line fields.
    """
    if con_id is not None:
        _focus_or_raise(con_id)
    inputs.reset_idle()
    inputs.type_text(text)
    return f"typed {len(text)} chars" + (f" into con {con_id}" if con_id is not None else "")


@mcp.tool()
def key(keys: str, con_id: int | None = None) -> str:
    """Press a key/chord in xdotool syntax (e.g. 'ctrl+s', 'Return'). If con_id is
    given, focus it first and fail if that focus can't be verified."""
    if con_id is not None:
        _focus_or_raise(con_id)
    inputs.reset_idle()
    inputs.key(keys)
    return f"pressed {keys}"


@mcp.tool()
def get_mouse() -> str:
    """Current global pointer location."""
    return json.dumps(inputs.mouse_location())


# --- clipboard (exact text in/out; no OCR) ----------------------------------

@mcp.tool()
def read_clipboard(selection: str = "clipboard") -> str:
    """Read the text of an X selection: 'clipboard' (Ctrl-C / Ctrl-Shift-V) or
    'primary' (select / middle-click). Returns '' if empty or unowned."""
    return clipboard.read_clipboard(selection)


@mcp.tool()
def write_clipboard(text: str, selection: str = "clipboard", secret: bool = False) -> str:
    """Set an X selection ('clipboard' or 'primary') to text, then paste with
    key('ctrl+v' or 'ctrl+shift+v') or a middle-click. Returns only a length
    summary — never the value. secret=True marks the value for redaction in this
    server's action log (not built yet) and keeps it out of the return; the text
    still transits this tool call, so it appears in the MCP client's transcript."""
    return clipboard.write_clipboard(text, selection, secret)


# --- i3 control (option 2: workspace / layout / launch) ---------------------

@mcp.tool()
def launch(command: str) -> str:
    """Launch an app via i3 `exec` (runs `command` through sh -c). Opens on the
    current workspace — pair with goto_workspace or move_window_to_workspace to
    place it elsewhere."""
    inputs.reset_idle()
    return i3control.launch(command)


@mcp.tool()
def goto_workspace(workspace: str) -> str:
    """Switch the focused output to a workspace (created if absent). A numeric
    name uses i3 `number` matching; other names are matched literally."""
    return i3control.goto_workspace(workspace)


@mcp.tool()
def move_window_to_workspace(con_id: int, workspace: str) -> str:
    """Move a window (by con id) to a workspace without switching your view."""
    return i3control.move_window_to_workspace(con_id, workspace)


@mcp.tool()
def set_floating(con_id: int, enable: bool = True) -> str:
    """Enable/disable floating for a window (by con id)."""
    return i3control.set_floating(con_id, enable)


@mcp.tool()
def set_layout(con_id: int, layout: str) -> str:
    """Set a container's layout: splith|splitv|tabbed|stacking|default|toggle split."""
    return i3control.set_layout(con_id, layout)


@mcp.tool()
def run_i3(command: str) -> str:
    """Escape hatch: run a raw i3 command over IPC (e.g. 'resize set 800 600',
    'move left', 'fullscreen toggle'). `exit` is blocked; there's no shell unless
    the command itself is an `exec`."""
    return i3control.run_i3(command)


# --- entry point ------------------------------------------------------------

def selftest() -> int:
    """Read-only smoke test: i3 tree + outputs + one capture + mouse query. No clicks."""
    print(f"DISPLAY={os.environ.get('DISPLAY')!r}")
    outs = i3tree.get_outputs()
    print(f"outputs: {[o['name'] for o in outs]}")
    wins = i3tree.list_windows()
    print(f"windows: {len(wins)} (focused: {[w['title'] for w in wins if w['focused']]})")
    prim = i3tree.primary_output(outs)
    if prim:
        r = prim["rect"]
        shot = capture.capture_region(r["x"], r["y"], r["w"], r["h"], f"output {prim['name']}")
        f = shot.frame
        print(
            f"capture {prim['name']}: {f.shot_w}x{f.shot_h} "
            f"scale_x={f.scale_x:.4f} scale_y={f.scale_y:.4f} png={len(shot.png)}B"
        )
    print(f"mouse: {inputs.mouse_location()}")
    print("selftest OK")
    return 0


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
