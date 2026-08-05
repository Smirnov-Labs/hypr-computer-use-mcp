"""Tests for i3control: guarded run_i3 + workspace/launch/layout wrappers."""
import pytest

from hypr_cua_mcp import i3control, i3tree


class _Reply:
    def __init__(self, success, error=None):
        self.success = success
        self.error = error


class _Conn:
    def __init__(self, replies):
        self._replies = replies
        self.cmds = []

    def command(self, s):
        self.cmds.append(s)
        return self._replies


def _patch(monkeypatch, replies):
    conn = _Conn(replies)
    monkeypatch.setattr(i3tree, "_conn", lambda: conn)
    return conn


def test_run_i3_blocks_exit(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    with pytest.raises(i3control.I3CommandError):
        i3control.run_i3("exit")
    assert conn.cmds == []  # never reached i3


def test_run_i3_blocks_exit_in_chained_command(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    with pytest.raises(i3control.I3CommandError):
        i3control.run_i3("workspace number 3; exit")
    assert conn.cmds == []


def test_run_i3_runs_allowed_command(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    i3control.run_i3("workspace number 3")
    assert conn.cmds == ["workspace number 3"]


def test_run_i3_raises_on_failure_reply(monkeypatch):
    _patch(monkeypatch, [_Reply(False, "boom")])
    with pytest.raises(i3control.I3CommandError):
        i3control.run_i3("bogus command")


def test_goto_workspace_numeric_uses_number(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    i3control.goto_workspace("5")
    assert conn.cmds == ["workspace number 5"]


def test_goto_workspace_named_is_quoted(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    i3control.goto_workspace("slack")
    assert conn.cmds == ['workspace "slack"']


def test_launch_builds_exec(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    i3control.launch("xterm -name foo")
    assert conn.cmds == ["exec xterm -name foo"]


def test_set_layout_rejects_unknown(monkeypatch):
    _patch(monkeypatch, [_Reply(True)])
    with pytest.raises(ValueError):
        i3control.set_layout(1, "pancake")


def test_move_to_workspace_checks_window_exists(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    monkeypatch.setattr(i3tree, "find_window", lambda cid: None)
    with pytest.raises(ValueError):
        i3control.move_window_to_workspace(999, "5")
    assert conn.cmds == []  # bailed before issuing the move


def test_move_to_workspace_reports_actual_placement(monkeypatch):
    conn = _patch(monkeypatch, [_Reply(True)])
    monkeypatch.setattr(i3tree, "find_window", lambda cid: {"id": cid, "workspace": "5"})
    out = i3control.move_window_to_workspace(42, "5")
    assert "5" in out
    assert conn.cmds == ['[con_id=42] move to workspace number 5']
