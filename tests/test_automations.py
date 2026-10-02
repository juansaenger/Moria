from __future__ import annotations

from typing import Any

import pytest

from homebot.automations import (
    AutomationError,
    AutomationTools,
    diff,
    from_yaml,
    to_yaml,
    validate,
)

PORCH = {
    "id": "porch_dusk",
    "alias": "Porch light at dusk",
    "triggers": [{"trigger": "sun", "event": "sunset"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}],
    "mode": "single",
}


class FakeHA:
    def __init__(self, configs: dict[str, dict[str, Any]] | None = None) -> None:
        self.configs = dict(configs or {})
        self.saved: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    async def get_states(self):
        return [
            {
                "entity_id": "automation.porch_light_at_dusk",
                "state": "on",
                "attributes": {"id": "porch_dusk", "friendly_name": "Porch light at dusk",
                               "last_triggered": "2026-09-30T23:10:00+00:00"},
            },
            {
                "entity_id": "automation.legacy_thing",
                "state": "off",
                "attributes": {"friendly_name": "Legacy thing"},  # no id: not editable
            },
            {"entity_id": "light.porch", "state": "on", "attributes": {}},
        ]

    async def get_automation_config(self, automation_id: str):
        if automation_id not in self.configs:
            raise RuntimeError("404 not found")
        return self.configs[automation_id]

    async def save_automation_config(self, automation_id: str, config: dict[str, Any]):
        self.saved.append((automation_id, config))
        self.configs[automation_id] = config

    async def delete_automation_config(self, automation_id: str):
        self.deleted.append(automation_id)
        self.configs.pop(automation_id, None)

    async def call_service(self, *a, **k):
        return None


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, summary: str) -> bool:
        self.asked.append(summary)
        return self.answer


# ---- validation catches what Home Assistant would accept silently


def test_rejects_automation_with_no_trigger():
    with pytest.raises(AutomationError, match="needs triggers"):
        validate({"alias": "x", "actions": [{"action": "light.turn_on"}]})


def test_rejects_automation_with_no_action():
    with pytest.raises(AutomationError, match="needs actions"):
        validate({"alias": "x", "triggers": [{"trigger": "sun"}]})


def test_rejects_unnamed_automation():
    with pytest.raises(AutomationError, match="alias"):
        validate({"triggers": [{"trigger": "sun"}], "actions": [{"action": "light.turn_on"}]})


def test_rejects_a_typo_in_a_key():
    with pytest.raises(AutomationError, match="Unknown automation keys: trigggers"):
        validate({"alias": "x", "trigggers": [], "triggers": [1], "actions": [1]})


def test_accepts_the_older_singular_spellings():
    validate({"alias": "x", "trigger": [{"platform": "sun"}], "action": [{"service": "light.turn_on"}]})


def test_bad_yaml_is_reported_as_such():
    with pytest.raises(AutomationError, match="not valid YAML"):
        from_yaml("alias: x\n  bad: [indent")


def test_yaml_that_is_not_a_mapping_is_rejected():
    with pytest.raises(AutomationError, match="mapping"):
        from_yaml("- just\n- a list")


def test_yaml_round_trip_keeps_key_order():
    assert to_yaml(PORCH).splitlines()[0].startswith("id:")


# ---- reading


@pytest.mark.asyncio
async def test_list_shows_ids_and_flags_uneditable_ones():
    out = await AutomationTools(FakeHA()).run("list_automations", {}, Approver(True))
    assert "porch_dusk" in out and "Porch light at dusk" in out
    assert "no id, read only" in out


@pytest.mark.asyncio
async def test_list_can_be_filtered():
    out = await AutomationTools(FakeHA()).run("list_automations", {"search": "legacy"}, Approver(True))
    assert "Legacy thing" in out and "porch_dusk" not in out


@pytest.mark.asyncio
async def test_get_returns_yaml():
    out = await AutomationTools(FakeHA({"porch_dusk": PORCH})).run(
        "get_automation", {"id": "porch_dusk"}, Approver(True)
    )
    assert "alias: Porch light at dusk" in out


@pytest.mark.asyncio
async def test_reading_an_unknown_automation_explains_why():
    with pytest.raises(AutomationError, match="automations.yaml"):
        await AutomationTools(FakeHA()).run("get_automation", {"id": "nope"}, Approver(True))


