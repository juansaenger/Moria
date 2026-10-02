from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from ha_mcp.ha_client import HomeAssistantError

from .media import MediaError
from .automations import AutomationError
from .followups import FollowupError
from .server import ServerError

# Called with a human-readable description of a sensitive action; returns True if approved.
Approver = Callable[[str], Awaitable[bool]]

log = logging.getLogger(__name__)

MAX_LISTED_ENTITIES = 150
MAX_HISTORY_POINTS = 60
# Keys that choose what a service call acts on. Targets must come through `entity_id`
# so the safety checks see them.
TARGET_KEYS = {"entity_id", "area_id", "device_id", "floor_id", "label_id"}


class HAClient(Protocol):
    async def get_states(self) -> list[dict[str, Any]]: ...
    async def get_state(self, entity_id: str) -> dict[str, Any]: ...
    async def get_services(self) -> Any: ...
    async def get_history(self, entity_id: str, hours: float) -> Any: ...
    async def call_service(
        self, domain: str, service: str, entity_id: str | None = None, data: dict[str, Any] | None = None
    ) -> Any: ...


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": (
            "List Home Assistant entities as 'entity_id | state | friendly name' lines. "
            "Filter by domain (e.g. 'light') and/or a case-insensitive search string matched against "
            "the id and name. Use this to find the right entity before acting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Only entities in this domain, e.g. light, lock, climate."},
                "search": {"type": "string", "description": "Substring to match in entity id or friendly name."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_entity",
        "description": "Get the full current state and attributes of one entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_services",
        "description": "List the services available in one domain, with their fields, e.g. domain='climate'.",
        "input_schema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_history",
        "description": "Get recent state changes for one entity over the last N hours (max 48).",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "number", "description": "How far back to look, default 24."},
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "call_service",
        "description": (
            "Call a Home Assistant service on one or more entities, e.g. domain='light', service='turn_on', "
            "entity_ids=['light.kitchen'], data={'brightness_pct': 40}. Sensitive actions (locks, garage, alarm, "
            "oven, ...) are sent to the user for approval automatically; the result says whether it was approved. "
            "Put targets only in entity_ids, never in data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "data": {"type": "object", "description": "Extra service fields, e.g. brightness_pct, temperature."},
            },
            "required": ["domain", "service", "entity_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a lasting fact or preference to your notes file, e.g. 'Bedtime means all downstairs lights off "
            "and thermostat to 66F'. Use when the user asks you to remember something. Notes load at the start of "
            "each new conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class SafetyPolicy:
    allowed_domains: frozenset[str]
    sensitive_domains: frozenset[str]
    sensitive_keywords: tuple[str, ...]


class ToolError(Exception):
    """An error reported back to Claude as a failed tool result."""


class ToolSet(Protocol):
    """An extra group of tools (e.g. the media stack) plugged into HomeTools."""

    @property
    def definitions(self) -> list[dict[str, Any]]: ...

    @property
    def names(self) -> set[str]: ...

    async def run(self, name: str, tool_input: dict[str, Any], approve: Approver) -> str: ...

    async def preview(self, name: str, tool_input: dict[str, Any]) -> str | None:
        """Optional. The approval this call would ask for, or None if it needs none."""
        ...


class HomeTools:
    def __init__(
        self, ha: HAClient, policy: SafetyPolicy, notes_path: Path, extra: list[ToolSet] | None = None
    ) -> None:
        self._ha = ha
        self._policy = policy
        self._notes_path = notes_path
        self._extra = list(extra or [])

    @property
    def definitions(self) -> list[dict[str, Any]]:
        defs = list(TOOL_DEFINITIONS)
        for toolset in self._extra:
            defs.extend(toolset.definitions)
        return defs

    async def run(self, name: str, tool_input: dict[str, Any], approve: Approver) -> str:
        """Run one tool. Raises ToolError for problems Claude should see and recover from."""
        for toolset in self._extra:
            if name in toolset.names:
                try:
                    return await toolset.run(name, tool_input, approve)
                except (MediaError, ServerError, FollowupError, AutomationError) as exc:
                    raise ToolError(str(exc)) from exc
        try:
            if name == "list_entities":
                return await self._list_entities(tool_input.get("domain"), tool_input.get("search"))
            if name == "get_entity":
                return json.dumps(await self._ha.get_state(_str(tool_input, "entity_id")), default=str)
            if name == "list_services":
                return await self._list_services(_str(tool_input, "domain"))
            if name == "get_history":
                return await self._get_history(_str(tool_input, "entity_id"), tool_input.get("hours", 24))
            if name == "call_service":
                return await self._call_service(tool_input, approve)
            if name == "remember":
                return self._remember(_str(tool_input, "fact"))
        except HomeAssistantError as exc:
            raise ToolError(str(exc)) from exc
        raise ToolError(f"Unknown tool {name!r}")

    async def _list_entities(self, domain: str | None, search: str | None) -> str:
        states = await self._ha.get_states()
        domain = (domain or "").strip().lower()
        needle = (search or "").strip().lower()
        lines = []
        for state in sorted(states, key=lambda s: s.get("entity_id", "")):
            entity_id = state.get("entity_id", "")
            name = state.get("attributes", {}).get("friendly_name", "")
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            if needle and needle not in entity_id.lower() and needle not in str(name).lower():
                continue
            lines.append(f"{entity_id} | {state.get('state')} | {name}")
        if not lines:
            return "No matching entities."
        if len(lines) > MAX_LISTED_ENTITIES:
            extra = len(lines) - MAX_LISTED_ENTITIES
            lines = lines[:MAX_LISTED_ENTITIES] + [f"... and {extra} more; narrow the search."]
        return "\n".join(lines)

    async def _list_services(self, domain: str) -> str:
        services = await self._ha.get_services()
        for entry in services or []:
            if entry.get("domain") == domain:
                return json.dumps(entry.get("services", {}), default=str)
        raise ToolError(f"No services found for domain {domain!r}")

    async def _get_history(self, entity_id: str, hours: Any) -> str:
        try:
            hours = min(max(float(hours), 0.1), 48.0)
        except (TypeError, ValueError) as exc:
            raise ToolError("hours must be a number") from exc
        history = await self._ha.get_history(entity_id, hours)
        points = history[0] if history else []
        points = points[-MAX_HISTORY_POINTS:]
        lines = [f"{p.get('last_changed', '?')}  {p.get('state')}" for p in points]
        return "\n".join(lines) or f"No changes for {entity_id} in the last {hours:g} hours."

    async def preview(self, name: str, tool_input: dict[str, Any]) -> str | None:
        """What this call would ask the user to approve, without performing it."""
        for toolset in self._extra:
            if name in toolset.names:
                previewer = getattr(toolset, "preview", None)
                if previewer is None:
                    return None
                try:
                    return await previewer(name, tool_input)
                except Exception:
                    log.exception("preview of %s failed", name)
                    return None
        if name != "call_service":
            return None
        try:
            domain = _str(tool_input, "domain").lower()
            service = _str(tool_input, "service").lower()
            entity_ids = tool_input.get("entity_ids")
            if not isinstance(entity_ids, list) or not entity_ids:
                return None
            if domain not in self._policy.allowed_domains:
                return None
            names = {}
            for entity_id in entity_ids:
                state = await self._ha.get_state(entity_id)
                names[entity_id] = str(state.get("attributes", {}).get("friendly_name", entity_id))
        except (ToolError, HomeAssistantError):
            return None
        if not self._is_sensitive(domain, names):
            return None
        return self._service_summary(domain, service, entity_ids, names, tool_input.get("data") or {})

    @staticmethod
    def _service_summary(
        domain: str, service: str, entity_ids: list[str], names: dict[str, str], data: dict[str, Any]
    ) -> str:
        targets = ", ".join(f"{names[e]} ({e})" for e in entity_ids)
        summary = f"{domain}.{service} on {targets}"
        if data:
            summary += f" with {json.dumps(data)}"
        return summary

    async def _call_service(self, tool_input: dict[str, Any], approve: Approver) -> str:
        domain = _str(tool_input, "domain").lower()
        service = _str(tool_input, "service").lower()
        entity_ids = tool_input.get("entity_ids")
        if not isinstance(entity_ids, list) or not entity_ids or not all(isinstance(e, str) and e for e in entity_ids):
            raise ToolError("entity_ids must be a non-empty list of entity ids")
        data = tool_input.get("data") or {}
        if not isinstance(data, dict):
            raise ToolError("data must be an object")
        if TARGET_KEYS & data.keys():
            raise ToolError("Put targets in entity_ids only; data may not contain entity_id/area_id/device_id/...")
        if domain not in self._policy.allowed_domains:
            raise ToolError(f"The {domain!r} domain is not allowed for this bot.")

        names = {}
        for entity_id in entity_ids:
            state = await self._ha.get_state(entity_id)  # also confirms the entity exists
            names[entity_id] = str(state.get("attributes", {}).get("friendly_name", entity_id))

        if self._is_sensitive(domain, names):
            summary = self._service_summary(domain, service, entity_ids, names, data)
            if not await approve(summary):
                return "The user DENIED this action (or did not answer in time). It was not performed."

        await self._ha.call_service(domain, service, entity_id=",".join(entity_ids), data=data)
        return f"Called {domain}.{service} on {', '.join(entity_ids)}. Check the state with get_entity if it matters."

    def _is_sensitive(self, domain: str, names: dict[str, str]) -> bool:
        if domain in self._policy.sensitive_domains:
            return True
        for entity_id, name in names.items():
            haystack = f"{entity_id} {name}".lower()
            if any(keyword in haystack for keyword in self._policy.sensitive_keywords):
                return True
        return False

    def _remember(self, fact: str) -> str:
        fact = " ".join(fact.split())
        self._notes_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.date.today().isoformat()
        with self._notes_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {fact} (saved {stamp})\n")
        return "Saved to notes. It will be loaded at the start of future conversations."


def _str(tool_input: dict[str, Any], key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} is required")
    return value.strip()
