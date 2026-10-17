# Known issues and limitations

What's rough or unverified today, and the workaround where there is one. The README
has the [short version](README.md#known-limitations); this is the detail.

## Window control needs a Lua config

The window-control tools send `hyprctl eval 'hl.dispatch(hl.dsp.…)'`, the
dispatcher API of a Lua config (`hyprland.lua`, Hyprland 0.55+). That covers
`focus_window`, `launch`, `goto_workspace`, `move_window_to_workspace`,
`set_floating`, `layout_message` and `run_dispatch`, plus `type_text`/`key` with a
`con_id` and `move_mouse(follow=True)`. On a classic `hyprland.conf` setup those
tools fail. Everything else works with either config.

## Focus: click, don't hover

- Under `input:follow_mouse = 1`, keyboard focus tracks the pointer, so whatever
  is under the cursor can take focus back after a focus dispatch. `focus_window`
  verifies (and briefly retries) but stays best-effort. The reliable way to focus a window
  for typing is to **click it**.
- Moving the synthetic pointer over a window doesn't trigger Hyprland's
  hover-focus (the physical mouse does). `move_mouse(follow=True)` focuses the
  window under the pointer explicitly.
- `type_text(con_id=…)` and `key(con_id=…)` focus first. If focus doesn't land,
  they raise without sending a keystroke.

## Typing is US-ASCII only

`type_text` maps characters to evdev keycodes for a US layout. It skips anything
outside that map. Non-US xkb layouts are untested. For exact text,
`write_clipboard` and paste instead (`key("ctrl+v")`, or `ctrl+shift+v` in a
terminal).

## Scaled monitors are unverified

The window tree and the pointer work in logical layout pixels, so they agree on a
scaled monitor. Capture isn't verified there. `capture.py` grabs the whole layout
with `grim -` in physical pixels, then crops by the logical rect. The two differ
when scale ≠ 1. All testing so far used scale 1.0. The likely fix is per-output
capture (`grim -o <name>`) with the crop scaled to match.

## Browser channel (`cdp_*`)

- `cdp_click` / `cdp_type` inject JavaScript (`element.click()`; set `value` and
  dispatch `input`/`change`) rather than sending `Input.dispatch*` events, so
  handlers that require trusted events won't fire.
- Chrome M111+ rejects the DevTools WebSocket without `--remote-allow-origins`.
  Auto-launch and `bin/hypr-cua-chrome` pass it; add it yourself if you start the
  browser some other way.
- The test suite mocks Chrome completely. The channel was smoke-tested live against
  a real Chrome, but unusual DevTools replies, slow launches and very large pages
  can still surprise it.

## Task runner (`task_*`)

A task is a detached tmux session on the isolated `hypr-cua` socket. `task_start`
wraps the command so that it prints its exit code on a sentinel line. The wrapper
then keeps the pane alive, so that line stays readable:

```
sh -c '<command>'; printf '\n__TASK_DONE__:%s\n' "$?"; exec sleep 2147483647
```

`task_status` classifies from that sentinel alone: `running` until it appears, then
`done` + `exit_code`; `missing` if the session is gone. Caveats:

- There's no "waiting for input" state. A task blocked on a prompt reads as
  `running`; read the `tail` from `task_status` and answer with `task_send`.
- `task_send(…, enter=False)` with `C-c` interrupts the wrapper too, so the task
  ends up `missing` rather than `done`. Use `task_stop` for a clean stop.
- Backgrounded commands (`cmd &`) report `done` immediately while the child keeps
  running. Run tasks in the foreground.
- If a command's last line of output is exactly `__TASK_DONE__:<n>`, the task
  reads as finished.
- A finished task's pane stays alive (the keep-alive `sleep`) until `task_stop`, so
  `tmux_list` still shows it as live.

## Smaller things

- `read_clipboard` is text-only: with an image on the clipboard it returns
  `<non-text clipboard: image/png>` instead of the data.
- A few failure paths surface raw subprocess errors instead of the friendlier
  messages used elsewhere: the `hyprctl monitors` and `cursorpos` queries in
  `inputs.py`, and a non-zero `grim` exit in `capture.py`.
- `move_window_to_workspace` and `set_floating` report success without re-reading
  the window's state.
- MCP clients read the tool list at connect time: after upgrading, reconnect
  (Claude Code: `/mcp` → reconnect).
