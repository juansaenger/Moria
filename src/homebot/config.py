from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .media import MediaConfig


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Add it to homebot.env.")
    return value


def _opt(name: str) -> str:
    return os.environ.get(name, "").strip()


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _time(name: str, default: str, tz: ZoneInfo) -> datetime.time | None:
    raw = os.environ.get(name, default).strip()
    if not raw or raw.lower() == "off":
        return None
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        return datetime.time(hour, minute, tzinfo=tz)
    except ValueError as exc:
        raise ConfigError(f"{name} must look like 22:30 or be 'off', got {raw!r}") from exc


# Domains the bot may call services on. Anything else (homeassistant.restart,
# shell_command, hassio, recorder, ...) is refused outright.
DEFAULT_ALLOWED_DOMAINS = (
    "light,switch,fan,climate,cover,lock,alarm_control_panel,media_player,scene,script,"
    "vacuum,todo,input_boolean,input_number,input_select,button,number,select,humidifier,"
    "water_heater,valve,siren,notify,timer"
)
# Service calls on these domains always need a tap on Approve.
DEFAULT_SENSITIVE_DOMAINS = "lock,alarm_control_panel"
# Service calls on entities whose id or name contains one of these words need approval too.
DEFAULT_SENSITIVE_KEYWORDS = "lock,garage,alarm,oven,stove,door,gate,security"


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    allowed_user_ids: frozenset[int]
    ha_url: str
    ha_token: str
    model: str
    effort: str | None
    timezone: ZoneInfo
    workspace: Path
    allowed_domains: frozenset[str]
    sensitive_domains: frozenset[str]
    sensitive_keywords: tuple[str, ...]
    nightly_check_time: datetime.time | None
    morning_summary_time: datetime.time | None
    media: MediaConfig = MediaConfig()
    approval_timeout_s: float = 300.0
    idle_reset_minutes: float = 30.0

    @classmethod
    def from_env(cls) -> "Config":
        tz = ZoneInfo(os.environ.get("TZ", "UTC") or "UTC")
        try:
            channel_id = int(_required("DISCORD_CHANNEL_ID"))
            user_ids = frozenset(int(uid) for uid in _csv("DISCORD_ALLOWED_USER_IDS"))
        except ValueError as exc:
            raise ConfigError("DISCORD_CHANNEL_ID and DISCORD_ALLOWED_USER_IDS must be numeric Discord IDs") from exc
        if not user_ids:
            raise ConfigError("DISCORD_ALLOWED_USER_IDS is empty, so nobody could use the bot.")
        _required("ANTHROPIC_API_KEY")  # read by the Anthropic client itself
        effort = os.environ.get("CLAUDE_EFFORT", "low").strip().lower() or None
        if effort not in (None, "low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"CLAUDE_EFFORT must be low, medium, high, xhigh or max, got {effort!r}")
        return cls(
            discord_token=_required("DISCORD_BOT_TOKEN"),
            channel_id=channel_id,
            allowed_user_ids=user_ids,
            ha_url=_required("HOMEASSISTANT_URL"),
            ha_token=_required("HOMEASSISTANT_TOKEN"),
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip(),
            effort=effort,
            timezone=tz,
            workspace=Path(os.environ.get("HOMEBOT_WORKSPACE", "workspace")),
            allowed_domains=frozenset(_csv("ALLOWED_DOMAINS", DEFAULT_ALLOWED_DOMAINS)),
            sensitive_domains=frozenset(_csv("SENSITIVE_DOMAINS", DEFAULT_SENSITIVE_DOMAINS)),
            sensitive_keywords=tuple(_csv("SENSITIVE_KEYWORDS", DEFAULT_SENSITIVE_KEYWORDS)),
            nightly_check_time=_time("NIGHTLY_CHECK_TIME", "22:30", tz),
            morning_summary_time=_time("MORNING_SUMMARY_TIME", "off", tz),
            media=MediaConfig(
                sonarr_url=_opt("SONARR_URL"),
                sonarr_api_key=_opt("SONARR_API_KEY"),
                radarr_url=_opt("RADARR_URL"),
                radarr_api_key=_opt("RADARR_API_KEY"),
                seerr_url=_opt("SEERR_URL"),
                seerr_api_key=_opt("SEERR_API_KEY"),
                qbit_url=_opt("QBIT_URL"),
                qbit_username=_opt("QBIT_USERNAME"),
                qbit_password=os.environ.get("QBIT_PASSWORD", ""),
            ),
        )
