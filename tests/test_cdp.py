"""Tests for the CDP browser client (HTTP /json + WebSocket send/recv mocked; no live Chrome).

Nothing here may launch a real browser: the autouse `_no_launch` fixture disables
autolaunch and turns `_spawn` into a hard failure, so a stray reachability miss can
never Popen Chrome on the developer's desktop.
"""
import json
import urllib.error
import urllib.request

import pytest

from hypr_cua_mcp import cdp


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    # Never spawn a real Chrome from a unit test. Autolaunch off means an
    # unreachable endpoint raises instead of Popen-ing; _spawn is a tripwire.
    monkeypatch.setenv("HYPR_CUA_CDP_AUTOLAUNCH", "0")
    monkeypatch.setattr(cdp, "_proc", None, raising=False)
    monkeypatch.setattr(
        cdp, "_spawn",
        lambda argv: pytest.fail(f"real Chrome spawn blocked in tests: {argv}"),
    )


def test_launch_chrome_includes_remote_allow_origins(monkeypatch):
    # Chrome M111+ 403-rejects the DevTools WebSocket unless the connecting origin
    # is allow-listed; the auto-launched browser must pass --remote-allow-origins.
    seen = {}
    monkeypatch.setattr(cdp, "_find_chrome", lambda binary=None: "/usr/bin/google-chrome")
    monkeypatch.setattr(cdp, "_wait_reachable", lambda base, timeout: None)

    class _P:
        pid = 4321

    monkeypatch.setattr(cdp, "_spawn", lambda argv: seen.update(argv=argv) or _P())
    info = cdp.launch_chrome(port=9333, user_data_dir="/tmp/x")
    assert "--remote-allow-origins=*" in seen["argv"]
    assert "--remote-debugging-port=9333" in seen["argv"]
    assert info["port"] == 9333


# --- fakes ------------------------------------------------------------------

class AutoWS:
    """A fake CDP websocket that answers each command with a canned result,
    tagging the reply with the request's own id so framing matches. Optionally
    emits N id-less events first, to prove they're skipped."""

    def __init__(self, result=None, error=None, events_before=0):
        self.sent = []            # parsed frames the client sent
        self.result = {} if result is None else result
        self.error = error
        self.events_before = events_before
        self.closed = False
        self._pending = []

    def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        for _ in range(self.events_before):
            self._pending.append({"method": "Page.frameNavigated", "params": {}})
        if self.error is not None:
            self._pending.append({"id": frame["id"], "error": self.error})
        else:
            self._pending.append({"id": frame["id"], "result": self.result})

    def recv(self):
        return json.dumps(self._pending.pop(0))

    def close(self):
        self.closed = True


def _http_router(targets, *, browser_ws="ws://127.0.0.1:9222/devtools/browser/xyz"):
    """Fake `_http_get_json`: version probe + /json target list, by URL suffix."""
    def fake(url):
        if url.endswith("/json/version"):
            return {"Browser": "Chrome/x", "webSocketDebuggerUrl": browser_ws}
        if url.endswith("/json"):
            return targets
        raise AssertionError(f"unexpected HTTP url in test: {url}")
    return fake


def _page(tid, **kw):
    d = {
        "id": tid, "type": "page", "title": f"title-{tid}", "url": f"http://{tid}",
        "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{tid}",
    }
    d.update(kw)
    return d


def _eval_result(value):
    """Shape of a Runtime.evaluate command-result carrying a by-value value."""
    return {"result": {"type": "object", "value": value}}


# --- _Session framing: monotonic id, method/params, event-skipping ----------

def test_command_frames_monotonic_ids_and_params():
    ws = AutoWS(result={"ok": 1}, events_before=2)  # 2 events precede each reply
    sess = cdp._Session(ws)
    r1 = sess.command("Domain.a", {"x": 1})
    r2 = sess.command("Domain.b")  # default params -> {}
    assert r1 == {"ok": 1}
    assert r2 == {"ok": 1}
    assert [f["id"] for f in ws.sent] == [1, 2]           # monotonic, starts at 1
    assert ws.sent[0]["method"] == "Domain.a"
    assert ws.sent[0]["params"] == {"x": 1}
    assert ws.sent[1]["params"] == {}                     # omitted params -> empty object


