"""Drive Chrome tabs over the Chrome DevTools Protocol — a focus-free "copilot" browser.

This is the browser channel: read + navigate + eval JS + click + type in a Chrome
tab *without* window focus, so it can run on a non-visible workspace. It never
touches the user's main profile — the endpoint is a DEDICATED Chrome on a debugging
port with an isolated `--user-data-dir` (launched on demand), or an existing
`--remote-debugging-port` you point it at.

Two IO boundaries, both mockable in tests (see tests/test_cdp.py):
- `_http_get_json(url)` — the DevTools HTTP endpoints (`/json`, `/json/version`),
  via stdlib urllib (no extra HTTP dependency).
- `_ws_connect(url)` — a WebSocket to a target's `webSocketDebuggerUrl`, via the
  synchronous `websocket-client` library. Commands are framed with a monotonic id
  and correlated on reply (unsolicited protocol events are skipped).

Synchronous by design (no asyncio): one connect-command-close per operation keeps
the surface small and stateless, matching the rest of this server.

Config (env, read per call so it's override-friendly):
- `HYPR_CUA_CDP_PORT`           debugging port (default 9222)
- `HYPR_CUA_CDP_USER_DATA_DIR`  isolated profile dir (default: a fresh temp dir)
- `HYPR_CUA_CDP_BIN`            chrome/chromium binary (default: first on PATH)
- `HYPR_CUA_CDP_AUTOLAUNCH`     "0" to require an already-running endpoint (default on)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

CMD_TIMEOUT = 10        # seconds; bounds each HTTP request and each WS command reply
LAUNCH_TIMEOUT = 20     # seconds to wait for a freshly launched Chrome's endpoint
DEFAULT_PORT = 9222

# chrome/chromium binary names, in preference order, when none is configured
_CHROME_BINARIES = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome",
)

_proc: subprocess.Popen | None = None  # a dedicated Chrome we launched (if any)


class CdpError(RuntimeError):
    """A Chrome DevTools Protocol operation failed."""


# --- config -----------------------------------------------------------------

def _port() -> int:
    try:
        return int(os.environ.get("HYPR_CUA_CDP_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _endpoint() -> str:
    return f"http://127.0.0.1:{_port()}"


def _autolaunch_enabled() -> bool:
    return os.environ.get("HYPR_CUA_CDP_AUTOLAUNCH", "1").lower() not in ("0", "false", "no")


# --- IO boundaries (mocked in tests) ----------------------------------------

def _http_get_json(url: str):
    """GET a DevTools HTTP endpoint and parse JSON. Wraps failures in CdpError."""
    try:
        with urllib.request.urlopen(url, timeout=CMD_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.URLError as exc:
        raise CdpError(f"CDP HTTP request to {url} failed: {exc}") from exc
    except OSError as exc:
        raise CdpError(f"CDP HTTP request to {url} failed: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CdpError(f"CDP HTTP response from {url} was not JSON: {exc}") from exc


def _ws_connect(url: str):
    """Open a synchronous WebSocket to a DevTools target (websocket-client)."""
    try:
        import websocket  # websocket-client
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise CdpError(
            "websocket-client is not installed; add it (`uv add websocket-client`)"
        ) from exc
    try:
        return websocket.create_connection(url, timeout=CMD_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - normalize any connect failure
        raise CdpError(f"could not open CDP websocket {url}: {exc}") from exc


# --- command framing --------------------------------------------------------

class _Session:
    """A thin CDP command channel over one websocket.

    `command()` sends `{id, method, params}` with a per-session monotonic id and
    reads replies until the one whose `id` matches — unsolicited protocol events
    (which have no `id`) are skipped. A CDP `error` reply, or no matching reply
    within CMD_TIMEOUT, raises CdpError.
    """

    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    def command(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + CMD_TIMEOUT
        while True:
            raw = self._ws.recv()
            if raw:
                msg = json.loads(raw)
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        err = msg.get("error") or {}
                        raise CdpError(
                            f"CDP {method} failed [{err.get('code')}]: {err.get('message')}"
                        )
                    return msg.get("result", {})
                # else: an event or a reply to a different command — keep reading
            if time.monotonic() >= deadline:
                raise CdpError(f"CDP {method} timed out after {CMD_TIMEOUT}s waiting for a reply")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


# --- endpoint discovery / launch --------------------------------------------

def _reachable(base: str) -> bool:
    try:
        _http_get_json(base + "/json/version")
        return True
    except CdpError:
        return False


def _find_chrome(binary: str | None = None) -> str:
    """Absolute path to a chrome/chromium binary, honoring an explicit choice or
    HYPR_CUA_CDP_BIN, else the first known name on PATH."""
    for cand in (binary, os.environ.get("HYPR_CUA_CDP_BIN"), *_CHROME_BINARIES):
        found = shutil.which(cand) if cand else None
        if found:
            return found
    raise CdpError(
        "no chrome/chromium binary found; install one or set HYPR_CUA_CDP_BIN"
    )


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Popen a detached Chrome with its own session (so it outlives this call)."""
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CdpError(f"could not launch {argv[0]!r}: {exc}") from exc


