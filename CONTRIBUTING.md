# Contributing

hypr-cua-mcp is [stable and feature-complete](README.md#project-status). The most
useful contributions are bug reports and fixes. Reports from untested setups help
most: scaled monitors, non-US keyboard layouts, other Hyprland versions. For
anything bigger than a fix, open an issue first so we can agree on the shape.

## Reporting a bug

Use the bug report form. It asks for your Hyprland version, config type
(`hyprland.lua` or `hyprland.conf`), monitors, MCP client, and
`uvx hypr-cua-mcp --selftest` output. That is usually enough to reproduce a bug.

Security issues: report privately instead, see [SECURITY.md](SECURITY.md).

## Making a change

[AGENTS.md](AGENTS.md) is the contributor guide. It is written for coding agents,
but it works for humans too. It covers setup, architecture, and the
Hyprland/Wayland conventions that bite.

- Write the failing test first. Tests are hermetic: they mock the compositor
  boundary, so you don't need a live Hyprland. `uv run pytest` must pass.
- Conventional commit messages (`fix:`, `feat:`, `docs:`).
- Learned something the hard way about Hyprland/Wayland? Add it to
  [docs/gotchas.md](docs/gotchas.md).
