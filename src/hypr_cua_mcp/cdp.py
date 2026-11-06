"""Drive Chrome tabs over the Chrome DevTools Protocol, without window focus.

browser.py decides which Chrome to talk to: the automation Chrome on port 9222, or
a private headless one it starts. This module speaks CDP to it: one WebSocket per
operation, commands framed with a monotonic id, and one deadline per operation
that covers target discovery, the connection and every command.

Two IO boundaries, both mocked in tests: browser.http_get_json (the DevTools HTTP
endpoints) and _ws_connect (a WebSocket to one target).
"""
from __future__ import annotations

import base64
import io
import json
import math
import time

import websocket
from PIL import Image as PILImage

from . import browser, capture
from .browser import CdpError, CdpTimeout

CMD_TIMEOUT = 10   # seconds: the deadline for operations without a timeout argument
MAX_TIMEOUT = 300  # seconds: the largest timeout a caller may ask for
POLL_S = 0.1       # seconds between selector polls
_CONTEXT_GONE = (
    "Execution context was destroyed",
    "Cannot find context",
    "Inspected target navigated or closed",
)
_NAV_HINT = "the page navigated during the eval; use cdp_navigate with wait_for, then retry"
_CLOSED = "the connection closed (tab or browser gone)"


def clamp_timeout(timeout) -> float:
    """A caller's timeout in seconds, clamped to 1..MAX_TIMEOUT."""
    return min(float(MAX_TIMEOUT), max(1.0, float(timeout)))


def _is_context_gone(exc: Exception) -> bool:
    return any(s in str(exc) for s in _CONTEXT_GONE)


