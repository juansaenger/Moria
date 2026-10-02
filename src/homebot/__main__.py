from __future__ import annotations

import asyncio
import logging
import sys

from anthropic import AsyncAnthropic

from ha_mcp.ha_client import HomeAssistantClient, HomeAssistantError

from .agent import HomeAgent
from .automations import AutomationTools
from .config import Config, ConfigError
from .followups import FollowupStore, FollowupTools
from .discord_bot import run
from .media import MediaTools
from .selfcode import SelfCodeTools
from .server import ServerTools
from .tools import HomeTools, SafetyPolicy


def describe_capabilities(
    config: Config, media_services: list[str], server_sources: list[str] | None = None
) -> str:
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
            f"a nightly check at {config.nightly_check_time.strftime('%H:%M')} that looks over the house "
            "and stays silent when nothing needs attention"
        )
    if config.download_check_time and media_services:
        schedule.append(
            f"a download check at {config.download_check_time.strftime('%H:%M')} that looks for stalled "
            "requests, queue warnings and dead torrents, and stays silent when the pipeline is healthy"
        )
    if config.server_check_time and server_sources:
        schedule.append(
            f"a server health check at {config.server_check_time.strftime('%H:%M')} covering drives, UPS, "
            "disk space and service uptime, silent when everything is healthy"
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
    lines.append("- When several sensitive actions are needed at once, they are offered as ONE approval message listing every item, and the user can approve all, approve a selection, or deny. Ask for the whole set in one turn rather than drip-feeding them.")
    lines.append("- You can read Home Assistant automations, and create, edit or delete them. Edits show the user a YAML diff and need Approve. Always read an automation before editing it and send back the whole config, never a fragment.")
    if media_services:
        lines.append(
            "- Media stack connected: "
            + ", ".join(media_services)
            + ". You can see requests, queues and torrents, search indexers, grab releases, and remove downloads. You can also look a title up and request it through Seerr, which needs no approval; always confirm the title and year with the person first, because search returns several things with the same name."
        )
    else:
        lines.append("- No media tools are connected, so you cannot see Plex downloads or requests.")
    if server_sources:
        lines.append(
            "- Server health readable: "
            + ", ".join(server_sources)
            + ". You can report on it but cannot change the server."
        )
    lines.append(
        "- You can schedule a one-off follow-up for yourself with schedule_followup, e.g. to re-check "
        "something in 30 minutes. It survives a restart and arrives as a message from 'Follow-up'. "
        "Use it instead of promising to check back, because you have no other way to act later."
    )
    if config.repo.can_propose:
        lines.append(
            "- You can read your own source at " + config.repo.path + " and propose changes as a pull "
            "request against " + config.repo.base_branch + " on " + config.repo.repo + ". Read a file before "
            "changing it and send its complete new contents. The user is shown a diff and must approve "
            "before anything is pushed. You cannot merge and cannot deploy."
        )
        if config.repo.deploy_on_merge:
            lines.append(
                "- Once the user merges your pull request, a watcher on the server ships it by "
                "itself within about five minutes: it runs the tests, rebuilds, checks you came "
                "back up, and restores the previous build if anything fails. So tell them to merge "
                "it and that it will be live shortly. Do NOT tell them to redeploy by hand."
            )
    elif config.repo.can_read:
        lines.append("- You can read your own source but not propose changes; no GitHub token is set.")
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
        server = ServerTools(config.server) if config.server.any else None
        answered_server: list[str] = []
        if server is not None:
            answered_server = await server.check()
            logging.info("server tools on: %s", ", ".join(answered_server) or "none answered (check the URLs)")
        followups = FollowupStore(config.workspace / "followups.json")
        followup_tools = FollowupTools(followups, config.timezone)
        logging.info("follow-ups: %s pending", len(followups.all()))
        automations = AutomationTools(ha)
        selfcode = SelfCodeTools(config.repo)
        if config.repo.can_propose:
            logging.info('self-code: reading %s, can open pull requests against %s',
                         config.repo.path, config.repo.base_branch)
        elif config.repo.can_read:
            logging.info('self-code: reading %s, read only (no GITHUB_TOKEN)', config.repo.path)
        extra = [x for x in (media, server, followup_tools, automations, selfcode) if x is not None]
        tools = HomeTools(ha, policy, config.workspace / "notes.md", extra=extra)
        agent = HomeAgent(
            AsyncAnthropic(),
            tools,
            model=config.model,
            effort=config.effort,
            workspace=config.workspace,
            timezone=config.timezone,
            cache_ttl=config.cache_ttl,
            capabilities=describe_capabilities(config, answered, answered_server),
        )
        try:
            await run(
                config,
                agent,
                media_enabled=media is not None,
                server_enabled=server is not None,
                followups=followups,
            )
        finally:
            for closeable in (media, server):
                if closeable is not None:
                    await closeable.aclose()


if __name__ == "__main__":
    asyncio.run(main())
