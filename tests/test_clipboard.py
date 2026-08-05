"""Tests for clipboard read + helpers (subprocess mocked; no X, no xclip needed)."""
import subprocess

import pytest

from hypr_cua_mcp import clipboard


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["xclip"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_read_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(clipboard, "_run", lambda args, **kw: _completed(0, stdout="cfut_ABC"))
    assert clipboard.read_clipboard() == "cfut_ABC"


def test_read_returns_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(clipboard, "_run", lambda args, **kw: _completed(1, stderr="no owner"))
    assert clipboard.read_clipboard() == ""


def test_read_passes_selection(monkeypatch):
    seen = {}
    monkeypatch.setattr(clipboard, "_run",
                        lambda args, **kw: seen.update(args=args) or _completed(0, "x"))
    clipboard.read_clipboard("primary")
    assert seen["args"] == ["-selection", "primary", "-o"]


def test_read_rejects_invalid_selection():
    with pytest.raises(ValueError):
        clipboard.read_clipboard("middle")


def test_run_sets_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return _completed(0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    clipboard._run(["-selection", "clipboard", "-o"])
    assert seen.get("timeout")


def test_run_raises_when_xclip_missing(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError("no xclip")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(clipboard.ClipboardError):
        clipboard._run(["-selection", "clipboard", "-o"])


def test_run_wraps_timeout_in_clipboarderror(monkeypatch):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=clipboard.CMD_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(clipboard.ClipboardError):
        clipboard._run(["-selection", "clipboard", "-o"])


def test_write_returns_char_count_and_excludes_value(monkeypatch):
    monkeypatch.setattr(clipboard, "_run", lambda args, **kw: _completed(0))
    out = clipboard.write_clipboard("s3kr3t-token")
    assert "12 chars" in out
    assert "s3kr3t-token" not in out


def test_write_secret_marks_and_excludes_value(monkeypatch):
    monkeypatch.setattr(clipboard, "_run", lambda args, **kw: _completed(0))
    out = clipboard.write_clipboard("s3kr3t", secret=True)
    assert "[secret]" in out
    assert "s3kr3t" not in out


def test_write_passes_selection_input_and_devnull(monkeypatch):
    seen = {}

    def fake(args, **kw):
        seen.update(args=args, input=kw.get("input"), capture=kw.get("capture"))
        return _completed(0)

    monkeypatch.setattr(clipboard, "_run", fake)
    clipboard.write_clipboard("hello", selection="primary")
    assert seen["args"] == ["-selection", "primary", "-i"]
    assert seen["input"] == "hello"
    assert seen["capture"] is False  # DEVNULL path so the holder can't hang us


def test_write_rejects_invalid_selection():
    with pytest.raises(ValueError):
        clipboard.write_clipboard("x", selection="middle")


def test_write_raises_on_nonzero_without_leaking_text(monkeypatch):
    monkeypatch.setattr(clipboard, "_run", lambda args, **kw: _completed(2, stderr="boom"))
    with pytest.raises(clipboard.ClipboardError) as e:
        clipboard.write_clipboard("s3kr3t-value")
    assert "s3kr3t-value" not in str(e.value)


def test_write_routes_subprocess_to_devnull(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return _completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    clipboard.write_clipboard("x")
    assert seen.get("stdout") is subprocess.DEVNULL
    assert seen.get("stderr") is subprocess.DEVNULL
