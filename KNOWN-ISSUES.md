# Known issues / open items (v0.1)

Built and unit-tested autonomously, then **live-validated on 2026-08-05** by driving
hexane's real 3-monitor Hyprland from an isolated scratch workspace.

## 1. Pointer focus into windows — RESOLVED (click-to-focus works)

Live test result: the essential interaction path **works end-to-end**.

**Verified live (2026-08-05):**
- Synthetic uinput pointer positioning is **pixel-accurate across monitors**
  (warped DP-1 → DP-2, landed within 1px of target).
- **Click focuses the clicked window.** Clicked the left of two tiled scratch
  terminals → `list_windows` focus moved off the right one onto the clicked one,
  and the focus border followed. This is all the practical `screenshot → click →
  type` flow needs.
- `type_text` and clipboard paste then land in the clicked window (see §3).

**Minor limitation (not blocking):** hover alone does **not** move focus with the
synthetic pointer — warping the cursor over a window under `input:follow_mouse=1`
does not trigger Hyprland's focus-follow (the *physical* mouse does focus-follow).
So last session's "focus didn't follow" was the hover path, not clicking.
- **Always click to focus; don't rely on hover.** The tools already do this
  (`click`, and `type_text(con_id=…)`/`key(con_id=…)` focus-first).
- **If hover-follow is ever wanted:** after the absolute warp, emit a tiny relative
  motion (`EV_REL REL_X +1/-1`) so wlroots processes a real motion delta and
  recomputes pointer focus. Needs `REL_X`/`REL_Y` added to the uinput caps and a
  live retest — deferred, since click-to-focus already covers the real use case.

## 2. `hyprtree.list_windows` `visible` flag — looks correct

Live multi-monitor readings were consistent: a window flipped to `visible:false`
exactly when its monitor switched away from its workspace, and the on-screen tiled
windows read `visible:true`. The false-positive worry from last session did not
reproduce. Keep an eye on it under stacked/special workspaces.

## 3. type_text / clipboard — live-verified

Verified live by typing into a focused terminal and reading the shell result back
from a file:
- `type_text` emits correct keystrokes incl. **capitals and shifted symbols**
  (`hypr_cua_TYPE_ok > /tmp/…` round-tripped exactly), and embedded `\n` = Enter.
- `write_clipboard` + `key('ctrl+shift+v')` pastes the exact value into the window.

Still **US-ASCII only** (unmapped chars skipped); non-US xkb layouts unverified
against the evdev keycodes.

## 3b. read_clipboard on binary selections — FIXED (2026-08-05)

Found live: with an **image on the clipboard** (browser "copy image" → `image/png`),
`read_clipboard` crashed with `UnicodeDecodeError: …byte 0x89` because
`subprocess.run(text=True)` tried to UTF-8-decode PNG bytes. Now it catches the
decode error and returns `"<non-text clipboard: image/png>"` (via `wl-paste
--list-types`) instead of raising. Covered by two new tests.

## 4. Monitor scale — capture path still needs a scaled monitor to verify

`hyprtree.get_outputs()` and `inputs._layout_extent()` report logical size
(`width/scale`), so tree + pointer coordinate spaces are consistent (all monitors
are scale 1.0 today, so this was a no-op in the live test). **Still open:**
`capture.py` grabs the whole layout with `grim -` (physical pixels) and crops by the
logical rect — for a `scale != 1` monitor those differ. Fix when a scaled monitor
exists: capture per-output with `grim -o <name>` and scale the crop.

## 5. Review-noted minor tidy-ups (deferred, not correctness-critical)

- `inputs._layout_extent`/`mouse_location` and `capture._grab_root` raise raw
  subprocess errors instead of friendly module exceptions (unlike hyprtree/hyprcontrol/clipboard).
- `move_window_to_workspace`/`set_floating` dispatch once and report success without
  re-verifying state (a wrong reverse-engineered Lua field would silently no-op).
- `server._focus_or_raise` + `hyprcontrol.focus_window` both call `find_window`
  (~5-7 subprocess spawns per `type_text(con_id=…)`) — latency only.
- Repo is **local-only** (not pushed). `.github/workflows/ci.yml` untested for the
  Wayland deps; `evdev` builds a C-extension — confirm it installs on CI before relying on it.

## 6. CDP browser channel (`cdp.py`) — unit-tested only, live-UNVERIFIED

New in this branch: a **focus-free Chrome channel** so browser work can run on a
non-visible ("copilot") workspace without stealing keyboard focus. `cdp.py` is a
synchronous CDP client (`websocket-client` for the WS, stdlib `urllib` for the
`/json` HTTP — no `requests`); MCP tools `cdp_targets`, `cdp_open`, `cdp_navigate`,
`cdp_eval`, `cdp_read`, `cdp_click`, `cdp_type`, `cdp_close`.

**What it does (by construction / unit tests):**
- Lists tabs via `http://127.0.0.1:<port>/json`; drives each tab over its
  `webSocketDebuggerUrl` with framed `Runtime`/`Page`/`Target` commands (monotonic
  `id`, unsolicited events skipped, `error`/`exceptionDetails` → `CdpError`).
- `eval_js` returns JS values by value and surfaces JS throws; `read_text` returns
  `{title,url,text}` (body.innerText); `click`/`type` inject `querySelector(...).click()`
  / value-set + `input`/`change` events (works without window focus). `type_text`
  returns a length summary, never the typed value.

**Profile/port isolation (safety):** the endpoint is a **DEDICATED** Chrome —
`ensure_browser()` auto-launches `google-chrome/chromium --remote-debugging-port=<port>
--user-data-dir=<isolated tmp dir> --no-first-run --no-default-browser-check` on
first use (default port **9222**, fresh temp profile), or attaches to an existing
`--remote-debugging-port`. It **never opens the user's main profile.** Configurable
via `HYPR_CUA_CDP_PORT` / `HYPR_CUA_CDP_USER_DATA_DIR` / `HYPR_CUA_CDP_BIN`; set
`HYPR_CUA_CDP_AUTOLAUNCH=0` to require an already-running endpoint instead of launching.

**⚠️ Caveat — the suite is FULLY MOCKED (no live Chrome).** Every test stubs the
HTTP `/json` and the WebSocket send/recv (and `_spawn` is a hard-fail tripwire), so
**real Chrome behavior is unverified**: the actual DevTools reply shapes, that a
freshly launched Chrome comes up within `LAUNCH_TIMEOUT`, `awaitPromise` timing,
large `innerText` frames, and CSP/cross-origin edge cases all still need a live smoke
test against a dedicated `--remote-debugging-port` Chrome on a scratch workspace.
`click`/`type` are v1 JS-injection (not `Input.dispatchMouseEvent`/`insertText`),
so they won't fire native-only handlers or trusted-event checks — the synthetic-input
path is a future stretch goal.
