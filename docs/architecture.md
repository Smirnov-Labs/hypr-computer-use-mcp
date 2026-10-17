# Architecture

`hypr-cua-mcp` is a small MCP server that lets a model **see**, **understand**, and
**drive** a Hyprland/Wayland desktop. The novel part isn't any single primitive
(grim, uinput, hyprctl all exist) — it's wiring them so pointer coordinates are
always expressed in the image the model was just shown. It is a fork of
[`i3-computer-use-mcp`](https://github.com/Smirnov-Labs/i3-computer-use-mcp) with the
X11 backends swapped for Wayland ones; `coords.py`,
`server.py`, and the frame-id contract carry over nearly unchanged.

## Module map

| File | Responsibility |
|------|----------------|
| `server.py` | MCP layer (mcp 2.0 `MCPServer`), tool definitions, the `[Image, caption]` response shape, `--selftest` |
| `capture.py` | Screenshot pipeline: `grim` whole-layout grab → Pillow crop → downscale to the vision-API budget |
| `hyprtree.py` | Read-only `hyprctl -j` (clients/monitors) → `list_windows`, `get_outputs`, `find_window` |
| `hyprcontrol.py` | Mutations via `hyprctl eval 'hl.dispatch(hl.dsp.…)'` (focus / workspace / float / launch / layout) |
| `inputs.py` | Input synthesis via a self-owned `evdev.UInput` device (abs pointer + wheel + keyboard) |
| `coords.py` | The mapping between screenshot pixels and global layout pixels; stores the last capture's frame |
| `clipboard.py` | `wl-clipboard` read / write |
| `tmux.py` / `taskrunner.py` | Hidden-tmux terminal + task channels on an isolated socket (no window, no focus) |
| `cdp.py` | Drive Chrome tabs over the DevTools protocol (dedicated profile, never the user's main one) |

## Data flow (perceive → reason → act)

```
                 ┌─────────────── perceive ───────────────┐
   hyprctl tree ─► list_windows / get_outputs (global geometry)
   layout ───────► screenshot(output) ─┐
                                        ├─► grim whole-layout, crop to one output/window
                                        └─► downscale to API budget ──► Image + CaptureFrame
                                                                          │
                 model reasons in *screenshot pixel space* ◄─────────────┘
                                        │
                 ┌──────────────── act ─┴──────────────────┐
   click(x,y) ───► CaptureFrame.to_global(x,y) ──► uinput abs move + button (global px)
   type/key ─────► uinput key events ──► focused window
```

## The coordinate model (core idea)

The hyprctl tree speaks **global layout pixels** — one plane spanning every
monitor. Screenshots are **cropped to one output** (or window) and **downscaled**.
Each records a `CaptureFrame`:

- `region_{x,y,w,h}` — the global rectangle it covers
- `scale_x` / `scale_y` — `screenshot_px = global_px * scale` per axis (≤ 1.0)

and pointer tools accept coordinates *in the screenshot's pixels* and map back:

```
global_x = region_x + screenshot_x / scale_x
global_y = region_y + screenshot_y / scale_y   (clamped to the region)
```

A `FrameStore` binds each capture to an opaque `frame_id`; the pointer tools take
it back, so a click resolves against *the exact screenshot it was reasoned
against*. A missing or stale id is rejected, not silently mapped against the wrong
frame. (Verified at monitor scale 1.0, where global px == physical px; capture on
scaled monitors is still open — see [`KNOWN-ISSUES.md`](../KNOWN-ISSUES.md).)

## Input: a self-owned uinput device

`inputs.py` creates one `evdev.UInput` (abs X/Y + buttons + wheel + US-ASCII
keyboard, `INPUT_PROP_POINTER`) that the server process owns for its lifetime.
Global pixels map to the device's abs axes by `abs = pixel * 65535 / layout_extent`
(extent from `hyprctl monitors`; the compositor maps `[0,65535]` onto the layout,
verified ~1px). Buttons/wheel/keys emit directly. Because these are *real* input
events, they reset the compositor idle timer — no synthetic-input self-lock, no
`ydotoold` daemon. See `docs/gotchas.md` for why ydotool (2× abs coords, no
scroll) was rejected.

## Control: Lua-wrapped hyprctl dispatch

With a Lua config (`hyprland.lua`, Hyprland 0.55+), `hyprctl dispatch X` is parsed
as Lua and fails, so mutations go through
`hyprctl eval 'hl.dispatch(hl.dsp.<path>({...}))'`. `hyprcontrol.py` builds
those Lua expressions (quoting/escaping user strings) and targets windows with the
`"address:0x…"` selector. It is kept separate from the read-only `hyprtree.py` so
the mutating surface stays small and auditable. `run_dispatch` is a raw escape
hatch that blocks `exit`.

## MCP layer (mcp 2.0 notes)

- High-level server is `mcp.server.mcpserver.MCPServer` (the old `FastMCP`).
- Wire objects are snake_case (`server_info`, `is_error`, `mime_type`).
- Image-returning tools use `@mcp.tool(structured_output=False)` and return
  `[Image(...), caption_str]`, which serializes to an `image` + `text` pair.

## Scope

**read + focus + input + Hyprland control** (workspace / float / launch / layout),
plus off-screen side channels: hidden tmux terminals, a task runner built on them,
and a Chrome DevTools browser channel. No X11, no sandbox (Wayland has no cheap nested-compositor equivalent of Xephyr —
supervise live runs).