def _wait_reachable(base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _reachable(base):
            return
        time.sleep(0.25)
    raise CdpError(f"Chrome DevTools endpoint {base} did not come up within {timeout}s")


def launch_chrome(
    port: int | None = None,
    user_data_dir: str | None = None,
    binary: str | None = None,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> dict:
    """Launch a DEDICATED Chrome for remote debugging and wait for its endpoint.

    Always uses an isolated `--user-data-dir` (a fresh temp dir unless one is given
    or HYPR_CUA_CDP_USER_DATA_DIR is set) so it never opens the user's main profile,
    plus `--no-first-run --no-default-browser-check`. Returns {pid, port,
    user_data_dir, binary}.
    """
    port = _port() if port is None else int(port)
    binary = _find_chrome(binary)
    if user_data_dir is None:
        user_data_dir = (
            os.environ.get("HYPR_CUA_CDP_USER_DATA_DIR")
            or tempfile.mkdtemp(prefix="hypr-cua-cdp-")
        )
    argv = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Chrome M111+ 403-rejects the DevTools WebSocket handshake unless the
        # connecting Origin is allow-listed. This is a dedicated, isolated,
        # loopback-only browser, so allow the local origin the client dials from.
        "--remote-allow-origins=*",
    ]
    if headless:
        argv.append("--headless=new")
    if extra_args:
        argv.extend(extra_args)
    proc = _spawn(argv)
    _wait_reachable(f"http://127.0.0.1:{port}", LAUNCH_TIMEOUT)
    global _proc
    _proc = proc
    return {"pid": getattr(proc, "pid", None), "port": port,
            "user_data_dir": user_data_dir, "binary": binary}


def ensure_browser() -> str:
    """Return the DevTools base URL, launching a dedicated Chrome if none answers.

    If the configured endpoint already responds (our own dedicated Chrome, or one
    you started), attach to it. Otherwise launch a dedicated, isolated-profile
    Chrome — unless autolaunch is disabled, in which case raise.
    """
    base = _endpoint()
    if _reachable(base):
        return base
    if not _autolaunch_enabled():
        raise CdpError(
            f"no Chrome DevTools endpoint at {base} and autolaunch is disabled; "
            "start a dedicated `chrome --remote-debugging-port` (isolated "
            "--user-data-dir) or unset HYPR_CUA_CDP_AUTOLAUNCH"
        )
    launch_chrome()
    return base


# --- target resolution ------------------------------------------------------

def _targets_raw() -> list[dict]:
    data = _http_get_json(_endpoint() + "/json")
    return data if isinstance(data, list) else []


def _resolve_ws(target: str) -> str:
    """WebSocket debugger URL for a target id (or a ws:// URL, passed through)."""
    if isinstance(target, str) and target.startswith("ws://"):
        return target
    for t in _targets_raw():
        if t.get("id") == target:
            ws = t.get("webSocketDebuggerUrl")
            if not ws:
                raise CdpError(
                    f"target {target} has no webSocketDebuggerUrl (already attached elsewhere?)"
                )
            return ws
    raise CdpError(f"no CDP target with id {target!r}; call cdp_targets to list tabs")


def _browser_ws() -> str:
    v = _http_get_json(_endpoint() + "/json/version")
    ws = (v or {}).get("webSocketDebuggerUrl")
    if not ws:
        raise CdpError("browser endpoint reported no webSocketDebuggerUrl")
    return ws


def _target_session(target: str) -> _Session:
    return _Session(_ws_connect(_resolve_ws(target)))


def _browser_session() -> _Session:
    return _Session(_ws_connect(_browser_ws()))


# --- JS evaluation helpers --------------------------------------------------

def _format_exception(exc: dict) -> str:
    """Human-readable text for a Runtime.ExceptionDetails."""
    ro = exc.get("exception") or {}
    parts: list[str] = []
    if exc.get("text"):
        parts.append(str(exc["text"]))
    if ro.get("description"):
        parts.append(str(ro["description"]))
    elif ro.get("value") is not None:
        parts.append(str(ro["value"]))
    return ": ".join(parts) or "unknown JS error"


def _evaluate(sess: _Session, expression: str, *, await_promise: bool = True):
    """Runtime.evaluate returning the JS value by value; JS throws -> CdpError."""
    res = sess.command("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })
    exc = res.get("exceptionDetails")
    if exc:
        raise CdpError("JS exception: " + _format_exception(exc))
    return res.get("result", {}).get("value")


# --- public operations ------------------------------------------------------

def list_targets() -> list[dict]:
    """Drivable tabs (type == 'page') as {id, title, url, type}. Launches the
    dedicated copilot Chrome if the endpoint isn't up yet."""
    ensure_browser()
    out: list[dict] = []
    for t in _targets_raw():
        if t.get("type") == "page":
            out.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "url": t.get("url"),
                "type": t.get("type"),
            })
    return out


