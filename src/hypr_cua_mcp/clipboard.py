"""X clipboard I/O via xclip — exact text in/out across apps (no OCR).

Kept separate from inputs.py: this is X-selection I/O, not xdotool input
synthesis. DISPLAY-faithful (xclip targets $DISPLAY). write_clipboard never
echoes the value it wrote.
"""
from __future__ import annotations

import subprocess

CMD_TIMEOUT = 5  # seconds; bound a hung xclip so it can't wedge a worker thread
_SELECTIONS = ("clipboard", "primary")


class ClipboardError(RuntimeError):
    """An xclip clipboard operation failed."""


def _sel(selection: str) -> str:
    s = str(selection).lower()
    if s not in _SELECTIONS:
        raise ValueError(
            f"selection must be one of {list(_SELECTIONS)}, got {selection!r}"
        )
    return s


def _run(args: list[str], *, input: str | None = None, capture: bool = True):
    """Run xclip. capture=True pipes stdout/stderr (read path); capture=False
    routes them to DEVNULL (write path) so xclip's backgrounded selection-holder
    can't keep a captured pipe open and hang us until timeout."""
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        return subprocess.run(
            ["xclip", *args],
            input=input,
            text=True,
            stdout=pipe,
            stderr=pipe,
            timeout=CMD_TIMEOUT,
        )
    except FileNotFoundError:
        raise ClipboardError("xclip not found; install it (e.g. `pacman -S xclip`)")
    except subprocess.TimeoutExpired as e:
        raise ClipboardError(f"xclip timed out after {CMD_TIMEOUT}s") from e


def read_clipboard(selection: str = "clipboard") -> str:
    """Return the text of an X selection ('clipboard' or 'primary').

    Returns "" when the selection is empty or unowned (xclip exits non-zero with
    no owner; treated as empty, not an error).
    """
    sel = _sel(selection)
    r = _run(["-selection", sel, "-o"])
    return r.stdout if r.returncode == 0 else ""


def write_clipboard(text: str, selection: str = "clipboard", secret: bool = False) -> str:
    """Set an X selection ('clipboard' or 'primary') to text; paste it with
    key()/click() afterward.

    Returns a length summary only — never the value. secret=True appends a
    [secret] marker and marks the value for redaction in the future task-runner
    action log; it does NOT hide `text` from the MCP tool call itself (which the
    client records), so it is not a substitute for treating the value as secret.
    """
    sel = _sel(selection)
    r = _run(["-selection", sel, "-i"], input=text, capture=False)
    if r.returncode != 0:
        raise ClipboardError(f"xclip write to {sel} failed (exit {r.returncode})")
    return f"wrote {len(text)} chars to {sel.upper()}" + (" [secret]" if secret else "")
