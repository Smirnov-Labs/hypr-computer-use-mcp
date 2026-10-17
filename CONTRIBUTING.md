# Contributing

hypr-cua-mcp is [stable and feature-complete](README.md#project-status), so the most
useful contributions are bug reports and fixes — especially from setups it hasn't
been tested on (scaled monitors, non-US keyboard layouts, other Hyprland versions).
For anything bigger than a fix, open an issue first so we can agree on the shape.

## Reporting a bug

Use the bug report form. It asks for your Hyprland version, config type
(`hyprland.lua` vs `hyprland.conf`), monitors, MCP client, and the output of
`uvx hypr-cua-mcp --selftest` — usually enough to reproduce.

Security issues: report privately instead, see [SECURITY.md](SECURITY.md).

## Making a change

[AGENTS.md](AGENTS.md) is the contributor guide — written for coding agents, but it
works for humans too: setup, architecture, and the Hyprland/Wayland conventions
that bite.

- Write the failing test first. Tests are hermetic — they mock the compositor
  boundary, so no live Hyprland is needed — and `uv run pytest` must pass.
- Conventional commit messages (`fix:`, `feat:`, `docs:`).
- Learned something the hard way about Hyprland/Wayland? Add it to
  [docs/gotchas.md](docs/gotchas.md).
