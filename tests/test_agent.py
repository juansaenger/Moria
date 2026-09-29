from __future__ import annotations

from zoneinfo import ZoneInfo

from homebot.agent import HomeAgent
from homebot.tools import HomeTools, SafetyPolicy

from .fakes import FakeAnthropic, FakeHA, response, text, tool_use

POLICY = SafetyPolicy(frozenset({"light", "lock"}), frozenset({"lock"}), ())


def make_agent(client, tmp_path, model="claude-opus-5-5"):
    (tmp_path / "role.md").write_text("You live in a test house.")
    tools = HomeTools(FakeHA(), POLICY, tmp_path / "notes.md")
    return HomeAgent(client, tools, model=model, effort="low", workspace=tmp_path, timezone=ZoneInfo("UTC"))


async def always(_summary: str) -> bool:
    return True


async def test_tool_loop_and_request_shape(tmp_path):
    client = FakeAnthropic(
        response("tool_use", text("Checking."), tool_use("t1", "list_entities", {"domain": "light"})),
        response("end_turn", text("The kitchen light is on.")),
    )
    agent = make_agent(client, tmp_path)
    convo = agent.new_conversation()
    reply = await agent.ask(convo, "Juan", "which lights are on?", always)

    assert reply == "The kitchen light is on."
    first, second = client.requests
    assert "test house" in first["system"]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["messages"][0]["content"].startswith("[Juan, ")
    # History is append-only: second request extends the first unchanged.
    assert second["messages"][: len(first["messages"])] == first["messages"]
    result = second["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and "light.kitchen" in result["content"]
    assert len(convo.messages) == 4


async def test_tool_errors_are_reported_not_raised(tmp_path):
    client = FakeAnthropic(
        response("tool_use", tool_use("t1", "get_entity", {"entity_id": "light.missing"})),
        response("end_turn", text("I couldn't find that light.")),
    )
    agent = make_agent(client, tmp_path)
    reply = await agent.ask(agent.new_conversation(), "Juan", "office light?", always)
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "404" in result["content"]
    assert reply == "I couldn't find that light."


async def test_refusal_resets_conversation(tmp_path):
    agent = make_agent(FakeAnthropic(response("refusal")), tmp_path)
    convo = agent.new_conversation()
    assert await agent.ask(convo, "Juan", "hmm", always) == "I can't help with that one."
    assert convo.messages == []


async def test_haiku_skips_effort_and_fallbacks(tmp_path):
    client = FakeAnthropic(response("end_turn", text("ok")))
    agent = make_agent(client, tmp_path, model="claude-haiku-4-5")
    await agent.ask(agent.new_conversation(), "Juan", "hi", always)
    request = client.requests[0]
    assert "output_config" not in request and "fallbacks" not in request and "betas" not in request


async def test_notes_frozen_per_conversation(tmp_path):
    client = FakeAnthropic(
        response("tool_use", tool_use("t1", "remember", {"fact": "Cats are named Moe and Joe"})),
        response("end_turn", text("Noted.")),
    )
    agent = make_agent(client, tmp_path)
    convo = agent.new_conversation()
    await agent.ask(convo, "Juan", "remember the cats' names", always)
    assert "Moe" not in client.requests[1]["system"]
    assert "Moe and Joe" in agent.new_conversation().system
