"""Tests for browser choice and the private Chrome's lifecycle. No real Chrome runs:
the `env` fixture fakes spawning, signals, timers and the idle clock."""
import os
import threading
import urllib.error
import urllib.request

import pytest

from hypr_cua_mcp import browser


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.alive = True
        self.returncode = None

    def poll(self):
        return None if self.alive else 0


class FakeTimer:
    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled, self.daemon = interval, fn, False, False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class Exited(BaseException):
    pass


def _raise_exited(code):
    raise Exited


def _profile_of(argv):
    return next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir="))


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"spawned": [], "killed": [], "timers": [], "now": 1000.0,
             "automation_up": False, "write_port": True}
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPR_CUA_CDP_AUTOLAUNCH", raising=False)
    monkeypatch.delenv("HYPR_CUA_CDP_PORT", raising=False)
    monkeypatch.setattr(browser, "find_chrome", lambda: "/usr/bin/chrome")
    monkeypatch.setattr(browser, "_now", lambda: state["now"])

    def fake_http(url, timeout=None):
        if state["automation_up"] and url.startswith("http://127.0.0.1:9222/"):
            return {"Browser": "Chrome/x"}
        raise browser.CdpError(f"unreachable: {url}")

    def fake_spawn(argv):
        proc = FakeProc(50000 + len(state["spawned"]))
        state["spawned"].append((argv, proc))
        if state["write_port"]:
            with open(os.path.join(_profile_of(argv), "DevToolsActivePort"), "w") as fh:
                fh.write("45678\n/devtools/browser/abc\n")
        return proc

    def fake_killpg(pid, sig):
        state["killed"].append((pid, sig))
        for _argv, proc in state["spawned"]:
            if proc.pid == pid:
                proc.alive = False

    def fake_timer(interval, fn):
        t = FakeTimer(interval, fn)
        state["timers"].append(t)
        return t

    monkeypatch.setattr(browser, "http_get_json", fake_http)
    monkeypatch.setattr(browser, "_spawn", fake_spawn)
    monkeypatch.setattr(browser, "_killpg", fake_killpg)
    monkeypatch.setattr(browser, "_Timer", fake_timer)
    state["manager"] = browser.BrowserManager()
    return state


def test_attaches_to_the_automation_chrome_when_it_answers(env):
    env["automation_up"] = True
    with env["manager"].use() as base:
        assert base == "http://127.0.0.1:9222"
    assert env["manager"].mode == "automation"
    assert env["spawned"] == []


def test_starts_a_private_headless_chrome_when_the_port_is_down(env, tmp_path):
    with env["manager"].use() as base:
        assert base == "http://127.0.0.1:45678"
    assert env["manager"].mode == "private"
    argv, _proc = env["spawned"][0]
    assert argv[1:3] == ["--headless=new", "--remote-debugging-port=0"]
    assert argv[-3:] == ["--no-first-run", "--no-default-browser-check", "about:blank"]
    profile = _profile_of(argv)
    assert os.path.dirname(profile) == str(tmp_path / "hypr-cua")
    assert os.stat(profile).st_mode & 0o777 == 0o700


def test_keeps_the_private_browser_while_it_runs(env):
    m = env["manager"]
    with m.use():
        pass
    env["automation_up"] = True
    with m.use() as base:
        assert base == "http://127.0.0.1:45678"
    assert len(env["spawned"]) == 1


def test_picks_again_after_the_private_chrome_dies(env):
    m = env["manager"]
    with m.use():
        pass
    argv, proc = env["spawned"][0]
    proc.alive = False
    env["automation_up"] = True
    with m.use() as base:
        assert base == "http://127.0.0.1:9222"
    assert m.mode == "automation"
    assert not os.path.exists(_profile_of(argv))


