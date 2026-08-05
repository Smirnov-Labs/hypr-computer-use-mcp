"""Tests for hyprtree read helpers (hyprctl mocked; no compositor)."""
from hypr_cua_mcp import hyprtree

_MONITORS = [
    {"id": 0, "name": "DP-1", "x": 0, "y": 0, "width": 3840, "height": 2160,
     "scale": 1.0, "focused": True, "activeWorkspace": {"name": "2"}},
    {"id": 1, "name": "DP-2", "x": 3840, "y": 0, "width": 3840, "height": 2160,
     "scale": 1.0, "focused": False, "activeWorkspace": {"name": "8"}},
]
_CLIENTS = [
    {"address": "0xaaa", "class": "Alacritty", "initialClass": "Alacritty",
     "title": "term", "workspace": {"name": "2"}, "monitor": 0,
     "at": [10, 20], "size": [800, 600], "floating": False, "focusHistoryID": 0,
     "hidden": False},
    {"address": "0xbbb", "class": "firefox", "title": "web",
     "workspace": {"name": "8"}, "monitor": 1, "at": [3850, 0], "size": [1000, 900],
     "floating": True, "focusHistoryID": 3, "hidden": True},
]


def _mock(monkeypatch, mapping):
    monkeypatch.setattr(hyprtree, "_json", lambda args: mapping[args[0]])


def test_list_windows_maps_fields(monkeypatch):
    _mock(monkeypatch, {"monitors": _MONITORS, "clients": _CLIENTS})
    a = next(w for w in hyprtree.list_windows() if w["id"] == "0xaaa")
    assert a["wm_class"] == "Alacritty"
    assert a["title"] == "term"
    assert a["workspace"] == "2"
    assert a["output"] == "DP-1"  # monitor index 0 resolved to name
    assert a["rect"] == {"x": 10, "y": 20, "w": 800, "h": 600}
    assert a["focused"] is True
    assert a["floating"] is False
    assert a["visible"] is True


def test_list_windows_focus_visibility_floating(monkeypatch):
    _mock(monkeypatch, {"monitors": _MONITORS, "clients": _CLIENTS})
    b = next(w for w in hyprtree.list_windows() if w["id"] == "0xbbb")
    assert b["focused"] is False   # focusHistoryID != 0
    assert b["visible"] is False   # hidden
    assert b["floating"] is True
    assert b["output"] == "DP-2"


def test_get_outputs_primary_is_focused(monkeypatch):
    _mock(monkeypatch, {"monitors": _MONITORS})
    outs = hyprtree.get_outputs()
    prim = hyprtree.primary_output(outs)
    assert prim["name"] == "DP-1"
    assert prim["rect"] == {"x": 0, "y": 0, "w": 3840, "h": 2160}
    assert prim["current_workspace"] == "2"


def test_get_outputs_reports_logical_size_for_scaled_monitor(monkeypatch):
    mon = [{"id": 0, "name": "DP-1", "x": 3840, "y": 0, "width": 3840, "height": 2160,
            "scale": 2.0, "focused": True, "activeWorkspace": {"name": "1"}}]
    monkeypatch.setattr(hyprtree, "_json", lambda args: mon)
    r = hyprtree.get_outputs()[0]["rect"]
    assert r == {"x": 3840, "y": 0, "w": 1920, "h": 1080}  # logical = physical / scale


def test_find_window(monkeypatch):
    _mock(monkeypatch, {"monitors": _MONITORS, "clients": _CLIENTS})
    assert hyprtree.find_window("0xbbb")["title"] == "web"
    assert hyprtree.find_window("0xzzz") is None


def test_get_focused_address(monkeypatch):
    monkeypatch.setattr(hyprtree, "_json", lambda args: {"address": "0xaaa"})
    assert hyprtree.get_focused_address() == "0xaaa"