def test_command_surfaces_protocol_error_as_cdperror():
    ws = AutoWS(error={"code": -32000, "message": "bad target"})
    sess = cdp._Session(ws)
    with pytest.raises(cdp.CdpError) as e:
        sess.command("Runtime.evaluate", {"expression": "1"})
    assert "bad target" in str(e.value)


def test_command_times_out_without_a_matching_reply(monkeypatch):
    class Flood:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(json.loads(data))

        def recv(self):
            return json.dumps({"method": "evt", "params": {}})  # never a matching id

        def close(self):
            pass

    monkeypatch.setattr(cdp, "CMD_TIMEOUT", 0)  # deadline elapses after first skip
    sess = cdp._Session(Flood())
    with pytest.raises(cdp.CdpError):
        sess.command("Runtime.evaluate")


# --- list_targets -----------------------------------------------------------

def test_list_targets_parses_pages_only(monkeypatch):
    targets = [
        _page("T1", title="Tab one", url="http://a"),
        {"id": "SW", "type": "service_worker", "title": "sw", "url": "http://sw"},
        _page("T2", title="Tab two", url="http://b"),
    ]
    monkeypatch.setattr(cdp, "_http_get_json", _http_router(targets))
    out = cdp.list_targets()
    assert [t["id"] for t in out] == ["T1", "T2"]          # background workers filtered out
    assert out[0] == {"id": "T1", "title": "Tab one", "url": "http://a", "type": "page"}


# --- eval_js: value out, exceptionDetails -> CdpError -----------------------

def test_eval_js_returns_value_and_frames_runtime_evaluate(monkeypatch):
    ws = AutoWS(result=_eval_result(42))
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    assert cdp.eval_js("T1", "40 + 2") == 42
    sent = ws.sent[0]
    assert sent["method"] == "Runtime.evaluate"
    assert sent["params"]["expression"] == "40 + 2"
    assert sent["params"]["returnByValue"] is True
    assert sent["params"]["awaitPromise"] is True
    assert ws.closed  # connection is closed after the call


def test_eval_js_surfaces_exception_details(monkeypatch):
    ws = AutoWS(result={
        "result": {"type": "object"},
        "exceptionDetails": {"text": "Uncaught",
                             "exception": {"description": "Error: kaboom\n  at <anonymous>"}},
    })
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    with pytest.raises(cdp.CdpError) as e:
        cdp.eval_js("T1", "throw new Error('kaboom')")
    assert "kaboom" in str(e.value)


def test_eval_js_propagates_protocol_error(monkeypatch):
    ws = AutoWS(error={"code": -32000, "message": "session gone"})
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    with pytest.raises(cdp.CdpError):
        cdp.eval_js("T1", "1")


def test_resolve_ws_unknown_target_raises(monkeypatch):
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    # _ws_connect intentionally NOT mocked: resolution must fail before any connect.
    with pytest.raises(cdp.CdpError) as e:
        cdp.eval_js("NOPE", "1")
    assert "NOPE" in str(e.value)


# --- read_text --------------------------------------------------------------

def test_read_text_returns_title_url_text(monkeypatch):
    payload = {"title": "Hello", "url": "http://p/", "text": "body innertext here"}
    ws = AutoWS(result=_eval_result(payload))
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    out = cdp.read_text("T1")
    assert out == payload
    assert "innerText" in ws.sent[0]["params"]["expression"]


# --- click / type: build the right CDP calls --------------------------------

def test_click_builds_queryselector_click(monkeypatch):
    ws = AutoWS(result=_eval_result(True))
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    cdp.click("T1", "#submit")
    expr = ws.sent[0]["params"]["expression"]
    assert ws.sent[0]["method"] == "Runtime.evaluate"
    assert "querySelector" in expr
    assert '"#submit"' in expr        # selector embedded as a JS string literal
    assert ".click()" in expr


def test_type_text_sets_value_dispatches_input_and_hides_text(monkeypatch):
    ws = AutoWS(result=_eval_result(True))
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    out = cdp.type_text("T1", "#email", "a@b.co")
    expr = ws.sent[0]["params"]["expression"]
    assert '"#email"' in expr
    assert '"a@b.co"' in expr          # value embedded as a JS string literal
    assert "dispatchEvent" in expr and "input" in expr
    assert "6 chars" in out            # returns a length summary...
    assert "a@b.co" not in out         # ...never echoes the typed value


