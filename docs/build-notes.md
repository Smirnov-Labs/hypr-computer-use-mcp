# Build notes — what we did and how

A record of how this server came to be, so future-us remembers the reasoning and
the verification, not just the code.

## Goal

Let an MCP client (Claude Code) drive the **whole** X11 desktop on hexane —
screenshot on its own, click UI elements, type — not just a browser. i3-aware,
because a tiling WM with a three-output framebuffer makes blind pixel-guessing a
bad bet.

## Prior-art check (before building)

Searched for an existing X11 + i3-tree computer-use MCP. The two halves exist,
but not the clean combination:

- **`caninja/i3wm-mcp`** — real i3 IPC control (workspaces/layout/gaps), but *no*
  screenshots or pixel clicking, and self-described "vibecoded/experimental."
- **`agent-sh/computer-use-linux`** — screenshot + click + compositor-aware
  *including* i3 IPC, but Wayland-first, X11 "best-effort," and its own docs say
  i3 window list/focus is degraded. Screenshot path leans on GNOME/portals.
- Generic X11 servers (Baltes, felipethecosta, zavora) do vision+click but are
  blind to the i3 tree.

Conclusion: worth building a focused X11 + i3-tree server. We did borrow ideas —
Baltes' coordinate-scaling to fit API image limits, and agent-sh's lesson that
input lands in the wrong window unless you target explicitly (which the i3 tree
fixes).

## Decisions

- **Python + uv** (i3ipc is cleanest here; matches the user's tooling), pinned to
  3.12 to avoid system-Python 3.14 wheel gaps.
- **Xephyr-sandboxed first** — the server is `$DISPLAY`-configurable; a nested
  `:1` X server is the default so it can't touch the real desktop until trusted.
- **Scope = read + focus + input.** Full i3 control scoped separately
  (`DESIGN-i3-control-extension.md`).

## How we verified (evidence, not assertions)

1. **Read-only selftest on live `:0`** — i3 tree + outputs + one primary-output
   capture (3840×2160 → 1430×804 @ scale 0.372) + mouse query. No clicks.
2. **Sandbox isolation** — with `$I3SOCK` still pointing at `:0`, the server
   resolved the *nested* `:1` socket, saw only the `default` output, and never
   the real windows. This is the check that caught the fail-open i3ipc bug.
3. **Coordinate mapping** — `move_mouse` to a screenshot-center landed the
   pointer at the exact expected global pixel.
4. **Full input loop in the sandbox** — `click` focused a target, and
   `focus_window` → `type_text` → `key("Return")` drove a sandboxed shell to
   create a proof file.
5. **MCP stdio transport** — a real client did `initialize` + `tools/list` (13
   tools) + `tools/call`, confirming the `screenshot` image serializes as an
   `image` + `text` pair.
6. **Live multi-window test** — opened four tiled xterms running `top`, a `watch`
   clock, and a counter; `list_windows` reported correct 320px geometry, a
   `screenshot` showed the live output, and `click`→`key(M)` targeted and
   controlled a specific `top` instance.

Every bug in `gotchas.md` was found *during* this verification, not guessed at.

## Operational model

- Sandbox tools: MCP `hypr-cua` (wrapper defaults `DISPLAY=:1`). Start the sandbox
  with `sandbox/run-xephyr.sh`.
- Live tools: MCP `hypr-cua-live` (`-e I3CUA_DISPLAY=:0`) — added separately,
  realized on the next Claude Code restart.
- On `:0`, prefer read-only tools; input reflows i3 and steals focus, so treat it
  with care around live work.

## Live `:0` shakedown (2026-07-31)

Drove the real desktop through the `hypr-cua-live` MCP tools, confined to two
throwaway targets (a floating scratch xterm + an incognito Chrome window) so no
real work windows were touched, and using `focus_window`+`type` so no mouse moved
for keyboard actions:

- `type_text`/`key` ran `neofetch` in the scratch terminal (it read hexane back:
  i3, i9-12900, RTX 3060, three panels).
- `key ctrl+l` + `type_text` navigated the incognito window to `i3wm.org`.
- `screenshot_window` + `click` (screenshot-pixel coords → global) clicked a nav
  link, with before/after captures.

Then it self-locked (see `gotchas.md`): xdotool input doesn't reset the DPMS idle
timer, so xss-lock → xsecurelock fired ~5 min after the last *physical* input,
mid-demo. Recovered by hand. Motivates the screensaver/DPMS-inhibit fix.

## Token cost

Screenshots are ~1,530 tokens flat (server caps at ~1.15 MP; a raw 4K grab would
be ~11k, so the cap saves ~7×). `list_windows` (full tree) ≈ ~2,700 tokens — the
heaviest call. A look→act cycle ≈ ~1,600 tokens; with `list_windows` targeting
≈ ~4,300. On par with Anthropic's computer-use reference (~1–1.15k/shot at XGA);
the main drag is verbose `list_windows` (a filter/compact mode would trim ~10×).
