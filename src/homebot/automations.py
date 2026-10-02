"""Reading and editing Home Assistant automations.

Reads are free. Anything that changes an automation is shown to the user as
YAML, as a diff when it is an edit, and waits for the Approve button. Automations
run the house unattended, so a bad edit is discovered at 3am, not at the keyboard.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Awaitable, Callable, Protocol

import yaml

log = logging.getLogger(__name__)

Approver = Callable[[str], Awaitable[bool]]

MAX_DIFF_LINES = 60
MAX_YAML_CHARS = 1500
# Keys Home Assistant accepts in an automation config. Anything else is almost
# certainly a mistake that would silently do nothing.
KNOWN_KEYS = {
    "id", "alias", "description", "mode", "max", "max_exceeded",
    "triggers", "conditions", "actions", "variables", "trace",
    # the older singular spellings, still accepted
    "trigger", "condition", "action",
}


class AutomationError(RuntimeError):
    """Reported back to Claude as a failed tool result."""


class HAClient(Protocol):
    async def get_states(self) -> list[dict[str, Any]]: ...
    async def get_automation_config(self, automation_id: str) -> dict[str, Any]: ...
    async def save_automation_config(self, automation_id: str, config: dict[str, Any]) -> Any: ...
    async def delete_automation_config(self, automation_id: str) -> Any: ...
    async def call_service(
        self, domain: str, service: str, entity_id: str | None = None, data: dict[str, Any] | None = None
    ) -> Any: ...


def to_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()


def from_yaml(text: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AutomationError(f"That is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AutomationError("An automation must be a YAML mapping with triggers and actions.")
    return parsed


def validate(config: dict[str, Any]) -> None:
    """Catch the mistakes Home Assistant would accept silently or reject unhelpfully."""
    unknown = set(config) - KNOWN_KEYS
    if unknown:
        raise AutomationError(
            f"Unknown automation keys: {', '.join(sorted(unknown))}. "
            f"Valid keys are {', '.join(sorted(KNOWN_KEYS))}."
        )
    if not (config.get("triggers") or config.get("trigger")):
        raise AutomationError("An automation needs triggers, or it will never run.")
    if not (config.get("actions") or config.get("action")):
        raise AutomationError("An automation needs actions, or it will do nothing.")
    if not config.get("alias"):
        raise AutomationError("Give the automation an alias, or it shows up unnamed in Home Assistant.")


def diff(before: str, after: str, name: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=f"{name} (now)", tofile=f"{name} (proposed)", lineterm=""
        )
    )
    if not lines:
        return "(no change)"
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... {len(lines) - MAX_DIFF_LINES} more lines"]
    return "\n".join(lines)


class AutomationTools:
    def __init__(self, ha: HAClient) -> None:
        self._ha = ha

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_automations",
                "description": (
                    "List Home Assistant automations with their id, name, whether they are on, and when "
                    "they last ran. Use the id with the other automation tools."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"search": {"type": "string", "description": "Only ones whose name matches."}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_automation",
                "description": "Read one automation's full configuration as YAML, using the id from list_automations.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "save_automation",
                "description": (
                    "Create or replace an automation. Pass the whole configuration as YAML, with at least "
                    "alias, triggers and actions. To edit one, read it with get_automation first and send "
                    "back the complete YAML with your change, not a fragment. The user is shown a diff and "
                    "must tap Approve. Home Assistant reloads automatically."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Existing id to replace, or a new slug like 'porch_light_at_dusk'.",
                        },
                        "yaml": {"type": "string", "description": "The complete automation config as YAML."},
                    },
                    "required": ["id", "yaml"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "delete_automation",
                "description": "Delete an automation. The user is shown what it does and must tap Approve.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        ]

    @property
    def names(self) -> set[str]:
        return {d["name"] for d in self.definitions}

    async def preview(self, name: str, tool_input: dict[str, Any]) -> str | None:
        """Describe the change without making it, so it can join a batch approval."""
        try:
            if name == "save_automation":
                return await self._save_summary(str(tool_input.get("id") or ""), str(tool_input.get("yaml") or ""))
            if name == "delete_automation":
                return await self._delete_summary(str(tool_input.get("id") or ""))
        except AutomationError:
            return None
        return None

    async def run(self, name: str, tool_input: dict[str, Any], approve: Approver) -> str:
        if name == "list_automations":
            return await self._list(tool_input.get("search"))
        if name == "get_automation":
            return await self._get(_str(tool_input, "id"))
        if name == "save_automation":
            return await self._save(_str(tool_input, "id"), _str(tool_input, "yaml"), approve)
        if name == "delete_automation":
            return await self._delete(_str(tool_input, "id"), approve)
        raise AutomationError(f"Unknown tool {name!r}")

    async def _list(self, search: str | None) -> str:
        needle = (search or "").strip().lower()
        lines = []
        for state in await self._ha.get_states():
            if not state.get("entity_id", "").startswith("automation."):
                continue
            attrs = state.get("attributes", {})
            name = str(attrs.get("friendly_name", ""))
            auto_id = attrs.get("id")
            if needle and needle not in name.lower() and needle not in str(auto_id).lower():
                continue
            if auto_id is None:
                # Automations defined outside automations.yaml cannot be edited by id.
                lines.append(f"(no id, read only) | {state.get('state')} | {name}")
                continue
            last = str(attrs.get("last_triggered") or "never")[:16].replace("T", " ")
            lines.append(f"{auto_id} | {state.get('state')} | last ran {last} | {name}")
        return "\n".join(sorted(lines)) or "No automations match."

    async def _config(self, automation_id: str) -> dict[str, Any]:
        try:
            config = await self._ha.get_automation_config(automation_id)
        except Exception as exc:
            raise AutomationError(
                f"Could not read automation {automation_id!r}: {exc}. "
                "Only automations stored in automations.yaml can be read this way."
            ) from exc
        if not isinstance(config, dict):
            raise AutomationError(f"Automation {automation_id!r} returned no configuration.")
        return config

    async def _get(self, automation_id: str) -> str:
        return to_yaml(await self._config(automation_id))

    async def _save_summary(self, automation_id: str, text: str) -> str:
        if not automation_id or not text.strip():
            raise AutomationError("id and yaml are both required")
        config = from_yaml(text)
        validate(config)
        proposed = to_yaml(config)
        try:
            current = to_yaml(await self._config(automation_id))
        except AutomationError:
            body = proposed if len(proposed) <= MAX_YAML_CHARS else proposed[:MAX_YAML_CHARS] + "\n... truncated"
            return f"CREATE automation '{config.get('alias')}' (id {automation_id}):\n```yaml\n{body}\n```"
        if current == proposed:
            raise AutomationError("That is identical to the current automation; nothing to change.")
        return (
            f"EDIT automation '{config.get('alias')}' (id {automation_id}):\n"
            f"```diff\n{diff(current, proposed, automation_id)}\n```"
        )

    async def _save(self, automation_id: str, text: str, approve: Approver) -> str:
        summary = await self._save_summary(automation_id, text)
        if not await approve(summary):
            return "The user DENIED this automation change (or did not answer in time). Nothing was saved."
        config = from_yaml(text)
        config.setdefault("id", automation_id)
        await self._ha.save_automation_config(automation_id, config)
        return f"Saved automation {automation_id}. Home Assistant has reloaded it."

    async def _delete_summary(self, automation_id: str) -> str:
        config = await self._config(automation_id)
        return f"DELETE automation '{config.get('alias', automation_id)}' (id {automation_id}). This cannot be undone."

    async def _delete(self, automation_id: str, approve: Approver) -> str:
        summary = await self._delete_summary(automation_id)
        if not await approve(summary):
            return "The user DENIED deleting this automation (or did not answer in time). Nothing changed."
        await self._ha.delete_automation_config(automation_id)
        return f"Deleted automation {automation_id}."


def _str(tool_input: dict[str, Any], key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AutomationError(f"{key} is required")
    return value.strip()
