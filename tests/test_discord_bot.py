import datetime
import types
from zoneinfo import ZoneInfo

import pytest

from homebot.discord_bot import split_message


def test_short_message_untouched():
    assert split_message("hello") == ["hello"]


def test_splits_on_newlines_under_limit():
    text = "\n".join(f"line {i}" * 20 for i in range(40))
    chunks = split_message(text, limit=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_hard_split_without_whitespace():
    chunks = split_message("x" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]


@pytest.mark.asyncio
async def test_house_and_download_checks_are_separate_routines(monkeypatch):
    """The house check stays at night; downloads get their own morning slot."""
    from homebot import discord_bot as db

    started: list[datetime.time] = []

    class FakeLoop:
        def __init__(self, when):
            self.when = when

        def start(self):
            started.append(self.when)

    monkeypatch.setattr(db.tasks, "loop", lambda time: (lambda func: FakeLoop(time)))
    tz = ZoneInfo("America/Chicago")
    bot = db.HomeBot.__new__(db.HomeBot)
    bot.config = types.SimpleNamespace(
        nightly_check_time=datetime.time(22, 30, tzinfo=tz),
        download_check_time=datetime.time(9, 0, tzinfo=tz),
        server_check_time=datetime.time(8, 45, tzinfo=tz),
        morning_summary_time=None,
    )
    bot._routines = []
    bot._media_enabled = True
    bot._server_enabled = True
    bot._followups = None
    await db.HomeBot.setup_hook(bot)
    assert sorted(x.strftime("%H:%M") for x in started) == ["08:45", "09:00", "22:30"]

    # Each optional stack drops only its own routine.
    started.clear()
    bot._routines = []
    bot._media_enabled = False
    await db.HomeBot.setup_hook(bot)
    assert sorted(x.strftime("%H:%M") for x in started) == ["08:45", "22:30"]

    started.clear()
    bot._routines = []
    bot._server_enabled = False
    await db.HomeBot.setup_hook(bot)
    assert [x.strftime("%H:%M") for x in started] == ["22:30"]


def test_each_routine_prompt_targets_its_own_tool():
    from homebot import discord_bot as db

    assert "stalled_media" not in db.NIGHTLY_PROMPT
    assert "stalled_media" in db.DOWNLOADS_PROMPT
    assert "server_health" in db.SERVER_PROMPT
    assert "server_health" not in db.NIGHTLY_PROMPT


@pytest.mark.asyncio
async def test_followup_ticker_only_starts_when_there_is_a_store(monkeypatch, tmp_path):
    from homebot import discord_bot as db
    from homebot.followups import FollowupStore

    seconds: list[float] = []

    class FakeLoop:
        def start(self):
            pass

    def fake_loop(time=None, seconds=None, **kw):
        if seconds is not None:
            globals().setdefault("_seen", []).append(seconds)
        return lambda func: FakeLoop()

    started: list[object] = []
    monkeypatch.setattr(db.tasks, "loop", lambda **kw: (started.append(kw) or (lambda f: FakeLoop())))
    bot = db.HomeBot.__new__(db.HomeBot)
    bot.config = types.SimpleNamespace(
        nightly_check_time=None, download_check_time=None, server_check_time=None, morning_summary_time=None
    )
    bot._routines = []
    bot._media_enabled = False
    bot._server_enabled = False

    bot._followups = None
    await db.HomeBot.setup_hook(bot)
    assert started == []

    bot._routines = []
    bot._followups = FollowupStore(tmp_path / "f.json")
    await db.HomeBot.setup_hook(bot)
    assert started == [{"seconds": db.FOLLOWUP_POLL_SECONDS}]