def open_tab(url: str = "about:blank") -> dict:
    """Open a new tab (via Target.createTarget on the browser endpoint). Returns
    {id, url}."""
    ensure_browser()
    url = url or "about:blank"
    sess = _browser_session()
    try:
        res = sess.command("Target.createTarget", {"url": url})
    finally:
        sess.close()
    tid = res.get("targetId")
    if not tid:
        raise CdpError(f"Target.createTarget returned no targetId: {res}")
    return {"id": tid, "url": url}


def navigate(target: str, url: str) -> dict:
    """Navigate a tab (by id) to url (Page.navigate). Returns {id, url, frameId}."""
    ensure_browser()
    sess = _target_session(target)
    try:
        res = sess.command("Page.navigate", {"url": url})
    finally:
        sess.close()
    if res.get("errorText"):
        raise CdpError(f"navigate to {url} failed: {res['errorText']}")
    return {"id": target, "url": url, "frameId": res.get("frameId")}


def eval_js(target: str, expression: str):
    """Evaluate a JS expression in a tab (by id) and return its value (by value).
    A JS exception is surfaced as CdpError with the thrown message."""
    ensure_browser()
    sess = _target_session(target)
    try:
        return _evaluate(sess, expression)
    finally:
        sess.close()


def read_text(target: str) -> dict:
    """Read a tab's visible text with its title + url: {title, url, text} where
    text is document.body.innerText."""
    ensure_browser()
    sess = _target_session(target)
    try:
        val = _evaluate(
            sess,
            "({title: document.title, url: location.href,"
            " text: (document.body ? document.body.innerText : '')})",
        )
    finally:
        sess.close()
    if isinstance(val, dict):
        return {"title": val.get("title"), "url": val.get("url"), "text": val.get("text")}
    return {"title": None, "url": None, "text": val}


def click(target: str, selector: str) -> str:
    """Click the first element matching a CSS selector in a tab (by id).

    v1 uses `document.querySelector(sel).click()` (works without window focus). A
    missing element raises CdpError (the injected JS throws). Input.dispatchMouseEvent
    for a real synthetic click is a future stretch goal.
    """
    ensure_browser()
    sel = json.dumps(selector)
    expr = (
        "(function(){var el=document.querySelector(" + sel + ");"
        "if(!el){throw new Error('no element matches selector '+" + sel + ");}"
        "el.scrollIntoView({block:'center',inline:'center'});"
        "el.click();return true;})()"
    )
    sess = _target_session(target)
    try:
        _evaluate(sess, expr)
    finally:
        sess.close()
    return f"clicked {selector} in {target}"


def type_text(target: str, selector: str, text: str) -> str:
    """Type into the element matching selector in a tab (by id): focus it, set its
    value (or textContent), and dispatch `input`+`change` events so frameworks
    notice. Returns a length summary — never echoes the typed text."""
    ensure_browser()
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
    sess = _target_session(target)
    try:
        _evaluate(sess, expr)
    finally:
        sess.close()
    return f"typed {len(text)} chars into {selector} in {target}"


def close_tab(target: str) -> str:
    """Close a tab (by id) via Target.closeTarget on the browser endpoint."""
    ensure_browser()
    sess = _browser_session()
    try:
        res = sess.command("Target.closeTarget", {"targetId": target})
    finally:
        sess.close()
    if res.get("success") is False:
        raise CdpError(f"could not close tab {target} (no such target?)")
    return f"closed tab {target}"