class _Deadline:
    """One operation's time budget, `timeout` seconds from creation."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.started = time.monotonic()
        self.at = self.started + timeout

    def expired(self, what: str) -> CdpTimeout:
        return CdpTimeout(
            f"CDP {what}: no reply within {self.timeout:g} s (long await, open dialog, "
            "or hung page); pass a larger timeout or check the page"
        )

    def left(self, what: str) -> float:
        """Seconds that remain; raises CdpTimeout when none do."""
        left = self.at - time.monotonic()
        if left <= 0:
            raise self.expired(what)
        return left


def _ws_connect(url: str, timeout: float):
    """Open a WebSocket to a DevTools target without an Origin header.

    Chrome checks Origin only when the header is present, so this works on every
    version without --remote-allow-origins."""
    try:
        return websocket.create_connection(url, timeout=timeout, suppress_origin=True)
    except (websocket.WebSocketTimeoutException, TimeoutError) as exc:
        raise CdpTimeout(f"CDP connect to {url}: no reply within {timeout:g} s") from exc
    except Exception as exc:  # noqa: BLE001 - normalize any connect failure
        raise CdpError(f"could not open CDP websocket {url}: {exc}") from exc


class _Session:
    """One CDP command channel over one WebSocket, bounded by one deadline.

    command() sends {id, method, params} and reads until the reply with that id.
    Events that arrive meanwhile are kept in `events` for wait_event()."""

    def __init__(self, ws, dl: _Deadline):
        self._ws = ws
        self._id = 0
        self.dl = dl
        self.events: list[dict] = []

    def _send(self, what: str, payload: str) -> None:
        self._ws.settimeout(self.dl.left(what))  # an expired deadline sends nothing
        try:
            self._ws.send(payload)
        except (websocket.WebSocketTimeoutException, TimeoutError):
            raise self.dl.expired(what) from None
        except (websocket.WebSocketConnectionClosedException, OSError) as exc:
            raise CdpError(f"CDP {what}: {_CLOSED}") from exc

    def _recv(self, what: str) -> dict:
        self._ws.settimeout(self.dl.left(what))
        try:
            raw = self._ws.recv()
        except (websocket.WebSocketTimeoutException, TimeoutError):
            raise self.dl.expired(what) from None
        except (websocket.WebSocketConnectionClosedException, OSError) as exc:
            raise CdpError(f"CDP {what}: {_CLOSED}") from exc
        if not raw:  # websocket-client returns "" for a close frame
            raise CdpError(f"CDP {what}: {_CLOSED}")
        return json.loads(raw)

    def command(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self._send(method, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = self._recv(method)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise CdpError(
                        f"CDP {method} failed [{err.get('code')}]: {err.get('message')}"
                    )
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def wait_event(self, predicate) -> dict | None:
        """The first kept or incoming event that matches; None once the deadline passes."""
        for i, ev in enumerate(self.events):
            if predicate(ev):
                return self.events.pop(i)
        while True:
            try:
                msg = self._recv("event wait")
            except CdpTimeout:
                return None
            if "method" in msg and predicate(msg):
                return msg

    def close(self) -> None:
        """Drop the socket at once. websocket-client's close() would first send a
        close frame on the last socket timeout, which can block past the deadline."""
        try:
            self._ws.shutdown()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


# --- target resolution -------------------------------------------------------

def _http(url: str, dl: _Deadline):
    return browser.http_get_json(url, timeout=min(browser.HTTP_TIMEOUT, dl.left(url)))


def _targets_raw(base: str, dl: _Deadline) -> list[dict]:
    data = _http(base + "/json", dl)
    return data if isinstance(data, list) else []


def _resolve_ws(base: str, target: str, dl: _Deadline) -> str:
    for t in _targets_raw(base, dl):
        if t.get("id") == target:
            ws = t.get("webSocketDebuggerUrl")
            if not ws:
                raise CdpError(
                    f"target {target} has no webSocketDebuggerUrl (already attached elsewhere?)"
                )
            return ws
    raise CdpError(f"no CDP target with id {target!r}; call cdp_targets to list tabs")


def _browser_ws(base: str, dl: _Deadline) -> str:
    v = _http(base + "/json/version", dl)
    ws = (v or {}).get("webSocketDebuggerUrl")
    if not ws:
        raise CdpError("browser endpoint reported no webSocketDebuggerUrl")
    return ws


def _page_session(base: str, target: str, dl: _Deadline) -> _Session:
    url = _resolve_ws(base, target, dl)
    return _Session(_ws_connect(url, dl.left("connect")), dl)


def _browser_session(base: str, dl: _Deadline) -> _Session:
    url = _browser_ws(base, dl)
    return _Session(_ws_connect(url, dl.left("connect")), dl)


# --- JS evaluation -----------------------------------------------------------

def _format_exception(exc: dict) -> str:
    ro = exc.get("exception") or {}
    parts: list[str] = []
    if exc.get("text"):
        parts.append(str(exc["text"]))
    if ro.get("description"):
        parts.append(str(ro["description"]))
    elif ro.get("value") is not None:
        parts.append(str(ro["value"]))
    return ": ".join(parts) or "unknown JS error"


def _evaluate(sess: _Session, expression: str):
    """Runtime.evaluate by value with promises awaited; a JS throw raises CdpError."""
    res = sess.command("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
    })
    exc = res.get("exceptionDetails")
    if exc:
        raise CdpError("JS exception: " + _format_exception(exc))
    return res.get("result", {}).get("value")


# --- operations --------------------------------------------------------------
# Each operation creates its deadline inside manager.use(), so a private Chrome's
# launch wait does not count against it.

def _use(timeout: float = CMD_TIMEOUT):
    """The browser for one call; its health probe gets at most the call's timeout."""
    return browser.manager.use(min(browser.HTTP_TIMEOUT, timeout))

