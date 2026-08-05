"""Wayland clipboard I/O via wl-clipboard — exact text in/out across apps (no OCR).

Kept separate from inputs.py: this is clipboard I/O, not input synthesis. Targets
$WAYLAND_DISPLAY (wl-copy / wl-paste). write_clipboard never echoes the value it wrote.
"""
from __future__ import annotations

import subprocess

CMD_TIMEOUT = 5  # seconds; bound a hung wl-clipboard call so it can't wedge a worker thread
_SELECTIONS = ("clipboard", "primary")


class ClipboardError(RuntimeError):
    """A wl-clipboard operation failed."""


def _sel(selection: str) -> str:
    s = str(selection).lower()
    if s not in _SELECTIONS:
        raise ValueError(
            f"selection must be one of {list(_SELECTIONS)}, got {selection!r}"
        )
    return s


def _run(argv: list[str], *, input: str | None = None, capture: bool = True):
    """Run a wl-clipboard command. capture=True pipes stdout/stderr (read path);
    capture=False routes them to DEVNULL (write path) so wl-copy's backgrounded
    selection-holder can't keep a captured pipe open and hang us until timeout."""
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        return subprocess.run(
            argv,
            input=input,
            text=True,
            stdout=pipe,
            stderr=pipe,
            timeout=CMD_TIMEOUT,
        )
    except FileNotFoundError:
        raise ClipboardError(
            "wl-clipboard not found; install it (e.g. `pacman -S wl-clipboard`)"
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"wl-clipboard timed out after {CMD_TIMEOUT}s") from exc


def _paste_argv(sel: str) -> list[str]:
    return ["wl-paste", "-n"] + (["--primary"] if sel == "primary" else [])


def _copy_argv(sel: str) -> list[str]:
    return ["wl-copy"] + (["--primary"] if sel == "primary" else [])


def read_clipboard(selection: str = "clipboard") -> str:
    """Return the text of a Wayland selection ('clipboard' or 'primary').

    Returns "" when the selection is empty (wl-paste exits non-zero with
    "Nothing is copied"; treated as empty, not an error).
    """
    sel = _sel(selection)
    r = _run(_paste_argv(sel))
    return r.stdout if r.returncode == 0 else ""


def write_clipboard(text: str, selection: str = "clipboard", secret: bool = False) -> str:
    """Set a Wayland selection ('clipboard' or 'primary') to text; paste it with
    key()/click() afterward.

    Returns a length summary only — never the value. secret=True appends a
    [secret] marker and marks the value for redaction in the future task-runner
    action log; it does NOT hide `text` from the MCP tool call itself (which the
    client records), so it is not a substitute for treating the value as secret.
    """
    sel = _sel(selection)
    r = _run(_copy_argv(sel), input=text, capture=False)
    if r.returncode != 0:
        raise ClipboardError(f"wl-copy to {sel} failed (exit {r.returncode})")
    return f"wrote {len(text)} chars to {sel.upper()}" + (" [secret]" if secret else "")
