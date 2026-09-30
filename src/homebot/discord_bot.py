from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import tasks

from .agent import Conversation, HomeAgent
from .config import Config

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!new"}
NOTHING_TO_REPORT = "NOTHING_TO_REPORT"

NIGHTLY_PROMPT = f"""\
Scheduled nightly check. Look for anything left on or open that shouldn't be at night: \
lights on, doors or windows open, locks unlocked, garage open, alarm not armed, media playing, \
unusual temperatures. Do not change anything. If something needs attention, list it briefly \
and offer to fix it. If everything looks fine, reply with exactly {NOTHING_TO_REPORT}."""

DOWNLOADS_PROMPT = f"""\
Scheduled download check. Call stalled_media once: it returns the quiet requests, the queue \
warnings and the stuck torrents together, each with a hint at the cause. Only dig further with \
other tools if a line is unclear. For each problem, say in one plain sentence what is wrong and \
what you would do (search again, grab a whole-series pack, remove and re-search), naming the \
person who asked when the line has one. Do not grab or remove anything by yourself; offer, and \
wait to be told. If the media side is fine too, the whole reply is still exactly {NOTHING_TO_REPORT}."""

MORNING_PROMPT = """\
Scheduled morning summary. Give a short rundown of the house: indoor and outdoor temperature, \
anything left on or open overnight, and anything unusual. Do not change anything. Keep it to a \
few lines."""


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks, preferring line breaks."""
    chunks = []
    text = text.strip() or "(no reply)"
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


class ApprovalView(discord.ui.View):
    def __init__(self, allowed_user_ids: frozenset[int], timeout: float) -> None:
        super().__init__(timeout=timeout)
        self._allowed = allowed_user_ids
        self.result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def _decide(self, interaction: discord.Interaction, approved: bool) -> None:
        if interaction.user.id not in self._allowed:
            await interaction.response.send_message("You can't approve actions for this bot.", ephemeral=True)
            return
        if self.result.done():
            await interaction.response.defer()
            return
        self.result.set_result(approved)
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        verdict = "Approved" if approved else "Denied"
        content = f"{interaction.message.content}\n**{verdict}** by {interaction.user.display_name}"
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._decide(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._decide(interaction, False)

    async def on_timeout(self) -> None:
        if not self.result.done():
            self.result.set_result(False)


class HomeBot(discord.Client):
    def __init__(self, config: Config, agent: HomeAgent, media_enabled: bool = False) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.agent = agent
        self._conversation: Conversation | None = None
        self._lock = asyncio.Lock()  # one request at a time, in order
        self._routines: list[tasks.Loop[Any]] = []
        self._media_enabled = media_enabled

    async def setup_hook(self) -> None:
        # The house check belongs at night; the download check belongs in the
        # morning, when there is time to act on what it finds.
        downloads = self.config.download_check_time if self._media_enabled else None
        for when, prompt, silent_ok in (
            (self.config.nightly_check_time, NIGHTLY_PROMPT, True),
            (downloads, DOWNLOADS_PROMPT, True),
            (self.config.morning_summary_time, MORNING_PROMPT, False),
        ):
            if when is None:
                continue
            loop = tasks.loop(time=when)(self._make_routine(prompt, silent_ok))
            loop.start()
            self._routines.append(loop)
            log.info("routine scheduled at %s", when.strftime("%H:%M %Z"))

    async def on_ready(self) -> None:
        log.info("logged in as %s; listening in channel %s", self.user, self.config.channel_id)

    def _channel(self) -> discord.abc.Messageable | None:
        channel = self.get_channel(self.config.channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def _current_conversation(self) -> Conversation:
        convo = self._conversation
        if convo is None or not convo.messages or convo.is_stale(self.config.idle_reset_minutes):
            convo = self.agent.new_conversation()
            self._conversation = convo
        return convo

    def _approver(self, channel: discord.abc.Messageable):
        async def approve(summary: str) -> bool:
            view = ApprovalView(self.config.allowed_user_ids, self.config.approval_timeout_s)
            minutes = int(self.config.approval_timeout_s // 60)
            await channel.send(f"**Approval needed:** {summary}\n(expires in {minutes} min)", view=view)
            return await view.result

        return approve

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id != self.config.channel_id:
            return
        if message.author.id not in self.config.allowed_user_ids:
            return
        text = message.content.strip()
        if not text:
            return
        if text.lower() in RESET_COMMANDS:
            self._conversation = None
            await message.reply("Starting fresh.", mention_author=False)
            return

        async with self._lock:
            convo = self._current_conversation()
            try:
                async with message.channel.typing():
                    reply = await self.agent.ask(
                        convo, message.author.display_name, text, self._approver(message.channel)
                    )
            except Exception:
                log.exception("request failed")
                self._conversation = None
                reply = "Something went wrong talking to Claude or Home Assistant. Check the logs; I've reset the chat."
        for chunk in split_message(reply):
            await message.channel.send(chunk)

    def _make_routine(self, prompt: str, silent_ok: bool):
        async def routine() -> None:
            channel = self._channel()
            if channel is None:
                log.error("routine skipped: channel %s not found", self.config.channel_id)
                return
            async with self._lock:
                convo = self.agent.new_conversation()
                try:
                    reply = await self.agent.ask(convo, "Scheduler", prompt, self._approver(channel))
                except Exception:
                    log.exception("routine failed")
                    return
                if silent_ok and NOTHING_TO_REPORT in reply:
                    log.info("routine: nothing to report")
                    return
                # Continue from the routine's conversation so a reply like "lock it" has context.
                self._conversation = convo
            for chunk in split_message(reply):
                await channel.send(chunk)

        return routine


async def run(config: Config, agent: HomeAgent, media_enabled: bool = False) -> None:
    bot = HomeBot(config, agent, media_enabled)
    async with bot:
        await bot.start(config.discord_token)
