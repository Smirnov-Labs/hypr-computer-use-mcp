"""Tests for the stdlib connector: fake services over socketpairs; no real socket
paths, processes or signals."""
import json
import socket
import sys
import threading
import time

import pytest

from hypr_cua_mcp import connect


def _until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


class Out:
    """A stdout stand-in that collects the bytes written to it."""

    def __init__(self):
        self.data, self.lock = b"", threading.Lock()

    def write(self, data):
        with self.lock:
            self.data += data

    def flush(self):
        pass

    def lines(self):
        with self.lock:
            return [json.loads(line) for line in self.data.splitlines()]

    def wait_for(self, n):
        _until(lambda: self.data.count(b"\n") >= n)
        return self.lines()


class Service:
    """The service side of one connection."""

    def __init__(self, sock):
        self.sock, self.file = sock, sock.makefile("rwb")

    def read(self):
        return json.loads(self.file.readline())

    def write(self, data: bytes):
        self.file.write(data)
        self.file.flush()

    def die(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.file.close()
        self.sock.close()


class Services:
    """Plays the shared service: each connect gets a fresh socketpair."""

    def __init__(self):
        self.conns, self.fail = [], False

    def __call__(self, path):
        if self.fail:
            raise ConnectionRefusedError(path)
        client, service = socket.socketpair()
        self.conns.append(Service(service))
        return client


def _line(obj):
    return (json.dumps(obj) + "\n").encode()


def _req(rid, method="tools/call"):
    return _line({"jsonrpc": "2.0", "id": rid, "method": method, "params": {}})


def _lost(rid):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": connect.LOST}}


@pytest.fixture
def exits(monkeypatch):
    codes = []
    monkeypatch.setattr(connect, "_exit", codes.append)
    return codes


def _connector(services, out):
    c = connect.Connector("/x", out, open_socket=services)
    c.attach(services("/x"))
    return c


def test_lines_flow_both_ways():
    services, out = Services(), Out()
    c = _connector(services, out)
    c.send(_req(1, "tools/list"))
    svc = services.conns[0]
    assert svc.read()["method"] == "tools/list"
    svc.write(_line({"jsonrpc": "2.0", "id": 1, "result": {}}))
    assert out.wait_for(1) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert c.pending == set()


def test_a_request_after_the_service_went_away_reconnects_without_an_error(exits):
    services, out = Services(), Out()
    c = _connector(services, out)
    services.conns[0].die()
    _until(lambda: c.sock is None)
    c.send(_req(2))
    assert services.conns[1].read()["id"] == 2
    assert out.data == b"" and exits == []


def test_a_request_the_service_ran_before_dying_gets_one_lost_error(exits):
    services, out = Services(), Out()
    c = _connector(services, out)
    c.send(_req(7))
    services.conns[0].read()
    services.conns[0].die()
    assert out.wait_for(1) == [_lost(7)]
    c.send(_req(8))
    assert services.conns[1].read()["id"] == 8
    time.sleep(0.05)
    assert out.lines() == [_lost(7)] and exits == []


def test_a_reply_cut_off_by_the_disconnect_becomes_the_lost_error(exits):
    services, out = Services(), Out()
    c = _connector(services, out)
    c.send(_req(5))
    svc = services.conns[0]
    svc.read()
    svc.write(b'{"jsonrpc":"2.0","id":5,"res')  # half a reply, then the service dies
    svc.die()
    assert out.wait_for(1) == [_lost(5)]


def test_a_legacy_client_exits_instead_of_reconnecting(exits):
    services, out = Services(), Out()
    c = _connector(services, out)
    c.send(_req(1, "initialize"))
    services.conns[0].read()
    services.conns[0].die()
    _until(lambda: exits == [1])
    assert out.lines() == [_lost(1)] and len(services.conns) == 1


def test_it_gives_up_after_the_reconnect_window(exits, monkeypatch):
    monkeypatch.setattr(connect, "RECONNECT_FOR", 0.3)
    services, out = Services(), Out()
    c = _connector(services, out)
    services.conns[0].die()
    _until(lambda: c.sock is None)
    services.fail = True
    c.send(_req(3))
    assert out.lines() == [_lost(3)] and exits == [1]


@pytest.mark.parametrize("case", ["no runtime dir", "no socket", "refused"])
def test_main_execs_the_stdio_server_when_no_service_answers(monkeypatch, tmp_path, case):
    if case == "no runtime dir":
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    if case == "refused":  # a socket file that nothing listens on
        (tmp_path / "hypr-cua").mkdir()
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(tmp_path / "hypr-cua" / "mcp.sock"))
        dead.close()
    calls = []

    def fake_execv(path, argv):
        calls.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(connect, "_execv", fake_execv)
    with pytest.raises(SystemExit):
        connect.main()
    assert calls == [[sys.executable, "-I", "-m", "hypr_cua_mcp.server"]]
