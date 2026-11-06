"""Tests for the CDP client. browser.http_get_json and the WebSocket are fakes, and
the autouse fixture swaps in a fake browser manager at a fixed base URL, with
browser._spawn as a tripwire. tests/conftest.py also points any stray real call at
port 1 with autolaunch off."""
import json
import time
from contextlib import contextmanager

import pytest
import websocket

from hypr_cua_mcp import browser, cdp

BASE = "http://127.0.0.1:9999"


@pytest.fixture(autouse=True)
def _fake_browser(monkeypatch):
    class FakeManager:
        mode = "private"

        @contextmanager
        def use(self):
            yield BASE

    monkeypatch.setattr(browser, "manager", FakeManager())
    monkeypatch.setattr(
        browser, "_spawn",
        lambda argv: pytest.fail(f"real Chrome spawn blocked in tests: {argv}"),
    )


class AutoWS:
    """Answers every command with one canned result or error, optionally after N
    id-less events, so framing and event handling are visible."""

    def __init__(self, result=None, error=None, events_before=0):
        self.sent, self.timeouts = [], []
        self.result = {} if result is None else result
        self.error = error
        self.events_before = events_before
        self.closed = False
        self.close_timeout = "unset"
        self._q = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        for _ in range(self.events_before):
            self._q.append({"method": "Page.frameNavigated", "params": {}})
        if self.error is not None:
            self._q.append({"id": frame["id"], "error": self.error})
        else:
            self._q.append({"id": frame["id"], "result": self.result})

    def recv(self):
        if not self._q:
            raise websocket.WebSocketTimeoutException("timed out")
        return json.dumps(self._q.pop(0))

    def close(self, timeout=None):
        self.closed, self.close_timeout = True, timeout


class ScriptWS:
    """Scripted by method: replies (dict, or callable(params) -> dict), CDP errors,
    methods that never get a reply, and events queued before or after a reply. An
    empty queue reads as a socket timeout, so deadlines pass at once."""

    def __init__(self, replies=None, errors=None, before=None, after=None, silent=()):
        self.replies = replies or {}
        self.errors = errors or {}
        self.before = before or {}
        self.after = after or {}
        self.silent = set(silent)
        self.sent, self.timeouts, self._q = [], [], []

    def settimeout(self, t):
        self.timeouts.append(t)

    def send(self, data):
        f = json.loads(data)
        self.sent.append(f)
        m = f["method"]
        self._q.extend(self.before.get(m, []))
        if m in self.errors:
            self._q.append({"id": f["id"], "error": self.errors[m]})
        elif m not in self.silent:
            r = self.replies.get(m, {})
            self._q.append({"id": f["id"], "result": r(f.get("params", {})) if callable(r) else r})
        self._q.extend(self.after.get(m, []))

    def recv(self):
        if not self._q:
            raise websocket.WebSocketTimeoutException("timed out")
        return json.dumps(self._q.pop(0))

    def close(self, timeout=None):
        pass


def _router(targets, browser_ws="ws://127.0.0.1:9999/devtools/browser/xyz"):
    def fake(url, timeout=None):
        if url == BASE + "/json/version":
            return {"Browser": "Chrome/x", "webSocketDebuggerUrl": browser_ws}
        if url == BASE + "/json":
            return targets
        raise AssertionError(f"unexpected HTTP url in test: {url}")
    return fake


def _page(tid, **kw):
    d = {"id": tid, "type": "page", "title": f"title-{tid}", "url": f"http://{tid}",
         "webSocketDebuggerUrl": f"ws://127.0.0.1:9999/devtools/page/{tid}"}
    d.update(kw)
    return d


def _eval_result(value):
    return {"result": {"type": "object", "value": value}}


def _evals(fn):
    """A Runtime.evaluate reply whose value is computed from the expression."""
    return lambda params: _eval_result(fn(params["expression"]))


def _wire(monkeypatch, ws, targets):
    monkeypatch.setattr(browser, "http_get_json", _router(targets))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url, timeout=None: ws)


# --- connection --------------------------------------------------------------

def test_ws_connect_sends_no_origin_header(monkeypatch):
    seen = {}
    monkeypatch.setattr(cdp.websocket, "create_connection",
                        lambda url, **kw: seen.update(url=url, **kw) or "WS")
    assert cdp._ws_connect("ws://127.0.0.1:1/devtools/page/T", 3) == "WS"
    assert seen["suppress_origin"] is True
    assert seen["timeout"] == 3


# --- _Session ----------------------------------------------------------------

