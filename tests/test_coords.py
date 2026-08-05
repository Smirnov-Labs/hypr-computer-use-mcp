"""Tests for coords: coordinate mapping + the capture-frame store."""
import pytest

from hypr_cua_mcp.coords import CaptureFrame, FrameStore, NoActiveCapture, StaleFrame


def _frame(**kw):
    d = dict(
        region_x=100, region_y=200, region_w=400, region_h=800,
        scale_x=0.5, scale_y=0.25, shot_w=200, shot_h=200, label="test",
    )
    d.update(kw)
    return CaptureFrame(**d)


def test_to_global_uses_separate_x_and_y_scale():
    f = _frame(scale_x=0.5, scale_y=0.25)
    # region origin + screenshot_px / per-axis scale
    assert f.to_global(10, 10) == (100 + 20, 200 + 40)  # (120, 240)


def test_to_global_clamps_to_region_bounds():
    f = _frame(region_x=0, region_y=0, region_w=100, region_h=100,
               scale_x=1.0, scale_y=1.0)
    assert f.to_global(9999, 9999) == (99, 99)   # far corner clamps to w-1,h-1
    assert f.to_global(-50, -50) == (0, 0)       # negative clamps to origin


def test_as_dict_reports_both_scales():
    d = _frame(scale_x=0.5, scale_y=0.25).as_dict()
    assert d["scale_x"] == 0.5 and d["scale_y"] == 0.25


def test_store_returns_distinct_ids_per_capture():
    store = FrameStore()
    assert store.set_last(_frame()) != store.set_last(_frame())


def test_resolve_raises_when_no_capture_taken():
    with pytest.raises(NoActiveCapture):
        FrameStore().resolve(None)


def test_resolve_raises_on_stale_frame_id():
    store = FrameStore()
    store.set_last(_frame())
    with pytest.raises(StaleFrame):
        store.resolve("cap-does-not-match")


def test_resolve_returns_frame_for_matching_id_or_none():
    store = FrameStore()
    f = _frame()
    fid = store.set_last(f)
    assert store.resolve(fid) is f
    assert store.resolve(None) is f  # None means "use the last capture"
