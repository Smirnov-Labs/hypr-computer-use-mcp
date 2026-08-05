"""Hyprland/Wayland computer-use MCP server.

Tools let a model SEE the desktop (per-output / per-window screenshots via grim),
UNDERSTAND it (the hyprctl tree), and DRIVE it (click / type / key / scroll via a
uinput device).

Two safety contracts enforced here:
- Pointer tools take the `frame_id` of the screenshot they were reasoned against,
  and the server rejects a stale/absent one instead of silently mapping against
  "whatever screenshot happened last" (races) or raw global pixels (no screenshot).
- Keyboard tools with a `con_id` (window address) VERIFY focus actually landed
  before sending any keystroke — a stale address fails loudly rather than typing
  into the wrong window.

Scope: read + focus + input + Hyprland control (workspaces / float / launch, in
hyprcontrol.py; the `run_dispatch` escape hatch blocks `exit`). Window ids are
Hyprland addresses ("0x...") from list_windows.
"""
from __future__ import annotations

import json
import os
import sys

from mcp.server.mcpserver import MCPServer, Image

from . import capture, cdp, clipboard, coords, hyprcontrol, hyprtree, inputs

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
    """List all Hyprland windows: id (address), class, title, workspace, output,
    global geometry, floating/focus/visibility. Use the `id` for focus/screenshot."""
    return json.dumps(hyprtree.list_windows(), indent=2)


@mcp.tool()
def get_outputs() -> str:
    """List active outputs (monitors) with their global geometry, scale, workspace."""
    return json.dumps(hyprtree.get_outputs(), indent=2)


@mcp.tool(structured_output=False)
def screenshot(output: str | None = None):
    """Screenshot one output (monitor); defaults to the focused output.

    Pass an output name from get_outputs (e.g. "DP-1") to target another monitor.
    Returns the image plus a frame_id — pass that frame_id to click/move/etc.
    """
    outs = hyprtree.get_outputs()
    if output:
        target = next((o for o in outs if o["name"] == output), None)
        if target is None:
            raise ValueError(
                f"no such output {output!r}; available: {[o['name'] for o in outs]}"
            )
    else:
        target = hyprtree.primary_output(outs)
    if target is None:
        raise RuntimeError("no active outputs found")
    r = target["rect"]
    shot = capture.capture_region(r["x"], r["y"], r["w"], r["h"], f"output {target['name']}")
    return _shot_response(shot)


@mcp.tool(structured_output=False)
def screenshot_window(con_id: str):
    """Full-resolution screenshot cropped to a single window (by address).

    NOTE: this crops the layout grab by the window's *geometry*. The window must be
    on its monitor's active workspace (else the region shows whatever IS there);
    a floating window sitting on top can also occlude it. A warning is added to the
    caption when the window isn't focused.
    """
    w = hyprtree.find_window(con_id)
    if w is None:
        raise ValueError(f"no window with address {con_id}; call list_windows first")
    if not w["visible"]:
        raise ValueError(
            f"window {con_id} ({w['wm_class']}) is not on its monitor's active "
            f"workspace ({w['workspace']!r}); focus_window it first, or screenshot its output"
        )
    r = w["rect"]
    label = f"window {con_id} ({w['wm_class'] or '?'}: {w['title'] or ''})".strip()
    if not w["focused"]:
        label += " [not focused — a floating window on top may occlude these pixels]"
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
def move_mouse(x: float, y: float, frame_id: str | None = None, follow: bool = False) -> str:
    """Move the pointer to (x,y) in screenshot `frame_id`'s pixel space.

    follow=True mimics a physical mouse's hover-focus: after moving, give keyboard
    focus to the window under the cursor. The synthetic pointer alone does NOT
    trigger Hyprland's own follow_mouse on a warp, so focus is set explicitly.
    No-op focus if the cursor lands on empty space or a bar. (A plain click already
    focuses what it hits — use follow to focus by hovering, without clicking.)
    """
    frame = coords.frames.resolve(frame_id)
    gx, gy = frame.to_global(x, y)
    inputs.reset_idle()
    inputs.move(gx, gy)
    msg = f"moved to screen({gx},{gy})"
    if follow:
        w = hyprtree.window_at(gx, gy)
        if w is None:
            msg += " · follow: no window there"
        elif hyprcontrol.focus_window(w["id"]):
            msg += f" · follow: focused {w['id']} ({w['wm_class']})"
        else:
            msg += f" · follow: {w['id']} focus did not land"
    return msg


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

def _focus_or_raise(con_id: str) -> None:
    """Focus con_id (address), or raise — so keystrokes never go to an unverified window."""
    if hyprtree.find_window(con_id) is None:
        raise ValueError(f"no window with address {con_id}; call list_windows first")
    if not hyprcontrol.focus_window(con_id):
        raise RuntimeError(
            f"could not focus window {con_id} (gone, or focus did not land); "
            "refusing to send input to the wrong window"
        )


@mcp.tool()
def focus_window(con_id: str) -> str:
    """Give keyboard focus to a window (by address). Raises if focus can't land."""
    _focus_or_raise(con_id)
    return f"focused {con_id}"


@mcp.tool()
def type_text(text: str, con_id: str | None = None) -> str:
    """Type text into the focused window. If con_id (address) is given, focus it
    first and fail (no keystrokes sent) if that focus can't be verified.

    NOTE: US-ASCII only (uinput has no unicode-compose path); unmapped characters
    are skipped, and embedded newlines are Enter keypresses.
    """
    if con_id is not None:
        _focus_or_raise(con_id)
    inputs.reset_idle()
    inputs.type_text(text)
    return f"typed {len(text)} chars" + (f" into {con_id}" if con_id is not None else "")


