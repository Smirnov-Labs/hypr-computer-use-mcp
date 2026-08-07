# hypr-cua-mcp

A **Hyprland/Wayland computer-use MCP server**. It gives an MCP client (Claude
Code, etc.) the ability to *see* your desktop, *understand* it via the Hyprland
tree, and *drive* it with mouse + keyboard.

Why this exists: generic Linux computer-use servers are blind to the compositor —
they hand the model a giant multi-monitor screenshot and hope its pixel guesses
land. This server crops to one output (or one window) with `grim`, downscales to
the vision-API budget, and exposes `hyprctl`'s tree so the model targets windows
by identity, not luck. Coordinates are always given *in the screenshot the model
was just shown*; the server maps them back to global layout pixels.

Built for **Wayland/Hyprland**: `grim` (capture) + a self-owned **uinput** device
(input) + `hyprctl` (tree/control) + `wl-clipboard`. It is the Wayland successor
to [i3-computer-use-mcp](https://github.com/issmirnov/i3-computer-use-mcp) (X11).

## Tools

| Tool | What it does |
|------|--------------|
| `list_windows` | Every Hyprland window: id (**address**), class, title, workspace, output, global rect, floating/focus/visibility |
| `get_outputs` | Active monitors: global geometry, scale, workspace |
| `screenshot(output?)` | Screenshot one output (default: focused), downscaled to the API budget |
| `screenshot_window(con_id)` | Full-res crop of a single window (by address) — best for small text |
| `click / double_click / move_mouse / drag / scroll` | Pointer actions; coords in a screenshot's pixel space (pass its `frame_id`) |
| `focus_window(con_id)` | Focus a window by address (best-effort — see follow_mouse caveat) |
| `type_text(text, con_id?) / key(keys, con_id?)` | Keyboard input (optionally focus first) |
| `get_mouse` | Current global pointer location |
| `read_clipboard(selection?)` / `write_clipboard(text, selection?, secret?)` | Exact text in/out of the `clipboard`/`primary` selection (no OCR); `write` returns only a length, never the value |
| `launch(command)` | Launch an app via Hyprland's exec dispatcher |
| `goto_workspace(ws)` / `move_window_to_workspace(con_id, ws)` | Switch to / move a window (by address) to a workspace |
| `set_floating(con_id, enable?)` | Toggle a window's floating state |
| `layout_message(message)` | Send a message to the active layout engine (e.g. `togglesplit`) |
| `run_dispatch(dsp_expr)` | Raw Hyprland dispatcher escape hatch (`exit` blocked) |

Window ids are **Hyprland addresses** (`0x…`) from `list_windows` — not integers.

## The coordinate model (the important bit)

The Hyprland tree uses **global layout pixels** spanning all monitors. Screenshots
are **cropped + downscaled**, so every screenshot records a mapping frame (region
origin + per-axis scale). Pointer tools accept coordinates *in the screenshot's
pixel space* and translate them:

```
global_x = region.x + screenshot_x / scale_x
global_y = region.y + screenshot_y / scale_y
```

Practically: take a screenshot (it returns a `frame_id`), then call
`click(x, y, frame_id=…)` with the pixel you see in *that* image. Pointer tools
reject a missing or stale `frame_id` rather than guessing against the wrong one.

The uinput pointer positions absolutely: global pixels map to the device's abs
axes via the layout extent read from `hyprctl monitors` (verified ~1px).

## Register with Claude Code

```bash
claude mcp add hypr-cua -- /path/to/hypr-computer-use-mcp/bin/hypr-cua-mcp
```

Reconnect Claude Code (`/mcp` → reconnect) or restart it to pick up the server.
First launch runs `uv sync` automatically. Smoke-test the plumbing (read-only, no
clicks) with:

```bash
uv run hypr-cua-mcp --selftest
```

## Install the dependencies

The server shells out to a handful of CLI tools. `screenshot` needs `grim`, the
clipboard tools need `wl-clipboard`, and everything reads/drives Hyprland through
`hyprctl`; `tmux` and a Chromium/Chrome are only needed for the optional terminal
and browser channels.

| Tool | Needed for | Arch | Debian/Ubuntu |
|------|-----------|------|---------------|
| `hyprctl` | tree + control (**required**) | bundled with `hyprland` | bundled with `hyprland` |
| `grim` | `screenshot` / `screenshot_window` (**required**) | `pacman -S grim` | `apt install grim` |
| `wl-clipboard` | `read_clipboard` / `write_clipboard` | `pacman -S wl-clipboard` | `apt install wl-clipboard` |
| `uv` | runs the server + syncs Python deps | `pacman -S uv` | see [astral.sh/uv](https://docs.astral.sh/uv/) |
| `tmux` | `tmux_*` / `task_*` tools (optional) | `pacman -S tmux` | `apt install tmux` |
| Chromium / Chrome | `cdp_*` browser tools (optional) | `pacman -S chromium` | `apt install chromium` |

Arch, in one go (Hyprland assumed already installed):

```bash
sudo pacman -S grim wl-clipboard uv tmux chromium
```

Input uses a self-owned **uinput** device, so the process needs read-write on
`/dev/uinput` — on most setups a `uaccess`/udev ACL already grants that to the
logged-in user (no root, no `ydotoold`). The Python deps (`mcp`, `pillow`,
`evdev`, `websocket-client`) are synced by `uv` on first launch.

Miss one and the matching tool says so: calling `screenshot` without `grim`
returns *"grim not found; install it …"* straight to the model, so it can tell you
what to install instead of failing on a cryptic traceback.

## Safety

- No auth — it's a local stdio server. Don't expose it over a socket.
- **There is no sandbox on Wayland**: it drives your real desktop and can click
  anything you can — terminals with sudo, your password manager, Slack, email.
  Supervise live runs.
- Emergency stop: kill the MCP server (its uinput device dies with it).
- **`follow_mouse=1` caveat:** Hyprland keyboard focus tracks the pointer, so
  `focus_window(address)` is best-effort — the reliable way to focus a window for
  typing is to **click it** via screenshot coordinates. `type_text(con_id=…)`
  raises rather than typing into an unverified window.
- Input is real uinput, so it resets the compositor idle timer naturally (no
  synthetic-input self-lock, unlike X11). See [`docs/gotchas.md`](./docs/gotchas.md).

## License

MIT — see [LICENSE](./LICENSE).
