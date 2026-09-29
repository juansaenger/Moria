from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ha_mcp.ha_client import HomeAssistantError

STATES = {
    "light.kitchen": {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
    "light.office": {"entity_id": "light.office", "state": "off", "attributes": {"friendly_name": "Office"}},
    "lock.front_door": {"entity_id": "lock.front_door", "state": "unlocked", "attributes": {"friendly_name": "Front Door"}},
    "cover.garage": {"entity_id": "cover.garage", "state": "open", "attributes": {"friendly_name": "Garage Door"}},
    "switch.plug_7": {"entity_id": "switch.plug_7", "state": "off", "attributes": {"friendly_name": "Oven Plug"}},
}


class FakeHA:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any] | None]] = []

    async def get_states(self) -> list[dict[str, Any]]:
        return list(STATES.values())

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        if entity_id not in STATES:
            raise HomeAssistantError(f"Home Assistant returned 404 for GET /states/{entity_id}")
        return STATES[entity_id]

    async def get_services(self) -> Any:
        return [{"domain": "light", "services": {"turn_on": {"fields": {}}}}]

    async def get_history(self, entity_id: str, hours: float) -> Any:
        return [[{"state": "on", "last_changed": "2026-09-29T20:00:00+00:00"}]]

    async def call_service(self, domain, service, entity_id=None, data=None) -> Any:
        self.calls.append((domain, service, entity_id, data))
        return []


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def tool_use(id_: str, name: str, input_: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(content))


class FakeAnthropic:
    """Returns scripted responses and records each request's parameters."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> SimpleNamespace:
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**params, "messages": list(params["messages"])})
        return self._responses.pop(0)
