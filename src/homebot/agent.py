from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .tools import Approver, HomeTools, ToolError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15
# Models that accept the server-side refusal fallback ("fallbacks": "default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

BASE_INSTRUCTIONS = """\
You are a home assistant bot that people message on Discord. You control the house through \
Home Assistant using your tools. If media tools are present you also look after the Plex library: \
Seerr takes requests, Sonarr (series) and Radarr (movies) find and import downloads, and \
qBittorrent downloads them.

- Find entities with list_entities before acting; never guess entity ids.
- Do what was asked, then reply in one or two short sentences saying what you did. Discord \
formatting is fine, but keep it brief.
- If a request is ambiguous (several matching rooms or devices), ask instead of guessing.
- If an action was denied, say so plainly and do not try to work around it.
- For "where is my show/movie": check media_requests, then download_queue, then the library. \
Common fixes, cheapest first: search_missing; then find_releases and grab_release (older shows \
often only exist as a whole-series pack; grab it with force=true if Sonarr rejected it only for \
being a full-series release); for a torrent stuck a day with no seeds, remove_download and search \
again. Explain in plain words, not app jargon.
- Each message starts with the sender's name and the local time in brackets. A message from \
"Scheduler" is one of your own scheduled runs firing, not a person typing.
- Answer questions about yourself from the <capabilities> block below, never from general \
assumptions about chatbots. You do run on a schedule when one is listed there."""


@dataclass
class Conversation:
    """One running chat with Claude. History is append-only; start a new one to reset."""

    system: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)

    def is_stale(self, idle_minutes: float) -> bool:
        return time.monotonic() - self.last_active > idle_minutes * 60


def build_system_prompt(workspace: Path, capabilities: str = "") -> str:
    parts = [BASE_INSTRUCTIONS]
    if capabilities.strip():
        parts.append(f"<capabilities>\n{capabilities.strip()}\n</capabilities>")
    role = workspace / "role.md"
    notes = workspace / "notes.md"
    if role.exists():
        parts.append(f"<role>\n{role.read_text(encoding='utf-8').strip()}\n</role>")
    if notes.exists() and notes.read_text(encoding="utf-8").strip():
        parts.append(f"<notes>\n{notes.read_text(encoding='utf-8').strip()}\n</notes>")
    return "\n\n".join(parts)


class HomeAgent:
    def __init__(
        self,
        client: AsyncAnthropic,
        tools: HomeTools,
        *,
        model: str,
        effort: str | None,
        workspace: Path,
        timezone: ZoneInfo,
        capabilities: str = "",
    ) -> None:
        self._client = client
        self._tools = tools
        self._model = model
        self._effort = effort
        self._workspace = workspace
        self._tz = timezone
        self._capabilities = capabilities

    def new_conversation(self) -> Conversation:
        # The system prompt is frozen per conversation so notes saved mid-chat
        # don't rewrite history (which would break caching and thinking replay).
        return Conversation(system=build_system_prompt(self._workspace, self._capabilities))

    async def ask(self, convo: Conversation, sender: str, text: str, approve: Approver) -> str:
        now = datetime.datetime.now(self._tz).strftime("%a %Y-%m-%d %H:%M")
        convo.messages.append({"role": "user", "content": f"[{sender}, {now}] {text}"})
        convo.last_active = time.monotonic()

        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._create(convo)
            if response.stop_reason == "refusal":
                # Drop the whole exchange so the next message starts clean.
                convo.messages.clear()
                return "I can't help with that one."
            if response.stop_reason == "max_tokens":
                convo.messages.clear()  # a cut-off turn can't be continued cleanly
                return _text_of(response) or "That answer ran too long. Try a narrower question."
            convo.messages.append({"role": "assistant", "content": response.content})

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return _text_of(response) or "Done."

            results = []
            for block in tool_uses:
                results.append(await self._run_tool(block, approve))
            convo.messages.append({"role": "user", "content": results})
            convo.last_active = time.monotonic()

        return "I stopped after too many steps. Try asking in a simpler way."

    async def _create(self, convo: Conversation) -> Any:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 16000,
            "system": convo.system,
            "tools": self._tools.definitions,
            "messages": convo.messages,
            "cache_control": {"type": "ephemeral"},
        }
        if self._effort and not self._model.startswith("claude-haiku"):
            params["output_config"] = {"effort": self._effort}
        if self._model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return await self._client.beta.messages.create(**params)

    async def _run_tool(self, block: Any, approve: Approver) -> dict[str, Any]:
        log.info("tool %s %s", block.name, block.input)
        try:
            output = await self._tools.run(block.name, dict(block.input or {}), approve)
            return {"type": "tool_result", "tool_use_id": block.id, "content": output}
        except ToolError as exc:
            log.warning("tool %s failed: %s", block.name, exc)
            message = str(exc)
        except Exception:
            log.exception("tool %s crashed", block.name)
            message = "The tool failed unexpectedly."
        return {"type": "tool_result", "tool_use_id": block.id, "content": message, "is_error": True}


def _text_of(response: Any) -> str:
    return "\n".join(block.text for block in response.content if block.type == "text").strip()
