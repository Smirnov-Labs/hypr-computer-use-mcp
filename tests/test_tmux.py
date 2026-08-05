"""Tests for the tmux copilot channel (subprocess mocked; no real tmux server)."""
import subprocess

import pytest

from hypr_cua_mcp import tmux


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- list_panes ---

def test_list_panes_parses_rows(monkeypatch):
    out = "work\t0.0\tzsh\t200x50\tlive\nbuild\t1.0\tnpm\t80x24\tlive\n"
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: _cp(0, stdout=out))
    panes = tmux.list_panes()
    assert panes[0] == {"session": "work", "pane": "0.0", "command": "zsh",
                        "size": "200x50", "state": "live"}
    assert panes[1]["session"] == "build" and panes[1]["command"] == "npm"


def test_list_panes_empty_when_no_server(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux",
                        lambda args, **k: _cp(1, stderr="no server running on /tmp/tmux/hypr-cua"))
    assert tmux.list_panes() == []  # not-yet-started server is "no panes", not an error


def test_list_panes_raises_on_real_error(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: _cp(1, stderr="boom"))
    with pytest.raises(tmux.TmuxError):
        tmux.list_panes()


# --- new_pane ---

def test_new_pane_builds_detached_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: seen.update(args=args) or _cp(0))
    assert tmux.new_pane("work") == "work"
    assert seen["args"] == ["new-session", "-d", "-s", "work"]


def test_new_pane_with_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: seen.update(args=args) or _cp(0))
    tmux.new_pane("srv", "npm run dev")
    assert seen["args"] == ["new-session", "-d", "-s", "srv", "npm run dev"]


def test_new_pane_rejects_bad_names():
    for bad in ["a:b", "a.b", "a b", ""]:
        with pytest.raises(ValueError):
            tmux.new_pane(bad)


def test_new_pane_raises_on_duplicate(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux",
                        lambda args, **k: _cp(1, stderr="duplicate session: work"))
    with pytest.raises(tmux.TmuxError):
        tmux.new_pane("work")


# --- run vs send_keys ---

def test_run_sends_literal_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: calls.append(args) or _cp(0))
    tmux.run("work", "echo hi")
    assert calls[0] == ["send-keys", "-t", "work", "-l", "echo hi"]  # -l = literal, no key lookup
    assert calls[1] == ["send-keys", "-t", "work", "Enter"]


def test_send_keys_is_interpreted(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: seen.update(args=args) or _cp(0))
    tmux.send_keys("work", "C-c")  # control sequence — must NOT be -l
    assert seen["args"] == ["send-keys", "-t", "work", "C-c"]


def test_run_raises_for_missing_target(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: _cp(1, stderr="can't find pane: gone"))
    with pytest.raises(tmux.TmuxError):
        tmux.run("gone", "echo hi")


# --- read_pane ---

def test_read_pane_with_scrollback(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux",
                        lambda args, **k: seen.update(args=args) or _cp(0, stdout="l1\nl2\n"))
    out = tmux.read_pane("work", lines=100)
    assert seen["args"] == ["capture-pane", "-p", "-t", "work", "-S", "-100"]
    assert out == "l1\nl2\n"


def test_read_pane_default_visible_only(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: seen.update(args=args) or _cp(0, stdout="x"))
    tmux.read_pane("work")
    assert seen["args"] == ["capture-pane", "-p", "-t", "work"]


# --- kill ---

def test_kill_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "_tmux", lambda args, **k: seen.update(args=args) or _cp(0))
    tmux.kill("work")
    assert seen["args"] == ["kill-session", "-t", "work"]


# --- _tmux transport: dedicated socket, missing binary, timeout ---

def test_tmux_uses_dedicated_socket(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.update(argv=argv) or _cp(0))
    tmux._tmux(["ls"])
    assert seen["argv"][:3] == ["tmux", "-L", tmux.SOCKET]  # isolated from the user's real sessions


def test_tmux_raises_when_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no tmux")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(tmux.TmuxError):
        tmux._tmux(["ls"])


def test_tmux_wraps_timeout(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=tmux.CMD_TIMEOUT)
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(tmux.TmuxError):
        tmux._tmux(["ls"])
