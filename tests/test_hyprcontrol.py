"""Tests for hyprcontrol dispatch builders (dispatch mocked; no compositor)."""
import subprocess

import pytest

from hypr_cua_mcp import hyprcontrol, hyprtree


def _cap_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(hyprcontrol, "_dispatch", lambda expr: calls.append(expr) or "ok")
    return calls


def test_goto_workspace_numeric(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    hyprcontrol.goto_workspace(5)
    assert calls == ["hl.dsp.focus({ workspace = 5 })"]


def test_goto_workspace_named(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    hyprcontrol.goto_workspace("mail")
    assert calls == ['hl.dsp.focus({ workspace = "mail" })']


def test_focus_window_verifies_landing(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a})
    monkeypatch.setattr(hyprtree, "get_focused_address", lambda: "0xaaa")
    assert hyprcontrol.focus_window("0xaaa") is True
    assert calls == ['hl.dsp.focus({ window = "address:0xaaa" })']


def test_focus_window_false_when_focus_did_not_land(monkeypatch):
    _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a})
    monkeypatch.setattr(hyprtree, "get_focused_address", lambda: "0xother")
    assert hyprcontrol.focus_window("0xaaa") is False


def test_focus_window_false_when_missing(monkeypatch):
    _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: None)
    assert hyprcontrol.focus_window("0xgone") is False


def test_move_window_to_workspace_builds_expr(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a, "workspace": "5"})
    out = hyprcontrol.move_window_to_workspace("0xaaa", 5)
    assert 'hl.dsp.window.move({ workspace = 5, follow = false, window = "address:0xaaa" })' in calls
    assert "workspace 5" in out


def test_move_window_raises_for_missing(monkeypatch):
    _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: None)
    with pytest.raises(ValueError):
        hyprcontrol.move_window_to_workspace("0xgone", 5)


def test_set_floating_toggles_when_state_differs(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a, "floating": False})
    hyprcontrol.set_floating("0xaaa", True)
    assert 'hl.dsp.window.float({ action = "toggle", window = "address:0xaaa" })' in calls


def test_set_floating_noop_when_already_in_state(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a, "floating": True})
    out = hyprcontrol.set_floating("0xaaa", True)
    assert calls == []  # no dispatch needed
    assert "already" in out


def test_launch_builds_exec_cmd(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    hyprcontrol.launch("alacritty -e htop")
    assert calls == ['hl.dsp.exec_cmd("alacritty -e htop")']


def test_layout_message(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    hyprcontrol.layout_message("togglesplit")
    assert calls == ['hl.dsp.layout("togglesplit")']


def test_run_dispatch_blocks_exit(monkeypatch):
    _cap_dispatch(monkeypatch)
    with pytest.raises(hyprcontrol.HyprCommandError):
        hyprcontrol.run_dispatch("hl.dsp.exit()")


def test_eval_raises_on_error_line(monkeypatch):
    class R:
        returncode = 0
        stdout = "error: something broke"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(hyprcontrol.HyprCommandError):
        hyprcontrol._eval("hl.dsp.foo()")
