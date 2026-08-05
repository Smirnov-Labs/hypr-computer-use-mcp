"""Input synthesis via a self-owned evdev uinput device.

Coordinates are global Wayland-layout pixels. The device's absolute axes span
[0, _ABS_MAX] and the compositor maps that range onto the monitor bounding box,
so we scale pixels -> abs using the layout extent read from `hyprctl monitors`
(verified: 65535 -> full layout width/height, linear, no offset).

Unlike X11 xdotool, these are REAL input events (uinput), so they reset the
compositor idle timer naturally — there is no synthetic-input self-lock, hence
reset_idle() is a documented no-op. The process owns /dev/uinput directly (an
ACL grants the user rw); no ydotoold daemon and no root.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

from evdev import AbsInfo, UInput
from evdev import ecodes as e

CMD_TIMEOUT = 5
_ABS_MAX = 65535       # absolute-axis resolution; compositor maps [0,_ABS_MAX] -> layout extent
_SETTLE_S = 1.0        # one-time wait for the compositor to bind the new uinput device
_CLICK_GAP_S = 0.02    # spacing between repeats so multi-clicks register distinctly

_BUTTON = {"left": e.BTN_LEFT, "middle": e.BTN_MIDDLE, "right": e.BTN_RIGHT}


# --- US-ASCII keyboard maps -------------------------------------------------

def _build_char_map() -> dict:
    m: dict = {}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        code = getattr(e, f"KEY_{ch.upper()}")
        m[ch] = (code, False)
        m[ch.upper()] = (code, True)
    for d, shifted in zip("1234567890", "!@#$%^&*()"):
        code = getattr(e, f"KEY_{d}")
        m[d] = (code, False)
        m[shifted] = (code, True)
    m.update({
        "-": (e.KEY_MINUS, False), "_": (e.KEY_MINUS, True),
        "=": (e.KEY_EQUAL, False), "+": (e.KEY_EQUAL, True),
        "[": (e.KEY_LEFTBRACE, False), "{": (e.KEY_LEFTBRACE, True),
        "]": (e.KEY_RIGHTBRACE, False), "}": (e.KEY_RIGHTBRACE, True),
        "\\": (e.KEY_BACKSLASH, False), "|": (e.KEY_BACKSLASH, True),
        ";": (e.KEY_SEMICOLON, False), ":": (e.KEY_SEMICOLON, True),
        "'": (e.KEY_APOSTROPHE, False), '"': (e.KEY_APOSTROPHE, True),
        "`": (e.KEY_GRAVE, False), "~": (e.KEY_GRAVE, True),
        ",": (e.KEY_COMMA, False), "<": (e.KEY_COMMA, True),
        ".": (e.KEY_DOT, False), ">": (e.KEY_DOT, True),
        "/": (e.KEY_SLASH, False), "?": (e.KEY_SLASH, True),
        " ": (e.KEY_SPACE, False),
        "\n": (e.KEY_ENTER, False), "\t": (e.KEY_TAB, False),
    })
    return m


_CHAR = _build_char_map()

_MODS = {
    "ctrl": e.KEY_LEFTCTRL, "control": e.KEY_LEFTCTRL,
    "shift": e.KEY_LEFTSHIFT,
    "alt": e.KEY_LEFTALT,
    "super": e.KEY_LEFTMETA, "meta": e.KEY_LEFTMETA, "win": e.KEY_LEFTMETA,
}

_NAMED = {
    "return": e.KEY_ENTER, "enter": e.KEY_ENTER, "escape": e.KEY_ESC, "esc": e.KEY_ESC,
    "tab": e.KEY_TAB, "space": e.KEY_SPACE, "backspace": e.KEY_BACKSPACE, "bs": e.KEY_BACKSPACE,
    "delete": e.KEY_DELETE, "del": e.KEY_DELETE, "insert": e.KEY_INSERT,
    "home": e.KEY_HOME, "end": e.KEY_END, "pageup": e.KEY_PAGEUP, "pagedown": e.KEY_PAGEDOWN,
    "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
}
_NAMED.update({f"f{i}": getattr(e, f"KEY_F{i}") for i in range(1, 13)})


def _key_capabilities() -> list[int]:
    codes = set(_MODS.values()) | set(_NAMED.values()) | set(_BUTTON.values())
    codes |= {c for c, _ in _CHAR.values()}
    return sorted(codes)


_ui: UInput | None = None
_extent: tuple[float, float] | None = None
_ui_lock = threading.Lock()


def _button(name: str) -> int:
    try:
        return _BUTTON[name]
    except KeyError:
        raise ValueError(f"unknown button {name!r}; use one of {sorted(_BUTTON)}")


def _layout_extent() -> tuple[float, float]:
    """(width, height) of the monitor bounding box in LOGICAL global px, from hyprctl.

    hyprctl reports width/height in PHYSICAL px but x/y in LOGICAL layout px, so a
    scaled monitor's logical size is width/scale. The cursor and hyprctl
    at/size/cursorpos are all logical, so the uinput abs axes must map to the
    LOGICAL extent — otherwise one scaled monitor skews pointer accuracy everywhere.
    """
    out = subprocess.run(
        ["hyprctl", "-j", "monitors"],
        capture_output=True, text=True, check=True, timeout=CMD_TIMEOUT,
    )
    mons = json.loads(out.stdout)
    w = max((m["x"] + m["width"] / (m.get("scale") or 1.0) for m in mons), default=0)
    h = max((m["y"] + m["height"] / (m.get("scale") or 1.0) for m in mons), default=0)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not determine layout extent from `hyprctl monitors`")
    return w, h


def _device() -> UInput:
    """The process-lifetime virtual input device (created + settled once).

    Double-checked locking so pipelined tool calls can't race two devices; and we
    compute the extent + build the device locally and only commit the globals after
    BOTH succeed, so one transient hyprctl failure doesn't permanently wedge input.
    """
    global _ui, _extent
    if _ui is None:
        with _ui_lock:
            if _ui is None:
                extent = _layout_extent()  # may raise -> nothing committed; retried next call
                cap = {
                    e.EV_KEY: _key_capabilities(),
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, 0, _ABS_MAX, 0, 0, 0)),
                        (e.ABS_Y, AbsInfo(0, 0, _ABS_MAX, 0, 0, 0)),
                    ],
                    e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
                }
                dev = UInput(cap, name="hypr-cua-virtual-input", input_props=[e.INPUT_PROP_POINTER])
                time.sleep(_SETTLE_S)  # let the compositor bind the device before first use
                _extent, _ui = extent, dev  # commit only after both succeed
    return _ui


def _scale(gx: int, gy: int) -> tuple[int, int]:
    """Map global pixels -> absolute-axis units for the current layout extent."""
    w, h = _extent  # set alongside the device
    ax = round(max(0, min(gx, w)) / w * _ABS_MAX)
    ay = round(max(0, min(gy, h)) / h * _ABS_MAX)
    return ax, ay


def _abs_move(ui: UInput, gx: int, gy: int) -> None:
    ax, ay = _scale(gx, gy)
    ui.write(e.EV_ABS, e.ABS_X, ax)
    ui.write(e.EV_ABS, e.ABS_Y, ay)
    ui.syn()


def move(gx: int, gy: int) -> None:
    _abs_move(_device(), gx, gy)


def click(gx: int, gy: int, button: str = "left", count: int = 1) -> None:
    b = _button(button)
    ui = _device()
    _abs_move(ui, gx, gy)
    for i in range(max(1, count)):
        if i:
            time.sleep(_CLICK_GAP_S)
        ui.write(e.EV_KEY, b, 1); ui.syn()
        ui.write(e.EV_KEY, b, 0); ui.syn()


def drag(gx1: int, gy1: int, gx2: int, gy2: int, button: str = "left") -> None:
    b = _button(button)
    ui = _device()
    _abs_move(ui, gx1, gy1)
    ui.write(e.EV_KEY, b, 1); ui.syn()
    _abs_move(ui, gx2, gy2)
    ui.write(e.EV_KEY, b, 0); ui.syn()


def scroll(gx: int, gy: int, direction: str = "down", amount: int = 3) -> None:
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    ui = _device()
    _abs_move(ui, gx, gy)
    step = 1 if direction == "up" else -1  # REL_WHEEL: +1 up, -1 down
    for _ in range(max(1, amount)):
        ui.write(e.EV_REL, e.REL_WHEEL, step); ui.syn()


def _tap(ui: UInput, code: int, shift: bool = False) -> None:
    if shift:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1); ui.syn()
    ui.write(e.EV_KEY, code, 1); ui.syn()
    ui.write(e.EV_KEY, code, 0); ui.syn()
    if shift:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0); ui.syn()


def type_text(text: str) -> None:
    """Type US-ASCII text. Characters outside the ASCII keymap are skipped
    (uinput has no unicode-compose path); embedded newlines are Enter."""
    ui = _device()
    for ch in text:
        m = _CHAR.get(ch)
        if m is None:
            continue  # best-effort: skip chars we can't map on a US layout
        code, shift = m
        _tap(ui, code, shift)


def _resolve_key(token: str) -> tuple[int, bool]:
    """Return (keycode, needs_shift) for a key token."""
    t = token.lower()
    if t in _NAMED:
        return _NAMED[t], False
    if len(token) == 1 and token in _CHAR:
        return _CHAR[token]  # (code, shift) — e.g. '!' -> (KEY_1, True)
    raise ValueError(f"unknown key {token!r}")


def key(keyseq: str) -> None:
    """Press a chord like 'ctrl+s', 'Return', 'alt+Tab', 'ctrl+shift+t', '!'."""
    parts = [p for p in keyseq.split("+") if p]
    if not parts:
        raise ValueError("empty key sequence")
    *mods, main = parts
    mod_codes = []
    for mod in mods:
        if mod.lower() not in _MODS:
            raise ValueError(f"unknown modifier {mod!r}; use ctrl/shift/alt/super")
        mod_codes.append(_MODS[mod.lower()])
    key_code, need_shift = _resolve_key(main)
    if need_shift and e.KEY_LEFTSHIFT not in mod_codes:
        mod_codes.append(e.KEY_LEFTSHIFT)  # a bare shifted char ('A', '!') implies shift
    ui = _device()
    for c in mod_codes:
        ui.write(e.EV_KEY, c, 1)
    ui.syn()
    ui.write(e.EV_KEY, key_code, 1); ui.syn()
    ui.write(e.EV_KEY, key_code, 0); ui.syn()
    for c in reversed(mod_codes):
        ui.write(e.EV_KEY, c, 0)
    ui.syn()


def mouse_location() -> dict:
    """Current global pointer position via `hyprctl cursorpos` -> {'x','y'}."""
    out = subprocess.run(
        ["hyprctl", "cursorpos"],
        capture_output=True, text=True, check=True, timeout=CMD_TIMEOUT,
    )
    x, _, y = out.stdout.strip().partition(",")
    return {"x": int(x.strip()), "y": int(y.strip())}


def reset_idle() -> None:
    """No-op on Wayland: uinput emits *real* input events, so the compositor's
    idle timer resets naturally (the X11 synthetic-input self-lock this guarded
    against cannot happen here). Kept so callers need no change."""
    return None
