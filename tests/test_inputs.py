"""Tests for inputs: evdev uinput event emission (device mocked; no real uinput/compositor)."""
import subprocess

import pytest
from evdev import ecodes as e

from hypr_cua_mcp import inputs


class FakeUI:
    def __init__(self):
        self.events = []  # (type, code, value)

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        pass


@pytest.fixture
def ui(monkeypatch):
    fake = FakeUI()
    monkeypatch.setattr(inputs, "_device", lambda: fake)
    monkeypatch.setattr(inputs, "_extent", (7680, 2880))
    return fake


def test_button_rejects_unknown():
    with pytest.raises(ValueError):
        inputs._button("purple")


def test_scroll_rejects_unknown_direction(ui):
    with pytest.raises(ValueError):
        inputs.scroll(10, 10, direction="sideways")


def test_scale_maps_pixels_to_abs(monkeypatch):
    monkeypatch.setattr(inputs, "_extent", (7680, 2880))
    assert inputs._scale(3840, 1440) == (32768, 32768)  # layout centre -> round(0.5*65535)


def test_scale_clamps_out_of_range(monkeypatch):
    monkeypatch.setattr(inputs, "_extent", (7680, 2880))
    assert inputs._scale(99999, -50) == (inputs._ABS_MAX, 0)


def test_move_emits_scaled_abs(ui):
    inputs.move(3840, 1440)
    assert (e.EV_ABS, e.ABS_X, 32768) in ui.events
    assert (e.EV_ABS, e.ABS_Y, 32768) in ui.events


def test_click_emits_button_press_and_release(ui):
    inputs.click(0, 0, button="left")
    assert (e.EV_KEY, e.BTN_LEFT, 1) in ui.events
    assert (e.EV_KEY, e.BTN_LEFT, 0) in ui.events


def test_click_count_repeats_presses(ui):
    inputs.click(0, 0, button="right", count=2)
    downs = [ev for ev in ui.events if ev == (e.EV_KEY, e.BTN_RIGHT, 1)]
    assert len(downs) == 2


def test_scroll_down_uses_negative_wheel(ui):
    inputs.scroll(0, 0, direction="down", amount=3)
    wheels = [v for (t, c, v) in ui.events if t == e.EV_REL and c == e.REL_WHEEL]
    assert wheels == [-1, -1, -1]


def test_scroll_up_uses_positive_wheel(ui):
    inputs.scroll(0, 0, direction="up", amount=2)
    wheels = [v for (t, c, v) in ui.events if t == e.EV_REL and c == e.REL_WHEEL]
    assert wheels == [1, 1]


def test_type_text_letters_with_shift(ui):
    inputs.type_text("aB")
    assert (e.EV_KEY, e.KEY_A, 1) in ui.events
    assert (e.EV_KEY, e.KEY_B, 1) in ui.events
    assert (e.EV_KEY, e.KEY_LEFTSHIFT, 1) in ui.events  # for the capital B


def test_type_text_skips_unmapped_chars(ui):
    inputs.type_text("aéb")  # 'é' not on the US-ASCII map -> skipped; a/b still typed
    assert (e.EV_KEY, e.KEY_A, 1) in ui.events
    assert (e.EV_KEY, e.KEY_B, 1) in ui.events


def test_key_chord_order_ctrl_s(ui):
    inputs.key("ctrl+s")
    seq = [(c, v) for (t, c, v) in ui.events if t == e.EV_KEY]
    assert seq.index((e.KEY_LEFTCTRL, 1)) < seq.index((e.KEY_S, 1))
    assert seq.index((e.KEY_S, 0)) < seq.index((e.KEY_LEFTCTRL, 0))


def test_key_named(ui):
    inputs.key("Return")
    assert (e.EV_KEY, e.KEY_ENTER, 1) in ui.events


def test_key_unknown_modifier_raises(ui):
    with pytest.raises(ValueError):
        inputs.key("hyper+x")


def test_key_unknown_key_raises(ui):
    with pytest.raises(ValueError):
        inputs.key("ctrl+notakey")


def test_reset_idle_is_noop():
    assert inputs.reset_idle() is None


def test_mouse_location_parses_cursorpos(monkeypatch):
    class R:
        stdout = "1234, 567\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert inputs.mouse_location() == {"x": 1234, "y": 567}
