# Security policy

hypr-cua-mcp drives your real desktop with real input events. It has no sandbox
and no authentication: it's a local stdio server, started by an MCP client you
trust, and it can do anything you can. That's the design rather than a
vulnerability — see [Safety](README.md#safety).

## Reporting a vulnerability

Please report privately through
[GitHub's private vulnerability reporting](https://github.com/Smirnov-Labs/hypr-computer-use-mcp/security/advisories/new),
not in a public issue.

Worth reporting: a tool doing more than it says it does. For example:

- `run_dispatch` getting past its `exit` block
- `write_clipboard` or `cdp_type` returning the value they promise to keep out of
  their output
- the tmux or task tools touching sessions outside the isolated `hypr-cua` socket
- the browser channel opening your main browser profile
- anything that exposes the server or the DevTools port beyond the local machine

## Supported versions

Fixes land in the latest release only.
