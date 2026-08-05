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

## 6. tmux copilot channel (new 2026-08-05) — shipped, live-validated

`tmux.py` + tools `tmux_list/new/run/keys/read/kill` drive terminals with **zero
screen footprint** on a dedicated **isolated socket** (`tmux -L hypr-cua`), so the
model can run + read terminal work on a non-visible "workspace" without ever
touching the user's real (default-socket) sessions. Live-validated (create → run →
capture → kill). Notes:
- The module execs `tmux` directly (list argv), so the user's zsh oh-my-zsh
  tmux-plugin alias is bypassed — but a shell caller needs `command tmux`.
- Captured output includes the user's chatty zsh startup (an alias-hint plugin
  prints `Alias for: …`) — cosmetic, not a capture bug.
- `run()` = `send-keys -l <cmd>` + a separate `Enter` (literal, no key reinterpret);
  `tmux_keys` sends interpreted keys (`C-c`, `Escape`, …).
- **MCP reconnect needed** to expose the new tmux tools in a running server.

## 7. CDP browser channel (`cdp.py`) — live smoke-tested 2026-08-05 ✓

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

**Live smoke-tested 2026-08-05** against a throwaway headless Chrome (dedicated port,
isolated profile): `list_targets`/`read_text`/`eval_js`/`type_text`/`click`/`close_tab`
all worked end-to-end — after fixing a bug the mocks missed: **Chrome M111+ 403-rejects
the DevTools WebSocket without `--remote-allow-origins`**, now in the auto-launch flags
(regression-tested). **The suite itself is still fully mocked** (no Chrome in CI): every test stubs the
HTTP `/json` and the WebSocket send/recv (and `_spawn` is a hard-fail tripwire), so
**real Chrome behavior is unverified**: the actual DevTools reply shapes, that a
freshly launched Chrome comes up within `LAUNCH_TIMEOUT`, `awaitPromise` timing,
large `innerText` frames, and CSP/cross-origin edge cases all still need a live smoke
test against a dedicated `--remote-debugging-port` Chrome on a scratch workspace.
`click`/`type` are v1 JS-injection (not `Input.dispatchMouseEvent`/`insertText`),
so they won't fire native-only handlers or trusted-event checks — the synthetic-input
path is a future stretch goal.

## 8. Task-runner channel (`taskrunner.py`, new 2026-08-05) — unit-tested + live-validated

Built on the §6 tmux channel: turns the copilot substrate into "launch a job and let
the model supervise it." A **task** is a detached tmux session on the isolated
`hypr-cua` socket — **no window, no focus, zero screen footprint** — that the model
polls, reads, feeds input, and stops. MCP tools `task_start`, `task_status`,
`task_list`, `task_send`, `task_stop`. Never touches the user's real (default-socket)
sessions; tasks are namespaced under a **`task-` session prefix** so they're also
distinct from the plain tmux-channel sessions (§6) on the same copilot socket.

**Completion detection (the whole point).** A bare `tmux new-session -d -s name <cmd>`
is torn down the instant `<cmd>` exits, losing its output/exit code. So `start` wraps
the command:

```
sh -c '<command>'; printf '\n__TASK_DONE__:%s\n' "$?"; exec sleep 2147483647
```

- the command runs in its **own `sh -c` subshell** (shlex-quoted, so quotes in the
  command are safe, and a bare `exit`/`exec` inside it only ends that child), then
- a **sentinel line** `__TASK_DONE__:<exit_code>` is printed (`$?` captured), then
- `exec sleep` **keeps the pane alive** so the sentinel stays readable after the
  command finishes. (Reaped by `task_stop` / socket teardown.)

`status`/`list` classify a captured pane purely from that sentinel:
`running` (not printed yet) · `done` + `exit_code` (0 = success) · `missing` (no such
session — decided by `tmux.list_panes()`, so a genuinely-finished task never reads as
missing thanks to the keep-alive). The **last** sentinel-shaped line wins.

**Live-validated 2026-08-05** end-to-end against a real tmux on an isolated throwaway
socket (`tmux -L hypr-cua-smoke-<pid>`, killed after — never the user's sessions):
`running → done` transition, exit-code capture (`0` and `3`), the `exit N`-in-command
case, shlex-quoted commands with embedded quotes, an interactive `read` prompt
answered via `task_send`, duplicate-name rejection, `task_list` states, and
`task_stop → missing`. (Unit tests still fully mock the tmux boundary — no server.)

**Caveats:**
- **Interactive-prompt / "needs attention" detection is best-effort.** There is no
  distinct `waiting-on-input` state — a task blocked on a prompt reads as `running`;
  the model infers the prompt from `status`'s `tail` and answers with `task_send`
  (text + Enter, the reliable path — verified live).
- **`task_send(..., enter=False)` for `C-c`** sends SIGINT to the pane's foreground
  group, which kills the **non-interactive** wrapper too — so the task tears down and
  then reads as `missing`, not a clean `done`. That's expected: use `task_stop` for a
  clean stop; reserve `enter=False` for TUI keys (arrows/Escape) and prompt keypresses.
- **Backgrounding (`cmd &`) is inherently uncatchable** — the subshell returns
  immediately, so the sentinel prints right away and the task reads `done` while the
  child keeps running. Tasks should be foreground commands.
- **The sentinel could in theory appear in a task's own output.** Mitigated by
  requiring a full-line `^__TASK_DONE__:<n>$` match and taking the last one (the real
  marker is always last, printed right before the keep-alive). A command whose final
  printed line is *exactly* that string would be misclassified — negligible.
- A **done** task's tmux pane still shows `live` (running the keep-alive `sleep`) in
  `tmux_list`/`list_panes`; task state comes from the sentinel, not tmux liveness. The
  lingering `sleep` is harmless and reaped by `task_stop`.
- **MCP reconnect needed** to expose the new tools in a running server (as with §6/§7).
