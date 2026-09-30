from __future__ import annotations

import asyncio
import logging
import sys

from anthropic import AsyncAnthropic

from ha_mcp.ha_client import HomeAssistantClient, HomeAssistantError

from .agent import HomeAgent
from .config import Config, ConfigError
from .discord_bot import run
from .media import MediaTools
from .tools import HomeTools, SafetyPolicy


def describe_capabilities(config: Config, media_services: list[str]) -> str:
    """Facts about this deployment, so the bot can answer questions about itself."""
    lines = [
        "This is what you actually are, on this deployment:",
        "- You run in Docker on the home server, restarted automatically, and answer in one Discord channel.",
        f"- Model {config.model}"
        + (f" at {config.effort} effort." if config.effort else ".")
        + f" House time zone {config.timezone}.",
    ]
    schedule = []
    if config.nightly_check_time:
        schedule.append(
            f"a nightly check at {config.nightly_check_time.strftime('%H:%M')} that looks over the house"
            + (" and the download pipeline" if media_services else "")
            + ", and stays silent when nothing needs attention"
        )
    if config.morning_summary_time:
        schedule.append(f"a morning summary at {config.morning_summary_time.strftime('%H:%M')}, which always posts")
    if schedule:
        lines.append(
            "- YES, you run on a schedule without anyone asking: " + "; ".join(schedule) + ". "
            "These arrive as messages from 'Scheduler'. Say so plainly if asked whether you can run on a timer."
        )
    else:
        lines.append("- No scheduled runs are configured right now; you only act when someone messages you.")
    lines.append(
        "- Home Assistant: you can read any entity and act on these domains: "
        + ", ".join(sorted(config.allowed_domains))
        + "."
    )
    if config.sensitive_domains or config.sensitive_keywords:
        lines.append(
            "- Actions on "
            + ", ".join(sorted(config.sensitive_domains))
            + " or anything named like "
            + ", ".join(config.sensitive_keywords)
            + " need the user to tap Approve first. You cannot bypass that."
        )
    if media_services:
        lines.append(
            "- Media stack connected: "
            + ", ".join(media_services)
            + ". You can see requests, queues and torrents, search indexers, grab releases, and remove downloads."
        )
    else:
        lines.append("- No media tools are connected, so you cannot see Plex downloads or requests.")
    lines.append("- You remember things across chats only via the remember tool; chat history resets when idle.")
    return "\n".join(lines)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")

    policy = SafetyPolicy(config.allowed_domains, config.sensitive_domains, config.sensitive_keywords)
    async with HomeAssistantClient(config.ha_url, config.ha_token) as ha:
        try:
            ha_config = await ha.get_config()  # fail fast if HA is unreachable or the token is wrong
        except HomeAssistantError as exc:
            sys.exit(f"Home Assistant check failed: {exc}")
        logging.info("connected to Home Assistant %s (%s)", ha_config.get("version"), ha_config.get("location_name"))
        media = MediaTools.from_config(config.media) if config.media.any else None
        answered: list[str] = []
        if media is not None:
            answered = await media.check()
            logging.info("media tools on: %s", ", ".join(answered) or "none answered (check the URLs and keys)")
        tools = HomeTools(ha, policy, config.workspace / "notes.md", extra=[media] if media else [])
        agent = HomeAgent(
            AsyncAnthropic(),
            tools,
            model=config.model,
            effort=config.effort,
            workspace=config.workspace,
            timezone=config.timezone,
            capabilities=describe_capabilities(config, answered),
        )
        try:
            await run(config, agent, media_enabled=media is not None)
        finally:
            if media is not None:
                await media.aclose()


if __name__ == "__main__":
    asyncio.run(main())
