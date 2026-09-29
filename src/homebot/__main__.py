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
        )
        try:
            await run(config, agent, media_enabled=media is not None)
        finally:
            if media is not None:
                await media.aclose()


if __name__ == "__main__":
    asyncio.run(main())