# --- navigate / open_tab / close_tab ----------------------------------------

def test_navigate_sends_page_navigate(monkeypatch):
    ws = AutoWS(result={"frameId": "F1", "loaderId": "L1"})
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([_page("T1")]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    out = cdp.navigate("T1", "https://example.com")
    assert ws.sent[0]["method"] == "Page.navigate"
    assert ws.sent[0]["params"] == {"url": "https://example.com"}
    assert out["url"] == "https://example.com"
    assert out["frameId"] == "F1"


def test_open_tab_creates_target_via_browser_ws(monkeypatch):
    ws = AutoWS(result={"targetId": "NEW42"})
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    out = cdp.open_tab("https://example.com")
    assert ws.sent[0]["method"] == "Target.createTarget"
    assert ws.sent[0]["params"] == {"url": "https://example.com"}
    assert out == {"id": "NEW42", "url": "https://example.com"}


def test_close_tab_closes_target(monkeypatch):
    ws = AutoWS(result={"success": True})
    monkeypatch.setattr(cdp, "_http_get_json", _http_router([]))
    monkeypatch.setattr(cdp, "_ws_connect", lambda url: ws)
    out = cdp.close_tab("T7")
    assert ws.sent[0]["method"] == "Target.closeTarget"
    assert ws.sent[0]["params"] == {"targetId": "T7"}
    assert "T7" in out


# --- HTTP boundary (_http_get_json) -----------------------------------------

def test_http_get_json_parses_body(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(b'[{"id":"T1"}]'))
    assert cdp._http_get_json("http://127.0.0.1:9222/json") == [{"id": "T1"}]


def test_http_get_json_wraps_urlerror_in_cdperror(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(cdp.CdpError):
        cdp._http_get_json("http://127.0.0.1:9222/json")


# --- endpoint config + launch isolation + ensure_browser --------------------

def test_endpoint_respects_port_env(monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_PORT", "9333")
    assert cdp._endpoint() == "http://127.0.0.1:9333"


def test_launch_chrome_builds_isolated_dedicated_argv(monkeypatch, tmp_path):
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(cdp, "_find_chrome", lambda binary=None: "/usr/bin/google-chrome")
    monkeypatch.setattr(cdp, "_spawn", lambda argv: seen.update(argv=argv) or _Proc())
    monkeypatch.setattr(cdp, "_wait_reachable", lambda base, timeout: None)
    info = cdp.launch_chrome(port=9333, user_data_dir=str(tmp_path))
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/google-chrome"
    assert "--remote-debugging-port=9333" in argv
    assert f"--user-data-dir={tmp_path}" in argv      # isolated profile, not the user's
    assert "--no-first-run" in argv
    assert "--no-default-browser-check" in argv
    assert info == {"pid": 4242, "port": 9333,
                    "user_data_dir": str(tmp_path), "binary": "/usr/bin/google-chrome"}


def test_ensure_browser_attaches_when_reachable(monkeypatch):
    monkeypatch.setattr(cdp, "_reachable", lambda base: True)
    monkeypatch.setattr(cdp, "launch_chrome",
                        lambda *a, **k: pytest.fail("must not launch when already reachable"))
    assert cdp.ensure_browser() == cdp._endpoint()


def test_ensure_browser_launches_when_unreachable_and_autolaunch_on(monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_AUTOLAUNCH", "1")
    monkeypatch.setattr(cdp, "_reachable", lambda base: False)
    launched = {}
    monkeypatch.setattr(cdp, "launch_chrome",
                        lambda: launched.setdefault("did", True))
    cdp.ensure_browser()
    assert launched.get("did")


def test_ensure_browser_raises_when_unreachable_and_autolaunch_off(monkeypatch):
    # autouse fixture already sets HYPR_CUA_CDP_AUTOLAUNCH=0
    monkeypatch.setattr(cdp, "_reachable", lambda base: False)
    with pytest.raises(cdp.CdpError):
        cdp.ensure_browser()
