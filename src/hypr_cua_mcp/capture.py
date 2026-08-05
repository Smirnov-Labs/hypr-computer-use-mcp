"""Screenshot capture: grim -> Pillow crop/downscale -> PNG bytes.

We grab the whole Wayland layout (all outputs, composited at their global
positions) once with `grim`, then crop to the requested region in Pillow. The
crop box is validated first (`_crop_box`) so an off-screen or degenerate
window/output rect fails with a clear error instead of an opaque
Pillow/ZeroDivision exception.
"""
from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass

from PIL import Image as PILImage

from .coords import CaptureFrame

# Vision-API image budget. Stay under the long-edge and total-pixel limits that
# would trigger *server-side* downsampling, so the pixels the model reasons about
# are exactly the pixels we scaled and mapped.
MAX_LONG_EDGE = 1568
MAX_PIXELS = 1_150_000
GRAB_TIMEOUT = 15  # seconds; bound a hung compositor / grim


class CaptureError(ValueError):
    """The requested region is not capturable (off-screen or degenerate)."""


@dataclass
class Shot:
    png: bytes
    frame: CaptureFrame


def _grab_root() -> PILImage.Image:
    """Grab the entire Wayland layout (all outputs) as a Pillow image.

    `grim` with no -o/-g captures every output composited into one image sized to
    the layout bounding box, with the image origin at global (0,0). That lets us
    crop by global coordinates exactly as the X11 root-grab did.
    """
    proc = subprocess.run(
        ["grim", "-t", "png", "-"],
        capture_output=True,
        check=True,
        timeout=GRAB_TIMEOUT,
    )
    return PILImage.open(io.BytesIO(proc.stdout)).convert("RGB")


def _crop_box(root_w: int, root_h: int, rx: int, ry: int, rw: int, rh: int):
    """Clamp (rx,ry,rw,rh) to the framebuffer; raise CaptureError if empty.

    Returns a Pillow crop box (x0, y0, x1, y1) with x1 > x0 and y1 > y0.
    """
    if rw <= 0 or rh <= 0:
        raise CaptureError(f"region has non-positive size ({rw}x{rh})")
    x0, y0 = max(0, rx), max(0, ry)
    x1, y1 = min(root_w, rx + rw), min(root_h, ry + rh)
    if x1 <= x0 or y1 <= y0:
        raise CaptureError(
            f"region ({rx},{ry},{rw},{rh}) is off-screen for the "
            f"{root_w}x{root_h} framebuffer — nothing to capture "
            "(window moved off-screen, or the output is asleep?)"
        )
    return x0, y0, x1, y1


def _fit_scale(w: int, h: int) -> float:
    """Largest scale <= 1.0 that fits both the long-edge and total-pixel budgets."""
    if w <= 0 or h <= 0:
        raise CaptureError(f"cannot scale a {w}x{h} image")
    scale = min(1.0, MAX_LONG_EDGE / max(w, h))
    if w * h * scale * scale > MAX_PIXELS:
        scale = (MAX_PIXELS / (w * h)) ** 0.5
    return scale


def capture_region(rx: int, ry: int, rw: int, rh: int, label: str) -> Shot:
    """Capture the global rectangle (rx,ry,rw,rh), downscaled to the API budget."""
    root = _grab_root()
    x0, y0, x1, y1 = _crop_box(root.width, root.height, rx, ry, rw, rh)

    crop = root.crop((x0, y0, x1, y1))
    cw, ch = crop.size

    scale = _fit_scale(cw, ch)
    if scale < 1.0:
        crop = crop.resize(
            (max(1, round(cw * scale)), max(1, round(ch * scale))),
            PILImage.LANCZOS,
        )

    buf = io.BytesIO()
    crop.save(buf, format="PNG", optimize=True)

    frame = CaptureFrame(
        region_x=x0,
        region_y=y0,
        region_w=cw,
        region_h=ch,
        # Achieved per-axis scale (Pillow rounds w/h independently).
        scale_x=crop.width / cw,
        scale_y=crop.height / ch,
        shot_w=crop.width,
        shot_h=crop.height,
        label=label,
    )
    return Shot(png=buf.getvalue(), frame=frame)
