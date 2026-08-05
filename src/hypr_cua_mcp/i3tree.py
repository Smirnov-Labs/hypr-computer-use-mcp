"""Read/focus helpers over the i3 IPC tree.

Scope is deliberately option-1: we *read* the tree (windows, geometry, outputs)
and *focus* windows. We do not move, resize, or relayout — that's the option-2
extension documented in DESIGN-i3-control-extension.md.
"""
from __future__ import annotations

import os
import subprocess

import i3ipc


def _i3_socket_path() -> str | None:
    """Resolve the i3 IPC socket for the *current $DISPLAY*.

    We read i3's ``I3_SOCKET_PATH`` atom off the display's root window instead of
    trusting i3ipc's default discovery — which prefers the ambient ``$I3SOCK``
    env var and would bind us to the login i3 on :0 even when $DISPLAY points at
    a Xephyr sandbox on :1. Following DISPLAY is what makes the isolation real.

    We deliberately do NOT fall back to ``$I3SOCK``: it is display-agnostic, so a
    fallback would silently reconnect a :1 sandbox server to your live :0 desktop
    (fail-open). If no per-display socket is found we return None and let the
    caller fail loudly.
    """
    try:
        import Xlib.display
        import Xlib.X

        disp = Xlib.display.Display()  # honors $DISPLAY
        prop = disp.screen().root.get_full_property(
            disp.intern_atom("I3_SOCKET_PATH"), Xlib.X.AnyPropertyType
        )
        disp.close()
        if prop and prop.value:
            v = prop.value
            return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    except Exception:
        pass
    # DISPLAY-respecting fallback (reads the same atom, minus $I3SOCK).
    try:
        out = subprocess.run(
            ["i3", "--get-socketpath"], capture_output=True, text=True, timeout=5
        )
        if out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _conn() -> i3ipc.Connection:
    path = _i3_socket_path()
    if not path:
        raise RuntimeError(
            f"No i3 IPC socket found for DISPLAY={os.environ.get('DISPLAY')!r}. "
            "Is i3 running on that display?"
        )
    return i3ipc.Connection(socket_path=path)


def list_windows() -> list[dict]:
    """Every i3-managed leaf window with geometry, class, title, workspace, focus."""
    conn = _conn()
    tree = conn.get_tree()
    focused = tree.find_focused()
    focused_id = focused.id if focused else None

    workspaces = conn.get_workspaces()
    ws_output = {w.name: w.output for w in workspaces}
    ws_visible = {w.name: w.visible for w in workspaces}

    out: list[dict] = []
    for con in tree.leaves():
        ws = con.workspace()
        wsname = ws.name if ws else None
        out.append(
            {
                "id": con.id,  # i3 container id — use this for focus_window / screenshot_window
                "x11_window": con.window,  # X11 window id
                "wm_class": con.window_class,
                "wm_instance": con.window_instance,
                "title": con.name,
                "workspace": wsname,
                "output": ws_output.get(wsname),
                "rect": {
                    "x": con.rect.x,
                    "y": con.rect.y,
                    "w": con.rect.width,
                    "h": con.rect.height,
                },
                "focused": con.id == focused_id,
                "visible": bool(ws_visible.get(wsname, False)),
                "urgent": con.urgent,
            }
        )
    return out


def get_outputs() -> list[dict]:
    """Active outputs (monitors) with their global geometry."""
    conn = _conn()
    outs: list[dict] = []
    for o in conn.get_outputs():
        if not o.active:
            continue
        outs.append(
            {
                "name": o.name,
                "primary": o.primary,
                "rect": {
                    "x": o.rect.x,
                    "y": o.rect.y,
                    "w": o.rect.width,
                    "h": o.rect.height,
                },
                "current_workspace": o.current_workspace,
            }
        )
    return outs


def primary_output(outs: list[dict]) -> dict | None:
    for o in outs:
        if o["primary"]:
            return o
    return outs[0] if outs else None


def find_window(con_id: int) -> dict | None:
    for w in list_windows():
        if w["id"] == con_id:
            return w
    return None


def focus_window(con_id: int) -> bool:
    conn = _conn()
    replies = conn.command(f"[con_id={int(con_id)}] focus")
    if not (replies and all(r.success for r in replies)):
        return False
    # i3 reports success even when the criteria matched NO container, so the
    # command "succeeding" is not proof. Confirm focus actually landed on the
    # requested window — this is the only reliable signal, and the guard that
    # keeps keystrokes from going to the wrong window.
    focused = conn.get_tree().find_focused()
    return focused is not None and focused.id == con_id
