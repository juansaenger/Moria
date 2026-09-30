from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from homebot.media import ArrClient, MediaConfig, MediaError, MediaTools, QbitClient, SeerrClient
from homebot.tools import HomeTools, SafetyPolicy, ToolError

from .fakes import FakeHA


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, summary: str) -> bool:
        self.asked.append(summary)
        return self.answer


class FakeServers:
    """One MockTransport shared by all clients; routes by host name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.qbit_logged_in = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode()
        self.calls.append((request.method, f"{host}{path}?{request.url.query.decode()}", body))

        if host == "sonarr":
            if path == "/api/v3/series":
                return httpx.Response(200, json=[SERIES])
            if path == "/api/v3/queue":
                return httpx.Response(200, json={"records": [QUEUE_ITEM]})
            if path == "/api/v3/wanted/missing":
                return httpx.Response(200, json={"records": [MISSING]})
            if path == "/api/v3/release" and request.method == "GET":
                return httpx.Response(200, json=RELEASES)
            if path == "/api/v3/release" and request.method == "POST":
                return httpx.Response(201, json=body)
            if path == "/api/v3/command":
                return httpx.Response(201, json={"id": 1, **body})
            if path.startswith("/api/v3/queue/"):
                return httpx.Response(200)
        if host == "seerr":
            if path == "/api/v1/request":
                return httpx.Response(200, json={"results": [REQUEST]})
            if path == "/api/v1/tv/100":
                return httpx.Response(200, json={"name": "Old Show", "firstAirDate": "1999-01-01"})
        if host == "qbit":
            if path == "/api/v2/auth/login":
                self.qbit_logged_in = True
                return httpx.Response(200, text="Ok.")
            if not self.qbit_logged_in:
                return httpx.Response(403, text="Forbidden")
            if path == "/api/v2/torrents/info":
                return httpx.Response(200, json=[TORRENT])
            if path == "/api/v2/torrents/resume":
                return httpx.Response(404)  # qBittorrent 5 dropped it
            if path.startswith("/api/v2/torrents/"):
                return httpx.Response(200)
        return httpx.Response(404, text=f"no route for {host}{path}")


SERIES = {
    "id": 7,
    "title": "Old Show",
    "year": 1999,
    "monitored": True,
    "status": "ended",
    "statistics": {"episodeFileCount": 0, "episodeCount": 22, "sizeOnDisk": 0},
    "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": {"episodeFileCount": 0, "episodeCount": 22}}],
}
QUEUE_ITEM = {
    "id": 55,
    "title": "Old.Show.S01.Complete-GRP",
    "series": {"title": "Old Show"},
    "episode": {"seasonNumber": 1, "episodeNumber": 1},
    "status": "warning",
    "trackedDownloadStatus": "warning",
    "trackedDownloadState": "downloading",
    "statusMessages": [{"title": "Old.Show.S01", "messages": ["The download is stalled with no connections"]}],
    "size": 10 * 1024**3,
    "sizeleft": 10 * 1024**3,
    "timeleft": None,
    "added": "2026-09-26T00:00:00Z",
}
MISSING = {"seriesId": 7, "series": {"title": "Old Show"}, "seasonNumber": 1, "episodeNumber": 3, "title": "Pilot", "airDateUtc": "1999-02-01T00:00:00Z"}
RELEASES = [
    {"guid": "g-rej", "indexerId": 2, "title": "Old.Show.COMPLETE.SERIES", "size": 60 * 1024**3, "seeders": 40, "indexer": "IdxA",
     "quality": {"quality": {"name": "WEBDL-1080p"}}, "rejected": True, "rejections": ["Full series release"], "fullSeason": True, "seasonNumber": 1, "age": 400},
    {"guid": "g-ok", "indexerId": 2, "title": "Old.Show.S01.Pack", "size": 8 * 1024**3, "seeders": 3, "indexer": "IdxA",
     "quality": {"quality": {"name": "HDTV-720p"}}, "rejected": False, "rejections": [], "fullSeason": True, "seasonNumber": 1, "age": 900},
]
REQUEST = {
    "id": 12, "status": 2, "type": "tv", "createdAt": "2026-09-26T01:00:00Z",
    "media": {"tmdbId": 100, "tvdbId": 200, "status": 3},
    "seasons": [{"seasonNumber": 1}], "requestedBy": {"displayName": "Shelby"},
}
TORRENT = {"hash": "abc123", "name": "Old.Show.S01.Complete-GRP", "state": "stalledDL", "progress": 0.0, "size": 10 * 1024**3,
           "dlspeed": 0, "num_seeds": 0, "num_complete": 0, "added_on": 1_700_000_000, "category": "tv-sonarr"}


@pytest.fixture
def servers(monkeypatch) -> FakeServers:
    fake = FakeServers()
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return fake


@pytest.fixture
def media(servers) -> MediaTools:
    return MediaTools(
        sonarr=ArrClient("Sonarr", "http://sonarr", "k"),
        seerr=SeerrClient("http://seerr", "k"),
        qbit=QbitClient("http://qbit", "admin", "pw"),
    )


def test_config_flags_and_tool_visibility():
    assert not MediaConfig().any
    only_sonarr = MediaTools.from_config(MediaConfig(sonarr_url="http://s", sonarr_api_key="k"))
    names = only_sonarr.names
    assert "find_releases" in names and "media_requests" not in names and "torrent_action" not in names
    assert not MediaConfig(qbit_url="http://q").has_qbit and MediaConfig(qbit_url="http://q", qbit_username="bypass").has_qbit
    assert only_sonarr.definitions[0]["input_schema"]  # every tool has a schema


async def test_requests_and_queue_show_the_stall(media):
    approve = Approver(True)
    requests = await media.run("media_requests", {}, approve)
    assert "Old Show (1999) S1" in requests and "Shelby" in requests and "processing" in requests

    queue = await media.run("download_queue", {}, approve)
    assert "queue_id=55" in queue and "stalled" in queue.lower()
    assert "hash=abc123" in queue and "stalledDL" in queue and "seeds 0" in queue

    missing = await media.run("missing_media", {}, approve)
    assert "S1E3" in missing


async def test_find_and_grab_with_force_and_size_approval(media, servers):
    approve = Approver(True)
    found = await media.run("find_releases", {"kind": "series", "id": 7}, approve)
    # Non-rejected releases come first; rejected ones still listed with the reason.
    assert found.index("g-ok") < found.index("g-rej")
    assert "REJECTED: Full series release" in found and "SEASON PACK" in found

    small = await media.run("grab_release", {"kind": "series", "guid": "g-ok", "indexer_id": 2, "title": "S01 pack", "size_gb": 8}, approve)
    assert approve.asked == [] and "Sent S01 pack" in small

    big = await media.run(
        "grab_release", {"kind": "series", "guid": "g-rej", "indexer_id": 2, "title": "Complete", "size_gb": 60, "force": True}, approve
    )
    assert len(approve.asked) == 1 and "60GB" in approve.asked[0]
    assert "Sent Complete" in big
    grab = [c for c in servers.calls if c[0] == "POST" and "/api/v3/release" in c[1]][-1]
    assert grab[2] == {"guid": "g-rej", "indexerId": 2, "shouldOverride": True}

    denied = Approver(False)
    result = await media.run("grab_release", {"kind": "series", "guid": "g-rej", "indexer_id": 2, "title": "Complete", "size_gb": 60}, denied)
    assert "DENIED" in result


async def test_search_remove_and_torrent_actions(media, servers):
    approve = Approver(True)
    assert "SeasonSearch" in await media.run("search_missing", {"kind": "series", "id": 7, "season": 1}, approve)
    assert "SeriesSearch" in await media.run("search_missing", {"kind": "series", "id": 7}, approve)

    removed = await media.run("remove_download", {"kind": "series", "queue_id": 55, "title": "stuck one"}, approve)
    assert "Removed stuck one" in removed and "blocklist" in approve.asked[-1]
    delete_call = [c for c in servers.calls if c[0] == "DELETE"][-1]
    assert "removeFromClient=true" in delete_call[1] and "blocklist=true" in delete_call[1]

    # resume falls back from /start (404 in our fake) to /resume... here /start succeeds.
    assert "Done: reannounce" in await media.run("torrent_action", {"action": "reannounce", "hash": "abc123"}, approve)
    assert "Done: resume" in await media.run("torrent_action", {"action": "resume", "hash": "abc123"}, approve)

    denied = Approver(False)
    assert "DENIED" in await media.run("torrent_action", {"action": "delete", "hash": "abc123", "name": "x"}, denied)
    assert len(denied.asked) == 1 and "keep files" in denied.asked[0]


async def test_qbit_relogs_in_after_403(media, servers):
    approve = Approver(True)
    await media.run("download_queue", {}, approve)
    servers.qbit_logged_in = False  # cookie expired
    out = await media.run("download_queue", {}, approve)
    assert "hash=abc123" in out
    logins = [c for c in servers.calls if "auth/login" in c[1]]
    assert len(logins) == 2


async def test_bad_input_and_unknown_kind(media):
    approve = Approver(True)
    with pytest.raises(MediaError):
        await media.run("find_releases", {"kind": "movie", "id": 1}, approve)  # radarr not configured
    with pytest.raises(MediaError):
        await media.run("grab_release", {"kind": "series", "guid": "g", "indexer_id": "two", "title": "t"}, approve)


async def test_home_tools_routes_media_and_reports_errors(media, tmp_path):
    tools = HomeTools(FakeHA(), SafetyPolicy(frozenset(), frozenset(), ()), tmp_path / "notes.md", extra=[media])
    names = [d["name"] for d in tools.definitions]
    assert "call_service" in names and "download_queue" in names
    approve = Approver(True)
    assert "queue_id=55" in await tools.run("download_queue", {}, approve)
    with pytest.raises(ToolError):
        await tools.run("find_releases", {"kind": "movie", "id": 1}, approve)


@pytest.mark.asyncio
async def test_stalled_media_reports_all_three_kinds(media):
    out = await media.run("stalled_media", {}, Approver(True))
    # The old, unavailable request with nothing downloading for it.
    assert "REQUESTS WITH NO ACTIVITY" in out
    assert "Old Show" in out and "Shelby" in out
    assert "never reached Sonarr" in out  # no externalServiceId on the request
    # The queue item Sonarr flagged.
    assert "QUEUE ITEMS WITH WARNINGS" in out
    assert "queue_id=55" in out and "stalled with no connections" in out
    # The torrent that is not moving, with the hash needed to act on it.
    assert "TORRENTS NOT MOVING" in out
    assert "hash=abc123" in out and "no seeds" in out


@pytest.mark.asyncio
async def test_stalled_media_ignores_requests_newer_than_the_window(media):
    out = await media.run("stalled_media", {"hours": 500000}, Approver(True))
    assert "REQUESTS WITH NO ACTIVITY" not in out
    assert "QUEUE ITEMS WITH WARNINGS" in out  # queue problems are not age-gated


@pytest.mark.asyncio
async def test_stalled_media_skips_requests_already_downloading(servers, monkeypatch):
    """A request whose Sonarr id is in the queue is being worked on, so it is not 'quiet'."""
    monkeypatch.setitem(QUEUE_ITEM, "seriesId", 7)
    monkeypatch.setitem(REQUEST["media"], "externalServiceId", 7)
    tools = MediaTools(
        sonarr=ArrClient("Sonarr", "http://sonarr", "k"),
        seerr=SeerrClient("http://seerr", "k"),
    )
    out = await tools.run("stalled_media", {}, Approver(True))
    assert "REQUESTS WITH NO ACTIVITY" not in out


@pytest.mark.asyncio
async def test_stalled_media_says_so_when_healthy(servers, monkeypatch):
    monkeypatch.setitem(QUEUE_ITEM, "status", "downloading")
    monkeypatch.setitem(QUEUE_ITEM, "trackedDownloadStatus", "ok")
    tools = MediaTools(sonarr=ArrClient("Sonarr", "http://sonarr", "k"))
    out = await tools.run("stalled_media", {}, Approver(True))
    assert "looks healthy" in out


def test_stalled_media_needs_an_arr_app():
    seerr_only = MediaTools(seerr=SeerrClient("http://seerr", "k"))
    assert "stalled_media" not in seerr_only.names
