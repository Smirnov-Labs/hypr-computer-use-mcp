"""Connect an MCP client's stdio to the shared hypr-cua service.

Standard library only, so it starts in milliseconds. Without the service's socket,
it execs the stdio server instead, so a client always gets hypr-cua.

When the service goes away, the connector answers each request still waiting for
a reply with an error that says it may have run, and reconnects when the next line
arrives. It never replays a request: a modern MCP client carries its session info
on every request, so a fresh connection needs no handshake. A legacy client (one
that sent `initialize`) can't continue on a new connection, so for it the
connector exits instead.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time

RECONNECT_FOR = 10.0  # seconds to keep trying after the service went away
RETRY_EVERY = 0.2
LOST = ("connection to the hypr-cua service was lost; the operation may have "
        "completed, so check the state before retrying")
_exit = os._exit    # test seam
_execv = os.execv   # test seam


def socket_path() -> str | None:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(rt, "hypr-cua", "mcp.sock") if rt else None


def _open(path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


def _parse(line: bytes):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def _lost_reply(request_id) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32000, "message": LOST}}) + "\n").encode()


class Connector:
    """Copies client lines to the service and service lines to the client."""

    def __init__(self, path, stdout, open_socket=_open):
        self.path, self.stdout, self._open = path, stdout, open_socket
        self.sock = None
        self.pending: set[str] = set()  # JSON-encoded ids of requests awaiting a reply
        self.legacy = False             # the client sent `initialize`
        self._lock = threading.Lock()   # guards sock, pending and legacy
        self._out = threading.Lock()    # one writer to stdout at a time

    def _write(self, data: bytes) -> None:
        with self._out:
            self.stdout.write(data)
            self.stdout.flush()

    def attach(self, sock) -> None:
        """Use `sock` from now on, and copy its lines to stdout."""
        with self._lock:
            self.sock = sock
        threading.Thread(target=self._pump, args=(sock,), daemon=True).start()

    def _pump(self, sock) -> None:
        try:
            with sock.makefile("rb") as f:
                for line in f:
                    if not line.endswith(b"\n"):
                        break  # a reply cut off by the disconnect; its request gets LOST
                    msg = _parse(line)
                    with self._lock:
                        if self.sock is not sock:
                            break  # already declared lost, and its requests answered
                        if msg is not None and "id" in msg and "method" not in msg:
                            self.pending.discard(json.dumps(msg["id"]))
                    self._write(line)
        except OSError:
            pass
        self._lost(sock)

    def _lost(self, sock) -> None:
        """The service went away: answer every request still waiting. A legacy
        client then exits; a modern one reconnects on its next line."""
        with self._lock:
            if self.sock is not sock:
                return
            self.sock = None
            waiting, self.pending = self.pending, set()
            legacy = self.legacy
        for raw in sorted(waiting):
            self._write(_lost_reply(json.loads(raw)))
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes a pump still blocked on this socket
        except OSError:
            pass
        sock.close()
        if legacy:
            _exit(1)

    def _reconnect(self):
        deadline = time.monotonic() + RECONNECT_FOR
        while True:
            try:
                sock = self._open(self.path)
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(RETRY_EVERY)
                continue
            self.attach(sock)
            return sock

    def send(self, line: bytes) -> None:
        """Forward one client line, reconnecting first if the service went away."""
        msg = _parse(line)
        is_request = msg is not None and "method" in msg and "id" in msg
        rid = json.dumps(msg["id"]) if is_request else None
        with self._lock:
            if is_request:
                self.pending.add(rid)
                if msg["method"] == "initialize":
                    self.legacy = True
            sock = self.sock
        if sock is None:
            sock = self._reconnect()
            if sock is None:  # the service stayed away for RECONNECT_FOR seconds
                if is_request:
                    with self._lock:
                        self.pending.discard(rid)
                    self._write(_lost_reply(msg["id"]))
                _exit(1)
                return
        try:
            sock.sendall(line)
        except OSError:
            self._lost(sock)  # answers this request too, since it is pending


def main() -> None:
    path = socket_path()
    try:
        sock = _open(path) if path else None
    except OSError:
        sock = None
    if sock is None:  # no shared service: become this session's stdio server
        _execv(sys.executable, [sys.executable, "-I", "-m", "hypr_cua_mcp.server"])
    signal.signal(signal.SIGINT, lambda *a: _exit(0))
    signal.signal(signal.SIGTERM, lambda *a: _exit(0))
    conn = Connector(path, sys.stdout.buffer)
    conn.attach(sock)
    for line in sys.stdin.buffer:  # ends at stdin EOF: the client has gone
        conn.send(line)
    _exit(0)


if __name__ == "__main__":
    main()
