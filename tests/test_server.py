from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from homebot.server import NutClient, ServerConfig, ServerTools, parse_disk_paths


class Approver:
    async def __call__(self, summary: str) -> bool:
        return True


SUMMARY = {
    "data": {
        "summary": {
            "0xAAA": {
                "device": {"device_name": "sdb", "model_name": "WDC WD220EDGZ", "capacity": 20 * 1024**4,
                           "device_status": 0, "device_serial_id": "ata-WDC"},
                "smart": {"temp": 30, "power_on_hours": 5000},
            },
            "0xBBB": {
                "device": {"device_name": "sdc", "model_name": "Seagate Dud", "capacity": 4 * 1024**4,
                           "device_status": 1, "device_serial_id": "ata-ST"},
                "smart": {"temp": 41, "power_on_hours": 60000},
            },
        }
    }
}


@pytest.fixture
def scrutiny(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/summary":
            return httpx.Response(200, json=SUMMARY)
        return httpx.Response(404)

    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def test_parse_disk_paths():
    assert parse_disk_paths("/host/media=Media, /host/appdata=AppData") == (
        ("/host/media", "Media"), ("/host/appdata", "AppData"))
    assert parse_disk_paths("/x") == (("/x", "/x"),)
    assert parse_disk_paths("") == ()


def test_no_tools_when_nothing_configured():
    assert not ServerConfig().any
    assert ServerTools(ServerConfig()).definitions == []


@pytest.mark.asyncio
async def test_failing_drive_is_reported_and_healthy_one_is_not(scrutiny):
    tools = ServerTools(ServerConfig(scrutiny_url="http://scrutiny"))
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" in out
    assert "sdc" in out and "SMART failed (SMART)" in out
    # The healthy drive is not noise in the problem list.
    assert "sdb" not in out.split("ALSO CHECKED")[0]


@pytest.mark.asyncio
async def test_full_lists_healthy_items_too(scrutiny):
    tools = ServerTools(ServerConfig(scrutiny_url="http://scrutiny"))
    out = await tools.run("server_health", {"full": True}, Approver())
    assert "sdb" in out and "healthy" in out


@pytest.mark.asyncio
async def test_disk_below_threshold_is_flagged(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"total": 1000, "free": 50, "used": 950})())
    tools = ServerTools(ServerConfig(disk_paths=((str(tmp_path), "Media"),), min_free_pct=10))
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" in out and "Media" in out and "below the 10% threshold" in out


@pytest.mark.asyncio
async def test_disk_above_threshold_is_quiet(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"total": 1000, "free": 500, "used": 500})())
    tools = ServerTools(ServerConfig(disk_paths=((str(tmp_path), "Media"),), min_free_pct=10))
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" not in out and "ALL HEALTHY" in out


class FakeNutServer:
    """Speaks just enough NUT to answer LIST UPS and LIST VAR."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.port = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader, writer):
        data = (await reader.read(4096)).decode()
        if "LIST UPS" in data:
            writer.write(b'BEGIN LIST UPS\nUPS cyberpower "CyberPower"\nEND LIST UPS\n')
        if "LIST VAR" in data:
            writer.write(
                f'BEGIN LIST VAR cyberpower\n'
                f'VAR cyberpower ups.status "{self.status}"\n'
                f'VAR cyberpower battery.charge "72"\n'
                f'VAR cyberpower battery.runtime "1200"\n'
                f'VAR cyberpower ups.load "21"\n'
                f'VAR cyberpower input.voltage "121.0"\n'
                f'END LIST VAR cyberpower\n'.encode()
            )
        await writer.drain()
        writer.close()

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


@pytest.mark.asyncio
async def test_ups_on_mains_is_quiet():
    server = await FakeNutServer("OL").start()
    try:
        tools = ServerTools(ServerConfig(nut_host="127.0.0.1", nut_port=server.port))
        out = await tools.run("server_health", {}, Approver())
        assert "NEEDS ATTENTION" not in out
        assert "72% charged" in out and "20min runtime" in out and "21% load" in out
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_ups_on_battery_is_reported():
    server = await FakeNutServer("OB DISCHRG").start()
    try:
        tools = ServerTools(ServerConfig(nut_host="127.0.0.1", nut_port=server.port))
        out = await tools.run("server_health", {}, Approver())
        assert "NEEDS ATTENTION" in out
        assert "mains power is out" in out
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_replace_battery_is_reported():
    server = await FakeNutServer("OL RB").start()
    try:
        tools = ServerTools(ServerConfig(nut_host="127.0.0.1", nut_port=server.port))
        out = await tools.run("server_health", {}, Approver())
        assert "needs REPLACING" in out
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_one_dead_source_does_not_hide_the_others(tmp_path, monkeypatch):
    """Scrutiny being down must not stop the disk section reporting."""
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"total": 1000, "free": 50, "used": 950})())
    tools = ServerTools(ServerConfig(scrutiny_url="http://127.0.0.1:9", disk_paths=((str(tmp_path), "Media"),)))
    out = await tools.run("server_health", {}, Approver())
    assert "Could not reach Scrutiny" in out
    assert "Media" in out


# ---- Uptime Kuma states, which are four, not two


def _kuma(monkeypatch, rows: list[tuple[str, int]]):
    body = "\n".join(f'monitor_status{{monitor_name="{n}"}} {v}' for n, v in rows)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return ServerTools(ServerConfig(kuma_url="http://kuma", kuma_api_key="uk1_x"))


@pytest.mark.asyncio
async def test_all_up_is_quiet(monkeypatch):
    tools = _kuma(monkeypatch, [("Plex", 1), ("Immich", 1)])
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" not in out and "all 2 monitored services up" in out


@pytest.mark.asyncio
async def test_a_down_service_is_named(monkeypatch):
    tools = _kuma(monkeypatch, [("Plex", 1), ("Immich", 0)])
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" in out and "DOWN" in out and "Immich" in out


@pytest.mark.asyncio
async def test_pending_is_reported_but_not_called_down(monkeypatch):
    tools = _kuma(monkeypatch, [("Plex", 1), ("Ollama", 2)])
    out = await tools.run("server_health", {}, Approver())
    assert "Ollama" in out and "still retrying" in out
    assert "DOWN" not in out


@pytest.mark.asyncio
async def test_maintenance_is_not_an_outage(monkeypatch):
    """A monitor paused on purpose must never be reported as a failure."""
    tools = _kuma(monkeypatch, [("Plex", 1), ("Old NAS", 3)])
    out = await tools.run("server_health", {}, Approver())
    assert "NEEDS ATTENTION" not in out
    assert "Old NAS" not in out
    assert "all 1 monitored services up" in out and "1 paused for maintenance" in out


@pytest.mark.asyncio
async def test_a_rejected_key_says_so_rather_than_implying_an_outage(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    tools = ServerTools(ServerConfig(kuma_url="http://kuma", kuma_api_key="stale"))
    out = await tools.run("server_health", {}, Approver())
    assert "rejected the API key" in out
    assert "DOWN" not in out
