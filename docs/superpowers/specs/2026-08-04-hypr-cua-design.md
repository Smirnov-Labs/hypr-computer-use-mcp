# hypr-cua — design (Hyprland/Wayland fork of i3-cua)

- **Date:** 2026-08-04
- **Status:** design approved (user chose Option B input backend); building under nonstop
- **Repo:** `~/Projects/1.Personal/hypr-computer-use-mcp`
- **Forked from:** `i3-computer-use-mcp` (X11) — identifiers renamed, backends being swapped.

## Goal

A Hyprland/Wayland analog of i3-cua: an MCP server that lets a model SEE the desktop (grim), UNDERSTAND it (hyprctl tree), and DRIVE it (uinput input synthesis), keeping i3-cua's frame-id coordinate contract. hexane migrated off i3/X11 to Hyprland on 2026-08-04, so this is a **clean fork**, not a dual-backend.

## Reused from i3-cua (unchanged / lightly adapted)

- `coords.py` — global-pixel `CaptureFrame` + `FrameStore` + the frame-id contract. Unchanged (Hyprland geometry is global pixels; scale guarded).
- `server.py` — tool wiring + safety contracts (frame-id resolution; focus-verify before keystrokes). Adapted: imports `hyprtree`/`hyprcontrol`; control tool bodies change.
- The ~21-tool surface and the secret-safe clipboard design.

## Backend swaps

| module | i3-cua (X11) | hypr-cua (Wayland) |
|---|---|---|
| `capture.py` | ImageMagick `import -window root` | **grim** — output `grim -o <name> -`, region `grim -g "<x>,<y> <w>x<h>" -` → PNG on stdout → same Pillow crop/downscale + per-axis scale. |
| `clipboard.py` | xclip | **wl-clipboard** — read `wl-paste -n` (`--primary` for PRIMARY); write `wl-copy` (`--primary`). Secret posture identical to i3-cua. |
| `hyprtree.py` (was `i3tree.py`) | i3ipc | **`hyprctl -j`** clients/monitors/workspaces/activewindow. Map: `address`→id (hex str), `at`+`size`→rect, `workspace.name`, `monitor` name, `focusHistoryID==0`→focused, `floating`/`hidden`. |
| `hyprcontrol.py` (was `i3control.py`) | i3 commands | **`hyprctl dispatch`** — `workspace N`; `movetoworkspacesilent N,address:0x…`; `togglefloating address:…`; `exec …`; `focuswindow address:…`. ⚠️ hyprctl is LUA-wrapped on this box — resolve the correct invocation at build time. |
| `inputs.py` | xdotool | **evdev `UInput`** device (Option B, below). |

## `inputs.py` — evdev uinput backend (Option B)

- One lazily-created module-level `UInput` device with capabilities: `EV_ABS` (`ABS_X`,`ABS_Y`, absinfo max large & fixed), `EV_KEY` (`BTN_LEFT/RIGHT/MIDDLE` + keyboard `KEY_*`), `EV_REL` (`REL_WHEEL`,`REL_HWHEEL`). Owns `/dev/uinput` directly (ACL grants `vania` rw — no sudo, no ydotoold).
- **Absolute positioning with self-calibration:** the device's abs range maps to the compositor layout by an unknown linear factor (a probe with ydotool's implicit device showed K≈2). So at startup emit a couple of known abs values, read `hyprctl cursorpos`, derive `K_x,K_y` (+offset), and map global `(X,Y)`→device coords. Never hardcode the factor.
- `move`, `click(button,count)`, `scroll(direction,amount)` via `REL_WHEEL` (native — ydotool's scroll gap is gone), `type_text` (char→`KEY_*`+shift), `key(chord)` (parse `ctrl+s`→modifier+key).
- **`reset_idle` is dropped:** uinput emits *real* input events, so the compositor's idle timer resets naturally — the X11 synthetic-input DPMS-self-lock problem does not recur. Document this.

## Coordinate model

Same as i3-cua (global pixels, per-axis scale in `CaptureFrame`). Hyprland monitors report `x,y,width,height,scale` (all 1.0 now); grim captures physical px, so at scale 1 global==physical. Guard scale≠1. The uinput self-calibration handles abs→layout.

## Dependencies

- **Add:** `evdev` (done, venv). **Drop:** `i3ipc`, `python-xlib` (X11-only). **Keep:** `mcp`, `pillow`.
- **System:** `grim`, `wl-clipboard`, `hyprctl` (Hyprland). ydotool NOT needed for Option B.

## Testing

- **Unit:** subprocess-mocked (grim/hyprctl/wl-clipboard) + `UInput`-mocked (no compositor/uinput needed), mirroring i3-cua's style.
- **Live smoke** on Hyprland: calibration, grim capture (decode PNG), hyprctl tree read, wl-clipboard round-trip, a scoped cursor move+`cursorpos` check, launch+kill a throwaway terminal on an empty workspace.

## Non-goals

No X11/dual-backend, no Xephyr sandbox (test live, scoped), no CDP/tmux/task-runner (i3-cua roadmap items, separate).

## Open items to resolve empirically during build

1. LUA-wrapped `hyprctl dispatch` invocation (test live).
2. `wl-copy`/`wl-paste` round-trip + selection behavior (verify).
3. evdev `UInput` abs calibration factor (measure live).
4. `type_text` keymap coverage (which chars map cleanly).