def test_command_frames_monotonic_ids_and_keeps_events():
    ws = AutoWS(result={"ok": 1}, events_before=2)
    sess = cdp._Session(ws, cdp._Deadline(10))
    assert sess.command("Domain.a", {"x": 1}) == {"ok": 1}
    assert sess.command("Domain.b") == {"ok": 1}
    assert [f["id"] for f in ws.sent] == [1, 2]
    assert ws.sent[0]["params"] == {"x": 1} and ws.sent[1]["params"] == {}
    assert len(sess.events) == 4


def test_command_surfaces_protocol_error():
    sess = cdp._Session(AutoWS(error={"code": -32000, "message": "bad target"}), cdp._Deadline(10))
    with pytest.raises(cdp.CdpError) as e:
        sess.command("Runtime.evaluate", {"expression": "1"})
    assert "bad target" in str(e.value)


def test_command_times_out_at_the_deadline():
    class Flood(AutoWS):
        def recv(self):
            return json.dumps({"method": "evt", "params": {}})

    with pytest.raises(cdp.CdpTimeout) as e:
        cdp._Session(Flood(), cdp._Deadline(0.05)).command("Runtime.evaluate")
    assert "no reply within 0.05 s" in str(e.value)


def test_an_expired_command_sends_nothing():
    ws = AutoWS(result={})
    dl = cdp._Deadline(5)
    dl.at = time.monotonic() - 1
    with pytest.raises(cdp.CdpTimeout):
        cdp._Session(ws, dl).command("Page.navigate")
    assert ws.sent == []


def test_socket_waits_only_for_the_time_that_remains():
    ws = AutoWS(result={})
    cdp._Session(ws, cdp._Deadline(5)).command("X")
    assert all(4 < t <= 5 for t in ws.timeouts) and len(ws.timeouts) == 2


def test_a_closed_connection_is_not_reported_as_a_timeout():
    class Closed(AutoWS):
        def recv(self):
            raise websocket.WebSocketConnectionClosedException("gone")

    with pytest.raises(cdp.CdpError) as e:
        cdp._Session(Closed(), cdp._Deadline(5)).command("Page.navigate")
    assert not isinstance(e.value, cdp.CdpTimeout)
    assert "connection closed" in str(e.value)


def test_a_close_frame_is_reported_as_closed():
    class CloseFrame(AutoWS):
        def recv(self):
            return ""  # what websocket-client returns for a close frame

    with pytest.raises(cdp.CdpError) as e:
        cdp._Session(CloseFrame(), cdp._Deadline(5)).command("Page.navigate")
    assert not isinstance(e.value, cdp.CdpTimeout)
    assert "connection closed" in str(e.value)


def test_a_failed_send_is_reported_as_closed():
    class BrokenSend(AutoWS):
        def send(self, data):
            raise websocket.WebSocketConnectionClosedException("gone")

    with pytest.raises(cdp.CdpError) as e:
        cdp._Session(BrokenSend(), cdp._Deadline(5)).command("Page.navigate")
    assert "connection closed" in str(e.value)


def test_close_skips_the_close_handshake():
    ws = AutoWS()
    cdp._Session(ws, cdp._Deadline(5)).close()
    assert ws.closed and ws.close_timeout == 0


def test_clamp_timeout():
    assert cdp.clamp_timeout(0) == 1
    assert cdp.clamp_timeout(1000) == 300
    assert cdp.clamp_timeout(12.5) == 12.5


# --- list_targets ------------------------------------------------------------

def test_list_targets_reports_the_browser_and_pages_only(monkeypatch):
    targets = [
        _page("T1", title="Tab one", url="http://a"),
        {"id": "SW", "type": "service_worker", "title": "sw", "url": "http://sw"},
        _page("T2", title="Tab two", url="http://b"),
    ]
    monkeypatch.setattr(browser, "http_get_json", _router(targets))
    out = cdp.list_targets()
    assert out["browser"] == "private"
    assert out["tabs"] == [{"id": "T1", "title": "Tab one", "url": "http://a"},
                           {"id": "T2", "title": "Tab two", "url": "http://b"}]


# --- eval_js -----------------------------------------------------------------

def test_eval_js_returns_value_and_frames_runtime_evaluate(monkeypatch):
    ws = AutoWS(result=_eval_result(42))
    _wire(monkeypatch, ws, [_page("T1")])
    assert cdp.eval_js("T1", "40 + 2") == 42
    p = ws.sent[0]["params"]
    assert ws.sent[0]["method"] == "Runtime.evaluate"
    assert p["expression"] == "40 + 2"
    assert p["returnByValue"] is True and p["awaitPromise"] is True
    assert ws.closed


