"""Launch a job and watch it work — hidden tmux tasks with detectable completion.

This is the task-runner channel: it turns the tmux copilot substrate (tmux.py)
into "start a job and let the model supervise it." A task runs as a detached tmux
session on the isolated ``hypr-cua`` socket — **no window, no focus, zero screen
footprint** — while the model polls its state, reads its output, answers prompts,
and stops it. It never touches the user's real (default-socket) sessions.

The hard part a raw ``tmux.new_pane(name, command)`` can't do is knowing when a job
*finished* and with what exit code: a bare command session is torn down by tmux the
instant the command exits, taking its output with it. So ``start`` wraps the command
to make completion **detectable and durable**::

    sh -c '<command>'; printf '\\n__TASK_DONE__:%s\\n' "$?"; exec sleep <forever>

- the command runs to completion in its **own subshell** (so a bare ``exit`` /
  ``exec`` in it can't skip the rest of the wrapper), then
- a **sentinel line** ``__TASK_DONE__:<exit_code>`` is printed (``$?`` captured
  before anything else can clobber it), then
- ``exec sleep`` keeps the pane alive so that sentinel stays readable after the
  command exits. (The pane is reaped by ``stop`` / socket teardown.)

``status`` / ``list`` then classify a captured pane purely from that sentinel:
running (not printed yet) · done+exit_code (printed) · missing (no such session).
Tasks live under a ``task-`` session prefix so they're distinct from plain
tmux-channel sessions on the same copilot socket.

Synchronous and dependency-free: a thin policy layer over tmux.py (which execs the
real ``tmux`` binary). All tmux I/O goes through that module so it stays the single
mocked boundary in tests.
"""
from __future__ import annotations

import re
import shlex

from . import tmux

SESSION_PREFIX = "task-"          # namespaces tasks apart from plain tmux-channel sessions
SENTINEL = "__TASK_DONE__"        # printed with the exit code when the command finishes
READ_LINES = 200                  # scrollback captured for classification (sentinel is last)
TAIL_LINES = 20                   # default rows of tail returned by status()
KEEPALIVE_SECONDS = 2147483647    # ~68y; keeps the pane readable after the command exits

# The real sentinel is a whole line ``__TASK_DONE__:<code>`` (printf puts it on its
# own line); requiring a full-line match ignores the string appearing mid-output.
_DONE_RE = re.compile(rf"^{re.escape(SENTINEL)}:(\d+)$")


class TaskError(RuntimeError):
    """A task-runner operation failed (bad/duplicate name, missing task, ...)."""


# --- names / sessions -------------------------------------------------------

def _check_name(name: str) -> str:
    # ':' '.' are tmux target separators and whitespace makes targets ambiguous;
    # '/' is rejected too so a name can never look like a path.
    if not name or any(c in name for c in ":. \t\n/"):
        raise ValueError(
            f"invalid task name {name!r}: must be non-empty with no ':', '.', '/', or whitespace"
        )
    return name


def _session(name: str) -> str:
    """tmux session name for a task (``task-<name>``); validates ``name`` first."""
    return SESSION_PREFIX + _check_name(name)


def _sessions() -> set[str]:
    """Set of session names currently on the copilot socket."""
    return {p.get("session", "") for p in tmux.list_panes()}


def _wrap(command: str) -> str:
    """Wrap ``command`` so completion lands a sentinel + exit code, then keep the
    pane alive so that sentinel survives the command exiting.

    The command runs in its OWN ``sh -c`` subshell so that a bare ``exit`` / ``exec``
    inside it only affects that child — the outer shell still captures its exit code,
    prints the sentinel, and keeps the pane readable. (Backgrounding, ``cmd &``, is
    inherently uncatchable: the subshell returns immediately, so the task reads done.)
    """
    return (
        f"sh -c {shlex.quote(command)}; "
        f"printf '\\n{SENTINEL}:%s\\n' \"$?\"; "
        f"exec sleep {KEEPALIVE_SECONDS}"
    )


