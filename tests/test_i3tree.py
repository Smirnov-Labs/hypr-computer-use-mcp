"""Tests for i3tree.focus_window — must verify focus actually landed."""
from hypr_cua_mcp import i3tree


class _Reply:
    def __init__(self, success):
        self.success = success


class _Con:
    def __init__(self, id):
        self.id = id


class _Tree:
    def __init__(self, focused_id):
        self._f = _Con(focused_id) if focused_id is not None else None

    def find_focused(self):
        return self._f


class _Conn:
    def __init__(self, replies, focused_id):
        self._replies = replies
        self._focused_id = focused_id

    def command(self, _s):
        return self._replies

    def get_tree(self):
        return _Tree(self._focused_id)


def _patch(monkeypatch, replies, focused_id):
    monkeypatch.setattr(i3tree, "_conn", lambda: _Conn(replies, focused_id))


def test_focus_true_when_focus_actually_lands(monkeypatch):
    _patch(monkeypatch, [_Reply(True)], focused_id=42)
    assert i3tree.focus_window(42) is True


def test_focus_false_on_unsuccessful_command(monkeypatch):
    _patch(monkeypatch, [_Reply(False)], focused_id=42)
    assert i3tree.focus_window(42) is False


def test_focus_false_on_empty_reply_list(monkeypatch):
    # the all([]) == True bug: an empty reply list must NOT read as success
    _patch(monkeypatch, [], focused_id=42)
    assert i3tree.focus_window(42) is False


def test_focus_false_when_criteria_matched_nothing(monkeypatch):
    # i3 reports success even when the criteria matched no window; focus stays
    # elsewhere, so verifying the focused id is the only reliable signal
    _patch(monkeypatch, [_Reply(True)], focused_id=999)
    assert i3tree.focus_window(42) is False
