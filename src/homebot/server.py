"""Server health: drive SMART data, UPS state, filesystem space, service uptime.

Everything here is optional and degrades one source at a time: if Scrutiny is
unreachable the UPS and disks are still reported. Nothing in this module can
change the server, so none of it needs approval.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Approver = Callable[[str], Awaitable[bool]]

TB = 1024**4
GB = 1024**3

# Scrutiny device_status is a bitfield: 0 is healthy, anything else is not.
SCRUTINY_STATUS = {0: "passed", 1: "failed (SMART)", 2: "failed (Scrutiny thresholds)", 3: "failed (both)"}
# Drive temperatures. Spinning disks are happy in the 30s; sustained 50+ shortens life.
TEMP_WARN_C = 50
TEMP_HOT_C = 60
# A consumer SSD past this is worth planning around, not panicking about.
POWER_ON_HOURS_OLD = 43800  # five years

# Uptime Kuma monitor states. Pending means failing but still inside its retry
# window; maintenance means someone paused it deliberately.
KUMA_DOWN = 0
KUMA_UP = 1
KUMA_PENDING = 2
KUMA_MAINTENANCE = 3

# NUT ups.status flags that mean something is wrong right now.
UPS_BAD_FLAGS = {
    "OB": "running on battery, mains power is out",
    "LB": "battery is LOW, shutdown is imminent",
    "RB": "battery needs REPLACING",
    "OVER": "UPS is overloaded",
    "ALARM": "UPS is reporting an alarm",
    "FSD": "forced shutdown in progress",
    "DISCHRG": "battery is discharging",
}


class ServerError(RuntimeError):
    """Reported back to Claude as a failed tool result."""


@dataclass(frozen=True)
class ServerConfig:
    scrutiny_url: str = ""
    nut_host: str = ""
    nut_port: int = 3493
    kuma_url: str = ""
    kuma_api_key: str = ""
    # Paths inside the container that map to host filesystems (mounted read-only).
    disk_paths: tuple[tuple[str, str], ...] = ()
    min_free_pct: float = 10.0

    @property
    def any(self) -> bool:
        return bool(self.scrutiny_url or self.nut_host or self.kuma_url or self.disk_paths)


def parse_disk_paths(raw: str) -> tuple[tuple[str, str], ...]:
    """'/host/media=Media,/host/data=AppData' -> (('/host/media','Media'), ...)"""
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        path, _, label = chunk.partition("=")
        path = path.strip()
        out.append((path, label.strip() or path))
    return tuple(out)


# --------------------------------------------------------------------------- sources


class ScrutinyClient:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base, timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def summary(self) -> dict[str, Any]:
        try:
            response = await self._http.get("/api/summary")
        except httpx.HTTPError as exc:
            raise ServerError(f"Could not reach Scrutiny at {self._base}: {exc}") from exc
        if response.status_code >= 400:
            raise ServerError(f"Scrutiny returned {response.status_code}")
        return (response.json() or {}).get("data", {}).get("summary", {}) or {}


class NutClient:
    """Minimal NUT (Network UPS Tools) client. Plain text over TCP, no auth for reads."""

    def __init__(self, host: str, port: int = 3493) -> None:
        self._host = host
        self._port = port

    async def _talk(self, *commands: str) -> list[str]:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ServerError(f"Could not reach the UPS daemon at {self._host}:{self._port}: {exc}") from exc
        lines: list[str] = []
        try:
            writer.write(("\n".join(commands) + "\nLOGOUT\n").encode())
            await writer.drain()
            # NUT answers in several packets, so one read can cut a list in half
            # and silently lose variables. Read until the server says goodbye.
            chunks: list[bytes] = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=10)
                if not chunk:
                    break
                chunks.append(chunk)
                text = b"".join(chunks)
                if b"OK Goodbye" in text or b"ERR " in text:
                    break
            lines = b"".join(chunks).decode(errors="replace").splitlines()
        except (OSError, asyncio.TimeoutError) as exc:
            raise ServerError(f"UPS daemon stopped responding: {exc}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        return lines

    async def names(self) -> list[str]:
        out = []
        for line in await self._talk("LIST UPS"):
            m = re.match(r'^UPS\s+(\S+)\s+"', line)
            if m:
                out.append(m.group(1))
        return out

    async def variables(self, ups: str) -> dict[str, str]:
        out = {}
        for line in await self._talk(f"LIST VAR {ups}"):
            m = re.match(r'^VAR\s+\S+\s+(\S+)\s+"(.*)"\s*$', line)
            if m:
                out[m.group(1)] = m.group(2)
        return out


class KumaClient:
    """Uptime Kuma exposes Prometheus metrics at /metrics, behind an API key."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base, timeout=30.0, auth=("", api_key))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def monitors(self) -> list[tuple[str, int]]:
        try:
            response = await self._http.get("/metrics")
        except httpx.HTTPError as exc:
            raise ServerError(f"Could not reach Uptime Kuma at {self._base}: {exc}") from exc
        if response.status_code in (401, 403):
            raise ServerError("Uptime Kuma rejected the API key.")
        if response.status_code >= 400:
            raise ServerError(f"Uptime Kuma returned {response.status_code}")
        out = []
        for line in response.text.splitlines():
            if not line.startswith("monitor_status{"):
                continue
            name = re.search(r'monitor_name="([^"]*)"', line)
            raw = line.rsplit(" ", 1)[-1].strip()
            if not name:
                continue
            try:
                out.append((name.group(1), int(float(raw))))
            except ValueError:
                continue
        return out


