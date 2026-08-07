"""Tests for hyprcontrol dispatch builders (dispatch mocked; no compositor)."""
import subprocess

import pytest

from hypr_cua_mcp import hyprcontrol, hyprtree


def _cap_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(hyprcontrol, "_dispatch", lambda expr: calls.append(expr) or "ok")
    # Default: pointer unreadable -> focus ops skip the cursor restore, so tests that
    # only care about the focus expr stay hermetic. Save/restore tests override this.
    monkeypatch.setattr(hyprcontrol, "_cursorpos", lambda: None, raising=False)
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


def test_run_dispatch_blocks_exit_and_lua_variants(monkeypatch):
    _cap_dispatch(monkeypatch)
    for expr in ('hl.dsp.exit()', 'hl.dsp .exit({})', 'hl . dsp . exit()',
                 'hl.dsp["exit"]({})', "hl.dsp['exit']()"):
        with pytest.raises(hyprcontrol.HyprCommandError):
            hyprcontrol.run_dispatch(expr)


def test_ws_field_non_ascii_digit_is_quoted(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    hyprcontrol.goto_workspace("²")  # superscript-2: isdigit() True but not a Lua numeral
    assert calls == ['hl.dsp.focus({ workspace = "²" })']


def test_cursorpos_parses_hyprctl_output(monkeypatch):
    class R:
        returncode = 0
        stdout = "2872, 611\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert hyprcontrol._cursorpos() == (2872, 611)


def test_cursorpos_none_when_unreadable(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert hyprcontrol._cursorpos() is None


def test_goto_workspace_restores_cursor_after_focus(monkeypatch):
    # A background agent's goto_workspace must not yank the user's shared cursor:
    # Hyprland warps it on the focus (cursor:no_warps=false), so we warp it back.
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprcontrol, "_cursorpos", lambda: (2872, 611))
    hyprcontrol.goto_workspace(5)
    assert calls == [
        "hl.dsp.focus({ workspace = 5 })",
        "hl.dsp.cursor.move({ x = 2872, y = 611 })",
    ]


def test_goto_workspace_still_focuses_when_cursor_unreadable(monkeypatch):
    calls = _cap_dispatch(monkeypatch)  # _cursorpos -> None by default
    hyprcontrol.goto_workspace(5)
    assert calls == ["hl.dsp.focus({ workspace = 5 })"]  # focus happens, no restore


def test_focus_window_restores_cursor_and_still_reports_landing(monkeypatch):
    # The synthetic cursor.move restore does NOT re-trigger follow_mouse (verified on
    # the live compositor), so focus still lands on the target and stays there.
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprcontrol, "_cursorpos", lambda: (100, 200))
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a})
    monkeypatch.setattr(hyprtree, "get_focused_address", lambda: "0xaaa")
    assert hyprcontrol.focus_window("0xaaa") is True
    assert calls == [
        'hl.dsp.focus({ window = "address:0xaaa" })',
        "hl.dsp.cursor.move({ x = 100, y = 200 })",
    ]


def test_focus_window_restores_cursor_before_verifying_focus(monkeypatch):
    # Restore must happen BEFORE focus is verified, so the verdict reflects the final
    # state (cursor back where the user had it) rather than the mid-dispatch state.
    order = []
    monkeypatch.setattr(hyprcontrol, "_dispatch",
                        lambda expr: order.append(("dispatch", expr)) or "ok")
    monkeypatch.setattr(hyprcontrol, "_cursorpos", lambda: (1, 2))
    monkeypatch.setattr(hyprtree, "find_window", lambda a: {"id": a})
    monkeypatch.setattr(hyprtree, "get_focused_address",
                        lambda: order.append(("verify", None)) or "0xaaa")
    hyprcontrol.focus_window("0xaaa")
    assert order == [
        ("dispatch", 'hl.dsp.focus({ window = "address:0xaaa" })'),
        ("dispatch", "hl.dsp.cursor.move({ x = 1, y = 2 })"),
        ("verify", None),
    ]


def test_focus_window_missing_skips_focus_and_restore(monkeypatch):
    calls = _cap_dispatch(monkeypatch)
    monkeypatch.setattr(hyprcontrol, "_cursorpos", lambda: (1, 2))
    monkeypatch.setattr(hyprtree, "find_window", lambda a: None)
    assert hyprcontrol.focus_window("0xgone") is False
    assert calls == []  # no focus dispatch, no cursor.move, no needless warp


def test_eval_raises_on_error_line(monkeypatch):
    class R:
        returncode = 0
        stdout = "error: something broke"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(hyprcontrol.HyprCommandError):
        hyprcontrol._eval("hl.dsp.foo()")
