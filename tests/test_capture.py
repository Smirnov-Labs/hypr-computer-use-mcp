"""Tests for capture geometry: crop-box validation, scale budget, frame build."""
import subprocess

import pytest
from PIL import Image as PILImage

from hypr_cua_mcp import capture
from hypr_cua_mcp.capture import CaptureError, _crop_box, _fit_scale, capture_region


def test_grab_root_hints_how_to_install_when_grim_missing(monkeypatch):
    def _no_grim(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "grim")

    monkeypatch.setattr(subprocess, "run", _no_grim)
    with pytest.raises(RuntimeError, match=r"grim not found.*install"):
        capture._grab_root()


def test_crop_box_clamps_to_framebuffer():
    assert _crop_box(1000, 1000, 900, 900, 400, 400) == (900, 900, 1000, 1000)


def test_crop_box_raises_when_region_off_right_edge():
    with pytest.raises(CaptureError):
        _crop_box(1000, 1000, 1200, 100, 400, 400)  # x0=1200 clamps past x1=1000


def test_crop_box_raises_when_region_off_top_edge():
    with pytest.raises(CaptureError):
        _crop_box(1000, 1000, 100, -500, 400, 400)  # y1 clamps to -100 < y0=0


def test_crop_box_raises_on_nonpositive_input():
    with pytest.raises(CaptureError):
        _crop_box(1000, 1000, 10, 10, 0, 400)


def test_fit_scale_guards_nonpositive():
    with pytest.raises(CaptureError):
        _fit_scale(0, 0)


def test_fit_scale_downscales_4k_within_budget():
    s = _fit_scale(3840, 2160)
    assert 0 < s < 1
    assert 3840 * s * (2160 * s) <= capture.MAX_PIXELS + 1


def test_capture_region_builds_frame_with_per_axis_scale(monkeypatch):
    monkeypatch.setattr(capture, "_grab_root",
                        lambda: PILImage.new("RGB", (1000, 1000), "white"))
    shot = capture_region(0, 0, 1000, 1000, "test")
    f = shot.frame
    assert (f.region_w, f.region_h) == (1000, 1000)
    assert f.scale_x == pytest.approx(f.shot_w / 1000)
    assert f.scale_y == pytest.approx(f.shot_h / 1000)
    assert shot.png[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG magic


def test_capture_region_rejects_offscreen_window(monkeypatch):
    monkeypatch.setattr(capture, "_grab_root",
                        lambda: PILImage.new("RGB", (1000, 1000), "white"))
    with pytest.raises(CaptureError):
        capture_region(5000, 5000, 400, 400, "offscreen")  # entirely past framebuffer
