"""Media stack tools: Seerr requests, Sonarr/Radarr libraries and queues, qBittorrent.

Everything here is optional. A service's tools are only offered to Claude when its
URL and key are configured. All calls stay on the home network.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Approver = Callable[[str], Awaitable[bool]]

MAX_RELEASES = 25
MAX_QUEUE_ITEMS = 40
MAX_TORRENTS = 40
# Grabs bigger than this ask for approval: they take days and fill disks.
BIG_GRAB_GB = 40.0
GB = 1024**3

SEERR_REQUEST_STATUS = {1: "pending approval", 2: "approved", 3: "declined", 4: "failed"}
SEERR_MEDIA_STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially available", 5: "available"}


class MediaError(RuntimeError):
    """Reported back to Claude as a failed tool result."""


@dataclass(frozen=True)
class MediaConfig:
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    seerr_url: str = ""
    seerr_api_key: str = ""
    qbit_url: str = ""
    qbit_username: str = ""
    qbit_password: str = ""

    @property
    def has_sonarr(self) -> bool:
        return bool(self.sonarr_url and self.sonarr_api_key)

    @property
    def has_radarr(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)

    @property
    def has_seerr(self) -> bool:
        return bool(self.seerr_url and self.seerr_api_key)

    @property
    def has_qbit(self) -> bool:
        # Username 'bypass' means the Web UI has auth bypass on for this subnet.
        return bool(self.qbit_url and self.qbit_username)

    @property
    def any(self) -> bool:
        return self.has_sonarr or self.has_radarr or self.has_seerr or self.has_qbit


# --------------------------------------------------------------------------- clients


class _Http:
    def __init__(self, name: str, base_url: str, headers: dict[str, str], timeout: float = 30.0) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaError(f"Could not reach {self.name} at {self._base}: {exc}") from exc
        if response.status_code in (401, 403):
            raise MediaError(f"{self.name} rejected the API key ({response.status_code}).")
        if response.status_code >= 400:
            raise MediaError(f"{self.name} returned {response.status_code} for {method} {path}: {response.text[:300]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text


class ArrClient:
    """Sonarr (kind='series') or Radarr (kind='movie'), v3 API."""

    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        self.kind = "series" if name.lower() == "sonarr" else "movie"
        self._http = _Http(name, base_url, {"X-Api-Key": api_key})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def library(self) -> list[dict[str, Any]]:
        return await self._http.request("GET", f"/api/v3/{self.kind}") or []

    async def item(self, item_id: int) -> dict[str, Any]:
        return await self._http.request("GET", f"/api/v3/{self.kind}/{item_id}")

    async def queue(self) -> list[dict[str, Any]]:
        include = "includeSeries=true&includeEpisode=true" if self.kind == "series" else "includeMovie=true"
        data = await self._http.request("GET", f"/api/v3/queue?pageSize=200&{include}")
        return (data or {}).get("records", [])

    async def missing(self) -> list[dict[str, Any]]:
        include = "includeSeries=true" if self.kind == "series" else "includeMovie=true"
        data = await self._http.request(
            "GET", f"/api/v3/wanted/missing?pageSize=60&sortKey=airDateUtc&sortDirection=descending&{include}"
        )
        return (data or {}).get("records", [])

    async def releases(self, item_id: int, season: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {f"{self.kind}Id": item_id}
        if self.kind == "series" and season is not None:
            params["seasonNumber"] = season
        return await self._http.request("GET", "/api/v3/release", params=params, timeout=120.0) or []

    async def grab(self, guid: str, indexer_id: int, force: bool) -> Any:
        payload: dict[str, Any] = {"guid": guid, "indexerId": indexer_id}
        if force:
            payload["shouldOverride"] = True
        return await self._http.request("POST", "/api/v3/release", json=payload, timeout=120.0)

    async def command(self, payload: dict[str, Any]) -> Any:
        return await self._http.request("POST", "/api/v3/command", json=payload)

    async def delete_queue_item(self, queue_id: int, remove_from_client: bool, blocklist: bool) -> Any:
        params = {
            "removeFromClient": str(remove_from_client).lower(),
            "blocklist": str(blocklist).lower(),
            "skipRedownload": "false",
        }
        return await self._http.request("DELETE", f"/api/v3/queue/{queue_id}", params=params)


class SeerrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._http = _Http("Seerr", base_url, {"X-Api-Key": api_key})
        self._titles: dict[tuple[str, int], str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def requests(self, take: int = 60) -> list[dict[str, Any]]:
        data = await self._http.request("GET", "/api/v1/request", params={"take": take, "sort": "added", "filter": "all"})
        return (data or {}).get("results", [])

    async def title(self, media_type: str, tmdb_id: int) -> str:
        key = (media_type, tmdb_id)
        if key not in self._titles:
            try:
                detail = await self._http.request("GET", f"/api/v1/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}")
                name = detail.get("name") or detail.get("title") or f"tmdb {tmdb_id}"
                year = (detail.get("firstAirDate") or detail.get("releaseDate") or "")[:4]
                self._titles[key] = f"{name} ({year})" if year else name
            except MediaError:
                self._titles[key] = f"tmdb {tmdb_id}"
        return self._titles[key]


class QbitClient:
    """qBittorrent Web API v2. Logs in lazily and again if the cookie expires."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._http = _Http("qBittorrent", base_url, {"Referer": base_url.rstrip("/")})
        self._username = username
        self._password = password
        self._logged_in = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        if self._username.lower() == "bypass":
            self._logged_in = True  # Web UI auth bypass for the local subnet
            return
        # qBittorrent 4.x answers "Ok."/"Fails." with 200; 5.x answers 204 with an
        # empty body and sets the session cookie, so an empty result is a success.
        result = await self._http.request(
            "POST", "/api/v2/auth/login", data={"username": self._username, "password": self._password}
        )
        text = "" if result is None else str(result).strip()
        if text and text != "Ok.":
            raise MediaError("qBittorrent login failed. Check QBIT_USERNAME and QBIT_PASSWORD.")
        self._logged_in = True

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._logged_in:
            await self._login()
        try:
            return await self._http.request(method, path, **kwargs)
        except MediaError as exc:
            if "403" in str(exc) or "rejected" in str(exc):
                self._logged_in = False
                await self._login()
                return await self._http.request(method, path, **kwargs)
            raise

    async def torrents(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/api/v2/torrents/info") or []

    async def action(self, action: str, torrent_hash: str, delete_files: bool = False) -> None:
        data: dict[str, Any] = {"hashes": torrent_hash}
        if action == "delete":
            data["deleteFiles"] = str(delete_files).lower()
            await self._call("POST", "/api/v2/torrents/delete", data=data)
            return
        paths = {
            "recheck": ["/api/v2/torrents/recheck"],
            "reannounce": ["/api/v2/torrents/reannounce"],
            # qBittorrent 5 renamed resume/pause to start/stop; try both.
            "resume": ["/api/v2/torrents/start", "/api/v2/torrents/resume"],
            "pause": ["/api/v2/torrents/stop", "/api/v2/torrents/pause"],
        }
        last: MediaError | None = None
        for path in paths[action]:
            try:
                await self._call("POST", path, data=data)
                return
            except MediaError as exc:
                last = exc
                if "404" not in str(exc):
                    raise
        raise last or MediaError("unknown torrent action")


# --------------------------------------------------------------------------- tools


def _gb(size: Any) -> str:
    try:
        return f"{float(size) / GB:.1f}GB"
    except (TypeError, ValueError):
        return "?GB"


def _age(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        when = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = datetime.datetime.now(datetime.timezone.utc) - when
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _epoch_age(seconds: Any) -> str:
    try:
        when = datetime.datetime.fromtimestamp(float(seconds), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return "?"
    return _age(when.isoformat())


def _int(tool_input: dict[str, Any], key: str, required: bool = True) -> int | None:
    value = tool_input.get(key)
    if value is None or value == "":
        if required:
            raise MediaError(f"{key} is required")
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"{key} must be a whole number") from exc


def _str(tool_input: dict[str, Any], key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MediaError(f"{key} is required")
    return value.strip()


class MediaTools:
    def __init__(
        self,
        sonarr: ArrClient | None = None,
        radarr: ArrClient | None = None,
        seerr: SeerrClient | None = None,
        qbit: QbitClient | None = None,
    ) -> None:
        self._arr = {c.kind: c for c in (sonarr, radarr) if c is not None}
        self._seerr = seerr
        self._qbit = qbit

    @classmethod
    def from_config(cls, cfg: MediaConfig) -> "MediaTools":
        return cls(
            sonarr=ArrClient("Sonarr", cfg.sonarr_url, cfg.sonarr_api_key) if cfg.has_sonarr else None,
            radarr=ArrClient("Radarr", cfg.radarr_url, cfg.radarr_api_key) if cfg.has_radarr else None,
            seerr=SeerrClient(cfg.seerr_url, cfg.seerr_api_key) if cfg.has_seerr else None,
            qbit=QbitClient(cfg.qbit_url, cfg.qbit_username, cfg.qbit_password) if cfg.has_qbit else None,
        )

    async def aclose(self) -> None:
        for client in (*self._arr.values(), self._seerr, self._qbit):
            if client is not None:
                await client.aclose()

    async def check(self) -> list[str]:
        """Touch each configured service once at startup. Returns the names that answered."""
        ok = []
        for name, coro in (
            ("Sonarr", self._arr["series"].library() if "series" in self._arr else None),
            ("Radarr", self._arr["movie"].library() if "movie" in self._arr else None),
            ("Seerr", self._seerr.requests(1) if self._seerr else None),
            ("qBittorrent", self._qbit.torrents() if self._qbit else None),
        ):
            if coro is None:
                continue
            try:
                await coro
                ok.append(name)
            except MediaError as exc:
                log.warning("%s check failed: %s", name, exc)
        return ok

    # ---- definitions

    @property
    def definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        kinds = sorted(self._arr)
        kind_desc = " or ".join(f"'{k}'" for k in kinds)
        if self._seerr:
            defs.append(
                {
                    "name": "media_requests",
                    "description": (
                        "List recent media requests from Seerr (what people asked for) with who asked, when, "
                        "and whether it is available yet. Start here for 'where is my show/movie' questions."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"only_open": {"type": "boolean", "description": "Hide requests that are fully available. Default true."}},
                        "additionalProperties": False,
                    },
                }
            )
        if self._arr:
            defs += [
                {
                    "name": "media_library_search",
                    "description": (
                        f"Search the Sonarr/Radarr library by title. Returns each match's kind ({kind_desc}), id, "
                        "year, whether it is monitored, and how many episodes/files exist versus expected. "
                        "Use the id with find_releases, search_missing and download_queue."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "Part of the title."}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "download_queue",
                    "description": (
                        "Show what Sonarr/Radarr are currently downloading or importing, with status, warnings, "
                        "size left and time left"
                        + (", plus the live torrent list from qBittorrent (state, progress, speed, seeds, age)." if self._qbit else ".")
                        + " Stalled means no seeds or no progress. Items with warnings usually explain themselves."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "include_seeding": {"type": "boolean", "description": "Also list finished torrents that are just seeding. Default false."}
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "missing_media",
                    "description": (
                        "List monitored episodes/movies that Sonarr/Radarr still have no file for (most recent first). "
                        "Useful to see what never downloaded."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"kind": {"type": "string", "enum": kinds, "description": "Limit to one app; default both."}},
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "find_releases",
                    "description": (
                        "Run an interactive indexer search for a series (optionally one season) or a movie and list "
                        "the releases found: guid, indexer, title, size, seeders, quality, season pack flag, and any "
                        "rejection reasons. Sonarr only auto-grabs single episodes and season packs, so when a show "
                        "only exists as a complete-series pack this is how you find it. Slow (up to a minute)."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": kinds},
                            "id": {"type": "integer", "description": "Series or movie id from media_library_search."},
                            "season": {"type": "integer", "description": "Series only: season number. Omit for all seasons / whole-series packs."},
                        },
                        "required": ["kind", "id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "grab_release",
                    "description": (
                        "Send one release from find_releases to the download client. Pass the exact guid and indexer_id "
                        "you were given. Set force=true to override a rejection (e.g. a whole-series pack that Sonarr "
                        f"declined). Releases over {BIG_GRAB_GB:.0f}GB ask the user for approval first."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": kinds},
                            "guid": {"type": "string"},
                            "indexer_id": {"type": "integer"},
                            "title": {"type": "string", "description": "Release title, for the approval message."},
                            "size_gb": {"type": "number", "description": "Size from find_releases, for the approval check."},
                            "force": {"type": "boolean"},
                        },
                        "required": ["kind", "guid", "indexer_id", "title"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "search_missing",
                    "description": (
                        "Tell Sonarr/Radarr to run an automatic search now for a series (optionally one season) or a "
                        "movie. Cheap first step when something never downloaded; use find_releases if it still finds nothing."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": kinds},
                            "id": {"type": "integer"},
                            "season": {"type": "integer"},
                        },
                        "required": ["kind", "id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "remove_download",
                    "description": (
                        "Remove an item from the Sonarr/Radarr queue, optionally deleting it from the download client "
                        "and blocklisting the release so it isn't grabbed again. Always asks the user for approval. "
                        "Use for stuck or wrong downloads before searching for a replacement."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": kinds},
                            "queue_id": {"type": "integer", "description": "The queue item id from download_queue."},
                            "title": {"type": "string", "description": "For the approval message."},
                            "blocklist": {"type": "boolean", "description": "Default true."},
                            "remove_from_client": {"type": "boolean", "description": "Default true."},
                        },
                        "required": ["kind", "queue_id", "title"],
                        "additionalProperties": False,
                    },
                },
            ]
        if self._arr:
            defs.append(
                {
                    "name": "stalled_media",
                    "description": (
                        "One-call health check of the whole download pipeline. Lists (a) requests that are older "
                        "than `hours` and still not available with nothing downloading for them, (b) queue items "
                        "with warnings or errors, and (c) torrents that are stalled or making no progress. "
                        "Each line ends with a hint at the likely cause. Use this for the scheduled nightly check "
                        "and for 'is anything stuck?' questions."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "number",
                                "description": "How old a request must be before silence counts as stuck. Default 24.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }
            )
        if self._qbit:
            defs.append(
                {
                    "name": "torrent_action",
                    "description": (
                        "Act on one torrent in qBittorrent by hash: recheck, reannounce (nudge trackers on a stalled "
                        "torrent), resume, pause, or delete. Delete asks the user for approval and can also delete files."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["recheck", "reannounce", "resume", "pause", "delete"]},
                            "hash": {"type": "string"},
                            "name": {"type": "string", "description": "Torrent name, for the approval message."},
                            "delete_files": {"type": "boolean", "description": "delete only: also remove the downloaded data."},
                        },
                        "required": ["action", "hash"],
                        "additionalProperties": False,
                    },
                }
            )
        return defs

    @property
    def names(self) -> set[str]:
        return {d["name"] for d in self.definitions}

    # ---- dispatch

    async def run(self, name: str, tool_input: dict[str, Any], approve: Approver) -> str:
        handler = {
            "media_requests": self._requests,
            "media_library_search": self._library_search,
            "download_queue": self._queue,
            "missing_media": self._missing,
            "find_releases": self._find_releases,
            "grab_release": self._grab,
            "search_missing": self._search_missing,
            "remove_download": self._remove_download,
            "torrent_action": self._torrent_action,
            "stalled_media": self._stalled,
        }.get(name)
        if handler is None or name not in self.names:
            raise MediaError(f"Unknown tool {name!r}")
        return await handler(tool_input, approve)

    def _client(self, tool_input: dict[str, Any]) -> ArrClient:
        kind = tool_input.get("kind")
        if kind not in self._arr:
            raise MediaError(f"kind must be one of {sorted(self._arr)}")
        return self._arr[kind]

    # ---- handlers

    async def _requests(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        assert self._seerr is not None
        only_open = tool_input.get("only_open", True)
        rows = await self._seerr.requests()
        lines = []
        for req in rows:
            media = req.get("media") or {}
            media_status = media.get("status", 1)
            if only_open and media_status == 5:
                continue
            media_type = req.get("type", "movie")
            title = await self._seerr.title(media_type, media.get("tmdbId", 0))
            who = (req.get("requestedBy") or {}).get("displayName", "?")
            seasons = ",".join(str(s.get("seasonNumber")) for s in req.get("seasons") or [])
            season_txt = f" S{seasons}" if seasons else ""
            ids = f"tvdb={media.get('tvdbId')}" if media_type == "tv" else f"tmdb={media.get('tmdbId')}"
            lines.append(
                f"#{req.get('id')} {media_type}: {title}{season_txt} | requested by {who} {_age(req.get('createdAt'))} | "
                f"request {SEERR_REQUEST_STATUS.get(req.get('status'), '?')} | media {SEERR_MEDIA_STATUS.get(media_status, '?')} | {ids}"
            )
        return "\n".join(lines) or ("No open requests." if only_open else "No requests.")

    async def _stalled(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        """Everything the nightly check needs, in one call."""
        try:
            hours = max(float(tool_input.get("hours", 24)), 0.0)
        except (TypeError, ValueError) as exc:
            raise MediaError("hours must be a number") from exc
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

        # What each app is actively working on, and which queue items look unhealthy.
        active: dict[str, set[int]] = {kind: set() for kind in self._arr}
        bad_queue: list[str] = []
        for kind, client in sorted(self._arr.items()):
            try:
                records = await client.queue()
            except MediaError as exc:
                bad_queue.append(f"{client.name} queue unreadable: {exc}")
                continue
            for rec in records:
                item_id = rec.get("seriesId") if kind == "series" else rec.get("movieId")
                if item_id:
                    active[kind].add(int(item_id))
                status = str(rec.get("status") or "").lower()
                tracked = str(rec.get("trackedDownloadStatus") or "").lower()
                notes = [
                    str(msg)
                    for entry in rec.get("statusMessages") or []
                    for msg in (entry.get("messages") or [entry.get("title")])
                    if msg
                ]
                if rec.get("errorMessage"):
                    notes.append(str(rec["errorMessage"]))
                if tracked in ("warning", "error") or status in ("warning", "failed", "stalled"):
                    title = rec.get("title") or "?"
                    left = _gb(rec.get("sizeleft", 0))
                    why = "; ".join(dict.fromkeys(notes))[:200] or status or tracked
                    bad_queue.append(
                        f"{kind} queue_id={rec.get('id')} | {title} | status={status}/{tracked} | {left} left | {why}"
                    )

        # Requests that have gone quiet: old, not available, and nothing downloading for them.
        quiet: list[str] = []
        if self._seerr:
            try:
                requests = await self._seerr.requests()
            except MediaError as exc:
                quiet.append(f"Seerr unreadable: {exc}")
                requests = []
            for req in requests:
                media = req.get("media") or {}
                if media.get("status") == 5:  # fully available
                    continue
                created = req.get("createdAt") or ""
                try:
                    when = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when > cutoff:
                    continue
                media_type = req.get("type", "movie")
                kind = "series" if media_type == "tv" else "movie"
                service_id = media.get("externalServiceId")
                if kind in active and service_id and int(service_id) in active[kind]:
                    continue  # something is downloading for it right now
                title = await self._seerr.title(media_type, media.get("tmdbId", 0))
                who = (req.get("requestedBy") or {}).get("displayName", "?")
                seasons = ",".join(str(s.get("seasonNumber")) for s in req.get("seasons") or [])
                season_txt = f" S{seasons}" if seasons else ""
                request_status = SEERR_REQUEST_STATUS.get(req.get("status"), "?")
                media_status = SEERR_MEDIA_STATUS.get(media.get("status", 1), "?")
                if req.get("status") == 1:
                    hint = "waiting for someone to approve it in Seerr"
                elif req.get("status") == 3:
                    hint = "declined in Seerr"
                elif not service_id:
                    hint = f"never reached {'Sonarr' if kind == 'series' else 'Radarr'}"
                else:
                    hint = (
                        f"{'Sonarr' if kind == 'series' else 'Radarr'} id={service_id}, nothing downloading; "
                        "try search_missing, then find_releases (a show that only exists as a complete-series "
                        "pack is never auto-grabbed)"
                    )
                quiet.append(
                    f"{kind} | {title}{season_txt} | asked by {who} {_age(created)} | request={request_status} "
                    f"media={media_status} | {hint}"
                )

        # Torrents that are not moving.
        dead: list[str] = []
        if self._qbit:
            try:
                torrents = await self._qbit.torrents()
            except MediaError as exc:
                dead.append(f"qBittorrent unreadable: {exc}")
                torrents = []
            for tor in torrents:
                progress = float(tor.get("progress") or 0)
                if progress >= 1:
                    continue  # finished; seeding is not a problem
                state = str(tor.get("state") or "")
                speed = int(tor.get("dlspeed") or 0)
                seeds = int(tor.get("num_seeds") or 0)
                age_seconds = 0.0
                try:
                    age_seconds = datetime.datetime.now(datetime.timezone.utc).timestamp() - float(tor.get("added_on") or 0)
                except (TypeError, ValueError):
                    pass
                idle = speed == 0 and age_seconds > 3600
                if "stalled" not in state.lower() and not idle and state not in ("error", "missingFiles"):
                    continue
                why = {
                    "error": "qBittorrent reports an error",
                    "missingFiles": "files are missing on disk; needs a recheck",
                }.get(state, "no seeds" if seeds == 0 else "seeds present but no data moving; try reannounce")
                dead.append(
                    f"{tor.get('name')} | {state} | {progress * 100:.0f}% | {seeds} seeds | added {_epoch_age(tor.get('added_on'))} "
                    f"| hash={tor.get('hash')} | {why}"
                )

        sections = []
        if quiet:
            sections.append(f"REQUESTS WITH NO ACTIVITY (older than {hours:g}h):\n" + "\n".join(quiet[:20]))
        if bad_queue:
            sections.append("QUEUE ITEMS WITH WARNINGS:\n" + "\n".join(bad_queue[:20]))
        if dead:
            sections.append("TORRENTS NOT MOVING:\n" + "\n".join(dead[:20]))
        if not sections:
            return "Download pipeline looks healthy: no quiet requests, no queue warnings, no stalled torrents."
        return "\n\n".join(sections)

    async def _library_search(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        needle = _str(tool_input, "query").lower()
        lines = []
        for kind, client in sorted(self._arr.items()):
            for item in await client.library():
                haystack = " ".join(str(x) for x in (item.get("title"), item.get("sortTitle"), *(a.get("title") for a in item.get("alternateTitles") or [])))
                if needle not in haystack.lower():
                    continue
                lines.append(self._describe_item(kind, item))
        return "\n".join(lines[:30]) or "Nothing in the library matches. It may need to be requested first."

    def _describe_item(self, kind: str, item: dict[str, Any]) -> str:
        stats = item.get("statistics") or {}
        base = f"{kind} id={item.get('id')} | {item.get('title')} ({item.get('year')}) | monitored={item.get('monitored')}"
        if kind == "series":
            seasons = []
            for season in item.get("seasons") or []:
                s = season.get("statistics") or {}
                if season.get("seasonNumber") == 0 and not s.get("episodeCount"):
                    continue
                seasons.append(
                    f"S{season.get('seasonNumber')}:{s.get('episodeFileCount', 0)}/{s.get('episodeCount', 0)}"
                    + ("" if season.get("monitored", True) else "(unmonitored)")
                )
            return (
                f"{base} | status={item.get('status')} | files {stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)}"
                f" on disk {_gb(stats.get('sizeOnDisk', 0))} | " + " ".join(seasons)
            )
        return f"{base} | has file={item.get('hasFile')} | status={item.get('status')} | {_gb(stats.get('sizeOnDisk', item.get('sizeOnDisk', 0)))}"

    async def _queue(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        include_seeding = bool(tool_input.get("include_seeding", False))
        sections = []
        for kind, client in sorted(self._arr.items()):
            records = await client.queue()
            lines = []
            for rec in records[:MAX_QUEUE_ITEMS]:
                if kind == "series":
                    ep = rec.get("episode") or {}
                    what = f"{(rec.get('series') or {}).get('title', '?')} S{ep.get('seasonNumber', '?')}E{ep.get('episodeNumber', '?')}"
                else:
                    what = f"{(rec.get('movie') or {}).get('title', '?')}"
                messages = "; ".join(
                    m for sm in rec.get("statusMessages") or [] for m in ([sm.get("title", "")] + list(sm.get("messages") or [])) if m
                )
                left = f"{_gb(rec.get('sizeleft'))} of {_gb(rec.get('size'))} left"
                lines.append(
                    f"queue_id={rec.get('id')} | {what} | {rec.get('title')} | {rec.get('status')}/{rec.get('trackedDownloadStatus')}/"
                    f"{rec.get('trackedDownloadState')} | {left} | eta {rec.get('timeleft') or '?'} | added {_age(rec.get('added'))}"
                    + (f" | {messages}" if messages else "")
                    + (f" | error: {rec.get('errorMessage')}" if rec.get("errorMessage") else "")
                )
            sections.append(f"{client.name} queue ({len(records)}):\n" + ("\n".join(lines) or "empty"))
        if self._qbit:
            torrents = await self._qbit.torrents()
            active, seeding = [], 0
            for t in torrents:
                state = str(t.get("state", ""))
                if state in ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP") and float(t.get("progress", 0)) >= 1:
                    seeding += 1
                    if not include_seeding:
                        continue
                active.append(
                    f"hash={t.get('hash')} | {t.get('name')} | {state} | {float(t.get('progress', 0)) * 100:.0f}% of {_gb(t.get('size'))} | "
                    f"{float(t.get('dlspeed', 0)) / 1024 / 1024:.1f}MB/s | seeds {t.get('num_seeds')}({t.get('num_complete')}) | "
                    f"added {_epoch_age(t.get('added_on'))} | category {t.get('category') or '-'}"
                )
            if len(active) > MAX_TORRENTS:
                active = active[:MAX_TORRENTS] + [f"... and {len(active) - MAX_TORRENTS} more"]
            sections.append(
                f"qBittorrent ({len(torrents)} torrents, {seeding} finished/seeding):\n" + ("\n".join(active) or "nothing downloading")
            )
        return "\n\n".join(sections)

    async def _missing(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        kind_filter = tool_input.get("kind")
        sections = []
        for kind, client in sorted(self._arr.items()):
            if kind_filter and kind != kind_filter:
                continue
            records = await client.missing()
            lines = []
            for rec in records:
                if kind == "series":
                    lines.append(
                        f"series id={rec.get('seriesId')} | {(rec.get('series') or {}).get('title', '?')} "
                        f"S{rec.get('seasonNumber')}E{rec.get('episodeNumber')} {rec.get('title', '')} | aired {(rec.get('airDateUtc') or '')[:10]}"
                    )
                else:
                    lines.append(f"movie id={rec.get('id')} | {rec.get('title')} ({rec.get('year')}) | status={rec.get('status')}")
            sections.append(f"{client.name} missing ({len(records)}):\n" + ("\n".join(lines) or "nothing missing"))
        return "\n\n".join(sections) or "No app selected."

    async def _find_releases(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        client = self._client(tool_input)
        item_id = _int(tool_input, "id")
        season = _int(tool_input, "season", required=False)
        assert item_id is not None
        releases = await client.releases(item_id, season)
        if not releases:
            return "The indexers returned nothing for that. Check Prowlarr's indexers are healthy, or try a season / the whole series."
        releases.sort(key=lambda r: (bool(r.get("rejected")), -int(r.get("seeders") or 0)))
        lines = []
        for r in releases[:MAX_RELEASES]:
            flags = []
            if r.get("fullSeason"):
                flags.append("SEASON PACK")
            if r.get("seasonNumber") is not None and client.kind == "series":
                flags.append(f"S{r.get('seasonNumber')}")
            if r.get("rejected"):
                flags.append("REJECTED: " + "; ".join(r.get("rejections") or []))
            quality = ((r.get("quality") or {}).get("quality") or {}).get("name", "?")
            lines.append(
                f"guid={r.get('guid')} indexer_id={r.get('indexerId')} | {r.get('title')} | {_gb(r.get('size'))} | "
                f"seeders {r.get('seeders')} | {quality} | {r.get('indexer')} | age {r.get('age')}d"
                + (f" | {' '.join(flags)}" if flags else "")
            )
        more = f"\n... {len(releases) - MAX_RELEASES} more not shown" if len(releases) > MAX_RELEASES else ""
        return f"{len(releases)} releases (best first). Rejected ones can still be grabbed with force=true if the reason is harmless.\n" + "\n".join(lines) + more

    async def _grab(self, tool_input: dict[str, Any], approve: Approver) -> str:
        client = self._client(tool_input)
        guid = _str(tool_input, "guid")
        indexer_id = _int(tool_input, "indexer_id")
        title = _str(tool_input, "title")
        force = bool(tool_input.get("force", False))
        size_gb = float(tool_input.get("size_gb") or 0)
        assert indexer_id is not None
        if size_gb >= BIG_GRAB_GB:
            if not await approve(f"download {title} ({size_gb:.0f}GB) via {client.name}"):
                return "The user DENIED this download (or did not answer in time). Nothing was grabbed."
        await client.grab(guid, indexer_id, force)
        return f"Sent {title} to the download client via {client.name}. Check download_queue in a minute to confirm it started."

    async def _search_missing(self, tool_input: dict[str, Any], _approve: Approver) -> str:
        client = self._client(tool_input)
        item_id = _int(tool_input, "id")
        season = _int(tool_input, "season", required=False)
        if client.kind == "series":
            payload = {"name": "SeasonSearch", "seriesId": item_id, "seasonNumber": season} if season is not None else {"name": "SeriesSearch", "seriesId": item_id}
        else:
            payload = {"name": "MoviesSearch", "movieIds": [item_id]}
        await client.command(payload)
        return f"{client.name} is searching now ({payload['name']}). Check download_queue in a couple of minutes; if nothing appears, use find_releases."

    async def _remove_download(self, tool_input: dict[str, Any], approve: Approver) -> str:
        client = self._client(tool_input)
        queue_id = _int(tool_input, "queue_id")
        title = _str(tool_input, "title")
        blocklist = bool(tool_input.get("blocklist", True))
        remove_from_client = bool(tool_input.get("remove_from_client", True))
        assert queue_id is not None
        detail = f"remove {title} from the {client.name} queue" + (", delete it from the torrent client" if remove_from_client else "") + (", and blocklist it" if blocklist else "")
        if not await approve(detail):
            return "The user DENIED removing this download (or did not answer in time). Nothing changed."
        await client.delete_queue_item(queue_id, remove_from_client, blocklist)
        return f"Removed {title} from the {client.name} queue."

    async def _torrent_action(self, tool_input: dict[str, Any], approve: Approver) -> str:
        assert self._qbit is not None
        action = _str(tool_input, "action")
        torrent_hash = _str(tool_input, "hash")
        name = tool_input.get("name") or torrent_hash[:8]
        delete_files = bool(tool_input.get("delete_files", False))
        if action == "delete":
            what = f"delete torrent {name}" + (" AND its files" if delete_files else " (keep files)")
            if not await approve(what):
                return "The user DENIED deleting this torrent (or did not answer in time). Nothing changed."
        await self._qbit.action(action, torrent_hash, delete_files)
        return f"Done: {action} on {name}."

