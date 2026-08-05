# Gotchas & environment notes

Hard-won lessons from building and verifying this on hexane (Arch, i3 4.25, X11,
three outputs). Most of these are environment-specific landmines that cost real
debugging time.

## i3ipc ignores `$DISPLAY` (the fail-open one)

i3ipc's socket discovery order is `$I3SOCK` → `$SWAYSOCK` → X root-window atom.
hexane **exports** `I3SOCK=/run/user/1000/i3/ipc-socket.<pid>` (the login `:0`
i3), so any i3ipc client binds to `:0` regardless of `DISPLAY`. A "sandbox" on
`:1` would have silently driven the real desktop.

**Fix:** resolve the socket from the `I3_SOCKET_PATH` atom on the target
`$DISPLAY`'s root window; never fall back to `$I3SOCK`; fail loudly if absent.
See `src/hypr_cua_mcp/i3tree.py::_i3_socket_path`.

## Nested i3 won't start under an inherited `$I3SOCK`

Launching the sandbox's nested i3 with `I3SOCK` still set makes it try to
*create* its socket at `:0`'s path:

```
[libi3] ERROR: Refusing to create UNIX socket at …ipc-socket.29373: Socket is already in use
```

**Fix:** start it with `env -u I3SOCK DISPLAY=:1 i3 -c sandbox/i3.config`.

## `fc-match monospace` → Noto Color Emoji → xterm dies

On hexane, generic `monospace` resolves to `NotoColorEmoji.ttf` (fallout from the
Noto emoji strong-append in the user's `fonts.conf`). xterm then fails with:

```
xterm: Selected font has no non-zero height for ISO-8859-1 encoding
```

**Fix:** use an explicit face, `xterm -fa "DejaVu Sans Mono"`, never the
`monospace` alias. Likely affects other legacy/core-font X clients too.

## zsh `noclobber`

The user's zsh sets `noclobber`, so `> existing_file` fails with "file exists"
and the command doesn't run (silently breaking `cmd > log &` setups). Use `>|`
to force, or `rm -f` the target first.

## Multi-output framebuffer

`import -window root` grabs **all** outputs — 7680×2880 across three monitors on
hexane. Downscaling that to the API budget destroys click accuracy, and
coordinates span monitors. Always capture **per output** (the default is the
primary). `grim` is installed but is Wayland-only; ignore it on X11.

## mcp 2.0 SDK changes

- `FastMCP` → `MCPServer`, moved to `mcp.server.mcpserver`.
- Result objects are snake_case: `server_info`, `is_error`, `mime_type`.
- To return `[Image, caption]` from a tool, decorate it
  `@mcp.tool(structured_output=False)` so the SDK doesn't try to synthesize
  structured output from the list.

## MCP servers load at client startup

A newly `claude mcp add`-ed server is **not** available in the running session —
restart / reconnect Claude Code to pick it up. That's why the sandbox and live
entries are separate registrations, each realized on the next restart.

## Automation self-locks the screen (DPMS / screensaver)

Synthetic `xdotool` (XTEST) input does **not** reliably reset the X DPMS /
screensaver idle timer. During a live `:0` session on 2026-07-31 the screen
idle-locked mid-demo: `xss-lock` → `xsecurelock` fired ~5 min after the last
*physical* input (DPMS `Standby=300`), and DPMS powered the panels off — landing
in a black lock screen with no visible prompt.

Two consequences:

- **A long automated run can lock you out.** Once locked, **do not send
  keystrokes** — they go straight into the (invisible) `xsecurelock` password
  field.
- **Fix (implemented):** every tool call runs `inputs.reset_idle()` —
  `xset s reset` + `xset dpms force on` — which resets the idle timer *while
  actively driving*. It deliberately does **not** globally disable the
  screensaver (`xset s off`), so the desktop still idle-locks normally once the
  tool goes quiet, and there is no disabled state left dangling if the server
  crashes (no fail-open, which the naive "disable + restore on exit" version
  has). See `src/hypr_cua_mcp/inputs.py::reset_idle`.

Recovery is manual: real mouse/keyboard input wakes DPMS and brings up the auth
prompt (a panel that was DPMS-off may not re-train and stay black — use a VT
switch as the escape hatch).

## focus_follows_mouse steals focus mid-sequence

i3 defaults to `focus_follows_mouse yes`. A pointer tool (`click`/`scroll`/
`move_mouse`/`drag`) moving over a different window changes keyboard focus — so a
later `type_text` with no `con_id` lands there, not where an earlier
`focus_window` put it. The sandbox config sets `focus_follows_mouse no`; on a
live desktop, prefer passing `con_id` to `type_text`/`key` (which re-focuses and
verifies immediately before typing) rather than relying on a stale focus.

## `pkill -f` self-matches the calling shell

`pkill -f 'Xephyr :1'` also matches the shell running the command, because that
pattern appears in the shell's own command line — so it SIGTERMs itself (exit
144) before finishing. Match the process **name** instead: `pkill -x Xephyr`.
