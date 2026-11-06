"""Suite-wide guard: no test may reach a real Chrome.

Port 1 never answers and autolaunch is off, so code that misses its fakes fails
fast instead of probing port 9222 or starting a browser."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_chrome(monkeypatch):
    monkeypatch.setenv("HYPR_CUA_CDP_PORT", "1")
    monkeypatch.setenv("HYPR_CUA_CDP_AUTOLAUNCH", "0")
