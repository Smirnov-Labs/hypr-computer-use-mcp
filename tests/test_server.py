"""Tests for server tool wiring: focus-verify safety, frame-id contract, errors."""
import pytest

from hypr_cua_mcp import clipboard, coords, i3control, i3tree, inputs, server


def _frame(**kw):
    d = dict(region_x=100, region_y=100, region_w=200, region_h=200,
             scale_x=1.0, scale_y=1.0, shot_w=200, shot_h=200, label="w")
    d.update(kw)
    return coords.CaptureFrame(**d)


# --- #1b: keyboard input must never fire when focus can't be verified ---

def test_type_text_raises_and_does_not_type_when_focus_fails(monkeypatch):
    monkeypatch.setattr(i3tree, "find_window", lambda cid: {"id": cid})
    monkeypatch.setattr(i3tree, "focus_window", lambda cid: False)  # focus didn't land
    typed = []
    monkeypatch.setattr(inputs, "type_text", lambda t: typed.append(t))
    monkeypatch.setattr(inputs, "reset_idle", lambda: None)
    with pytest.raises(Exception):
        server.type_text("rm -rf /", con_id=57)
    assert typed == []  # the whole point: no keystrokes went anywhere


def test_type_text_raises_for_missing_window(monkeypatch):
    monkeypatch.setattr(i3tree, "find_window", lambda cid: None)
    monkeypatch.setattr(inputs, "reset_idle", lambda: None)
    with pytest.raises(Exception):
        server.type_text("x", con_id=999)


# --- #5 / #4: pointer tools require a valid, current capture frame ---

def test_click_without_screenshot_raises(monkeypatch):
    monkeypatch.setattr(coords, "frames", coords.FrameStore())  # no capture yet
    monkeypatch.setattr(inputs, "reset_idle", lambda: None)
    with pytest.raises(coords.NoActiveCapture):
        server.click(10, 10)


def test_click_rejects_stale_frame_id(monkeypatch):
    store = coords.FrameStore()
    store.set_last(_frame())
    monkeypatch.setattr(coords, "frames", store)
    monkeypatch.setattr(inputs, "reset_idle", lambda: None)
    monkeypatch.setattr(inputs, "click", lambda *a, **k: pytest.fail("must not click"))
    with pytest.raises(coords.StaleFrame):
        server.click(10, 10, frame_id="cap-bogus")


def test_click_maps_through_resolved_frame(monkeypatch):
    store = coords.FrameStore()
    fid = store.set_last(_frame())  # region origin (100,100), scale 1.0
    monkeypatch.setattr(coords, "frames", store)
    monkeypatch.setattr(inputs, "reset_idle", lambda: None)
    got = {}
    monkeypatch.setattr(inputs, "click",
                        lambda gx, gy, button="left": got.update(gx=gx, gy=gy))
    server.click(10, 10, frame_id=fid)
    assert (got["gx"], got["gy"]) == (110, 110)


# --- #12: validation failures raise (SDK turns them into is_error) ---

def test_screenshot_unknown_output_raises(monkeypatch):
    monkeypatch.setattr(i3tree, "get_outputs",
                        lambda: [{"name": "DP-0", "primary": True,
                                  "rect": {"x": 0, "y": 0, "w": 100, "h": 100}}])
    with pytest.raises(ValueError):
        server.screenshot(output="NOPE")


# --- i3control wiring ---

def test_launch_resets_idle_and_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(inputs, "reset_idle", lambda: seen.setdefault("idle", True))
    monkeypatch.setattr(i3control, "launch", lambda cmd: seen.setdefault("cmd", cmd) or "ok")
    server.launch("xterm -name foo")
    assert seen == {"idle": True, "cmd": "xterm -name foo"}


def test_move_window_to_workspace_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(i3control, "move_window_to_workspace",
                        lambda cid, ws: seen.update(cid=cid, ws=ws) or "moved")
    server.move_window_to_workspace(42, "5")
    assert seen == {"cid": 42, "ws": "5"}


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
