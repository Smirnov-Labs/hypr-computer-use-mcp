# AGENTS.md

Instructions for coding agents (Claude Code, Codex, …) working in this repo. Human
contributors: this doubles as the contributor quickstart.

`hypr-cua-mcp` is a **Hyprland/Wayland computer-use MCP server** — it lets a model
*see* the desktop (`grim` screenshots), *understand* it (`hyprctl` tree), and *drive*
it (a self-owned `uinput` device). Python, `uv`-managed, stdio transport.

## Setup & run

```bash
uv sync                            # install deps into .venv (first launch also does this)
uv run hypr-cua-mcp                # start the stdio server (normally the MCP client launches it)
uv run hypr-cua-mcp --selftest     # read-only smoke test: tree + outputs + one capture + mouse (no clicks)
```

Entry point is `hypr_cua_mcp.server:main` (`pyproject.toml`); `bin/hypr-cua-mcp` is the
wrapper an MCP client registers. System CLI deps (`grim`, `wl-clipboard`, `hyprctl`,
optional `tmux` / Chromium) are listed in the README's install table.

## Tests

```bash
uv run pytest                      # the whole suite — also what CI runs
```

- **TDD, always.** New tool or bugfix → write the failing test first, watch it fail,
  then implement the minimum to pass.
- Tests are **hermetic**: they mock the compositor boundary (`hyprcontrol._dispatch`,
  `capture._grab_root`, `subprocess.run`, `tmux.*`) and never require a live Hyprland.
  Keep it that way — don't add tests that shell out to `hyprctl`/`grim`. The suite runs
  in well under a second; keep it fast.

## Architecture (where things live)

Full write-up in [`docs/architecture.md`](docs/architecture.md); hard-won environment
lessons in [`docs/gotchas.md`](docs/gotchas.md). The short version:

| File | Responsibility |
|------|----------------|
| `server.py` | MCP layer (`MCPServer`), tool defs, the `[Image, caption]` response shape, `--selftest` |
| `capture.py` | `grim` whole-layout grab → Pillow crop → downscale to the vision-API budget |
| `coords.py` | screenshot-pixel ↔ global-pixel mapping; the `frame_id` store |
| `hyprtree.py` | read-only `hyprctl -j` (windows / outputs) |
| `hyprcontrol.py` | mutations via `hyprctl eval 'hl.dispatch(hl.dsp.…)'` (focus / workspace / float / launch) |
| `inputs.py` | input synthesis via a self-owned `evdev.UInput` (abs pointer + wheel + keyboard) |
| `tmux.py` / `taskrunner.py` | hidden-tmux terminal + task channels (no window, no focus) |
| `cdp.py` | drive Chrome tabs over the DevTools protocol (attach-only) |
| `clipboard.py` | `wl-clipboard` read / write |

## Conventions that bite if you ignore them

- **This server targets Lua-configured Hyprland (0.55+).** With a `hyprland.lua`
  config, `hyprctl dispatch X` and `hyprctl keyword` are DEAD (parsed as Lua). Mutations go through `hyprctl eval 'hl.dispatch(hl.dsp.<path>({…}))'`
  — build these in `hyprcontrol.py`, quoting user strings with `_lua_str`. Window
  targets are the `"address:0x…"` selector.
- **The `frame_id` contract is load-bearing.** Pointer tools take coordinates *in a
  specific screenshot's pixel space* plus its `frame_id`, and reject a missing/stale id
  rather than mapping against the wrong frame. Don't add a pointer tool that takes raw
  global pixels.
- **`follow_mouse=1` ties keyboard focus to the pointer.** Focusing a window/workspace
  warps the cursor, so `hyprcontrol._focus_keeping_cursor` restores it afterwards — a
  background agent must not yank the user's real, shared cursor. The restore has to be a
  *synthetic* `hl.dsp.cursor.move` (a real uinput move would re-trigger follow_mouse and
  steal focus back). Prefer click-to-focus over `focus_window` when you need to type.
- **Image-returning tools** use `@mcp.tool(structured_output=False)` and return
  `[Image(...), caption_str]`.
- **Missing a CLI dep should self-explain.** When a tool shells out, catch
  `FileNotFoundError` and raise a message naming what to install (see `capture.py`,
  `clipboard.py`, `tmux.py`) — the client shows it to the model.
- **No sandbox.** This drives the real desktop with real input events; there is no
  Wayland equivalent of Xephyr. Assume any change can move the user's mouse/keyboard.
  Supervise live runs.

## Commits & PRs

- Conventional commits (`fix:`, `feat:`, `docs:`) — match the existing history.
- `uv run pytest` green before pushing; CI (`.github/workflows/ci.yml`) enforces it.
- Learned something the hard way about Hyprland/Wayland? Add it to `docs/gotchas.md`.

## Releasing

1. Bump `version` in `pyproject.toml` and in **both** `version` fields of
   `server.json`, then `uv lock` (CI's `uv sync --locked` fails on a stale lock).
   `__version__` reads the package metadata, so there is no third place.
2. Merge to `main`, then publish a GitHub release tagged `vX.Y.Z`. That runs
   `publish.yml`, which uploads to PyPI via Trusted Publishing.
3. Re-list on the MCP registry: `gh workflow run publish-mcp.yml` (needs the
   `MCP_DNS_PRIVATE_KEY` repo secret), or locally with
   `mcp-publisher login dns --domain smirnovlabs.com --private-key <hex> && mcp-publisher publish`.
