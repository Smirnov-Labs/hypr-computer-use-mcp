"""Coordinate mapping between screenshot pixels and global X11 coordinates.

xdotool and the i3 tree both speak *global* X coordinates — one plane spanning
every output. Screenshots we hand the model are cropped to a single output (or
window) and downscaled to fit the vision API's image budget. A ``CaptureFrame``
records the mapping from one screenshot's pixel space back to global X pixels.

The ``FrameStore`` binds a screenshot to an opaque id so that a later click can
name *the exact screenshot it reasoned about* — the pointer tools take that id
and the store rejects a stale one instead of silently resolving against
"whatever screenshot happened last" (which races under concurrent tool calls).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from threading import Lock


class CaptureFrameError(RuntimeError):
    """Base class for capture-frame resolution errors."""


class NoActiveCapture(CaptureFrameError):
    """A pointer tool was called before any screenshot was taken."""


class StaleFrame(CaptureFrameError):
    """A pointer tool named a frame id that is no longer the current one."""


@dataclass(frozen=True)
class CaptureFrame:
    """The mapping from one screenshot's pixel space back to global X pixels.

    Scale is stored per-axis: Pillow rounds width and height independently, so
    the achieved horizontal and vertical scale can differ slightly, and a single
    isotropic factor would drift by up to ~1px on extreme aspect ratios.
    """

    region_x: int  # global-X origin of the captured region
    region_y: int
    region_w: int  # size of the captured region in global px (pre-scale)
    region_h: int
    scale_x: float  # screenshot_px = global_px * scale   (per axis, <= 1.0)
    scale_y: float
    shot_w: int  # resulting screenshot size in px
    shot_h: int
    label: str  # human label, e.g. "output DP-0" or "window 94 (Slack)"

    def to_global(self, sx: float, sy: float) -> tuple[int, int]:
        """Map a screenshot-pixel coordinate to a global X coord, clamped to region."""
        gx = self.region_x + sx / self.scale_x
        gy = self.region_y + sy / self.scale_y
        gx = min(max(gx, self.region_x), self.region_x + self.region_w - 1)
        gy = min(max(gy, self.region_y), self.region_y + self.region_h - 1)
        return int(round(gx)), int(round(gy))

    def as_dict(self) -> dict:
        return {
            "region": {
                "x": self.region_x,
                "y": self.region_y,
                "w": self.region_w,
                "h": self.region_h,
            },
            "scale_x": round(self.scale_x, 5),
            "scale_y": round(self.scale_y, 5),
            "screenshot_size": {"w": self.shot_w, "h": self.shot_h},
            "captured": self.label,
        }


class FrameStore:
    """Holds the most recent capture and hands out/validates opaque frame ids."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counter = itertools.count(1)
        self._last_id: str | None = None
        self._last_frame: CaptureFrame | None = None

    def set_last(self, frame: CaptureFrame) -> str:
        """Store ``frame`` as the current capture and return its new opaque id."""
        with self._lock:
            self._last_id = f"cap-{next(self._counter)}"
            self._last_frame = frame
            return self._last_id

    def get_last(self) -> tuple[str | None, CaptureFrame | None]:
        with self._lock:
            return self._last_id, self._last_frame

    def resolve(self, frame_id: str | None = None) -> CaptureFrame:
        """Return the frame for ``frame_id``.

        ``frame_id=None`` means "use the last capture". Raises ``NoActiveCapture``
        if nothing has been captured yet, or ``StaleFrame`` if a specific id was
        given that is no longer current.
        """
        with self._lock:
            if self._last_frame is None:
                raise NoActiveCapture(
                    "no screenshot has been taken yet — call screenshot first"
                )
            if frame_id is not None and frame_id != self._last_id:
                raise StaleFrame(
                    f"frame {frame_id!r} is stale (current is {self._last_id!r}); "
                    "take a fresh screenshot before acting on it"
                )
            return self._last_frame


# Process-wide default store used by the MCP server.
frames = FrameStore()