def test_attach_only_error_names_both_ways_out(env, monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_AUTOLAUNCH", "0")
    with pytest.raises(browser.CdpError) as e:
        with env["manager"].use():
            pass
    assert "HYPR_CUA_CDP_AUTOLAUNCH=0" in str(e.value)
    assert "bin/hypr-cua-chrome" in str(e.value)
    assert env["spawned"] == []


def test_concurrent_first_calls_launch_one_chrome(env):
    m = env["manager"]
    barrier = threading.Barrier(4)
    bases = []

    def worker():
        barrier.wait()
        with m.use() as base:
            bases.append(base)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(env["spawned"]) == 1
    assert bases == ["http://127.0.0.1:45678"] * 4


def test_launch_timeout_stops_chrome_and_removes_its_profile(env, monkeypatch):
    env["write_port"] = False
    monkeypatch.setattr(browser, "LAUNCH_TIMEOUT", 0.2)
    with pytest.raises(browser.CdpError) as e:
        with env["manager"].use():
            pass
    assert "DevTools port" in str(e.value)
    argv, proc = env["spawned"][0]
    assert env["killed"][0] == (proc.pid, browser.signal.SIGTERM)
    assert not os.path.exists(_profile_of(argv))


def test_a_failed_spawn_removes_the_new_profile(env, monkeypatch, tmp_path):
    def broken_spawn(argv):
        raise browser.CdpError("could not launch")

    monkeypatch.setattr(browser, "_spawn", broken_spawn)
    with pytest.raises(browser.CdpError):
        with env["manager"].use():
            pass
    assert os.listdir(tmp_path / "hypr-cua") == []


def test_idle_stop_comes_600_seconds_after_the_last_call(env):
    m = env["manager"]
    with m.use():
        pass
    timer = env["timers"][-1]
    assert timer.interval == browser.IDLE_SECONDS == 600
    env["now"] += 599
    timer.fn()
    assert m.mode == "private"
    env["now"] += 1
    timer.fn()
    assert m.mode is None
    assert env["killed"]
    with m.use():
        pass
    assert len(env["spawned"]) == 2


def test_idle_stop_never_fires_during_a_call(env):
    m = env["manager"]
    with m.use():
        pass
    with m.use():
        env["now"] += 10_000
        env["timers"][-1].fn()
        assert m.mode == "private"


def test_shutdown_stops_chrome_and_removes_its_profile_then_exits(env, monkeypatch):
    m = env["manager"]
    monkeypatch.setattr(browser, "manager", m)
    monkeypatch.setattr(browser, "_shutting_down", False)
    with m.use():
        pass
    argv, proc = env["spawned"][0]
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise Exited

    monkeypatch.setattr(browser, "_exit", fake_exit)
    with pytest.raises(Exited):
        browser.shutdown()
    assert exits == [0]
    assert env["killed"][0] == (proc.pid, browser.signal.SIGTERM)
    assert not os.path.exists(_profile_of(argv))


def test_a_second_signal_during_cleanup_sigkills_the_group(env, monkeypatch):
    m = env["manager"]
    monkeypatch.setattr(browser, "manager", m)
    monkeypatch.setattr(browser, "_shutting_down", False)
    with m.use():
        pass
    sent = []

    def stubborn_killpg(pid, sig):  # Chrome ignores SIGTERM, and a second signal lands
        sent.append(sig)
        if sig == browser.signal.SIGTERM:
            browser.shutdown()

    monkeypatch.setattr(browser, "_killpg", stubborn_killpg)
    monkeypatch.setattr(browser, "_exit", _raise_exited)
    with pytest.raises(Exited):
        browser.shutdown()
    assert sent == [browser.signal.SIGTERM, browser.signal.SIGKILL]


def test_a_signal_during_an_idle_stop_still_reaches_chrome(env, monkeypatch):
    m = env["manager"]
    monkeypatch.setattr(browser, "manager", m)
    monkeypatch.setattr(browser, "_shutting_down", False)
    with m.use():
        pass
    _argv, proc = env["spawned"][0]
    seen = []

    def killpg_then_signal(pid, sig):  # the idle stop sends SIGTERM, then SIGINT arrives
        seen.append((pid, sig))
        if len(seen) == 1:
            browser.shutdown()

    monkeypatch.setattr(browser, "_killpg", killpg_then_signal)
    monkeypatch.setattr(browser, "_exit", _raise_exited)
    env["now"] += browser.IDLE_SECONDS
    with pytest.raises(Exited):
        env["timers"][-1].fn()
    assert seen[1] == (proc.pid, browser.signal.SIGTERM)


def test_install_exit_handlers_hooks_sigint_sigterm_and_atexit(monkeypatch):
    seen = {}
    monkeypatch.setattr(browser.signal, "signal", lambda sig, fn: seen.setdefault(sig, fn))
    monkeypatch.setattr(browser.atexit, "register", lambda fn: seen.setdefault("atexit", fn))
    browser.install_exit_handlers()
    assert seen[browser.signal.SIGINT] is browser.shutdown
    assert seen[browser.signal.SIGTERM] is browser.shutdown
    assert seen["atexit"] is browser._atexit_cleanup


def test_automation_endpoint_respects_the_port_env(monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_PORT", "9333")
    assert browser.automation_endpoint() == "http://127.0.0.1:9333"


def test_http_get_json_parses_the_body(monkeypatch):
    class _Resp:
        def read(self):
            return b'[{"id":"T1"}]'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    assert browser.http_get_json("http://127.0.0.1:9222/json") == [{"id": "T1"}]


def test_http_get_json_wraps_urlerror(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(browser.CdpError) as e:
        browser.http_get_json("http://127.0.0.1:9222/json")
    assert not isinstance(e.value, browser.CdpTimeout)


def test_http_get_json_reports_a_timeout_as_cdptimeout(monkeypatch):
    def slow(url, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    with pytest.raises(browser.CdpTimeout):
        browser.http_get_json("http://127.0.0.1:9222/json", timeout=0.5)
