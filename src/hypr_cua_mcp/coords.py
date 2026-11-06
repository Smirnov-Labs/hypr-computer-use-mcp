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

import contextvars
import itertools
import secrets
from dataclasses import dataclass
from threading import Lock


class CaptureFrameError(RuntimeError):
    """Base class for capture-frame resolution errors."""


class NoActiveCapture(CaptureFrameError):
    """A pointer tool was called before any screenshot was taken."""


class StaleFrame(CaptureFrameError):
    """A pointer tool named a frame id that is no longer the current one."""


# Which MCP session a tool call belongs to. The shared service sets it per
# connection; stdio mode leaves it None (one implicit session).
session_key: contextvars.ContextVar = contextvars.ContextVar("hypr_cua_session_key", default=None)
require_session = False  # the shared service sets this: a frame call outside a session is a bug


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
    """Holds each session's most recent capture and hands out / validates frame ids.

    Ids carry a random per-process prefix (cap-<8 hex>-<n>), so an id from another
    session, or from before a restart, never matches. The session comes from the
    `session_key` contextvar."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._prefix = secrets.token_hex(4)
        self._counter = itertools.count(1)
        self._last: dict = {}  # session key -> (frame id, frame)

    def _key(self):
        key = session_key.get()
        if key is None and require_session:
            raise RuntimeError("frame store used outside an MCP session (session_key unset)")
        return key

    def set_last(self, frame: CaptureFrame) -> str:
        """Store `frame` as this session's current capture and return its new id."""
        key = self._key()
        with self._lock:
            frame_id = f"cap-{self._prefix}-{next(self._counter)}"
            self._last[key] = (frame_id, frame)
            return frame_id

    def resolve(self, frame_id: str | None = None) -> CaptureFrame:
        """This session's frame for `frame_id`; None means its latest capture.

        Raises NoActiveCapture if this session has no capture yet, or StaleFrame if
        the id is not this session's current one."""
        key = self._key()
        with self._lock:
            last = self._last.get(key)
            if last is None:
                raise NoActiveCapture("no screenshot has been taken yet; call screenshot first")
            if frame_id is not None and frame_id != last[0]:
                raise StaleFrame(
                    f"frame {frame_id!r} is stale (current is {last[0]!r}); "
                    "take a fresh screenshot before acting on it"
                )
            return last[1]

    def drop(self, key) -> None:
        """Forget a session's capture (its connection ended)."""
        with self._lock:
            self._last.pop(key, None)


# Process-wide default store used by the MCP server.
frames = FrameStore()