def list_targets() -> dict:
    """The tabs (type == "page") of the browser in use, and which browser that is."""
    with _use() as base:
        dl = _Deadline(CMD_TIMEOUT)
        tabs = [{"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
                for t in _targets_raw(base, dl) if t.get("type") == "page"]
        mode = browser.manager.mode
    return {"browser": mode, "tabs": tabs}


_STATE_JS = "({url: location.href, title: document.title, ready_state: document.readyState})"


def _nav_result(target, state, dl: _Deadline, timed_out: bool) -> dict:
    state = state if isinstance(state, dict) else {}
    return {
        "id": target,
        "url": state.get("url"),
        "title": state.get("title"),
        "ready_state": state.get("ready_state"),
        "waited_ms": round((time.monotonic() - dl.started) * 1000),
        "timed_out": timed_out,
    }


def _is_load_of(loader_id: str):
    def match(msg: dict) -> bool:
        p = msg.get("params") or {}
        return (msg.get("method") == "Page.lifecycleEvent" and p.get("name") == "load"
                and p.get("loaderId") == loader_id)
    return match


def _navigate_and_wait(sess: _Session, target: str, url: str, wait_for: str | None) -> dict:
    """Navigate sess's page, then wait for that navigation's load event and for
    `wait_for`. At the deadline, return timed_out=True with null page fields."""
    dl = sess.dl
    try:
        sess.command("Page.enable")
        sess.command("Page.setLifecycleEventsEnabled", {"enabled": True})
        res = sess.command("Page.navigate", {"url": url})
    except CdpTimeout:
        return _nav_result(target, None, dl, timed_out=True)
    if res.get("errorText"):
        raise CdpError(f"navigate to {url} failed: {res['errorText']}")
    loader = res.get("loaderId")  # absent for a same-document navigation
    if loader and sess.wait_event(_is_load_of(loader)) is None:
        return _nav_result(target, None, dl, timed_out=True)
    if wait_for:
        probe = "!!document.querySelector(" + json.dumps(wait_for) + ")"
        while True:
            try:
                if _evaluate(sess, probe):
                    break
            except CdpTimeout:
                return _nav_result(target, None, dl, timed_out=True)
            except CdpError as exc:
                if not _is_context_gone(exc):
                    raise
            left = dl.at - time.monotonic()
            if left <= 0:
                return _nav_result(target, None, dl, timed_out=True)
            time.sleep(min(POLL_S, left))
    try:
        state = _evaluate(sess, _STATE_JS)
    except CdpTimeout:
        return _nav_result(target, None, dl, timed_out=True)
    return _nav_result(target, state, dl, timed_out=False)


def open_tab(url: str = "about:blank", wait_for: str | None = None, timeout: float = 30) -> dict:
    """Open a tab, navigate it to `url` and wait like navigate()."""
    url = url or "about:blank"
    timeout = clamp_timeout(timeout)
    with _use(timeout) as base:
        dl = _Deadline(timeout)
        try:
            bs = _browser_session(base, dl)
            try:
                tid = bs.command("Target.createTarget", {"url": "about:blank"}).get("targetId")
            finally:
                bs.close()
        except CdpTimeout:
            return _nav_result(None, None, dl, timed_out=True)
        if not tid:
            raise CdpError("Target.createTarget returned no targetId")
        if url == "about:blank" and not wait_for:
            return _nav_result(tid, {"url": "about:blank", "title": "", "ready_state": "complete"},
                               dl, timed_out=False)
        try:
            sess = _page_session(base, tid, dl)
        except CdpTimeout:
            return _nav_result(tid, None, dl, timed_out=True)
        try:
            return _navigate_and_wait(sess, tid, url, wait_for)
        finally:
            sess.close()


def navigate(target: str, url: str, wait_for: str | None = None, timeout: float = 30) -> dict:
    """Navigate a tab and wait for that navigation's load event (and for CSS selector
    `wait_for`). Returns {id, url, title, ready_state, waited_ms, timed_out}."""
    timeout = clamp_timeout(timeout)
    with _use(timeout) as base:
        dl = _Deadline(timeout)
        try:
            sess = _page_session(base, target, dl)
        except CdpTimeout:
            return _nav_result(target, None, dl, timed_out=True)
        try:
            return _navigate_and_wait(sess, target, url, wait_for)
        finally:
            sess.close()


def eval_js(target: str, expression: str, timeout: float = 30):
    """Evaluate a JS expression in a tab and return its value. `timeout` (clamped to
    1..300 s) bounds the whole call; a JS throw raises CdpError."""
    timeout = clamp_timeout(timeout)
    with _use(timeout) as base:
        sess = _page_session(base, target, _Deadline(timeout))
        try:
            return _evaluate(sess, expression)
        except CdpError as exc:
            if _is_context_gone(exc):
                raise CdpError(f"{exc}; {_NAV_HINT}") from exc
            raise
        finally:
            sess.close()


def read_text(target: str, selector: str | None = None, max_chars: int = 20000) -> dict:
    """Visible text of the page, or of the first element matching `selector`.

    The page itself cuts the text to max_chars, so at most that much crosses CDP.
    Returns {title, url, text, chars_total, truncated}."""
    max_chars = max(1, int(max_chars))
    sel = json.dumps(selector) if selector else "null"
    expr = (
        "(function(){var s=" + sel + ";"
        "var el=s?document.querySelector(s):document.body;"
        "if(s&&!el){throw new Error('no element matches selector '+s);}"
        "var t=el?el.innerText:'';var c=t.slice(0," + str(max_chars) + ");"
        "var e=c.charCodeAt(c.length-1);if(e>=0xD800&&e<=0xDBFF){c=c.slice(0,-1);}"
        "return {title:document.title,url:location.href,text:c,chars_total:t.length};})()"
    )
    with _use() as base:
        sess = _page_session(base, target, _Deadline(CMD_TIMEOUT))
        try:
            val = _evaluate(sess, expr)
        finally:
            sess.close()
    val = val if isinstance(val, dict) else {}
    total = int(val.get("chars_total") or 0)
    return {"title": val.get("title"), "url": val.get("url"), "text": val.get("text") or "",
            "chars_total": total, "truncated": total > max_chars}


def click(target: str, selector: str) -> str:
    """Click the first element matching a CSS selector (element.click() in the page)."""
    sel = json.dumps(selector)
    expr = (
        "(function(){var el=document.querySelector(" + sel + ");"
        "if(!el){throw new Error('no element matches selector '+" + sel + ");}"
        "el.scrollIntoView({block:'center',inline:'center'});"
        "el.click();return true;})()"
    )
    with _use() as base:
        sess = _page_session(base, target, _Deadline(CMD_TIMEOUT))
        try:
            _evaluate(sess, expr)
        finally:
            sess.close()
    return f"clicked {selector} in {target}"


def type_text(target: str, selector: str, text: str) -> str:
    """Set the value of the element matching selector and dispatch input/change.
    Returns a length summary, never the text."""
    sel = json.dumps(selector)
    val = json.dumps(text)
    expr = (
        "(function(){var el=document.querySelector(" + sel + ");"
        "if(!el){throw new Error('no element matches selector '+" + sel + ");}"
        "el.focus();"
        "if('value' in el){el.value=" + val + ";}else{el.textContent=" + val + ";}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return true;})()"
    )
    with _use() as base:
        sess = _page_session(base, target, _Deadline(CMD_TIMEOUT))
        try:
            _evaluate(sess, expr)
        finally:
            sess.close()
    return f"typed {len(text)} chars into {selector} in {target}"


def close_tab(target: str) -> str:
    """Close a tab via Target.closeTarget."""
    with _use() as base:
        sess = _browser_session(base, _Deadline(CMD_TIMEOUT))
        try:
            res = sess.command("Target.closeTarget", {"targetId": target})
        finally:
            sess.close()
    if res.get("success") is False:
        raise CdpError(f"could not close tab {target} (no such target?)")
    return f"closed tab {target}"


def _usable(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v) and v > 0


def screenshot(target: str) -> tuple[bytes, dict]:
    """PNG of a tab's viewport, scaled to the vision budget inside Chrome.

    The scale is divided by window.devicePixelRatio, so a high-DPI browser lands on
    the same budget. Returns (png, {title, url, width, height})."""
    with _use() as base:
        sess = _page_session(base, target, _Deadline(CMD_TIMEOUT))
        try:
            vp = sess.command("Page.getLayoutMetrics").get("cssVisualViewport") or {}
            w, h = vp.get("clientWidth"), vp.get("clientHeight")
            if not (_usable(w) and _usable(h)):
                raise CdpError(f"the page reported an unusable viewport size {w!r}x{h!r}")
            info = _evaluate(sess, "({title: document.title, url: location.href,"
                                   " dpr: window.devicePixelRatio})") or {}
            dpr = info.get("dpr") if _usable(info.get("dpr")) else 1
            shot = sess.command("Page.captureScreenshot", {"format": "png", "clip": {
                "x": vp.get("pageX", 0), "y": vp.get("pageY", 0),
                "width": w, "height": h, "scale": capture._fit_scale(w, h) / dpr}})
        finally:
            sess.close()
    png = base64.b64decode(shot.get("data") or "")
    width, height = PILImage.open(io.BytesIO(png)).size  # reads the header only
    return png, {"title": info.get("title"), "url": info.get("url"),
                 "width": width, "height": height}
