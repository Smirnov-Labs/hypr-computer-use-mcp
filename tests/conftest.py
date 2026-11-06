"""Suite-wide guard: no test may reach a real Chrome or signal a real process.

Port 1 never answers and autolaunch is off, so code that misses its fakes fails
fast instead of probing port 9222. Spawning Chrome and signalling a process group
are tripwires; the lifecycle tests replace them with fakes."""
import pytest

from hypr_cua_mcp import browser


@pytest.fixture(autouse=True)
def _no_real_chrome(monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_PORT", "1")
    monkeypatch.setenv("HYPR_CUA_CDP_AUTOLAUNCH", "0")
    monkeypatch.setattr(
        browser, "_spawn", lambda argv: pytest.fail(f"real Chrome spawn blocked in tests: {argv}"))
    monkeypatch.setattr(
        browser, "_killpg", lambda pid, sig: pytest.fail(f"real signal blocked in tests: {pid} {sig}"))