# --------------------------------------------------------------------------- tools


@dataclass
class ServerTools:
    config: ServerConfig
    _scrutiny: ScrutinyClient | None = field(default=None, init=False)
    _nut: NutClient | None = field(default=None, init=False)
    _kuma: KumaClient | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.config.scrutiny_url:
            self._scrutiny = ScrutinyClient(self.config.scrutiny_url)
        if self.config.nut_host:
            self._nut = NutClient(self.config.nut_host, self.config.nut_port)
        if self.config.kuma_url and self.config.kuma_api_key:
            self._kuma = KumaClient(self.config.kuma_url, self.config.kuma_api_key)

    async def aclose(self) -> None:
        for client in (self._scrutiny, self._kuma):
            if client is not None:
                await client.aclose()

    async def check(self) -> list[str]:
        """Touch each source once at startup; returns the names that answered."""
        ok = []
        for name, coro in (
            ("drive health", self._scrutiny.summary() if self._scrutiny else None),
            ("UPS", self._nut.names() if self._nut else None),
            ("uptime monitors", self._kuma.monitors() if self._kuma else None),
        ):
            if coro is None:
                continue
            try:
                await coro
                ok.append(name)
            except ServerError as exc:
                log.warning("%s check failed: %s", name, exc)
        if self.config.disk_paths:
            live = [p for p, _ in self.config.disk_paths if os.path.isdir(p)]
            if live:
                ok.append(f"disk space ({len(live)} path{'s' if len(live) != 1 else ''})")
            else:
                log.warning("none of DISK_PATHS exist in the container: %s", self.config.disk_paths)
        return ok

    @property
    def definitions(self) -> list[dict[str, Any]]:
        if not self.config.any:
            return []
        return [
            {
                "name": "server_health",
                "description": (
                    "One-call health check of the server itself: drive SMART status and temperatures, "
                    "UPS power and battery state, free space on each filesystem, and which monitored "
                    "services are down. Read-only. Use for the scheduled morning check and for "
                    "'is the server ok?' questions. Reports only what is wrong unless full=true."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "full": {
                            "type": "boolean",
                            "description": "Also list healthy items, for a complete status readout. Default false.",
                        }
                    },
                    "additionalProperties": False,
                },
            }
        ]

    @property
    def names(self) -> set[str]:
        return {d["name"] for d in self.definitions}

    async def run(self, name: str, tool_input: dict[str, Any], _approve: Approver) -> str:
        if name != "server_health" or name not in self.names:
            raise ServerError(f"Unknown tool {name!r}")
        full = bool(tool_input.get("full", False))
        problems: list[str] = []
        healthy: list[str] = []
        for section in (self._disks, self._drives, self._ups, self._services):
            try:
                bad, good = await section()
            except ServerError as exc:
                problems.append(str(exc))
                continue
            problems.extend(bad)
            healthy.extend(good)
        out = []
        if problems:
            out.append("NEEDS ATTENTION:\n" + "\n".join(f"- {p}" for p in problems))
        if full or not problems:
            out.append(("ALSO CHECKED:\n" if problems else "ALL HEALTHY:\n") + "\n".join(f"- {h}" for h in healthy))
        return "\n\n".join(out) or "Nothing is configured to check."

    async def _disks(self) -> tuple[list[str], list[str]]:
        bad, good = [], []
        for path, label in self.config.disk_paths:
            if not os.path.isdir(path):
                continue
            try:
                usage = shutil.disk_usage(path)
            except OSError as exc:
                bad.append(f"{label}: could not read free space ({exc})")
                continue
            pct = usage.free / usage.total * 100 if usage.total else 0
            line = f"{label}: {usage.free / TB:.2f}TB free of {usage.total / TB:.2f}TB ({pct:.0f}% free)"
            if pct < self.config.min_free_pct:
                bad.append(f"{line} - below the {self.config.min_free_pct:g}% threshold")
            else:
                good.append(line)
        return bad, good

    async def _drives(self) -> tuple[list[str], list[str]]:
        if self._scrutiny is None:
            return [], []
        bad, good = [], []
        for _wwn, entry in (await self._scrutiny.summary()).items():
            device = entry.get("device") or {}
            smart = entry.get("smart") or {}
            name = device.get("device_name", "?")
            model = str(device.get("model_name") or "?")
            serial = str(device.get("device_serial_id") or "")
            cap = (device.get("capacity") or 0) / TB
            status = device.get("device_status", 0) or 0
            temp = smart.get("temp")
            hours = smart.get("power_on_hours") or 0
            label = f"{name} ({model}, {cap:.1f}TB)"
            if status:
                bad.append(f"{label} SMART {SCRUTINY_STATUS.get(status, status)} - back it up and plan a replacement. {serial}")
                continue
            notes = []
            if isinstance(temp, (int, float)) and temp >= TEMP_HOT_C:
                bad.append(f"{label} is running HOT at {temp}C - check airflow")
                continue
            if isinstance(temp, (int, float)) and temp >= TEMP_WARN_C:
                notes.append(f"warm at {temp}C")
            if hours and hours >= POWER_ON_HOURS_OLD:
                notes.append(f"{hours / 8760:.1f} years powered on")
            line = f"{label} healthy" + (f", {temp}C" if temp is not None else "")
            if notes:
                bad.append(f"{label} is fine but worth noting: {', '.join(notes)}")
            else:
                good.append(line)
        return bad, good

    async def _ups(self) -> tuple[list[str], list[str]]:
        if self._nut is None:
            return [], []
        bad, good = [], []
        for ups in await self._nut.names():
            data = await self._nut.variables(ups)
            flags = (data.get("ups.status") or "").split()
            charge = data.get("battery.charge")
            runtime = data.get("battery.runtime")
            load = data.get("ups.load")
            volts = data.get("input.voltage")
            runtime_txt = f"{int(runtime) // 60}min" if str(runtime).isdigit() else "?"
            summary = f"UPS {ups}: {charge}% charged, {runtime_txt} runtime, {load}% load, {volts}V in"
            hit = [UPS_BAD_FLAGS[f] for f in flags if f in UPS_BAD_FLAGS]
            if hit:
                bad.append(f"{summary} - {'; '.join(hit)}")
            else:
                good.append(f"{summary} (on mains)")
        return bad, good

    async def _services(self) -> tuple[list[str], list[str]]:
        if self._kuma is None:
            return [], []
        monitors = await self._kuma.monitors()
        down = sorted(n for n, status in monitors if status == KUMA_DOWN)
        pending = sorted(n for n, status in monitors if status == KUMA_PENDING)
        # Maintenance is deliberate, so reporting it as an outage would cry wolf
        # every time a monitor is paused on purpose.
        watched = [n for n, status in monitors if status != KUMA_MAINTENANCE]
        bad = []
        if down:
            bad.append(f"{len(down)} monitored service(s) DOWN: {', '.join(down[:12])}")
        if pending:
            bad.append(
                f"{len(pending)} service(s) failing their checks but still retrying, so not confirmed down "
                f"yet: {', '.join(pending[:12])}"
            )
        if bad:
            return bad, []
        paused = len(monitors) - len(watched)
        note = f"all {len(watched)} monitored services up"
        return [], [note + (f" ({paused} paused for maintenance)" if paused else "")]