# --- classification ---------------------------------------------------------

def _classify(text: str) -> tuple[str, int | None]:
    """('done', exit_code) if the sentinel is present, else ('running', None).

    The **last** matching line wins, so a task that happens to print a
    sentinel-shaped line earlier can't pre-empt the real completion marker.
    """
    exit_code = None
    for line in text.splitlines():
        m = _DONE_RE.match(line.strip())
        if m:
            exit_code = int(m.group(1))
    if exit_code is None:
        return "running", None
    return "done", exit_code


def _tail_of(text: str, n: int) -> str:
    lines = text.splitlines()
    # tmux capture-pane pads the pane grid with trailing blank rows; drop them so
    # the tail shows the actual last output (e.g. the sentinel), not empty lines.
    while lines and not lines[-1].strip():
        lines.pop()
    if n is None or n <= 0:
        return "\n".join(lines)
    return "\n".join(lines[-n:])


# --- operations -------------------------------------------------------------

def start(name: str, command: str) -> dict:
    """Launch ``command`` as task ``name`` in its own hidden tmux session.

    The command is sentinel-wrapped so ``status`` can later detect completion and
    its exit code. Raises ValueError for an invalid name / empty command, and
    TaskError if a task by that name already exists. Returns {name, state}.
    """
    session = _session(name)
    if not command or not str(command).strip():
        raise ValueError("command must be a non-empty string")
    if session in _sessions():
        raise TaskError(f"task {name!r} already exists (stop it first)")
    tmux.new_pane(session, _wrap(command))
    return {"name": name, "state": "running"}


def status(name: str, tail: int = TAIL_LINES) -> dict:
    """State of task ``name``: {name, state, exit_code?, tail}.

    state is ``running`` (sentinel not printed yet), ``done`` (with ``exit_code``,
    success == 0), or ``missing`` (no such task session). ``tail`` is the last
    ``tail`` lines of captured output.
    """
    session = _session(name)
    if session not in _sessions():
        return {"name": name, "state": "missing", "tail": ""}
    try:
        text = tmux.read_pane(session, lines=READ_LINES)
    except tmux.TmuxError:
        # Raced with a stop/kill between listing and reading — treat as gone.
        return {"name": name, "state": "missing", "tail": ""}
    state, exit_code = _classify(text)
    out = {"name": name, "state": state, "tail": _tail_of(text, tail)}
    if exit_code is not None:
        out["exit_code"] = exit_code
    return out


def list() -> list[dict]:  # noqa: A001 - deliberate public API name (task_list)
    """Every task on the copilot socket with its state: [{name, state, exit_code?}].

    Derived from ``tmux.list_panes()`` — only ``task-`` sessions are reported
    (plain tmux-channel sessions are skipped), each classified like ``status``.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for pane in tmux.list_panes():
        session = pane.get("session", "")
        if not session.startswith(SESSION_PREFIX) or session in seen:
            continue
        seen.add(session)
        try:
            text = tmux.read_pane(session, lines=READ_LINES)
        except tmux.TmuxError:
            continue  # vanished mid-iteration; leave it out
        state, exit_code = _classify(text)
        entry = {"name": session[len(SESSION_PREFIX):], "state": state}
        if exit_code is not None:
            entry["exit_code"] = exit_code
        out.append(entry)
    return out


def send(name: str, text: str, enter: bool = True) -> str:
    """Send input to a running task — e.g. answer an interactive prompt.

    Default (``enter=True``) types ``text`` and presses Enter (delegates to
    ``tmux.run``). ``enter=False`` sends interpreted tmux keys (``tmux.send_keys``)
    for control sequences like 'C-c' / 'Up' / 'Escape'.
    """
    session = _session(name)
    if enter:
        return tmux.run(session, text)
    return tmux.send_keys(session, text)


def stop(name: str) -> str:
    """Stop task ``name`` by killing its tmux session (delegates to ``tmux.kill``)."""
    session = _session(name)
    tmux.kill(session)
    return f"stopped task {name}"