# ---- writing, which always asks first


@pytest.mark.asyncio
async def test_creating_shows_the_yaml_and_needs_approval():
    ha = FakeHA()
    tools = AutomationTools(ha)
    approver = Approver(True)
    out = await tools.run("save_automation", {"id": "porch_dusk", "yaml": to_yaml(PORCH)}, approver)
    assert "CREATE automation" in approver.asked[0] and "```yaml" in approver.asked[0]
    assert ha.saved and ha.saved[0][0] == "porch_dusk"
    assert "Saved automation" in out


@pytest.mark.asyncio
async def test_editing_shows_a_diff_not_the_whole_file():
    ha = FakeHA({"porch_dusk": PORCH})
    changed = dict(PORCH, mode="restart")
    approver = Approver(True)
    await AutomationTools(ha).run("save_automation", {"id": "porch_dusk", "yaml": to_yaml(changed)}, approver)
    summary = approver.asked[0]
    assert "EDIT automation" in summary and "```diff" in summary
    assert "-mode: single" in summary and "+mode: restart" in summary
    assert ha.saved[0][1]["mode"] == "restart"


@pytest.mark.asyncio
async def test_denying_changes_nothing():
    ha = FakeHA({"porch_dusk": PORCH})
    approver = Approver(False)
    out = await AutomationTools(ha).run(
        "save_automation", {"id": "porch_dusk", "yaml": to_yaml(dict(PORCH, mode="restart"))}, approver
    )
    assert "DENIED" in out
    assert ha.saved == []


@pytest.mark.asyncio
async def test_an_identical_save_is_refused_rather_than_asked_about():
    ha = FakeHA({"porch_dusk": PORCH})
    with pytest.raises(AutomationError, match="identical"):
        await AutomationTools(ha).run("save_automation", {"id": "porch_dusk", "yaml": to_yaml(PORCH)}, Approver(True))


@pytest.mark.asyncio
async def test_invalid_yaml_never_reaches_the_user_or_the_server():
    ha = FakeHA()
    approver = Approver(True)
    with pytest.raises(AutomationError, match="needs actions"):
        await AutomationTools(ha).run(
            "save_automation", {"id": "x", "yaml": "alias: x\ntriggers: [{trigger: sun}]"}, approver
        )
    assert approver.asked == [] and ha.saved == []


@pytest.mark.asyncio
async def test_delete_names_the_automation_and_needs_approval():
    ha = FakeHA({"porch_dusk": PORCH})
    approver = Approver(True)
    out = await AutomationTools(ha).run("delete_automation", {"id": "porch_dusk"}, approver)
    assert "Porch light at dusk" in approver.asked[0] and "cannot be undone" in approver.asked[0]
    assert ha.deleted == ["porch_dusk"] and "Deleted" in out


@pytest.mark.asyncio
async def test_denied_delete_keeps_it():
    ha = FakeHA({"porch_dusk": PORCH})
    out = await AutomationTools(ha).run("delete_automation", {"id": "porch_dusk"}, Approver(False))
    assert "DENIED" in out and ha.deleted == []


# ---- batching


@pytest.mark.asyncio
async def test_preview_matches_what_the_user_would_be_asked():
    ha = FakeHA({"porch_dusk": PORCH})
    tools = AutomationTools(ha)
    payload = {"id": "porch_dusk", "yaml": to_yaml(dict(PORCH, mode="restart"))}
    preview = await tools.preview("save_automation", payload)
    approver = Approver(False)
    await tools.run("save_automation", payload, approver)
    assert approver.asked == [preview]


@pytest.mark.asyncio
async def test_reads_need_no_approval_so_they_do_not_preview():
    tools = AutomationTools(FakeHA({"porch_dusk": PORCH}))
    assert await tools.preview("list_automations", {}) is None
    assert await tools.preview("get_automation", {"id": "porch_dusk"}) is None


def test_long_diffs_are_truncated():
    before = "\n".join(f"line {i}" for i in range(200))
    after = "\n".join(f"changed {i}" for i in range(200))
    out = diff(before, after, "big")
    assert "more lines" in out and len(out.splitlines()) < 70