@mcp.tool()
def key(keys: str, con_id: str | None = None) -> str:
    """Press a key/chord (e.g. 'ctrl+s', 'Return', 'alt+Tab'). If con_id (address)
    is given, focus it first and fail if that focus can't be verified."""
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
    """Read the text of a Wayland selection: 'clipboard' (Ctrl-C / Ctrl-Shift-V)
    or 'primary' (select / middle-click). Returns '' if empty."""
    return clipboard.read_clipboard(selection)


@mcp.tool()
def write_clipboard(text: str, selection: str = "clipboard", secret: bool = False) -> str:
    """Set a Wayland selection ('clipboard' or 'primary') to text, then paste with
    key('ctrl+v' or 'ctrl+shift+v') or a middle-click. Returns only a length
    summary — never the value. secret=True marks the value for redaction in this
    server's action log (not built yet) and keeps it out of the return; the text
    still transits this tool call, so it appears in the MCP client's transcript."""
    return clipboard.write_clipboard(text, selection, secret)


# --- CDP browser (drive Chrome tabs over the DevTools protocol; no focus) ---

@mcp.tool()
def cdp_targets() -> str:
    """List drivable Chrome tabs (id, title, url) in the copilot browser. Starts a
    DEDICATED Chrome (isolated profile, debugging port) on first use if none is up —
    it never touches your main Chrome. Use a tab's `id` with the other cdp_* tools."""
    return json.dumps(cdp.list_targets(), indent=2)


@mcp.tool()
def cdp_open(url: str = "about:blank") -> str:
    """Open a new tab in the copilot browser. Returns the new tab's {id, url} as JSON."""
    return json.dumps(cdp.open_tab(url))


@mcp.tool()
def cdp_navigate(target: str, url: str) -> str:
    """Navigate a tab (by id from cdp_targets) to a URL. Returns {id, url, frameId}."""
    return json.dumps(cdp.navigate(target, url))


@mcp.tool()
def cdp_eval(target: str, expression: str) -> str:
    """Evaluate a JavaScript expression in a tab (by id) and return its value as
    JSON. A thrown JS exception is surfaced as an error, not swallowed."""
    return json.dumps(cdp.eval_js(target, expression))


@mcp.tool()
def cdp_read(target: str) -> str:
    """Read a tab's visible text (document.body.innerText) plus its title and url,
    as JSON {title, url, text} — a focus-free way to see page content."""
    return json.dumps(cdp.read_text(target), indent=2)


@mcp.tool()
def cdp_click(target: str, selector: str) -> str:
    """Click the first element matching a CSS selector in a tab (by id). Errors if
    nothing matches the selector."""
    return cdp.click(target, selector)


@mcp.tool()
def cdp_type(target: str, selector: str, text: str) -> str:
    """Type text into the element matching a CSS selector in a tab (by id): sets its
    value and dispatches input/change events. Returns a length summary — not the text."""
    return cdp.type_text(target, selector, text)


@mcp.tool()
def cdp_close(target: str) -> str:
    """Close a tab (by id) in the copilot browser."""
    return cdp.close_tab(target)


# --- Hyprland control (workspace / float / launch / layout) -----------------

@mcp.tool()
def launch(command: str) -> str:
    """Launch an app via Hyprland's exec dispatcher. Opens on the current
    workspace — pair with goto_workspace or move_window_to_workspace to place it."""
    inputs.reset_idle()
    return hyprcontrol.launch(command)


@mcp.tool()
def goto_workspace(workspace: str) -> str:
    """Switch the focused monitor to a workspace (created if absent). Numeric names
    match by number; other names are matched literally."""
    return hyprcontrol.goto_workspace(workspace)


@mcp.tool()
def move_window_to_workspace(con_id: str, workspace: str) -> str:
    """Move a window (by address) to a workspace without following it."""
    return hyprcontrol.move_window_to_workspace(con_id, workspace)


@mcp.tool()
def set_floating(con_id: str, enable: bool = True) -> str:
    """Enable/disable floating for a window (by address)."""
    return hyprcontrol.set_floating(con_id, enable)


@mcp.tool()
def layout_message(message: str) -> str:
    """Send a message to the active layout engine (dwindle/master), e.g.
    'togglesplit', 'swapsplit', 'orientationnext'. (Hyprland has no per-window
    splith/tabbed/stacking like i3.)"""
    return hyprcontrol.layout_message(message)


@mcp.tool()
def run_dispatch(dsp_expr: str) -> str:
    """Escape hatch: run a raw Hyprland dispatcher as the Lua expression passed to
    hl.dispatch(...), e.g. 'hl.dsp.window.fullscreen({})' or 'hl.dsp.window.move({ direction = "l" })'.
    `exit` is blocked."""
    return hyprcontrol.run_dispatch(dsp_expr)


# --- entry point ------------------------------------------------------------

def selftest() -> int:
    """Read-only smoke test: hypr tree + outputs + one capture + mouse query. No clicks."""
    print(f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')!r}")
    outs = hyprtree.get_outputs()
    print(f"outputs: {[o['name'] for o in outs]}")
    wins = hyprtree.list_windows()
    print(f"windows: {len(wins)} (focused: {[w['title'] for w in wins if w['focused']]})")
    prim = hyprtree.primary_output(outs)
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
