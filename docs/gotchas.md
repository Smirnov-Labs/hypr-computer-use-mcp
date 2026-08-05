# Gotchas & environment notes

Hard-won lessons from building and verifying this on hexane (Arch, Hyprland
0.56.1, Wayland, multi-output). Most cost real debugging time.

## `hyprctl dispatch` is Lua-wrapped (the big one)

This machine configures Hyprland in **Lua** (`~/.dotfiles/hypr/hyprland.lua`), so
a plain `hyprctl dispatch movecursor 300 300` is parsed as Lua and fails:

```
error: return hl.dispatch(movecursor 300 300)  -- ')' expected near '300'
```

Dispatchers are Lua objects under `hl.dsp.*`; `hl.dsp.X(...)` builds a closure and
`hl.dispatch(closure)` runs it. So the working form is:

```
hyprctl eval 'hl.dispatch(hl.dsp.focus({ workspace = 5 }))'
hyprctl eval 'hl.dispatch(hl.dsp.window.move({ workspace = 5, follow = false, window = "address:0x…" }))'
hyprctl eval 'hl.dispatch(hl.dsp.window.float({ action = "toggle", window = "address:0x…" }))'
hyprctl eval 'hl.dispatch(hl.dsp.focus({ window = "address:0x…" }))'
hyprctl eval 'hl.dispatch(hl.dsp.exec_cmd("alacritty"))'
```

Window targets use the `"address:0x…"` selector (the exact form `hyprctl clients`
reports). `hyprctl eval` prints only `ok`/`error:` — never a value — so to
introspect the API force values through `error("…")`. See `hyprcontrol.py`.

## ydotool absolute coords are ~2× and clamp (why we don't use it)

`ydotool mousemove -a -x 300 -y 300` lands at ~`(600,600)`; `-x 5200` clamps to
the framebuffer edge. Its uinput abs-axis range doesn't match the multi-monitor
layout. Also, **ydotool has no scroll/wheel command** (`click/mousemove/type/key`
only). So instead of ydotool we own a uinput device directly (below).

## Self-owned uinput device (the input backend)

An `evdev.UInput` with `EV_ABS` X/Y (range `0..65535`), `EV_KEY` (buttons + a
US-ASCII keymap), and `EV_REL` wheel, created with `input_props=[INPUT_PROP_POINTER]`
(**required** — without it the compositor treats an abs device as a *touchscreen*
and won't move the cursor). Findings:

- **Abs mapping is linear:** the compositor maps `[0, 65535] → full layout extent`
  per axis, so `abs = pixel * 65535 / extent` (extent from `hyprctl monitors`).
  Round-trip accuracy is ~1px.
- **~1s settle:** after creating the device, wait ~1s for the compositor to bind
  it before the first event, or the first move is dropped. The MCP server creates
  it once at first use and keeps it.
- **Rootless:** `/dev/uinput` is usually `crw-rw----+` with a `uaccess` ACL
  granting the logged-in user rw — so no root and no `ydotoold` daemon.
- **Real input resets idle:** uinput events are genuine input, so they reset the
  compositor idle timer — none of the X11 synthetic-input self-lock problem, hence
  `inputs.reset_idle()` is a no-op.

## `follow_mouse = 1` steals focus

Hyprland's default `follow_mouse = 1` makes keyboard focus track the pointer, so a
`hl.dsp.focus({window=…})` dispatch is immediately reverted to whatever is under
the cursor. `focus_window(address)` is therefore best-effort. The reliable way to
focus a window for typing is to **click it** via screenshot coordinates.
`type_text(con_id=…)` verifies focus and raises rather than typing into the wrong
window.

## Workspace → monitor affinity

On multi-monitor, `goto_workspace(N)` (`hl.dsp.focus({workspace=N})`) sends you to
wherever workspace N lives — not necessarily the currently focused monitor. Don't
assume a launched window landed on the monitor you were looking at; re-query
`list_windows`/`get_outputs` for ground truth.

## grim captures the whole layout at global origin

`grim -t png -` (no `-o`/`-g`) composites **every** output into one image sized to
the layout bounding box, with image `(0,0)` = global `(0,0)`. That lets us crop by
global coordinates exactly. Downscaling the whole multi-monitor grab would destroy
click accuracy, so we always crop to one output/window first.

## wl-copy backgrounds a holder (pipe-hang)

`wl-copy` forks a resident process to serve the selection. Under `subprocess.run`
with captured pipes, that holder can keep the pipe open and hang the call until
timeout — so writes route stdout/stderr to `DEVNULL` (`_run(capture=False)`), same
trick the X11 xclip port used.

## MCP servers load at client startup

A newly `claude mcp add`-ed server (or new code in an existing one) isn't in the
running session — `/mcp` → reconnect, or restart Claude Code, to pick it up.
