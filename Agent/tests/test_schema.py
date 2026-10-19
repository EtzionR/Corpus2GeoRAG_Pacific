"""AskResult model, schema_version and the committed JSON Schema file. Fake models, no network."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from geo_agent.agent import GeoAgent
from geo_agent.graph_store import GraphStore
from geo_agent.schema import SCHEMA_PATH, SCHEMA_VERSION, AskResult, schema_text

GRAPH = Path(__file__).resolve().parent.parent / "data" / "sample_graph.json"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")


class FakeToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FailingModel(FakeToolModel):
    def _generate(self, *args, **kwargs):
        raise TimeoutError("provider timed out")


def call(name, id, **args):
    return {"name": name, "args": args, "id": id}


def ai(*calls):
    return AIMessage("", tool_calls=list(calls))


def final(answer="done", ids=(), context=()):
    return ai(call("AgentAnswer", "final", answer=answer, answer_ids=list(ids), context_ids=list(context),
                   answer_sets=[], sources=[]))


def ask(responses, question="q", model_cls=FakeToolModel):
    async def go():
        agent = await GeoAgent.create(graph_path=GRAPH, llm=model_cls(responses=responses))
        return await agent.ask(question)
    return asyncio.run(go())


def check(out):
    """Validates, and every top-level key is a declared field (the model covers the whole reply)."""
    parsed = AskResult.model_validate(out)
    assert set(out) == set(AskResult.model_fields), set(out) ^ set(AskResult.model_fields)
    assert out["schema_version"] == SCHEMA_VERSION
    return parsed


REPLIES = {
    "answer": [ai(call("search_entities", "c1", query="Battle of Midway")),
               ai(call("get_locations", "c2", entity_id="event:battle_of_midway")),
               final("At Midway Atoll.", ["loc:midway_atoll"], ["event:battle_of_midway"])],
    "clarification": [ai(call("search_entities", "c1", query="Battle of Guam")),
                      final("Did you mean the Battle of Guam (1941) or the Battle of Guam (1944)?")],
    "off_topic": [final("Roses are red ...")],
    "resolution": [ai(call("search_entities", "c1", query="Chungking")),
                   ai(call("find_relations", "c2", target_id="loc:chongqing")),
                   final("The IJN bombed Chongqing.", ["org:ijn"], ["loc:chongqing"])],
}


@pytest.mark.parametrize("kind", sorted(REPLIES))
def test_real_replies_validate(kind):
    parsed = check(ask(REPLIES[kind]))
    assert parsed.error is None
    if kind == "answer":
        assert parsed.geojson.features[0].properties.role == "answer"
    if kind == "off_topic":
        assert parsed.warnings == ["off_topic"]
    if kind == "resolution":
        assert parsed.resolved_entities and parsed.resolved_entities[0].quality in ("clear", "uncertain")


def test_error_replies_validate():
    model_err = check(ask([final()], model_cls=FailingModel))
    assert model_err.error.stage == "model_call"

    class Boom:
        async def ainvoke(self, *a, **k):
            raise ValueError("x")
    check(asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), Boom()).ask("q")))


def test_runner_stop_and_busy_replies_validate():
    from geo_agent.agent import RunTimeout, error_result, stopped_result
    from geo_agent.runner import ThreadBusy
    assert check(stopped_result("r", "timeout", [])).error.type == RunTimeout.__name__
    assert check(stopped_result("r", "cancelled", [])).error.stage == "cancelled"
    assert check(error_result("r", ThreadBusy("busy"), "busy", [])).error.stage == "busy"


def test_unknown_stage_is_rejected_and_extra_keys_are_allowed():
    out = ask(REPLIES["answer"])
    later = {**out, "source_refs": [{"provider": "wikipedia", "record_id": "Q1", "url": None, "page": "Battle of Midway"}],
             "graph": {"name": "x"}}  # "graph" stands for a key added in a later version
    later["geojson"]["features"][0]["properties"]["new_prop"] = 1
    parsed = AskResult.model_validate(later)
    assert parsed.source_refs[0].provider == "wikipedia" and parsed.model_extra["graph"] == {"name": "x"}
    bad = {**out, "error": {"type": "X", "message": "m", "stage": "nope"}}
    with pytest.raises(Exception):
        AskResult.model_validate(bad)


def test_schema_file_is_up_to_date():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text(), "run: uv run python -m geo_agent.schema"
    schema = json.loads(SCHEMA_PATH.read_text())
    assert set(schema["required"]) == set(AskResult.model_fields)
    assert schema["additionalProperties"] is True
