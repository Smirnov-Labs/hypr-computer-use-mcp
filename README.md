# hypr-cua-mcp

An **i3-aware computer-use MCP server** for X11. It gives an MCP client (Claude
Code, etc.) the ability to *see* your desktop, *understand* it via the i3 tree,
and *drive* it with mouse + keyboard.

Why this exists: generic Linux computer-use MCP servers are blind to the window
manager — they hand the model a giant multi-monitor screenshot and hope its
pixel guesses land. On a tiling WM with a **7680×2880 three-output** framebuffer
that's a bad bet. This server crops to one output (or one window), downscales to
the vision-API budget, and — crucially — exposes i3's tree so the model targets
windows by identity, not luck. Coordinates are always given *in the screenshot
the model was just shown*; the server maps them back to global X pixels.

Built for X11 (`xdotool` + ImageMagick `import` + i3 IPC). Not Wayland.

## Tools

| Tool | What it does |
|------|--------------|
| `list_windows` | Every i3 window: con id, class, title, workspace, output, global rect, focus/visibility |
| `get_outputs` | Active monitors and their global geometry |
| `screenshot(output?)` | Screenshot one output (default: primary), downscaled to the API budget |
| `screenshot_window(con_id)` | Full-res crop of a single window — best for reading small text |
| `click / double_click / move_mouse / drag / scroll` | Pointer actions; coords in a screenshot's pixel space (pass its `frame_id`) |
| `focus_window(con_id)` | i3-focus a window before typing |
| `type_text(text, con_id?) / key(keys, con_id?)` | Keyboard input (optionally focus first) |
| `get_mouse` | Current global pointer location |
| `read_clipboard(selection?)` / `write_clipboard(text, selection?, secret?)` | Exact text in/out of the X `clipboard`/`primary` selection (no OCR); `write` returns only a length, never the value |
| `launch(command)` | Launch an app via i3 `exec` |
| `goto_workspace(ws)` / `move_window_to_workspace(con_id, ws)` | Switch to / move a window to a workspace |
| `set_floating(con_id, enable?)` / `set_layout(con_id, layout)` | Toggle floating / set container layout |
| `run_i3(command)` | Raw i3-command escape hatch (`exit` blocked) |

Scope is **read + focus + input + i3 control** (workspaces / layout / launch).
The `run_i3` escape hatch blocks `exit`; details in
[`DESIGN-i3-control-extension.md`](./DESIGN-i3-control-extension.md).

## The coordinate model (the important bit)

`xdotool` and the i3 tree use **global** X coordinates spanning all monitors.
Screenshots are **cropped + downscaled**. So every screenshot records a mapping
frame (region origin + scale), and the pointer tools accept coordinates *in the
screenshot's pixel space* and translate them:

```
global_x = region.x + screenshot_x / scale_x
global_y = region.y + screenshot_y / scale_y
```

Practically: take a screenshot (it returns a `frame_id`), then call
`click(x, y, frame_id=…)` with the pixel you see in *that* image. Pointer tools
reject a missing or stale `frame_id` rather than guessing against the wrong one.

## Run it safely first (Xephyr sandbox)

This server can move your real mouse and type into your real windows. Don't point
it at `:0` until you've watched it behave. Instead:

```bash
# terminal 1 — a sandboxed nested desktop on :1
./sandbox/run-xephyr.sh

# terminal 2 — prove the plumbing against the sandbox (read-only, no clicks)
DISPLAY=:1 uv run hypr-cua-mcp --selftest
```

The `bin/hypr-cua-mcp` wrapper **defaults to `:1`**, so a freshly-registered
server targets the sandbox until you opt into live.

## Register with Claude Code

```bash
# sandbox (safe default, DISPLAY=:1):
claude mcp add hypr-cua -- /home/vania/Projects/1.Personal/i3-computer-use-mcp/bin/hypr-cua-mcp

# live desktop (:0) — add when you trust it, as a separate entry:
claude mcp add hypr-cua-live -e I3CUA_DISPLAY=:0 -- \
    /home/vania/Projects/1.Personal/i3-computer-use-mcp/bin/hypr-cua-mcp
```

Restart / reconnect Claude Code to pick up a newly-added server. First launch
runs `uv sync` automatically (downloads Python 3.12 + deps into `.venv`).

## Requirements

System: `xdotool`, `imagemagick` (`import`), `xclip`, `i3`, and for the sandbox `Xephyr`
(`xorg-server-xephyr`) + `xterm`. Python deps (`mcp`, `i3ipc`, `pillow`) are
managed by `uv`.

## Safety

- No auth — it's a local stdio server. Don't expose it over a socket.
- On `:0` it can click anything you can: terminals with sudo, your password
  manager, Slack, email. Prefer the sandbox; supervise live runs.
- Emergency stop for a live run: kill the MCP server, or `pkill xdotool`.
- Input tools (mouse, keyboard) and `launch` reset the idle timer (`xset s reset`)
  so active runs don't self-lock; perception, clipboard, and workspace/layout tools
  don't. The screen still locks normally when idle. See [`docs/gotchas.md`](./docs/gotchas.md).

## License

MIT — see [LICENSE](./LICENSE).
