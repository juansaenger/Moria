from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import tasks

from .agent import Conversation, HomeAgent
from .config import Config
from .followups import FollowupStore

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!new"}
NOTHING_TO_REPORT = "NOTHING_TO_REPORT"
FOLLOWUP_POLL_SECONDS = 30
# Discord select menus cap at 25 options.
MAX_PICKABLE = 25
# Image types Claude accepts, and limits that keep requests reasonable.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FOLLOWUP_PREFIX = (
    "This is a follow-up you scheduled earlier, firing now. Nobody is asking; you asked yourself. "
    "Do it, then say what you found in a line or two. Your instruction to yourself was: "
)

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

SERVER_PROMPT = f"""\
Scheduled server health check. Call server_health once. It returns only what needs \
attention: failing or hot drives, UPS problems, filesystems running out of room, and \
monitored services that are down. Explain each one in plain words, say how urgent it is, \
and say what you would do about it. A drive with a SMART failure is the one thing worth \
being blunt about. Do not change anything. If nothing needs attention, reply with exactly \
{NOTHING_TO_REPORT}."""

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


async def read_images(attachments: list[discord.Attachment]) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Download supported image attachments. Returns (images, names of skipped files)."""
    images: list[tuple[str, bytes]] = []
    skipped: list[str] = []
    for att in attachments:
        media_type = (att.content_type or "").split(";")[0].strip().lower()
        if media_type not in IMAGE_TYPES:
            skipped.append(f"{att.filename} (not a supported image)")
            continue
        if att.size > MAX_IMAGE_BYTES:
            skipped.append(f"{att.filename} (over 5 MB)")
            continue
        if len(images) >= MAX_IMAGES:
            skipped.append(f"{att.filename} (more than {MAX_IMAGES} images)")
            continue
        try:
            images.append((media_type, await att.read()))
        except Exception:
            log.exception("could not download attachment %s", att.filename)
            skipped.append(f"{att.filename} (download failed)")
    return images, skipped


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


class _ItemPicker(discord.ui.Select):
    """Optional narrowing: tick only the items you want to go ahead."""

    def __init__(self, count: int) -> None:
        super().__init__(
            placeholder="Optional: pick just some of them",
            min_values=0,
            max_values=count,
            options=[discord.SelectOption(label=str(i + 1), value=str(i)) for i in range(count)],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: BatchApprovalView = self.view  # type: ignore[assignment]
        if interaction.user.id not in view.allowed:
            await interaction.response.send_message("You can't approve actions for this bot.", ephemeral=True)
            return
        view.chosen = {int(v) for v in self.values}
        await interaction.response.defer()


class BatchApprovalView(discord.ui.View):
    """One message covering several sensitive actions: all, some, or none.

    Claude often asks for a dozen deletes in one turn. Asking per item buries
    the user and hides the shape of what is about to happen.
    """

    def __init__(self, allowed_user_ids: frozenset[int], timeout: float, count: int) -> None:
        super().__init__(timeout=timeout)
        self.allowed = allowed_user_ids
        self.count = count
        self.chosen: set[int] = set()
        self.result: asyncio.Future[list[bool]] = asyncio.get_running_loop().create_future()
        if 1 < count <= MAX_PICKABLE:
            self.add_item(_ItemPicker(count))

    def _settle(self, verdicts: list[bool]) -> None:
        if not self.result.done():
            self.result.set_result(verdicts)
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()

    async def _may_decide(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.allowed:
            await interaction.response.send_message("You can't approve actions for this bot.", ephemeral=True)
            return False
        if self.result.done():
            await interaction.response.defer()
            return False
        return True

    async def _close(self, interaction: discord.Interaction, verdict: str) -> None:
        verdict_line = f"**{verdict}** by {interaction.user.display_name}"
        content = "\n".join([interaction.message.content, verdict_line])
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="Approve all", style=discord.ButtonStyle.success, row=1)
    async def approve_all(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._may_decide(interaction):
            return
        self._settle([True] * self.count)
        await self._close(interaction, f"All {self.count} approved")

    @discord.ui.button(label="Approve selected", style=discord.ButtonStyle.primary, row=1)
    async def approve_selected(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._may_decide(interaction):
            return
        if not self.chosen:
            await interaction.response.send_message("Pick some items first, or use Approve all.", ephemeral=True)
            return
        self._settle([i in self.chosen for i in range(self.count)])
        await self._close(interaction, f"{len(self.chosen)} of {self.count} approved")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, row=1)
    async def deny(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._may_decide(interaction):
            return
        self._settle([False] * self.count)
        await self._close(interaction, "Denied")

    async def on_timeout(self) -> None:
        if not self.result.done():
            self.result.set_result([False] * self.count)


class _ChannelApprover:
    """Asks the user to approve, one action at a time or a whole round at once."""

    def __init__(self, channel: discord.abc.Messageable, allowed: frozenset[int], timeout: float) -> None:
        self._channel = channel
        self._allowed = allowed
        self._timeout = timeout

    @property
    def _expires(self) -> str:
        return f"(expires in {int(self._timeout // 60)} min)"

    async def __call__(self, summary: str) -> bool:
        view = ApprovalView(self._allowed, self._timeout)
        await self._channel.send(f"**Approval needed:** {summary}\n{self._expires}", view=view)
        return await view.result

    async def many(self, summaries: list[str]) -> list[bool]:
        view = BatchApprovalView(self._allowed, self._timeout, len(summaries))
        listed = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(summaries))
        body = f"**Approval needed for {len(summaries)} actions:**\n{listed}\n{self._expires}"
        chunks = split_message(body)
        for chunk in chunks[:-1]:
            await self._channel.send(chunk)
        await self._channel.send(chunks[-1], view=view)
        return await view.result


class HomeBot(discord.Client):
    def __init__(
        self,
        config: Config,
        agent: HomeAgent,
        media_enabled: bool = False,
        server_enabled: bool = False,
        followups: FollowupStore | None = None,
    ) -> None:
        self._followups = followups
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.agent = agent
        self._conversation: Conversation | None = None
        self._lock = asyncio.Lock()  # one request at a time, in order
        self._routines: list[tasks.Loop[Any]] = []
        self._media_enabled = media_enabled
        self._server_enabled = server_enabled

    async def setup_hook(self) -> None:
        # The house check belongs at night; the download check belongs in the
        # morning, when there is time to act on what it finds.
        downloads = self.config.download_check_time if self._media_enabled else None
        health = self.config.server_check_time if self._server_enabled else None
        for when, prompt, silent_ok in (
            (self.config.nightly_check_time, NIGHTLY_PROMPT, True),
            (health, SERVER_PROMPT, True),
            (downloads, DOWNLOADS_PROMPT, True),
            (self.config.morning_summary_time, MORNING_PROMPT, False),
        ):
            if when is None:
                continue
            loop = tasks.loop(time=when)(self._make_routine(prompt, silent_ok))
            loop.start()
            self._routines.append(loop)
            log.info("routine scheduled at %s", when.strftime("%H:%M %Z"))
        if self._followups is not None:
            ticker = tasks.loop(seconds=FOLLOWUP_POLL_SECONDS)(self._deliver_followups)
            ticker.start()
            self._routines.append(ticker)
            pending = len(self._followups.all())
            log.info("follow-ups on, %s pending", pending)

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

    def _approver(self, channel: discord.abc.Messageable) -> "_ChannelApprover":
        return _ChannelApprover(channel, self.config.allowed_user_ids, self.config.approval_timeout_s)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id != self.config.channel_id:
            return
        if message.author.id not in self.config.allowed_user_ids:
            return
        text = message.content.strip()
        if text.lower() in RESET_COMMANDS:
            self._conversation = None
            await message.reply("Starting fresh.", mention_author=False)
            return

        images, skipped = await read_images(list(message.attachments))
        if not text and not images:
            return
        if skipped:
            note = "(Attachments not shown to you: " + ", ".join(skipped) + ")"
            text = f"{text}\n{note}" if text else note
        if images and not text:
            text = "(sent an image with no text)"

        async with self._lock:
            convo = self._current_conversation()
            try:
                async with message.channel.typing():
                    reply = await self.agent.ask(
                        convo,
                        message.author.display_name,
                        text,
                        self._approver(message.channel),
                        images=images or None,
                    )
            except Exception:
                log.exception("request failed")
                self._conversation = None
                reply = "Something went wrong talking to Claude or Home Assistant. Check the logs; I've reset the chat."
        for chunk in split_message(reply):
            await message.channel.send(chunk)

    async def _deliver_followups(self) -> None:
        assert self._followups is not None
        due = self._followups.pop_due()
        if not due:
            return
        channel = self._channel()
        if channel is None:
            log.error("follow-up skipped: channel %s not found", self.config.channel_id)
            return
        for item in due:
            log.info("delivering follow-up %s: %s", item.id, item.message)
            async with self._lock:
                convo = self.agent.new_conversation()
                try:
                    reply = await self.agent.ask(
                        convo, "Follow-up", FOLLOWUP_PREFIX + item.message, self._approver(channel)
                    )
                except Exception:
                    log.exception("follow-up %s failed", item.id)
                    reply = f"A follow-up failed: {item.message}"
                else:
                    # Let the user reply to it with context, like a routine.
                    self._conversation = convo
            for chunk in split_message(reply):
                await channel.send(chunk)

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


async def run(
    config: Config,
    agent: HomeAgent,
    media_enabled: bool = False,
    server_enabled: bool = False,
    followups: FollowupStore | None = None,
) -> None:
    bot = HomeBot(config, agent, media_enabled, server_enabled, followups)
    async with bot:
        await bot.start(config.discord_token)