def test_eval_js_uses_the_callers_clamped_timeout(monkeypatch):
    ws = AutoWS(result=_eval_result(1))
    _wire(monkeypatch, ws, [_page("T1")])
    cdp.eval_js("T1", "1", timeout=0)
    assert ws.timeouts and all(t <= 1 for t in ws.timeouts)


def test_one_deadline_covers_discovery_and_connect(monkeypatch):
    seen = {"http": []}
    router = _router([_page("T1")])

    def http(url, timeout=None):
        seen["http"].append(timeout)
        return router(url)

    ws = AutoWS(result=_eval_result(1))
    monkeypatch.setattr(browser, "http_get_json", http)
    monkeypatch.setattr(cdp, "_ws_connect", lambda url, timeout=None: seen.update(ws=timeout) or ws)
    cdp.eval_js("T1", "1", timeout=2)
    assert seen["http"] and all(t <= 2 for t in seen["http"])
    assert seen["ws"] <= 2


def test_eval_js_surfaces_exception_details(monkeypatch):
    ws = AutoWS(result={"result": {"type": "object"},
                        "exceptionDetails": {"text": "Uncaught",
                                             "exception": {"description": "Error: kaboom"}}})
    _wire(monkeypatch, ws, [_page("T1")])
    with pytest.raises(cdp.CdpError) as e:
        cdp.eval_js("T1", "throw new Error('kaboom')")
    assert "kaboom" in str(e.value)


def test_eval_js_adds_a_hint_when_the_page_navigated(monkeypatch):
    ws = AutoWS(error={"code": -32000, "message": "Execution context was destroyed."})
    _wire(monkeypatch, ws, [_page("T1")])
    with pytest.raises(cdp.CdpError) as e:
        cdp.eval_js("T1", "location.href = 'x'")
    assert "use cdp_navigate with wait_for" in str(e.value)


def test_unknown_target_raises_before_connecting(monkeypatch):
    monkeypatch.setattr(browser, "http_get_json", _router([_page("T1")]))
    with pytest.raises(cdp.CdpError) as e:
        cdp.eval_js("NOPE", "1")
    assert "NOPE" in str(e.value)


# --- read / click / type / navigate / open / close ----------------------------

def test_read_text_returns_title_url_text(monkeypatch):
    payload = {"title": "Hello", "url": "http://p/", "text": "body innertext here"}
    ws = AutoWS(result=_eval_result(payload))
    _wire(monkeypatch, ws, [_page("T1")])
    assert cdp.read_text("T1") == payload


def test_click_builds_queryselector_click(monkeypatch):
    ws = AutoWS(result=_eval_result(True))
    _wire(monkeypatch, ws, [_page("T1")])
    cdp.click("T1", "#submit")
    expr = ws.sent[0]["params"]["expression"]
    assert '"#submit"' in expr and ".click()" in expr


def test_type_text_sets_value_and_hides_text(monkeypatch):
    ws = AutoWS(result=_eval_result(True))
    _wire(monkeypatch, ws, [_page("T1")])
    out = cdp.type_text("T1", "#email", "a@b.co")
    expr = ws.sent[0]["params"]["expression"]
    assert '"#email"' in expr and '"a@b.co"' in expr and "dispatchEvent" in expr
    assert "6 chars" in out and "a@b.co" not in out


def test_navigate_sends_page_navigate(monkeypatch):
    ws = AutoWS(result={"frameId": "F1", "loaderId": "L1"})
    _wire(monkeypatch, ws, [_page("T1")])
    out = cdp.navigate("T1", "https://example.com")
    assert ws.sent[0]["method"] == "Page.navigate"
    assert out["frameId"] == "F1"


def test_open_tab_creates_target_via_browser_ws(monkeypatch):
    ws = AutoWS(result={"targetId": "NEW42"})
    _wire(monkeypatch, ws, [])
    assert cdp.open_tab("https://example.com") == {"id": "NEW42", "url": "https://example.com"}
    assert ws.sent[0]["method"] == "Target.createTarget"


def test_close_tab_closes_target(monkeypatch):
    ws = AutoWS(result={"success": True})
    _wire(monkeypatch, ws, [])
    assert "T7" in cdp.close_tab("T7")
    assert ws.sent[0] == {"id": 1, "method": "Target.closeTarget", "params": {"targetId": "T7"}}
