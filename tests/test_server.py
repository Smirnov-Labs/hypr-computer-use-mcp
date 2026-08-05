"""Tests for server tool wiring: focus-verify safety, frame-id contract, errors."""
import pytest

from hypr_cua_mcp import clipboard, coords, hyprcontrol, hyprtree, inputs, server, tmux


def _frame(**kw):
    d = dict(region_x=100, region_y=100, region_w=200, region_h=200,
             scale_x=1.0, scale_y=1.0, shot_w=200, shot_h=200, label="w")
    d.update(kw)
    return coords.CaptureFrame(**d)


# --- keyboard input must never fire when focus can't be verified ---

def test_type_text_raises_and_does_not_type_when_focus_fails(monkeypatch):
    monkeypatch.setattr(hyprtree, "find_window", lambda cid: {"id": cid})
    monkeypatch.setattr(hyprcontrol, "focus_window", lambda cid: False)  # focus didn't land
    typed = []
    monkeypatch.setattr(inputs, "type_text", lambda t: typed.append(t))
    with pytest.raises(Exception):
        server.type_text("rm -rf /", con_id="0xabc")
    assert typed == []  # the whole point: no keystrokes went anywhere


def test_type_text_raises_for_missing_window(monkeypatch):
    monkeypatch.setattr(hyprtree, "find_window", lambda cid: None)
    with pytest.raises(Exception):
        server.type_text("x", con_id="0xgone")


# --- pointer tools require a valid, current capture frame ---

def test_click_without_screenshot_raises(monkeypatch):
    monkeypatch.setattr(coords, "frames", coords.FrameStore())  # no capture yet
    with pytest.raises(coords.NoActiveCapture):
        server.click(10, 10)


def test_click_rejects_stale_frame_id(monkeypatch):
    store = coords.FrameStore()
    store.set_last(_frame())
    monkeypatch.setattr(coords, "frames", store)
    monkeypatch.setattr(inputs, "click", lambda *a, **k: pytest.fail("must not click"))
    with pytest.raises(coords.StaleFrame):
        server.click(10, 10, frame_id="cap-bogus")


def test_click_maps_through_resolved_frame(monkeypatch):
    store = coords.FrameStore()
    fid = store.set_last(_frame())  # region origin (100,100), scale 1.0
    monkeypatch.setattr(coords, "frames", store)
    got = {}
    monkeypatch.setattr(inputs, "click",
                        lambda gx, gy, button="left": got.update(gx=gx, gy=gy))
    server.click(10, 10, frame_id=fid)
    assert (got["gx"], got["gy"]) == (110, 110)


# --- move_mouse(follow=...) : opt-in hover-focus ---

def test_move_mouse_follow_focuses_window_under_cursor(monkeypatch):
    store = coords.FrameStore()
    fid = store.set_last(_frame())  # origin (100,100), scale 1.0
    monkeypatch.setattr(coords, "frames", store)
    moved = {}
    monkeypatch.setattr(inputs, "move", lambda gx, gy: moved.update(gx=gx, gy=gy))
    monkeypatch.setattr(hyprtree, "window_at", lambda gx, gy: {"id": "0xwin", "wm_class": "X"})
    focused = {}
    monkeypatch.setattr(hyprcontrol, "focus_window",
                        lambda cid: focused.update(cid=cid) or True)
    out = server.move_mouse(10, 10, frame_id=fid, follow=True)
    assert (moved["gx"], moved["gy"]) == (110, 110)  # still warps
    assert focused == {"cid": "0xwin"}               # and focuses what's there
    assert "0xwin" in out


def test_move_mouse_default_does_not_focus(monkeypatch):
    store = coords.FrameStore()
    fid = store.set_last(_frame())
    monkeypatch.setattr(coords, "frames", store)
    monkeypatch.setattr(inputs, "move", lambda gx, gy: None)
    monkeypatch.setattr(hyprcontrol, "focus_window",
                        lambda cid: pytest.fail("must not focus when follow is off"))
    server.move_mouse(10, 10, frame_id=fid)  # follow defaults False


def test_move_mouse_follow_noop_on_empty_space(monkeypatch):
    store = coords.FrameStore()
    fid = store.set_last(_frame())
    monkeypatch.setattr(coords, "frames", store)
    monkeypatch.setattr(inputs, "move", lambda gx, gy: None)
    monkeypatch.setattr(hyprtree, "window_at", lambda gx, gy: None)
    monkeypatch.setattr(hyprcontrol, "focus_window",
                        lambda cid: pytest.fail("no window under cursor -> no focus"))
    out = server.move_mouse(10, 10, frame_id=fid, follow=True)
    assert "no window" in out


# --- validation failures raise (SDK turns them into is_error) ---

def test_screenshot_unknown_output_raises(monkeypatch):
    monkeypatch.setattr(hyprtree, "get_outputs",
                        lambda: [{"name": "DP-1", "primary": True,
                                  "rect": {"x": 0, "y": 0, "w": 100, "h": 100}}])
    with pytest.raises(ValueError):
        server.screenshot(output="NOPE")


# --- hyprcontrol wiring ---

def test_launch_resets_idle_and_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(inputs, "reset_idle", lambda: seen.setdefault("idle", True))
    monkeypatch.setattr(hyprcontrol, "launch", lambda cmd: seen.setdefault("cmd", cmd) or "ok")
    server.launch("alacritty")
    assert seen == {"idle": True, "cmd": "alacritty"}


def test_move_window_to_workspace_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(hyprcontrol, "move_window_to_workspace",
                        lambda cid, ws: seen.update(cid=cid, ws=ws) or "moved")
    server.move_window_to_workspace("0xabc", "5")
    assert seen == {"cid": "0xabc", "ws": "5"}


# --- clipboard wiring (delegate; write muzzles the value) ---

def test_read_clipboard_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(clipboard, "read_clipboard",
                        lambda sel: seen.update(sel=sel) or "cfut_X")
    assert server.read_clipboard("primary") == "cfut_X"
    assert seen == {"sel": "primary"}


def test_write_clipboard_delegates_and_hides_value(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        clipboard, "write_clipboard",
        lambda text, sel, secret: seen.update(text=text, sel=sel, secret=secret)
        or "wrote 3 chars to CLIPBOARD [secret]",
    )
    out = server.write_clipboard("abc", selection="clipboard", secret=True)
    assert seen == {"text": "abc", "sel": "clipboard", "secret": True}
    assert "abc" not in out


# --- tmux copilot wiring (delegate to the tmux module) ---

def test_tmux_run_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "run",
                        lambda target, command: seen.update(t=target, c=command) or "ran")
    assert server.tmux_run("work", "echo hi") == "ran"
    assert seen == {"t": "work", "c": "echo hi"}


def test_tmux_read_passes_lines(monkeypatch):
    seen = {}
    monkeypatch.setattr(tmux, "read_pane",
                        lambda target, lines: seen.update(t=target, n=lines) or "out")
    assert server.tmux_read("work", lines=120) == "out"
    assert seen == {"t": "work", "n": 120}


def test_tmux_list_delegates(monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: [{"session": "work"}])
    assert "work" in server.tmux_list()
