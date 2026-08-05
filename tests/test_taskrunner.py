"""Tests for the task-runner channel (the tmux module is mocked; no real tmux server).

The task-runner is a thin layer over the tmux copilot channel, so these tests stub
`taskrunner.tmux.*` at the module boundary and never spawn tmux. They pin the two
things that matter: the completion-sentinel wrapper `start` builds, and how `status`
/`list` classify a captured pane into running | done(exit_code) | missing.
"""
import pytest

from hypr_cua_mcp import taskrunner, tmux


def _panes(*sessions):
    """A tmux.list_panes()-shaped row per session name."""
    return [
        {"session": s, "pane": "0.0", "command": "sleep",
         "size": "80x24", "state": "live"}
        for s in sessions
    ]


# --- start: sentinel-wrapped command on a prefixed session ------------------

def test_start_wraps_command_with_sentinel_and_creates_session(monkeypatch):
    calls = {}
    monkeypatch.setattr(tmux, "list_panes", lambda: [])
    monkeypatch.setattr(
        tmux, "new_pane",
        lambda name, command=None: calls.update(name=name, command=command) or name,
    )
    out = taskrunner.start("build", "make all")
    assert calls["name"] == "task-build"          # tasks live under a task- prefix
    cmd = calls["command"]
    assert "make all" in cmd                       # the user's command is embedded...
    assert taskrunner.SENTINEL in cmd              # ...followed by the completion sentinel
    assert "$?" in cmd                             # ...carrying the exit code
    assert "sleep" in cmd                          # ...then a keep-alive so it stays readable
    assert out == {"name": "build", "state": "running"}


def test_start_rejects_bad_names(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: [])
    monkeypatch.setattr(tmux, "new_pane",
                        lambda *a, **k: pytest.fail("must not create a badly named task"))
    for bad in ["a:b", "a.b", "a b", "", "a/b", "a\tb"]:
        with pytest.raises(ValueError):
            taskrunner.start(bad, "echo hi")


def test_start_rejects_empty_command(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: [])
    monkeypatch.setattr(tmux, "new_pane",
                        lambda *a, **k: pytest.fail("must not create a task with no command"))
    for empty in ["", "   ", "\n"]:
        with pytest.raises(ValueError):
            taskrunner.start("job", empty)


def test_start_rejects_duplicate(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-build"))
    monkeypatch.setattr(tmux, "new_pane",
                        lambda *a, **k: pytest.fail("must not clobber an existing task"))
    with pytest.raises(taskrunner.TaskError):
        taskrunner.start("build", "make all")


# --- status: classify the captured pane -------------------------------------

def test_status_done_success(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))
    monkeypatch.setattr(tmux, "read_pane",
                        lambda target, lines=None: "compiling\ndone\n\n__TASK_DONE__:0\n")
    st = taskrunner.status("job")
    assert st["name"] == "job"
    assert st["state"] == "done"
    assert st["exit_code"] == 0
    assert "done" in st["tail"]


def test_status_done_failure_carries_exit_code(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))
    monkeypatch.setattr(tmux, "read_pane",
                        lambda target, lines=None: "boom\n\n__TASK_DONE__:1\n")
    st = taskrunner.status("job")
    assert st["state"] == "done"
    assert st["exit_code"] == 1


def test_status_running_without_sentinel(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))
    monkeypatch.setattr(tmux, "read_pane", lambda target, lines=None: "still working...\n")
    st = taskrunner.status("job")
    assert st["state"] == "running"
    assert "exit_code" not in st


def test_status_missing_when_session_absent(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-other"))
    monkeypatch.setattr(tmux, "read_pane",
                        lambda *a, **k: pytest.fail("a missing task must not be read"))
    st = taskrunner.status("job")
    assert st == {"name": "job", "state": "missing", "tail": ""}


def test_status_missing_when_pane_vanished_mid_read(monkeypatch):
    # Existed at list_panes time, gone by read_pane (raced with a stop/kill).
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))

    def gone(target, lines=None):
        raise tmux.TmuxError("can't find pane: task-job")

    monkeypatch.setattr(tmux, "read_pane", gone)
    assert taskrunner.status("job")["state"] == "missing"


def test_status_uses_last_sentinel_and_ignores_echoed_lookalike(monkeypatch):
    # A task that echoes a sentinel-lookalike mid-run, then really finishes (code 2).
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))
    body = "echo __TASK_DONE__:0\n__TASK_DONE__:0\nmore work\n__TASK_DONE__:2\n"
    monkeypatch.setattr(tmux, "read_pane", lambda target, lines=None: body)
    st = taskrunner.status("job")
    assert st["state"] == "done"
    assert st["exit_code"] == 2                     # last real sentinel line wins


def test_status_tail_limits_returned_lines(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: _panes("task-job"))
    body = "\n".join(f"line{i}" for i in range(50)) + "\n__TASK_DONE__:0\n"
    monkeypatch.setattr(tmux, "read_pane", lambda target, lines=None: body)
    st = taskrunner.status("job", tail=3)
    tail_lines = st["tail"].splitlines()
    assert len(tail_lines) == 3
    assert tail_lines[-1] == "__TASK_DONE__:0"


def test_status_rejects_bad_name():
    with pytest.raises(ValueError):
        taskrunner.status("a b")


# --- list: one entry per task session, each classified ----------------------

def test_list_derives_state_and_ignores_non_task_sessions(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes",
                        lambda: _panes("task-a", "task-b", "plainshell"))

    def fake_read(target, lines=None):
        if target == "task-a":
            return "compiled\n__TASK_DONE__:0\n"
        if target == "task-b":
            return "working\n"
        pytest.fail(f"non-task session read: {target}")

    monkeypatch.setattr(tmux, "read_pane", fake_read)
    by_name = {t["name"]: t for t in taskrunner.list()}
    assert set(by_name) == {"a", "b"}               # the plain tmux session is not a task
    assert by_name["a"]["state"] == "done" and by_name["a"]["exit_code"] == 0
    assert by_name["b"]["state"] == "running"
    assert "exit_code" not in by_name["b"]


def test_list_empty_when_no_panes(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: [])
    assert taskrunner.list() == []


# --- send / stop: delegate to the tmux channel ------------------------------

def test_send_types_text_and_enter_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "run",
                        lambda target, command: seen.update(t=target, c=command) or "ran")
    monkeypatch.setattr(tmux, "send_keys",
                        lambda *a, **k: pytest.fail("default send answers a prompt via run()"))
    out = taskrunner.send("job", "yes")
    assert seen == {"t": "task-job", "c": "yes"}     # text + Enter into the running task
    assert out == "ran"


def test_send_raw_keys_when_enter_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "send_keys",
                        lambda target, keys: seen.update(t=target, k=keys) or "sent")
    monkeypatch.setattr(tmux, "run",
                        lambda *a, **k: pytest.fail("enter=False sends interpreted keys"))
    taskrunner.send("job", "C-c", enter=False)
    assert seen == {"t": "task-job", "k": "C-c"}     # control sequence, no Enter


def test_stop_kills_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "kill", lambda target: seen.update(t=target) or "killed")
    out = taskrunner.stop("job")
    assert seen == {"t": "task-job"}
    assert "job" in out


def test_send_and_stop_reject_bad_names():
    for bad in ["a b", "a:b", ""]:
        with pytest.raises(ValueError):
            taskrunner.send(bad, "x")
        with pytest.raises(ValueError):
            taskrunner.stop(bad)
