"""Which Chrome the CDP channel drives, and the private headless Chrome's lifecycle.

The first CDP call picks a browser, and later calls keep it while it stays healthy:

1. the automation Chrome on HYPR_CUA_CDP_PORT (default 9222), when it answers;
2. otherwise, unless HYPR_CUA_CDP_AUTOLAUNCH=0, a private headless Chrome that this
   process starts on a port Chrome picks itself.

The private Chrome stops IDLE_SECONDS after the last call finished, and on exit
(SIGINT, SIGTERM or a normal exit). One lock serializes choosing, launching and
stopping, so concurrent first calls start one Chrome.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

HTTP_TIMEOUT = 10    # seconds per DevTools HTTP request, at most
LAUNCH_TIMEOUT = 20  # seconds for a private Chrome to write DevToolsActivePort
IDLE_SECONDS = 600   # stop the private Chrome this long after the last call finished
DEFAULT_PORT = 9222
_CHROME_BINARIES = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome",
)

# Seams the tests replace: the idle clock, timers, process-group signals, exit.
_now = time.monotonic
_Timer = threading.Timer
_killpg = os.killpg
_exit = os._exit


class CdpError(RuntimeError):
    """A Chrome DevTools Protocol operation failed."""


class CdpTimeout(CdpError):
    """A CDP operation ran out of time before its deadline."""


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT):
    """GET a DevTools HTTP endpoint and parse its JSON. Running out of time raises
    CdpTimeout; other failures raise CdpError."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except TimeoutError as exc:
        raise CdpTimeout(f"CDP HTTP request to {url}: no reply within {timeout:g} s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise CdpTimeout(f"CDP HTTP request to {url}: no reply within {timeout:g} s") from exc
        raise CdpError(f"CDP HTTP request to {url} failed: {exc}") from exc
    except OSError as exc:
        raise CdpError(f"CDP HTTP request to {url} failed: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CdpError(f"CDP HTTP response from {url} was not JSON: {exc}") from exc


def reachable(base: str, timeout: float = HTTP_TIMEOUT) -> bool:
    """True when a DevTools endpoint answers at `base` within `timeout` seconds."""
    try:
        http_get_json(base + "/json/version", timeout=timeout)
        return True
    except CdpError:
        return False


def automation_endpoint() -> str:
    """Base URL of the automation Chrome (HYPR_CUA_CDP_PORT, default 9222)."""
    try:
        port = int(os.environ.get("HYPR_CUA_CDP_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return f"http://127.0.0.1:{port}"


def _autolaunch_enabled() -> bool:
    return os.environ.get("HYPR_CUA_CDP_AUTOLAUNCH", "1").lower() not in ("0", "false", "no")


def find_chrome() -> str:
    """Absolute path of HYPR_CUA_CDP_BIN, or of the first known Chrome on PATH."""
    for cand in (os.environ.get("HYPR_CUA_CDP_BIN"), *_CHROME_BINARIES):
        found = shutil.which(cand) if cand else None
        if found:
            return found
    raise CdpError("no chrome/chromium binary found; install one or set HYPR_CUA_CDP_BIN")


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Start Chrome in its own process group, so one signal reaches all of it."""
    try:
        return subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:  # not found, permission denied, bad executable format
        raise CdpError(f"could not launch {argv[0]!r}: {exc}") from exc


def _new_profile_dir() -> str:
    """A fresh 0700 directory: $XDG_RUNTIME_DIR/hypr-cua/chrome-*, else a temp dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        parent = os.path.join(runtime, "hypr-cua")
        os.makedirs(parent, mode=0o700, exist_ok=True)
        return tempfile.mkdtemp(prefix="chrome-", dir=parent)
    return tempfile.mkdtemp(prefix="hypr-cua-chrome-")


def _read_port(profile: str, proc) -> int:
    """Wait for Chrome to write DevToolsActivePort; return the port on its first line."""
    path = os.path.join(profile, "DevToolsActivePort")
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while True:
        try:
            with open(path) as fh:
                first = fh.readline().strip()
            if first:
                return int(first)
        except (FileNotFoundError, ValueError):
            pass
        if proc.poll() is not None:
            raise CdpError(
                f"private Chrome exited (code {proc.returncode}) before opening its DevTools port"
            )
        if time.monotonic() >= deadline:
            raise CdpError(
                f"private Chrome did not open its DevTools port within {LAUNCH_TIMEOUT}s"
            )
        time.sleep(0.05)


def _kill_group(proc, wait: float) -> None:
    """SIGTERM Chrome's process group, wait up to `wait` s, then SIGKILL what is left."""
    try:
        _killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + wait
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        _killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class BrowserManager:
    """Picks the browser for CDP calls and runs the private Chrome's lifecycle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode: str | None = None   # "automation" | "private" | None
        self.base: str | None = None   # http://127.0.0.1:<port>
        self._proc = None              # the private Chrome (Popen), if running
        self._dir: str | None = None   # its profile directory
        self._active = 0               # calls in progress
        self._last_done = 0.0          # _now() when the last call finished
        self._timer = None
        self._spawning = False         # a launch is between spawn and publishing _proc

    @contextmanager
    def use(self, probe_timeout: float = HTTP_TIMEOUT):
        """Yield the base URL for one CDP call; the idle stop waits while it runs.
        Health probes of the automation Chrome get at most `probe_timeout` seconds."""
        with self._lock:
            if not self._healthy(probe_timeout):
                self._choose(probe_timeout)
            self._active += 1
            base = self.base
        try:
            yield base
        finally:
            with self._lock:
                self._active -= 1
                self._last_done = _now()
                if self.mode == "private":
                    self._arm_idle_timer()

    def _healthy(self, probe_timeout: float) -> bool:
        if self.mode == "private":
            return self._proc is not None and self._proc.poll() is None
        if self.mode == "automation":
            return reachable(self.base, probe_timeout)
        return False

    def _choose(self, probe_timeout: float) -> None:
        self._stop_private(wait=2.0)  # clears a dead private Chrome's leftovers
        self.mode = self.base = None
        base = automation_endpoint()
        if reachable(base, probe_timeout):
            self.mode, self.base = "automation", base
            return
        if not _autolaunch_enabled():
            raise CdpError(
                f"No Chrome DevTools endpoint at {base}, and HYPR_CUA_CDP_AUTOLAUNCH=0. "
                "Start the automation Chrome with bin/hypr-cua-chrome, or unset the "
                "variable so hypr-cua starts a private headless Chrome."
            )
        self._launch_private()

    def _launch_private(self) -> None:
        if _shutting_down:
            raise CdpError("hypr-cua is shutting down; not starting a private Chrome")
        binary = find_chrome()
        profile = _new_profile_dir()
        self._spawning = True  # shutdown() waits briefly for _proc while this is set
        try:
            try:
                proc = _spawn([
                    binary, "--headless=new", "--remote-debugging-port=0",
                    f"--user-data-dir={profile}", "--no-first-run",
                    "--no-default-browser-check", "about:blank",
                ])
            except CdpError:
                shutil.rmtree(profile, ignore_errors=True)
                raise
            self._proc, self._dir = proc, profile  # shutdown() can see it from here on
        finally:
            self._spawning = False
        try:
            port = _read_port(profile, proc)
        except CdpError:
            self._stop_private(wait=2.0)
            raise
        self.mode, self.base = "private", f"http://127.0.0.1:{port}"

    def _stop_private(self, wait: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        proc, profile = self._proc, self._dir
        if proc is not None:
            _kill_group(proc, wait)  # still visible to shutdown() while this waits
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
        self._proc = self._dir = None
        if self.mode == "private":
            self.mode = self.base = None

    def _arm_idle_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = _Timer(IDLE_SECONDS, self._idle_check)
        self._timer.daemon = True
        self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            if self.mode != "private" or self._active:
                return
            if _now() - self._last_done < IDLE_SECONDS:
                return
            self._stop_private(wait=2.0)


manager = BrowserManager()
_shutting_down = False


def shutdown(signum=None, frame=None) -> None:
    """SIGINT/SIGTERM handler: stop the private Chrome, then exit.

    Lock-free, to fit Claude Code's SIGINT, then SIGTERM 100 ms later, then SIGKILL
    400 ms after that. A second signal, even one that lands during the first
    cleanup, SIGKILLs Chrome's group and exits at once. A SIGKILL during the profile
    removal is harmless, because Chrome is already stopped. No new private Chrome
    starts once shutdown begins, and one caught between spawn and publish is waited
    for (up to 0.1 s) so it gets stopped too."""
    global _shutting_down
    if _shutting_down:
        proc = manager._proc
        if proc is not None:
            try:
                _killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _exit(0)
        return
    _shutting_down = True
    deadline = time.monotonic() + 0.1
    while manager._spawning and manager._proc is None and time.monotonic() < deadline:
        time.sleep(0.005)
    proc, profile = manager._proc, manager._dir
    if proc is not None:
        _kill_group(proc, 0.15)
    if profile:
        shutil.rmtree(profile, ignore_errors=True)
    _exit(0)


def _atexit_cleanup() -> None:
    proc, profile = manager._proc, manager._dir
    if proc is not None:
        _kill_group(proc, 2.0)
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


def install_exit_handlers() -> None:
    """Route SIGINT, SIGTERM and a normal exit through the private-Chrome cleanup."""
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    atexit.register(_atexit_cleanup)
