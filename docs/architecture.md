# Architecture

`hypr-cua-mcp` is a small MCP server that lets a model **see**, **understand**, and
**drive** an X11 desktop running i3. The novel part isn't any single primitive
(screenshots, xdotool, i3 IPC all exist) — it's wiring them together so pointer
coordinates are always expressed in the image the model was just shown, and so
the whole thing follows `$DISPLAY` faithfully (making the Xephyr sandbox real).

## Module map

| File | Responsibility |
|------|----------------|
| `server.py` | MCP layer (mcp 2.0 `MCPServer`), tool definitions, the `[Image, caption]` response shape, `--selftest` |
| `capture.py` | Screenshot pipeline: ImageMagick `import` → Pillow crop → downscale to the vision-API budget |
| `i3tree.py` | i3 IPC reads (`list_windows`, `get_outputs`) + `focus_window`, and the **display-faithful socket resolution** |
| `inputs.py` | Input synthesis via `xdotool` (global X coordinates) |
| `coords.py` | The mapping between screenshot pixels and global X pixels; stores the last capture's frame |

## Data flow (perceive → reason → act)

```
                 ┌─────────────── perceive ───────────────┐
   i3 tree ──────► list_windows / get_outputs (global geometry)
   framebuffer ──► screenshot(output) ─┐
                                       ├─► crop to one output/window
                                       └─► downscale to API budget ──► Image + CaptureFrame
                                                                          │
                 model reasons in *screenshot pixel space* ◄─────────────┘
                                       │
                 ┌──────────────── act ─┴──────────────────┐
   click(x,y) ───► CaptureFrame.to_global(x,y) ──► xdotool mousemove+click (global px)
   type/key ─────► xdotool type/key ──► focused window
```

## The coordinate model (core idea)

`xdotool` and the i3 tree both speak **global** X coordinates — one plane
spanning every monitor. Screenshots are **cropped to one output** (or window) and
**downscaled**. So each screenshot records a `CaptureFrame`:

- `region_{x,y,w,h}` — the global-X rectangle it covers
- `scale_x` / `scale_y` — `screenshot_px = global_px * scale` per axis (≤ 1.0;
  Pillow rounds width/height independently, so the axes can differ slightly)

and pointer tools accept coordinates *in the screenshot's pixels* and map back:

```
global_x = region_x + screenshot_x / scale_x
global_y = region_y + screenshot_y / scale_y   (clamped to the region)
```

A `FrameStore` binds each capture to an opaque `frame_id`; `screenshot` returns
it and the pointer tools take it back, so a click resolves against *the exact
screenshot it was reasoned against*. A missing id (no screenshot yet) or a stale
one (a newer screenshot replaced it — possible under concurrent tool calls) is
rejected with a clear error, not silently mapped against the wrong frame.

## Display & socket resolution (why the sandbox is real)

i3ipc's default socket discovery prefers the ambient `$I3SOCK` env var over
`$DISPLAY`. On a machine that exports `I3SOCK` (hexane does), that binds every
client to the login `:0` i3 — so a server told to use `DISPLAY=:1` would still
drive the real desktop. **Fail-open, and dangerous.**

`i3tree._i3_socket_path()` instead reads i3's `I3_SOCKET_PATH` atom off the root
window of the **current `$DISPLAY`** (via python-xlib), and passes that explicit
path to `i3ipc.Connection`. It deliberately does **not** fall back to `$I3SOCK`;
if no per-display socket exists it raises, so a misconfigured sandbox fails
loudly instead of silently touching `:0`.

This is what makes `DISPLAY=:1` (Xephyr sandbox) vs `:0` (live) a clean switch —
the same code, one env var, no cross-talk.

## Capture pipeline

1. `import -silent -window root png:-` grabs the **entire** X root (all outputs)
   to stdout. On hexane that's 7680×2880 spanning three monitors — which is
   exactly why we never hand that to the model whole.
2. Pillow crops to the requested output/window's global rect.
3. `_fit_scale` picks the largest scale ≤ 1.0 satisfying both a long-edge cap
   (1568 px) and a total-pixel cap (~1.15 MP), so the API doesn't re-downsample
   underneath us (which would desync the coordinate mapping).
4. Re-encode PNG; return bytes + the `CaptureFrame`.

## Input

Thin `xdotool` wrappers, all in global coordinates: `mousemove --sync`, `click`,
`mousedown/up` (drag), wheel buttons 4/5 (scroll), `type --clearmodifiers --`,
`key --clearmodifiers`. Typing targets whatever window has focus — hence
`focus_window` (or the `con_id` arg on `type_text`/`key`) first.

## MCP layer (mcp 2.0 notes)

- High-level server is `mcp.server.mcpserver.MCPServer` (the old `FastMCP`).
- Wire objects are snake_case (`server_info`, `is_error`, `mime_type`).
- Image-returning tools use `@mcp.tool(structured_output=False)` and return a
  Python list `[Image(...), caption_str]`, which serializes to an `image` +
  `text` content pair.

## Scope

**read + focus + input + i3 control.** i3 mutation (workspaces / layout / launch)
lives in `i3control.py`, kept separate from the read-only `i3tree.py` so the
mutating surface stays small and auditable; `run_i3` is a raw escape hatch that
blocks `exit`. See `../DESIGN-i3-control-extension.md`.
