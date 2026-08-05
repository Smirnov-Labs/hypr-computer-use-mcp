"""Input synthesis via xdotool. All coordinates here are *global* X pixels."""
from __future__ import annotations

import subprocess

_BUTTON = {"left": 1, "middle": 2, "right": 3}
CMD_TIMEOUT = 5  # seconds; bound a hung xdotool/xset so it can't wedge a worker thread


def _button(name: str) -> int:
    try:
        return _BUTTON[name]
    except KeyError:
        raise ValueError(f"unknown button {name!r}; use one of {sorted(_BUTTON)}")


def _run(args: list[str]) -> None:
    r = subprocess.run(
        ["xdotool", *args], capture_output=True, text=True, timeout=CMD_TIMEOUT
    )
    if r.returncode != 0:
        raise RuntimeError(f"xdotool {' '.join(args)} failed: {r.stderr.strip()}")


def move(gx: int, gy: int) -> None:
    _run(["mousemove", "--sync", str(gx), str(gy)])


def click(gx: int, gy: int, button: str = "left", count: int = 1) -> None:
    b = _button(button)
    args = ["mousemove", "--sync", str(gx), str(gy), "click"]
    if count > 1:
        args += ["--repeat", str(count), "--delay", "120"]
    args.append(str(b))
    _run(args)


def drag(gx1: int, gy1: int, gx2: int, gy2: int, button: str = "left") -> None:
    b = _button(button)
    _run(["mousemove", "--sync", str(gx1), str(gy1)])
    _run(["mousedown", str(b)])
    _run(["mousemove", "--sync", str(gx2), str(gy2)])
    _run(["mouseup", str(b)])


def scroll(gx: int, gy: int, direction: str = "down", amount: int = 3) -> None:
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    b = 5 if direction == "down" else 4  # X wheel buttons: 4 up, 5 down
    _run(
        ["mousemove", "--sync", str(gx), str(gy),
         "click", "--repeat", str(max(1, amount)), str(b)]
    )


def type_text(text: str) -> None:
    # NOTE: xdotool translates embedded '\n' into Return keypresses, so multi-line
    # text submits line-by-line in single-line fields. Callers should be aware.
    _run(["type", "--clearmodifiers", "--", text])


def key(keyseq: str) -> None:
    # xdotool key syntax, e.g. "ctrl+s", "Return", "Escape", "alt+Tab".
    _run(["key", "--clearmodifiers", keyseq])


def mouse_location() -> dict:
    r = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"],
        capture_output=True,
        text=True,
        check=True,
        timeout=CMD_TIMEOUT,
    )
    d: dict = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.lower()] = int(v) if v.lstrip("-").isdigit() else v
    return d


def reset_idle() -> None:
    """Best-effort: reset the X screensaver/DPMS idle timer.

    Synthetic xdotool input does NOT reset the idle timer, so a long automated
    session self-locks (xss-lock -> xsecurelock). Calling this at the start of
    each tool keeps the screen awake *while actively driving*. It deliberately
    does NOT globally disable the screensaver/DPMS (`xset s off`), so the desktop
    still locks normally once the tool goes idle — and there's no state to
    restore on crash (no fail-open). Errors (e.g. no `xset`) are swallowed.
    """
    for argv in (["xset", "s", "reset"], ["xset", "dpms", "force", "on"]):
        try:
            subprocess.run(argv, capture_output=True, timeout=CMD_TIMEOUT)
        except Exception:
            pass
