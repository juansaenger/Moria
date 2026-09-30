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
        morning_summary_time=None,
    )
    bot._routines = []
    bot._media_enabled = True
    await db.HomeBot.setup_hook(bot)
    assert sorted(t.strftime("%H:%M") for t in started) == ["09:00", "22:30"]

    # With no media stack there is nothing to check, so no 09:00 routine.
    started.clear()
    bot._routines = []
    bot._media_enabled = False
    await db.HomeBot.setup_hook(bot)
    assert [t.strftime("%H:%M") for t in started] == ["22:30"]


def test_nightly_prompt_no_longer_carries_downloads():
    from homebot import discord_bot as db

    assert "stalled_media" not in db.NIGHTLY_PROMPT
    assert "stalled_media" in db.DOWNLOADS_PROMPT
