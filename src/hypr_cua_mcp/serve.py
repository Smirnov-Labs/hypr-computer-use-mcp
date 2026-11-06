"""One hypr-cua server per desktop login, reached through a Unix socket.

systemd's hypr-cua.socket listens on $XDG_RUNTIME_DIR/hypr-cua/mcp.sock and starts
hypr-cua.service (`hypr-cua-mcp --serve`) on the first connection, handing over the
listening socket as fd 3. Each accepted connection is one MCP session, framed with
newlines like stdio and served by the same MCPServer as stdio mode. Socket I/O is
async, so idle connections hold no worker threads.
"""
from __future__ import annotations

import logging
import os
import socket
import sys

import anyio
import anyio.abc
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.server.stdio import stdio_server

from . import browser, coords

LISTEN_FD = 3
MAX_LINE = 4 * 1024 * 1024  # an incoming line over this closes its connection
log = logging.getLogger("hypr_cua_mcp.serve")


class ActivationError(RuntimeError):
    """The process was not started by systemd socket activation as expected."""


def listening_socket(fd: int = LISTEN_FD) -> socket.socket:
    """The listening Unix stream socket systemd passed in, after checking it is one.

    Like sd_listen_fds(), it then removes the LISTEN_* variables, so processes the
    service starts (Chrome, tmux) don't inherit them."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise ActivationError(
            "hypr-cua-mcp --serve needs systemd socket activation: install "
            "contrib/systemd/hypr-cua.socket and hypr-cua.service, or for a manual run use "
            "`systemd-socket-activate -l $XDG_RUNTIME_DIR/hypr-cua/mcp.sock hypr-cua-mcp --serve`"
        )
    sock = socket.socket(fileno=fd)
    if (sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM
            or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
        sock.detach()
        raise ActivationError(f"fd {fd} is not a listening Unix stream socket")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    return sock


class _LineReader:
    """`async for line` over a socket stream, as stdio_server expects of stdin."""

    def __init__(self, stream):
        self._buf = BufferedByteReceiveStream(stream)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            line = await self._buf.receive_until(b"\n", MAX_LINE)
        except (anyio.EndOfStream, anyio.IncompleteRead,
                anyio.BrokenResourceError, anyio.ClosedResourceError):
            raise StopAsyncIteration from None
        except anyio.DelimiterNotFound:
            log.warning("closing a connection: an incoming line passed %d bytes", MAX_LINE)
            raise StopAsyncIteration from None
        return line.decode("utf-8", "replace") + "\n"


class _LineWriter:
    """write() / flush() over a socket stream, as stdio_server expects of stdout."""

    def __init__(self, stream):
        self._stream = stream

    async def write(self, text: str) -> None:
        try:
            await self._stream.send(text.encode("utf-8"))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass  # the client has gone; a call that finished after it left has no one to tell

    async def flush(self) -> None:
        pass


async def serve_connection(stream) -> None:
    """Run one MCP session over `stream`, isolated from every other session."""
    from .server import mcp

    key = object()
    coords.session_key.set(key)  # before stdio_server starts its reader: it captures the context
    try:
        async with stream:
            async with stdio_server(stdin=_LineReader(stream), stdout=_LineWriter(stream)) as (read, write):
                server = mcp._lowlevel_server
                await server.run(read, write, server.create_initialization_options())
    except Exception:  # noqa: BLE001 - one broken connection must not stop the others
        log.exception("an MCP session ended with an error")
    finally:
        coords.frames.drop(key)


async def _serve(sock: socket.socket) -> None:
    listener = await anyio.abc.SocketListener.from_socket(sock)
    await listener.serve(serve_connection)


def main() -> int:
    """Entry point for `hypr-cua-mcp --serve`."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        sock = listening_socket()
    except ActivationError as exc:
        print(f"hypr-cua-mcp: {exc}", file=sys.stderr)
        return 2
    coords.require_session = True
    browser.install_exit_handlers()  # SIGTERM from systemd stops a private Chrome, then exits
    anyio.run(_serve, sock)
    return 0
