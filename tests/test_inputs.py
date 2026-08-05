"""Tests for inputs: validation, timeouts, idle-reset (all subprocess mocked)."""
import subprocess

import pytest

from hypr_cua_mcp import inputs


def test_click_rejects_unknown_button(monkeypatch):
    monkeypatch.setattr(inputs, "_run", lambda args: None)
    with pytest.raises(ValueError):
        inputs.click(10, 10, button="purple")


def test_scroll_rejects_unknown_direction(monkeypatch):
    monkeypatch.setattr(inputs, "_run", lambda args: None)
    with pytest.raises(ValueError):
        inputs.scroll(10, 10, direction="sideways")


def test_scroll_down_uses_wheel_button_5(monkeypatch):
    calls = []
    monkeypatch.setattr(inputs, "_run", lambda args: calls.append(args))
    inputs.scroll(10, 10, direction="down")
    assert "5" in calls[0]


def test_scroll_up_uses_wheel_button_4(monkeypatch):
    calls = []
    monkeypatch.setattr(inputs, "_run", lambda args: calls.append(args))
    inputs.scroll(10, 10, direction="up")
    assert "4" in calls[0]


def test_run_sets_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    inputs._run(["mousemove", "0", "0"])
    assert seen.get("timeout")


def test_run_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: type("R", (), {"returncode": 1, "stderr": "boom"})())
    with pytest.raises(RuntimeError):
        inputs._run(["key", "x"])


def test_reset_idle_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no xset")

    monkeypatch.setattr(subprocess, "run", boom)
    inputs.reset_idle()  # must not raise
