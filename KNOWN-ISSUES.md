# Known issues / open items (v0.1)

Built and unit-tested autonomously on hexane's multi-monitor Hyprland. These need a
human's eyes before trusting the interaction path end-to-end.

## 1. Pointer focus into windows — VERIFY FIRST (the important one)

**What works (verified live):** the self-owned uinput mouse **moves the cursor
accurately** (~1px), Hyprland **lists it as a recognized Mouse**, and **monitor
focus follows** it (cursor on DP-1 → DP-1 focused, etc.).

**What didn't verify:** in synthetic tests, hovering/clicking a window did NOT move
the **keyboard/window** focus (`hyprctl activewindow`) onto it — focus stayed pinned
on one window. `input:follow_mouse=1` and `input:mouse_refocus=true` are both set,
so a *physical* mouse should focus-follow.

**Most likely cause:** the test harness's `visible` heuristic (window's ws == its
monitor's active ws) gave false positives on this multi-monitor + Lua-config setup,
so the tests hovered windows that weren't actually on-screen → clicks landed on
empty/covered regions. In the real flow — screenshot the *actual* visible screen,
click what you see — this should work.

**To resolve (interactive):**
1. Does the *physical* mouse focus-follow between windows on DP-1/DP-2/HDMI-A-2?
2. `screenshot(output)` a monitor → `click` a clearly-visible window → does focus
   land (check `list_windows` focused / `get_mouse`)?
3. If a genuinely-visible window still won't focus on click, the virtual pointer
   needs deeper seat-integration work (or a `hl.dsp.focus` + pointer-warp combo).

## 2. `hyprtree.list_windows` `visible` flag

`visible = (window's workspace == its monitor's active workspace) and not hidden`.
Confirm this is accurate on multi-monitor — it drives `screenshot_window`'s
"not visible" guard and (above) confused the focus tests. If wrong,
`screenshot_window` can grab the wrong region.

## 3. Smaller items

- `type_text` is **US-ASCII only** (unmapped chars skipped); non-US xkb layouts
  unverified against the evdev keycodes. Emission is unit-tested; live char output
  into a genuinely-focused window not yet confirmed.
- `focus_window(address)` is best-effort under `follow_mouse=1` (documented) —
  prefer click-to-focus.
- Repo is **local-only** (not pushed). `.github/workflows/ci.yml` is untested for
  the Wayland deps; `evdev` builds a C-extension — confirm it installs on the CI
  runner before relying on CI.
