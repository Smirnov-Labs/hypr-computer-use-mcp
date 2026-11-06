"""Hyprland/Wayland computer-use MCP server."""


def __getattr__(name: str):
    # Read on first use, so the stdlib connector (connect.py) starts without
    # loading importlib.metadata: about 6 MB and 25 ms per MCP session.
    if name == "__version__":
        from importlib.metadata import version

        return version("hypr-cua-mcp")  # single source of truth: pyproject.toml
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
