"""Tests for serve mode: real MCPServer sessions over socketpairs; no real socket
activation, desktop or process."""
import json
import logging
import os
import socket
import time

import anyio
import anyio.abc
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream

from hypr_cua_mcp import capture, coords, hyprtree, inputs, serve, tmux
from hypr_cua_mcp.coords import CaptureFrame


def _frame():
    return CaptureFrame(region_x=0, region_y=0, region_w=100, region_h=100,
                        scale_x=1.0, scale_y=1.0, shot_w=100, shot_h=100, label="output DP-1")


class Client:
    """One test-side MCP client over a socketpair end."""

    def __init__(self, stream):
        self.stream, self.buf, self.next_id = stream, BufferedByteReceiveStream(stream), 1

    async def request(self, method, params=None):
        rid, self.next_id = self.next_id, self.next_id + 1
        await self.stream.send((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                            "params": params or {}}) + "\n").encode())
        while True:
            msg = json.loads(await self.buf.receive_until(b"\n", serve.MAX_LINE))
            if msg.get("id") == rid:
                return msg

    async def notify(self, method):
        await self.stream.send((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())

    async def start(self):
        res = await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}})
        assert "result" in res
        await self.notify("notifications/initialized")

    async def call(self, name, args=None):
        return (await self.request("tools/call", {"name": name, "arguments": args or {}}))["result"]


async def _pair(tg):
    a, b = socket.socketpair()
    server_side = await anyio.abc.SocketStream.from_socket(a)
    tg.start_soon(serve.serve_connection, server_side)
    return Client(await anyio.abc.SocketStream.from_socket(b))


def _text(result):
    return result["content"][-1]["text"]


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(coords, "frames", coords.FrameStore())
    monkeypatch.setattr(coords, "require_session", True)
    monkeypatch.setattr(hyprtree, "get_outputs", lambda: [
        {"name": "DP-1", "primary": True, "rect": {"x": 0, "y": 0, "w": 100, "h": 100}}])
    monkeypatch.setattr(capture, "capture_region",
                        lambda x, y, w, h, label: capture.Shot(png=b"\x89PNG", frame=_frame()))
    clicks = []
    monkeypatch.setattr(inputs, "click", lambda gx, gy, button="left": clicks.append((gx, gy)))
    return clicks


def test_two_sessions_keep_their_frames_apart(desktop):
    async def main():
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                a, b = await _pair(tg), await _pair(tg)
                await a.start()
                await b.start()
                fid_a = _text(await a.call("screenshot")).split("frame_id='")[1].split("'")[0]
                await b.call("screenshot")
                stale = await b.call("click", {"x": 1, "y": 1, "frame_id": fid_a})
                ok = await a.call("click", {"x": 1, "y": 1, "frame_id": fid_a})
                tg.cancel_scope.cancel()
        return stale, ok

    stale, ok = anyio.run(main)
    assert stale.get("isError") is True and "stale" in _text(stale)
    assert not ok.get("isError") and desktop == [(1, 1)]


# A 2026-07-28 request carries the client's protocol version and info itself.
_MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
           "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
           "io.modelcontextprotocol/clientCapabilities": {}}


def test_a_modern_client_needs_no_handshake_on_a_new_connection(desktop):
    """What the connector's reconnect relies on: a fresh connection serves a
    2026-07-28 request with no initialize, and sessions still keep their frames."""
    async def main():
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                a, b = await _pair(tg), await _pair(tg)
                shot = await a.request("tools/call", {"name": "screenshot", "arguments": {},
                                                      "_meta": _MODERN})
                fid = _text(shot["result"]).split("frame_id='")[1].split("'")[0]
                other = await b.request("tools/call", {"name": "click", "_meta": _MODERN,
                                                       "arguments": {"x": 1, "y": 1, "frame_id": fid}})
                tg.cancel_scope.cancel()
        return shot, other

    shot, other = anyio.run(main)
    assert not shot["result"].get("isError")
    # A new connection is a new session with no frames, as after a reconnect.
    assert other["result"].get("isError") is True
    assert "no screenshot has been taken yet" in _text(other["result"])
    assert desktop == []  # the other session's click sent nothing


def test_a_closed_connection_ends_its_session_and_drops_its_frames(desktop):
    async def main():
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                client = await _pair(tg)
                await client.start()
                await client.call("screenshot")
                assert len(coords.frames._last) == 1
                await client.stream.aclose()
            # the task group exits only once serve_connection has returned

    anyio.run(main)
    assert coords.frames._last == {}


def test_a_client_gone_before_its_reply_is_not_logged_as_an_error(monkeypatch, caplog):
    """A modern session with an open listen stream (as Claude Code keeps) finishes
    a running call after its client exits, and the reply meets a closed socket.
    That is a normal exit, not a session error."""
    monkeypatch.setattr(coords, "require_session", True)
    monkeypatch.setattr(tmux, "list_panes", lambda: time.sleep(0.3) or [])

    async def main():
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                client = await _pair(tg)
                await client.stream.send((json.dumps({
                    "jsonrpc": "2.0", "id": 5, "method": "subscriptions/listen",
                    "params": {"_meta": _MODERN, "notifications": {"toolsListChanged": True}}}) + "\n").encode())
                assert b"acknowledged" in await client.buf.receive_until(b"\n", serve.MAX_LINE)
                await client.stream.send((json.dumps({
                    "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": "tmux_list", "arguments": {}, "_meta": _MODERN}}) + "\n").encode())
                await client.stream.aclose()  # the client exits before the reply
            # the task group exits once serve_connection has returned

    with caplog.at_level(logging.WARNING, logger="hypr_cua_mcp.serve"):
        anyio.run(main)
    assert [r.getMessage() for r in caplog.records if r.name == "hypr_cua_mcp.serve"] == []


def test_idle_connections_do_not_starve_an_active_one(monkeypatch):
    monkeypatch.setattr(coords, "require_session", True)
    monkeypatch.setattr(tmux, "list_panes", lambda: [{"session": "w"}])

    async def main():
        async with anyio.create_task_group() as tg:
            idle = [await _pair(tg) for _ in range(45)]
            active = await _pair(tg)
            with anyio.fail_after(10):
                await active.start()
                result = await active.call("tmux_list")
            tg.cancel_scope.cancel()
        return idle, result

    idle, result = anyio.run(main)
    assert len(idle) == 45 and json.loads(_text(result)) == [{"session": "w"}]


def test_listening_socket_needs_systemd_activation(monkeypatch):
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    with pytest.raises(serve.ActivationError) as e:
        serve.listening_socket()
    assert "systemd-socket-activate" in str(e.value)


def test_listening_socket_rejects_a_socket_that_is_not_listening(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    with pytest.raises(serve.ActivationError):
        serve.listening_socket(fd=a.fileno())
    a.close()
    b.close()


def test_listening_socket_accepts_a_listening_unix_socket(monkeypatch, tmp_path):
    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsock.bind(str(tmp_path / "mcp.sock"))
    lsock.listen()
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    got = serve.listening_socket(fd=os.dup(lsock.fileno()))
    assert got.family == socket.AF_UNIX
    assert "LISTEN_PID" not in os.environ and "LISTEN_FDS" not in os.environ
    got.close()
    lsock.close()


def test_serve_mode_without_activation_exits_with_a_message(monkeypatch, capsys):
    monkeypatch.delenv("LISTEN_PID", raising=False)
    assert serve.main() == 2
    assert "socket activation" in capsys.readouterr().err
