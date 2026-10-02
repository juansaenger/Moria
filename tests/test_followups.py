from __future__ import annotations

import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from homebot.followups import (
    FollowupError,
    FollowupStore,
    FollowupTools,
    parse_when,
)

TZ = ZoneInfo("America/Chicago")
NOW = datetime.datetime(2026, 10, 1, 14, 0, tzinfo=TZ)


async def approve(_summary: str) -> bool:
    return True


# ---- when to fire


def test_delay_in_minutes():
    assert parse_when(30, None, TZ, NOW) == NOW + datetime.timedelta(minutes=30)


def test_clock_time_later_today():
    assert parse_when(None, "16:30", TZ, NOW).hour == 16


def test_clock_time_already_passed_means_tomorrow():
    due = parse_when(None, "09:00", TZ, NOW)
    assert due.day == NOW.day + 1 and due.hour == 9


def test_explicit_date():
    due = parse_when(None, "2026-10-05 07:15", TZ, NOW)
    assert (due.month, due.day, due.hour) == (10, 5, 7)
    assert due.tzinfo is not None  # never naive, or the comparison at fire time breaks


def test_rejects_nothing_at_all():
    with pytest.raises(FollowupError, match="either"):
        parse_when(None, None, TZ, NOW)


def test_rejects_too_soon_and_too_far():
    with pytest.raises(FollowupError, match="soonest"):
        parse_when(0.1, None, TZ, NOW)
    with pytest.raises(FollowupError, match="30 days"):
        parse_when(60 * 24 * 40, None, TZ, NOW)


def test_rejects_unparseable_time():
    with pytest.raises(FollowupError, match="Could not read"):
        parse_when(None, "half past tea", TZ, NOW)


# ---- the store


def test_add_list_cancel(tmp_path):
    store = FollowupStore(tmp_path / "f.json")
    item = store.add(NOW + datetime.timedelta(minutes=5), "check the torrent", "Juan")
    assert [i.id for i in store.all()] == [item.id]
    assert store.cancel(item.id).message == "check the torrent"
    assert store.all() == []
    assert store.cancel("nope") is None


def test_survives_a_restart(tmp_path):
    path = tmp_path / "f.json"
    first = FollowupStore(path)
    first.add(NOW + datetime.timedelta(hours=2), "look again", "Juan")
    # A new process, as after a container rebuild.
    second = FollowupStore(path)
    assert [i.message for i in second.all()] == ["look again"]


def test_only_due_items_pop(tmp_path):
    store = FollowupStore(tmp_path / "f.json")
    soon = store.add(NOW - datetime.timedelta(minutes=1), "overdue", "Juan")
    store.add(NOW + datetime.timedelta(days=1), "later", "Juan")
    popped = store.pop_due(datetime.datetime.now(datetime.timezone.utc))
    assert [i.id for i in popped] == [soon.id]
    # Popping removes it for good, so it cannot fire twice.
    assert [i.message for i in store.all()] == ["later"]
    assert [i.message for i in FollowupStore(tmp_path / "f.json").all()] == ["later"]


def test_corrupt_file_does_not_crash_the_bot(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert FollowupStore(path).all() == []


def test_malformed_entry_is_dropped_and_the_rest_kept(tmp_path):
    path = tmp_path / "f.json"
    good = {"id": "ab", "due": "2030-01-01T00:00:00+00:00", "message": "ok", "asked_by": "Juan", "created": "x"}
    path.write_text(json.dumps([good, {"id": "bad"}, {"id": "cd", "due": "nonsense", "message": "m",
                                                      "asked_by": "x", "created": "y"}]), encoding="utf-8")
    assert [i.id for i in FollowupStore(path).all()] == ["ab"]


def test_pending_limit(tmp_path):
    store = FollowupStore(tmp_path / "f.json")
    for _ in range(50):
        store.add(NOW + datetime.timedelta(hours=1), "x", "Juan")
    with pytest.raises(FollowupError, match="already"):
        store.add(NOW + datetime.timedelta(hours=1), "one too many", "Juan")


# ---- the tools


@pytest.mark.asyncio
async def test_schedule_and_list_and_cancel_through_tools(tmp_path):
    tools = FollowupTools(FollowupStore(tmp_path / "f.json"), TZ)
    out = await tools.run("schedule_followup", {"delay_minutes": 45, "message": "recheck", "_sender": "Juan"}, approve)
    assert "saved for" in out
    listed = await tools.run("list_followups", {}, approve)
    assert "recheck" in listed and "Juan" in listed
    fid = listed.split(" | ")[0].strip()
    assert "Cancelled" in await tools.run("cancel_followup", {"id": fid}, approve)
    assert "No follow-ups pending" in await tools.run("list_followups", {}, approve)


@pytest.mark.asyncio
async def test_cancelling_a_missing_id_is_an_error(tmp_path):
    tools = FollowupTools(FollowupStore(tmp_path / "f.json"), TZ)
    with pytest.raises(FollowupError, match="No pending follow-up"):
        await tools.run("cancel_followup", {"id": "zz"}, approve)


@pytest.mark.asyncio
async def test_message_is_required(tmp_path):
    tools = FollowupTools(FollowupStore(tmp_path / "f.json"), TZ)
    with pytest.raises(FollowupError, match="message is required"):
        await tools.run("schedule_followup", {"delay_minutes": 45}, approve)


def test_tool_surface(tmp_path):
    tools = FollowupTools(FollowupStore(tmp_path / "f.json"), TZ)
    assert tools.names == {"schedule_followup", "list_followups", "cancel_followup"}
