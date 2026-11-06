# hypr-cua-mcp

[![Status: stable · maintained](https://img.shields.io/badge/status-stable%20%C2%B7%20maintained-brightgreen)](https://github.com/Smirnov-Labs/hypr-computer-use-mcp#project-status)
[![CI](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hypr-cua-mcp)](https://pypi.org/project/hypr-cua-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/hypr-cua-mcp)](https://pypi.org/project/hypr-cua-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/LICENSE)

A **Hyprland/Wayland computer-use MCP server**. It gives an MCP client (Claude
Code, etc.) the ability to *see* your desktop, *understand* it via the Hyprland
tree, and *drive* it with mouse + keyboard. It runs locally over stdio.

Why this exists: generic Linux computer-use servers are blind to the compositor —
they hand the model a giant multi-monitor screenshot and hope its pixel guesses
land. This server crops to one output (or one window) with `grim`, downscales to
the vision-API budget, and exposes `hyprctl`'s tree so the model targets windows
by identity, not luck. Coordinates are always given *in the screenshot the model
was just shown*; the server maps them back to global layout pixels.

Built for **Wayland/Hyprland**: `grim` (capture) + a self-owned **uinput** device
(input) + `hyprctl` (tree/control) + `wl-clipboard`. It is the Wayland successor
to [i3-computer-use-mcp](https://github.com/Smirnov-Labs/i3-computer-use-mcp) (X11).

## Tools

Window ids are **Hyprland addresses** (`0x…`) from `list_windows` — not integers.

**See**

| Tool | What it does |
|------|--------------|
| `list_windows` | Every Hyprland window: id (**address**), class, title, workspace, output, global rect, floating/focus/visibility |
| `get_outputs` | Active monitors: global geometry, scale, workspace |
| `screenshot(output?)` | Screenshot one output (default: focused), downscaled to the API budget |
| `screenshot_window(con_id)` | Full-res crop of a single window (by address) — best for small text |
| `get_mouse` | Current global pointer location |

**Act**

| Tool | What it does |
|------|--------------|
| `click / double_click / move_mouse / drag / scroll` | Pointer actions; coords in a screenshot's pixel space (pass its `frame_id`). `move_mouse(follow=True)` also focuses the window under the pointer |
| `type_text(text, con_id?) / key(keys, con_id?)` | Keyboard input; with a `con_id`, focus that window first and refuse to type if focus doesn't land |
| `read_clipboard(selection?)` / `write_clipboard(text, selection?, secret?)` | Exact text in/out of the `clipboard`/`primary` selection (no OCR); `write` returns only a length, never the value |

**Control Hyprland** (needs a Lua config, see [Requirements](#requirements))

| Tool | What it does |
|------|--------------|
| `focus_window(con_id)` | Focus a window by address (best-effort; see the follow_mouse caveat under [Safety](#safety)) |
| `launch(command)` | Launch an app via Hyprland's exec dispatcher |
| `goto_workspace(ws)` / `move_window_to_workspace(con_id, ws)` | Switch to / move a window (by address) to a workspace |
| `set_floating(con_id, enable?)` | Toggle a window's floating state |
| `layout_message(message)` | Send a message to the active layout engine (e.g. `togglesplit`) |
| `run_dispatch(dsp_expr)` | Raw Hyprland dispatcher escape hatch (`exit` blocked) |

**Work off-screen** (no window, no focus stolen)

| Tool | What it does |
|------|--------------|
| `tmux_list / tmux_new / tmux_run / tmux_keys / tmux_read / tmux_kill` | Drive terminals in detached tmux sessions on an isolated `hypr-cua` socket; it never touches your own tmux sessions |
| `task_start / task_status / task_list / task_send / task_stop` | Run a command as a background task and poll it (`running` → `done` + exit code); answer prompts with `task_send` |
| `cdp_targets / cdp_open / cdp_navigate / cdp_read / cdp_eval / cdp_screenshot / cdp_click / cdp_type / cdp_close` | Drive Chrome tabs over the DevTools protocol, never your main profile (see [Browser channel](#browser-channel)) |

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
reject a stale or unknown `frame_id` rather than guessing against the wrong one.
Without a `frame_id`, they use your latest screenshot.

The uinput pointer positions absolutely: global pixels map to the device's abs
axes via the layout extent read from `hyprctl monitors` (verified ~1px).

## Requirements

- **Hyprland on Wayland.** Developed and tested on Hyprland 0.56 across three
  monitors at scale 1.
- **A Lua config (`hyprland.lua`, Hyprland 0.55+) for the window-control tools.**
  These tools go through `hyprctl eval` and Hyprland's Lua `hl.dispatch` API:
  `focus_window`, `launch`, `goto_workspace`, `move_window_to_workspace`,
  `set_floating`, `layout_message`, `run_dispatch`, plus `type_text`/`key` with a
  `con_id` and `move_mouse(follow=True)`. Everything else works with any config:
  screenshots, pointer and keyboard input, the window tree, the clipboard, and the
  tmux/task/browser channels.
- **Read-write access to `/dev/uinput`** for mouse and keyboard input (see below).
- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/).
- It runs **inside your Hyprland session**: it needs `WAYLAND_DISPLAY`,
  `XDG_RUNTIME_DIR` and `HYPRLAND_INSTANCE_SIGNATURE` from the environment. MCP
  clients launched from your desktop pass these through.

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
`/dev/uinput`. It needs no root and no `ydotoold`. Run `getfacl /dev/uinput`. If it
lists your user with `rw` (typically through a `uaccess` udev rule), you're set. If
not, add a rule that grants access to the user logged in at the seat:

```bash
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-hypr-cua-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
```

The Python deps (`mcp`, `pillow`, `evdev`, `websocket-client`) are synced by `uv`
on first launch.

Miss one and the matching tool says so: calling `screenshot` without `grim`
returns *"grim not found; install it …"* straight to the model, so it can tell you
what to install instead of failing on a cryptic traceback.

## Register with your MCP client

It's a local stdio server. The client starts it on demand, and you don't need a
clone.

**Claude Code:**

```bash
claude mcp add hypr-cua -- uvx hypr-cua-mcp
```

**Clients with an `mcpServers` JSON config** (Cursor, Gemini CLI, Cline, …):

```json
{
  "mcpServers": {
    "hypr-cua": {
      "command": "uvx",
      "args": ["hypr-cua-mcp"]
    }
  }
}
```

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.hypr-cua]
command = "uvx"
args = ["hypr-cua-mcp"]
```

**From a local checkout:** the wrapper syncs deps on first launch. When a
[shared server](#one-shared-server-per-login) is running, the wrapper connects to it.

```bash
claude mcp add hypr-cua -- /path/to/hypr-computer-use-mcp/bin/hypr-cua-mcp
```

Reconnect your client (in Claude Code: `/mcp` → reconnect) to pick up the server.
Smoke-test the plumbing first (read-only, no clicks):

```bash
uvx hypr-cua-mcp --selftest
```

## One shared server per login

By default each MCP client session starts its own hypr-cua. From a checkout you can
run one shared server per desktop login instead. A session then connects in
milliseconds and costs about 12 MB instead of about 94 MB. `bin/hypr-cua-mcp`
connects to the shared server when it runs. When it doesn't, the wrapper starts a
stdio server for the session, so the registration stays the same.

1. Install the units. Run this in the checkout:

   ```bash
   uv sync
   mkdir -p ~/.config/systemd/user
   cp contrib/systemd/hypr-cua.socket ~/.config/systemd/user/
   sed "s|@CHECKOUT@|$PWD|" contrib/systemd/hypr-cua.service > ~/.config/systemd/user/hypr-cua.service
   systemctl --user daemon-reload
   ```

2. Start the socket from your Hyprland startup, so each new server gets the current
   session's variables. In `hyprland.lua`, inside your startup handler:

   ```lua
   hl.exec_cmd("systemctl --user import-environment DISPLAY WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP; systemctl --user stop hypr-cua.service; systemctl --user restart hypr-cua.socket")
   ```

   Run the same command in a terminal once to start it without logging out.

3. Reconnect your MCP sessions (in Claude Code: `/mcp` → reconnect).

To upgrade, run `git pull && uv sync && systemctl --user restart hypr-cua.service`.
Open sessions reconnect by themselves. A call that was running during the restart
fails once with a message that it may have completed. The restart also ends
hypr-cua's tmux sessions and tasks.

Sessions share the server's browser. Its `HYPR_CUA_CDP_*` settings come from the
service's `Environment=` lines, not from the client. Screenshots stay per session:
the server rejects another session's `frame_id` as stale.

## Browser channel

The `cdp_*` tools drive Chrome or Chromium tabs over the DevTools protocol, so
browser work needs no window focus. They never open your main profile.

The first `cdp_*` call picks a browser, and later calls keep it while it stays up:

1. If a browser answers on port 9222, the tools attach to it. Run your automation
   profile there to keep logins.
2. Otherwise they start a private headless Chrome with a throwaway profile. Chrome
   picks a free port, so it never collides with 9222. It stops after 10 minutes
   without a call, and when the server exits.

`cdp_targets` reports which one is in use, `"browser": "automation"` or
`"private"`. All sessions of one server share that browser, its tabs and its
cookies.

For logins that persist, start a dedicated automation profile on port 9222. From a
checkout, `bin/hypr-cua-chrome` does that. Otherwise run:

```bash
chromium --user-data-dir="$HOME/.local/share/hypr-cua-chrome" \
  --remote-debugging-port=9222 --no-first-run --no-default-browser-check
```

Log in only to what you're happy to automate. Any local process that can reach a
DevTools port can drive that browser, and that includes the private one.

`cdp_open` and `cdp_navigate` wait for the page's load event, and for a CSS
selector if you pass `wait_for`. A page still loading at `timeout` (default 30 s)
comes back with `"timed_out": true` instead of an error. `cdp_read` takes a
`selector` and `max_chars`. `cdp_screenshot` returns an image of a tab's viewport.

| Variable | Default | Effect |
|----------|---------|--------|
| `HYPR_CUA_CDP_PORT` | `9222` | Port of the automation browser to attach to |
| `HYPR_CUA_CDP_AUTOLAUNCH` | `1` | `0` = attach-only: never start a private Chrome, and fail if nothing answers on the port |
| `HYPR_CUA_CDP_BIN` | first Chrome/Chromium on `PATH` | Browser binary for the private Chrome |
| `HYPR_CUA_CDP_USER_DATA_DIR` | `~/.local/share/hypr-cua-chrome` | Profile directory for `bin/hypr-cua-chrome` |

Set them in your client's server config. In Claude Code:
`claude mcp add hypr-cua -e HYPR_CUA_CDP_AUTOLAUNCH=0 -- uvx hypr-cua-mcp`. In JSON
configs, add an `"env": {…}` block next to `command`.

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
  synthetic-input self-lock, unlike X11). See
  [`docs/gotchas.md`](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/docs/gotchas.md).

## Known limitations

- Typing is US-ASCII only: `type_text` skips any other character. For anything
  else, `write_clipboard` and paste.
- Capture on monitors with scale ≠ 1 is untested, and the crop is likely off there.
- The synthetic pointer doesn't hover-focus. Click to focus, or use
  `move_mouse(follow=True)`.
- `cdp_click` / `cdp_type` inject JavaScript, so pages that insist on trusted input
  events won't react.

Details, plus the task runner's edge cases:
[`KNOWN-ISSUES.md`](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/KNOWN-ISSUES.md).

## Project status

**Stable and maintained.** hypr-cua-mcp does what it set out to do, so the commit
log is quiet on purpose. Expect bug fixes, Hyprland-compatibility updates and
dependency bumps, not a stream of new features. A quiet repo here means done, not
abandoned. Issues and PRs still get answered.

## Contributing

Bug reports and fixes are welcome; for anything bigger, open an issue first. See
[`CONTRIBUTING.md`](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/CONTRIBUTING.md),
and [`AGENTS.md`](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/AGENTS.md)
for setup, tests, and the Hyprland conventions that bite.

## Registry

On PyPI as [`hypr-cua-mcp`](https://pypi.org/project/hypr-cua-mcp/) and in the MCP
registry as `com.smirnovlabs/hypr-cua-mcp` (manifest:
[`server.json`](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/server.json)).

mcp-name: com.smirnovlabs/hypr-cua-mcp

## License

MIT — see [LICENSE](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/blob/main/LICENSE).
