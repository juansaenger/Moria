from __future__ import annotations

import pytest

from homebot.tools import HomeTools, SafetyPolicy, ToolError

from .fakes import FakeHA

POLICY = SafetyPolicy(
    allowed_domains=frozenset({"light", "switch", "lock", "cover"}),
    sensitive_domains=frozenset({"lock"}),
    sensitive_keywords=("garage", "oven"),
)


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, summary: str) -> bool:
        self.asked.append(summary)
        return self.answer


@pytest.fixture
def ha() -> FakeHA:
    return FakeHA()


@pytest.fixture
def tools(ha: FakeHA, tmp_path) -> HomeTools:
    return HomeTools(ha, POLICY, tmp_path / "notes.md")


async def test_plain_light_runs_without_approval(tools, ha):
    approve = Approver(False)
    result = await tools.run(
        "call_service",
        {"domain": "light", "service": "turn_off", "entity_ids": ["light.kitchen"], "data": {"transition": 2}},
        approve,
    )
    assert approve.asked == []
    assert ha.calls == [("light", "turn_off", "light.kitchen", {"transition": 2})]
    assert "Called light.turn_off" in result


@pytest.mark.parametrize(
    ("domain", "service", "entity"),
    [
        ("lock", "unlock", "lock.front_door"),  # sensitive domain
        ("cover", "open_cover", "cover.garage"),  # keyword in id and name
        ("switch", "turn_on", "switch.plug_7"),  # keyword only in friendly name
    ],
)
async def test_sensitive_actions_need_approval(tools, ha, domain, service, entity):
    denied = Approver(False)
    result = await tools.run("call_service", {"domain": domain, "service": service, "entity_ids": [entity]}, denied)
    assert len(denied.asked) == 1 and entity in denied.asked[0]
    assert "DENIED" in result
    assert ha.calls == []

    approved = Approver(True)
    await tools.run("call_service", {"domain": domain, "service": service, "entity_ids": [entity]}, approved)
    assert ha.calls == [(domain, service, entity, {})]


async def test_mixed_batch_needs_approval(tools, ha):
    denied = Approver(False)
    await tools.run(
        "call_service",
        {"domain": "cover", "service": "close_cover", "entity_ids": ["cover.garage"]},
        denied,
    )
    await tools.run(
        "call_service",
        {"domain": "switch", "service": "turn_off", "entity_ids": ["switch.plug_7"]},
        denied,
    )
    assert len(denied.asked) == 2 and ha.calls == []


@pytest.mark.parametrize(
    "tool_input",
    [
        {"domain": "homeassistant", "service": "restart", "entity_ids": ["light.kitchen"]},  # domain not allowed
        {"domain": "light", "service": "turn_on", "entity_ids": ["light.kitchen"], "data": {"area_id": "garage"}},
        {"domain": "lock", "service": "unlock", "entity_ids": []},
        {"domain": "light", "service": "turn_on", "entity_ids": ["light.nope"]},  # unknown entity
    ],
)
async def test_rejected_calls(tools, ha, tool_input):
    approve = Approver(True)
    with pytest.raises(ToolError):
        await tools.run("call_service", tool_input, approve)
    assert ha.calls == [] and approve.asked == []


async def test_list_entities_filters(tools):
    out = await tools.run("list_entities", {"domain": "light"}, Approver(True))
    assert "light.kitchen | on | Kitchen" in out and "lock." not in out
    out = await tools.run("list_entities", {"search": "garage"}, Approver(True))
    assert out.splitlines() == ["cover.garage | open | Garage Door"]


async def test_remember_appends(tools, tmp_path):
    await tools.run("remember", {"fact": "Bedtime means  downstairs lights off"}, Approver(True))
    await tools.run("remember", {"fact": "Office max 60%"}, Approver(True))
    lines = (tmp_path / "notes.md").read_text().splitlines()
    assert len(lines) == 2 and lines[0].startswith("- Bedtime means downstairs lights off")
