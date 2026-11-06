"""The unit templates must agree with the connector and hold no host paths."""
from pathlib import Path

from hypr_cua_mcp import connect

UNITS = Path(__file__).resolve().parent.parent / "contrib" / "systemd"


def test_socket_unit_listens_where_the_connector_looks(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "%t")  # systemd's specifier for the runtime dir
    assert f"ListenStream={connect.socket_path()}\n" in (UNITS / "hypr-cua.socket").read_text()


def test_service_unit_is_a_template_without_host_paths():
    service = (UNITS / "hypr-cua.service").read_text()
    assert "ExecStart=@CHECKOUT@/.venv/bin/hypr-cua-mcp --serve\n" in service
    assert "/home/" not in service + (UNITS / "hypr-cua.socket").read_text()
