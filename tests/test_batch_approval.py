from __future__ import annotations

from typing import Any

import httpx
import pytest

from homebot.agent import _settled
from homebot.media import ArrClient, MediaTools, QbitClient

from .test_media import QUEUE_ITEM, Approver, FakeServers, servers  # noqa: F401


class Block:
    """Stands in for an Anthropic tool_use content block."""

    def __init__(self, block_id: str, name: str, payload: dict[str, Any]) -> None:
        self.id = block_id
        self.name = name
        self.input = payload


class FakeTools:
    """Minimal ToolSet: previews whatever the test says, records what ran."""

    def __init__(self, previews: dict[str, str | None]) -> None:
        self._previews = previews
        self.ran: list[tuple[str, bool]] = []

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return []

    async def preview(self, name: str, tool_input: dict[str, Any]) -> str | None:
        return self._previews.get(name)

    async def run(self, name: str, tool_input: dict[str, Any], approve) -> str:
        verdict = await approve(f"do {name}")
        self.ran.append((name, verdict))
        return "ok" if verdict else "denied"


class BatchRecorder:
    """An approver that supports batching, like the Discord one."""

    def __init__(self, verdicts: list[bool]) -> None:
        self._verdicts = verdicts
        self.single_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    async def __call__(self, summary: str) -> bool:
        self.single_calls.append(summary)
        return True

    async def many(self, summaries: list[str]) -> list[bool]:
        self.batch_calls.append(summaries)
        return self._verdicts


class SingleOnly:
    """An approver with no batch support, like an older caller."""

    def __init__(self) -> None:
        self.single_calls: list[str] = []

    async def __call__(self, summary: str) -> bool:
        self.single_calls.append(summary)
        return True


def _agent(tools):
    from homebot.agent import HomeAgent
    from zoneinfo import ZoneInfo

    return HomeAgent(
        client=None, tools=tools, model="claude-opus-5", effort=None,
        workspace=__import__("pathlib").Path("."), timezone=ZoneInfo("UTC"),
    )


@pytest.mark.asyncio
async def test_several_approvals_become_one_question():
    tools = FakeTools({"a": "delete thing 1", "b": "delete thing 2", "c": "delete thing 3"})
    approver = BatchRecorder([True, True, True])
    blocks = [Block("1", "a", {}), Block("2", "b", {}), Block("3", "c", {})]
    decisions = await _agent(tools)._pre_approve(blocks, approver)
    assert approver.batch_calls == [["delete thing 1", "delete thing 2", "delete thing 3"]]
    assert approver.single_calls == []
    assert decisions == {"1": True, "2": True, "3": True}


@pytest.mark.asyncio
async def test_a_single_approval_is_not_batched():
    tools = FakeTools({"a": "delete thing 1"})
    approver = BatchRecorder([True])
    decisions = await _agent(tools)._pre_approve([Block("1", "a", {})], approver)
    assert approver.batch_calls == []
    assert decisions == {}  # falls through to the tool's own prompt


@pytest.mark.asyncio
async def test_calls_needing_no_approval_are_not_offered():
    tools = FakeTools({"a": "delete thing 1", "b": None, "c": "delete thing 3"})
    approver = BatchRecorder([True, True])
    blocks = [Block("1", "a", {}), Block("2", "b", {}), Block("3", "c", {})]
    decisions = await _agent(tools)._pre_approve(blocks, approver)
    assert approver.batch_calls == [["delete thing 1", "delete thing 3"]]
    assert set(decisions) == {"1", "3"}


@pytest.mark.asyncio
async def test_partial_approval_maps_to_the_right_items():
    tools = FakeTools({"a": "one", "b": "two", "c": "three"})
    approver = BatchRecorder([True, False, True])
    blocks = [Block("1", "a", {}), Block("2", "b", {}), Block("3", "c", {})]
    decisions = await _agent(tools)._pre_approve(blocks, approver)
    assert decisions == {"1": True, "2": False, "3": True}


@pytest.mark.asyncio
async def test_approver_without_batching_still_works():
    tools = FakeTools({"a": "one", "b": "two"})
    approver = SingleOnly()
    decisions = await _agent(tools)._pre_approve([Block("1", "a", {}), Block("2", "b", {})], approver)
    assert decisions == {}  # each tool asks for itself, as before


@pytest.mark.asyncio
async def test_a_broken_preview_does_not_block_the_round():
    class Exploding(FakeTools):
        async def preview(self, name, tool_input):
            raise RuntimeError("boom")

    tools = Exploding({})
    approver = BatchRecorder([True])
    decisions = await _agent(tools)._pre_approve([Block("1", "a", {}), Block("2", "b", {})], approver)
    assert decisions == {}


@pytest.mark.asyncio
async def test_a_settled_decision_is_not_asked_again():
    asked: list[str] = []

    async def approve(summary: str) -> bool:
        asked.append(summary)
        return True

    assert await _settled(approve, False)("anything") is False
    assert await _settled(approve, True)("anything") is True
    assert asked == []  # neither reached the user
    assert await _settled(approve, None)("fresh") is True
    assert asked == ["fresh"]


# ---- previews must describe exactly what the tool would do


@pytest.mark.asyncio
async def test_media_preview_matches_the_real_prompt(servers):  # noqa: F811
    tools = MediaTools(
        sonarr=ArrClient("Sonarr", "http://sonarr", "k"),
        qbit=QbitClient("http://qbit", "admin", "pw"),
    )
    payload = {"kind": "series", "queue_id": 55, "title": "Old Show", "blocklist": True}
    preview = await tools.preview("remove_download", payload)
    assert preview is not None and "Old Show" in preview and "blocklist" in preview

    recorder = Approver(False)
    await tools.run("remove_download", payload, recorder)
    assert recorder.asked == [preview]  # the user sees the same sentence either way


@pytest.mark.asyncio
async def test_delete_torrent_previews_but_pause_does_not(servers):  # noqa: F811
    tools = MediaTools(qbit=QbitClient("http://qbit", "admin", "pw"))
    assert await tools.preview("torrent_action", {"action": "pause", "hash": "abc123"}) is None
    preview = await tools.preview("torrent_action", {"action": "delete", "hash": "abc123", "name": "Thing"})
    assert preview is not None and "delete torrent Thing" in preview

    recorder = Approver(False)
    await tools.run("torrent_action", {"action": "delete", "hash": "abc123", "name": "Thing"}, recorder)
    assert recorder.asked == [preview]


@pytest.mark.asyncio
async def test_a_big_grab_previews_and_a_small_one_does_not(servers):  # noqa: F811
    tools = MediaTools(sonarr=ArrClient("Sonarr", "http://sonarr", "k"))
    small = {"kind": "series", "guid": "g", "indexer_id": 2, "title": "Small", "size_gb": 2}
    big = {"kind": "series", "guid": "g", "indexer_id": 2, "title": "Huge Pack", "size_gb": 120}
    assert await tools.preview("grab_release", small) is None
    assert "Huge Pack" in (await tools.preview("grab_release", big) or "")
