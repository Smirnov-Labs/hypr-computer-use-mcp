"""Read helpers over the Hyprland IPC (`hyprctl -j`).

Scope is deliberately read-only: windows (clients), outputs (monitors), and
geometry, exposed in the same dict shape i3-cua's i3tree used so coords.py and
server.py port unchanged. Mutations (focus, workspace, float, launch) live in
hyprcontrol.py. The window "id" here is the Hyprland address string ("0x...").
"""
from __future__ import annotations

import json
import subprocess

CMD_TIMEOUT = 5


class HyprctlError(RuntimeError):
    """A hyprctl query failed."""


def _json(args: list[str]):
    try:
        out = subprocess.run(
            ["hyprctl", "-j", *args],
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
    except FileNotFoundError:
        raise HyprctlError("hyprctl not found; is Hyprland running?")
    except subprocess.TimeoutExpired as exc:
        raise HyprctlError(
            f"hyprctl {' '.join(args)} timed out after {CMD_TIMEOUT}s"
        ) from exc
    if out.returncode != 0:
        raise HyprctlError(f"hyprctl {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def list_windows() -> list[dict]:
    """Every Hyprland client: address (id), class, title, workspace, output,
    global geometry, floating/focus/visibility."""
    monitors = _json(["monitors"])
    mon_name = {m["id"]: m["name"] for m in monitors}
    mon_active = {m["id"]: (m.get("activeWorkspace") or {}).get("name") for m in monitors}
    out: list[dict] = []
    for c in _json(["clients"]):
        at = c.get("at") or [0, 0]
        size = c.get("size") or [0, 0]
        wsname = (c.get("workspace") or {}).get("name")
        mon = c.get("monitor")
        out.append({
            "id": c.get("address"),  # Hyprland window address "0x..." — use for focus/screenshot
            "wm_class": c.get("class"),
            "wm_instance": c.get("initialClass") or c.get("class"),
            "title": c.get("title"),
            "workspace": wsname,
            "output": mon_name.get(mon),
            "rect": {"x": at[0], "y": at[1], "w": size[0], "h": size[1]},
            "floating": bool(c.get("floating", False)),
            "focused": c.get("focusHistoryID") == 0,
            # visible only when on the monitor's active workspace (and not hidden) —
            # otherwise a geometry-cropped grab shows whatever IS on that region.
            "visible": bool(wsname and wsname == mon_active.get(mon)
                            and not c.get("hidden", False)),
            "urgent": bool(c.get("urgent", False)),
        })
    return out


def get_outputs() -> list[dict]:
    """Active outputs (monitors) with their global geometry (LOGICAL px) and scale."""
    outs: list[dict] = []
    for m in _json(["monitors"]):
        sc = m.get("scale") or 1.0
        outs.append({
            "name": m["name"],
            # Wayland has no "primary"; the focused monitor is the sensible default target.
            "primary": bool(m.get("focused")),
            # width/height are PHYSICAL px but x/y are LOGICAL; report the logical
            # rect so it matches window at/size and the pointer's coordinate space.
            "rect": {"x": m["x"], "y": m["y"],
                     "w": round(m["width"] / sc), "h": round(m["height"] / sc)},
            "scale": sc,
            "current_workspace": (m.get("activeWorkspace") or {}).get("name"),
        })
    return outs


def primary_output(outs: list[dict]) -> dict | None:
    for o in outs:
        if o["primary"]:
            return o
    return outs[0] if outs else None


def find_window(con_id: str) -> dict | None:
    for w in list_windows():
        if w["id"] == con_id:
            return w
    return None


def get_focused_address() -> str | None:
    """Address of the currently focused window, or None."""
    aw = _json(["activewindow"])
    return aw.get("address") if aw else None


def window_at(gx: int, gy: int) -> dict | None:
    """The visible window whose global rect contains (gx, gy), or None.

    Only considers windows on their monitor's active workspace (`visible`). When
    windows overlap, prefers a floating one (it stacks above tiled); among equals,
    the last listed. Used for hover-follow (`move_mouse(follow=True)`); the tree has
    no true z-order, so the floating heuristic is best-effort for stacked floats.
    """
    hits = [
        w for w in list_windows()
        if w["visible"]
        and w["rect"]["x"] <= gx < w["rect"]["x"] + w["rect"]["w"]
        and w["rect"]["y"] <= gy < w["rect"]["y"] + w["rect"]["h"]
    ]
    if not hits:
        return None
    floating = [w for w in hits if w["floating"]]
    return (floating or hits)[-1]
