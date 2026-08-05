"""Drive terminals invisibly via a dedicated tmux socket — no window, no focus.

The copilot substrate: create / run / read / kill panes on an ISOLATED tmux
server (socket ``hypr-cua``), so the model can do terminal work with zero screen
footprint and never touch the user's real (default-socket) tmux sessions. A
detached session has no attached client and no Hyprland window at all, yet
``capture-pane`` still reads its output — that is the "work on a non-visible
workspace" path.

Targets are tmux session names (created via ``new_pane``); tmux resolves a bare
session name to its active pane. Invoked as ``tmux -L hypr-cua <args>`` via
subprocess (execs the real binary directly, so the user's zsh ``tmux`` plugin
alias never applies).
"""
from __future__ import annotations

import subprocess

SOCKET = "hypr-cua"   # dedicated tmux server — isolated from the user's own sessions
CMD_TIMEOUT = 10      # seconds; bound a hung tmux call so it can't wedge a worker thread

# One row per pane; tab-separated so fields with spaces (command) stay intact.
_LIST_FMT = "\t".join((
    "#{session_name}",
    "#{window_index}.#{pane_index}",
    "#{pane_current_command}",
    "#{pane_width}x#{pane_height}",
    "#{?pane_dead,dead,live}",
))
# "no server yet" is not an error — it just means no copilot panes exist.
_NO_SERVER = ("no server running", "error connecting", "no such file")


class TmuxError(RuntimeError):
    """A tmux command failed."""


def _tmux(args: list[str], *, input: str | None = None):
    """Run ``tmux -L hypr-cua <args>``. Returns the CompletedProcess (caller checks rc)."""
    try:
        return subprocess.run(
            ["tmux", "-L", SOCKET, *args],
            input=input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CMD_TIMEOUT,
        )
    except FileNotFoundError:
        raise TmuxError("tmux not found; install it (e.g. `pacman -S tmux`)")
    except subprocess.TimeoutExpired as exc:
        raise TmuxError(f"tmux timed out after {CMD_TIMEOUT}s") from exc


def _ok(r: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if r.returncode != 0:
        raise TmuxError(f"{what} failed: {(r.stderr or r.stdout or '').strip()}")
    return r


def _check_name(name: str) -> str:
    # ':' and '.' are tmux target separators; whitespace makes send-keys/targets ambiguous.
    if not name or any(c in name for c in ":. \t\n"):
        raise ValueError(
            f"invalid session name {name!r}: non-empty, and no ':', '.', or whitespace"
        )
    return name


def list_panes() -> list[dict]:
    """Every pane on the copilot socket: session, pane index, command, size, state.

    Returns [] when no copilot tmux server has been started yet (not an error).
    """
    r = _tmux(["list-panes", "-a", "-F", _LIST_FMT])
    if r.returncode != 0:
        if any(s in (r.stderr or "").lower() for s in _NO_SERVER):
            return []
        raise TmuxError(f"list-panes failed: {(r.stderr or '').strip()}")
    panes: list[dict] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        f = (line.split("\t") + ["", "", "", "", ""])[:5]
        panes.append({
            "session": f[0], "pane": f[1], "command": f[2],
            "size": f[3], "state": f[4],
        })
    return panes


def new_pane(name: str, command: str | None = None) -> str:
    """Create a detached session ``name`` running a shell (or ``command``). Returns name.

    Detached = no window/client anywhere; drive it with run()/send_keys(), read it
    with read_pane(). With ``command`` the session runs that process and ends when it
    exits — for a readable-after-exit job, start a shell and use run() instead.
    """
    _check_name(name)
    args = ["new-session", "-d", "-s", name]
    if command:
        args.append(command)
    _ok(_tmux(args), f"new-session {name}")
    return name


def run(target: str, command: str) -> str:
    """Type a shell command line into ``target`` and press Enter (read output later)."""
    # -l = literal: send the exact characters, so a command like "C-c foo" isn't
    # reinterpreted as a key name. Enter is a separate, interpreted keypress.
    _ok(_tmux(["send-keys", "-t", target, "-l", command]), f"run in {target}")
    _ok(_tmux(["send-keys", "-t", target, "Enter"]), f"enter in {target}")
    return f"ran in {target}: {command}"


def send_keys(target: str, keys: str) -> str:
    """Send interpreted tmux keys to ``target`` (e.g. 'C-c', 'Escape', 'Up', 'q')."""
    _ok(_tmux(["send-keys", "-t", target, keys]), f"send-keys to {target}")
    return f"sent to {target}: {keys}"


def read_pane(target: str, lines: int | None = None) -> str:
    """Capture ``target``'s text. With ``lines``, include that many rows of scrollback."""
    args = ["capture-pane", "-p", "-t", target]
    if lines:
        args += ["-S", f"-{int(lines)}"]
    return _ok(_tmux(args), f"capture-pane {target}").stdout


def kill(target: str) -> str:
    """Kill copilot session ``target``."""
    _ok(_tmux(["kill-session", "-t", target]), f"kill-session {target}")
    return f"killed {target}"
