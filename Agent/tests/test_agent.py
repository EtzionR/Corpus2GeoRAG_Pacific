"""GeoAgent.ask() output and error handling (spec R10, R11). Stub agents, no network."""

import asyncio
from pathlib import Path

import httpx
import openai
import pytest
from langgraph.errors import GraphRecursionError

import geo_agent.agent as agent_mod
from geo_agent.agent import GeoAgent
from geo_agent.graph_store import GraphStore

GRAPH = Path(__file__).resolve().parent.parent / "data" / "sample_graph.json"
OUTPUT_KEYS = {"answer", "entity_ids", "sources", "geojson", "tool_calls", "run_id", "warnings", "error"}


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))


class RaisingAgent:
    def __init__(self, exc):
        self.exc = exc

    async def ainvoke(self, *args, **kwargs):
        raise self.exc


REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


@pytest.mark.parametrize(
    "exc,stage",
    [
        (GraphRecursionError("Recursion limit of 25 reached"), "step_limit"),
        (openai.APIConnectionError(request=REQUEST), "model_call"),
        (openai.APITimeoutError(request=REQUEST), "model_call"),
        (httpx.ConnectError("refused", request=REQUEST), "model_call"),
        (ValueError("tool blew up outside the loop"), "unknown"),
    ],
)
def test_failures_return_error_result(exc, stage):
    out = asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(exc)).ask("q"))
    assert set(out) >= OUTPUT_KEYS
    assert out["error"] == {"type": type(exc).__name__, "message": str(exc), "stage": stage}
    assert out["answer"] == "" and out["entity_ids"] == [] and out["sources"] == []
    assert out["geojson"] == {"type": "FeatureCollection", "features": []}
    assert out["tool_calls"] == [] and len(out["run_id"]) == 32


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_interrupts_are_not_swallowed(exc):
    with pytest.raises(type(exc)):
        asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(exc)).ask("q"))


def test_cli_exits_nonzero_on_error(monkeypatch, capsys):
    async def fake_create(cls, *args, **kwargs):
        return GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(TimeoutError("slow")))
    monkeypatch.setattr(GeoAgent, "create", classmethod(fake_create))
    assert asyncio.run(agent_mod._run(["Who attacked Midway?"])) == 1
    assert "Error at stage model_call" in capsys.readouterr().out


def test_model_requests_have_a_timeout(monkeypatch):
    """A stalled provider request must fail (model_call) instead of hanging the run."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.delenv("GEO_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("GEO_AGENT_MAX_RETRIES", raising=False)
    llm = agent_mod.get_llm()
    assert (llm.request_timeout, llm.max_retries) == (60.0, 2)
    monkeypatch.setenv("GEO_AGENT_TIMEOUT", "15")
    monkeypatch.setenv("GEO_AGENT_MAX_RETRIES", "0")
    llm = agent_mod.get_llm()
    assert (llm.request_timeout, llm.max_retries) == (15.0, 0)


def test_env_example_holds_no_secrets():
    """.env.example is committed; real values belong in the gitignored .env."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    values = dict(line.split("=", 1) for line in example.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert values["OPENROUTER_API_KEY"] == "", "remove the API key from .env.example (put it in .env)"


# ---------------------------------------------------------------------------
# finalize_answer: evidence checks on ids, sources and coordinates (spec R7, R8, R11)

import json
from types import SimpleNamespace

from geo_agent.agent import AgentAnswer, coordinates_in_text, finalize_answer

STORE = GraphStore.from_json(GRAPH)
NEW_KEYS = {"dropped_ids", "unverified_sources", "unverified_numbers"}


def msg(type, content="", tool_calls=(), name=None):
    return SimpleNamespace(type=type, content=content, tool_calls=list(tool_calls), name=name)


def turn(*tool_payloads, question="q"):
    """An earlier turn (ignored) plus this turn: question, one AI step, tool results."""
    old = [msg("human", "earlier"), msg("tool", json.dumps([{"id": "loc:tokyo"}]), name="search_entities")]
    calls = [{"name": n, "args": {}, "id": f"c{i}"} for i, (n, _) in enumerate(tool_payloads)]
    return old + [msg("human", question), msg("ai", tool_calls=calls)] + [
        msg("tool", p if isinstance(p, str) else json.dumps(p), name=n) for n, p in tool_payloads
    ]


MIDWAY_TOOLS = turn(
    ("search_entities", [{"id": "event:battle_of_midway", "name": "Battle of Midway", "score": 100}]),
    ("get_locations", [{"id": "loc:midway_atoll", "name": "Midway Atoll", "lat": 28.21, "lon": -177.37}]),
    ("search_source_text", [{"entity": {"id": "event:battle_of_midway"}, "text": "...",
                             "sources": [{"page": "Battle of Midway", "paragraph": 0}]}]),
)


def answer(text="At Midway Atoll.", ids=("loc:midway_atoll",), sources=("Battle of Midway",)):
    return AgentAnswer(answer_sets=[], answer=text, context_ids=[], answer_ids=list(ids), sources=list(sources))


def test_finalize_normal_case_keeps_supported_ids_and_sources():
    out = finalize_answer(STORE, answer(ids=["loc:midway_atoll", "event:battle_of_midway"]), MIDWAY_TOOLS)
    assert {"answer", "entity_ids", "sources", "geojson", "tool_calls", "warnings"} | NEW_KEYS <= set(out)
    assert out["entity_ids"] == ["loc:midway_atoll", "event:battle_of_midway"]
    assert out["sources"] == ["Battle of Midway"] and out["dropped_ids"] == [] and out["warnings"] == []
    assert [c["name"] for c in out["tool_calls"]] == ["search_entities", "get_locations", "search_source_text"]


def test_finalize_drops_unknown_and_unsupported_ids():
    # loc:tokyo appeared only in the EARLIER turn, so it is not evidence for this one
    out = finalize_answer(STORE, answer(ids=["loc:midway_atoll", "loc:tokyo", "loc:atlantis"]), MIDWAY_TOOLS)
    assert out["entity_ids"] == ["loc:midway_atoll"]
    assert out["dropped_ids"] == [{"id": "loc:tokyo", "reason": "not_in_tool_output"}, {"id": "loc:atlantis", "reason": "unknown"}]
    assert "ids_dropped" in out["warnings"]
    assert [f["properties"]["id"] for f in out["geojson"]["features"]] == ["loc:midway_atoll"]


def test_finalize_moves_unverified_sources_and_fills_missing_ones():
    out = finalize_answer(STORE, answer(sources=["Battle of Midway", "Pacific War"]), MIDWAY_TOOLS)
    assert out["sources"] == ["Battle of Midway"] and out["unverified_sources"] == ["Pacific War"]
    assert "sources_unverified" in out["warnings"]
    filled = finalize_answer(STORE, answer(sources=[]), MIDWAY_TOOLS)
    assert filled["sources"] == ["Midway Atoll"]  # from the kept node's own sources


def test_finalize_tolerates_non_json_tool_content():
    messages = turn(("mcp_lookup", "plain text from an MCP tool"), ("get_locations", [{"id": "loc:midway_atoll", "lat": 28.21, "lon": -177.37}]))
    assert finalize_answer(STORE, answer(sources=[]), messages)["entity_ids"] == ["loc:midway_atoll"]


def test_coordinates_in_text_cases():
    assert coordinates_in_text("Midway is at 28.21, -177.37.") == [28.21, -177.37]
    assert coordinates_in_text("Henderson Field (9.43°S, 160.05°E)") == [-9.43, 160.05]
    assert coordinates_in_text("within 20.5 km, in 1942") == []


def test_finalize_flags_coordinates_not_in_tool_output():
    ok = finalize_answer(STORE, answer(text="Midway Atoll lies at 28.21, -177.37."), MIDWAY_TOOLS)
    assert ok["unverified_numbers"] == [] and "text_coordinates_unverified" not in ok["warnings"]
    bad = finalize_answer(STORE, answer(text="Midway Atoll lies at 28.5, -177.0, about 20.5 km across, 1942."), MIDWAY_TOOLS)
    assert bad["unverified_numbers"] == [28.5, -177.0] and "text_coordinates_unverified" in bad["warnings"]
    assert bad["answer"].startswith("Midway Atoll lies at 28.5")  # text is flagged, never changed


def test_error_result_has_all_keys():
    out = asyncio.run(GeoAgent(STORE, RaisingAgent(TimeoutError("x"))).ask("q"))
    assert OUTPUT_KEYS | NEW_KEYS <= set(out)


# ---------------------------------------------------------------------------
# Resolution record, interpretation, year check (spec R21, R22)

from geo_agent.graph_store import Node


def search_turn(question, *queries, store=STORE):
    """A turn where the model searched each query; tool messages carry real search_entities output."""
    calls = [{"name": "search_entities", "args": {"query": q}, "id": f"s{i}"} for i, q in enumerate(queries)]
    tools = [SimpleNamespace(type="tool", name="search_entities", tool_call_id=f"s{i}", tool_calls=[],
                             content=json.dumps(store.search_entities(q))) for i, q in enumerate(queries)]
    return [msg("human", question), msg("ai", tool_calls=calls), *tools]


def final_ids(*ids):
    return AgentAnswer(answer_sets=[], answer="answer", context_ids=[], answer_ids=list(ids), sources=[])


def test_r21_alias_hit_gets_an_interpretation_line():
    out = finalize_answer(STORE, final_ids("loc:chongqing"), search_turn("Who bombed Chungking?", "Chungking"))
    (rec,) = out["resolved_entities"]
    assert (rec["typed"], rec["matched_label"], rec["match"], rec["quality"]) == ("Chungking", "Chungking", "alias", "clear")
    assert out["interpretation"] == ["'Chungking' read as Chongqing (alias)"]
    assert not {"uncertain_match_used", "model_rewritten_query"} & set(out["warnings"])


def test_r21_exact_name_has_no_interpretation_line():
    out = finalize_answer(STORE, final_ids("loc:guadalcanal"), search_turn("Where is Guadalcanal?", "Guadalcanal"))
    assert out["resolved_entities"][0]["match"] == "name" and out["interpretation"] == []


def test_r21_uncertain_match_used_in_an_answer():
    out = finalize_answer(STORE, final_ids("loc:tarawa"), search_turn("What happened at Tara?", "Tara"))
    assert out["resolved_entities"][0]["quality"] == "uncertain"
    assert "uncertain_match_used" in out["warnings"]


def test_r21_model_rewritten_query():
    out = finalize_answer(STORE, final_ids("loc:chongqing"), search_turn("Who bombed China's wartime capital?", "Chongqing"))
    assert out["resolved_entities"][0]["model_rewritten_query"] is True
    assert "model_rewritten_query" in out["warnings"]


def test_r21_clarification_reply_has_no_warning():
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Did you mean 1941 or 1944?", context_ids=[], answer_ids=[], sources=[]), search_turn("Who won the Battle of Guam?", "Battle of Guam"))
    assert out["resolved_entities"] == [] and out["warnings"] == []


def test_r21_hit_reached_through_another_tool_is_not_a_resolution():
    turn_ = search_turn("Where was the Battle of Midway?", "Battle of Midway")
    turn_ += [msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:battle_of_midway"}, "id": "g"}]),
              msg("tool", json.dumps(STORE.locations_of("event:battle_of_midway")), name="get_locations")]
    out = finalize_answer(STORE, final_ids("loc:midway_atoll", "event:battle_of_midway"), turn_)
    assert [r["id"] for r in out["resolved_entities"]] == ["event:battle_of_midway"]
    assert "uncertain_match_used" not in out["warnings"]


def test_r22_year_outside_event_dates():
    q = "What happened in the Battle of Guam in 1944?"
    wrong = finalize_answer(STORE, final_ids("event:battle_of_guam_1941"), search_turn(q, "Battle of Guam"))
    assert wrong["resolved_entities"][0]["context_mismatch"] == {"question_year": 1944, "start_date": "1941-12-08", "end_date": "1941-12-10"}
    assert "context_mismatch" in wrong["warnings"]
    right = finalize_answer(STORE, final_ids("event:battle_of_guam_1944"), search_turn(q, "Battle of Guam"))
    assert "context_mismatch" not in right["warnings"]


def test_r22_skipped_without_year_or_dates():
    no_year = finalize_answer(STORE, final_ids("event:battle_of_guam_1941"), search_turn("What happened in the Battle of Guam?", "Battle of Guam"))
    assert "context_mismatch" not in no_year["warnings"]
    undated = GraphStore([Node(id="event:x", name="Battle of X", type="event")], [])
    out = finalize_answer(undated, final_ids("event:x"), search_turn("Battle of X in 1942?", "Battle of X", store=undated))
    assert out["resolved_entities"] and "context_mismatch" not in out["warnings"]


# ---------------------------------------------------------------------------
# Confidence (spec R9)

def edge(src, tgt, count, page="Battle of Corregidor"):
    return {"source": {"id": src}, "relation": "ATTACKED", "target": {"id": tgt}, "count": count, "sources": [page]}


def corregidor_turn(count, pages=("Battle of Corregidor",)):
    return turn(("find_relations", [edge("org:ija", "loc:corregidor", count)]),
                ("search_source_text", [{"entity": {"id": "loc:corregidor"}, "sources": [{"page": p} for p in pages]}]))


def corregidor_answer(sources):
    return AgentAnswer(answer_sets=[], answer="The IJA.", context_ids=[], answer_ids=["loc:corregidor", "org:ija"], sources=list(sources))


def test_r9_high_medium_low():
    high = finalize_answer(STORE, corregidor_answer(["Battle of Corregidor", "Corregidor"]), corregidor_turn(3, ("Battle of Corregidor", "Corregidor")))
    assert high["confidence"] == {"level": "high", "basis": {"max_edge_count": 3, "n_source_pages": 2, "uncertain_match_used": False}}
    medium = finalize_answer(STORE, corregidor_answer(["Battle of Corregidor"]), corregidor_turn(2))
    assert medium["confidence"]["level"] == "medium" and "low_confidence" not in medium["warnings"]
    unsupported = turn(("get_entity", {"id": "loc:corregidor", "sources": []}))
    low = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Corregidor.", context_ids=[], answer_ids=["loc:corregidor"], sources=["Nope"]), unsupported)
    assert low["confidence"]["level"] == "low" and "low_confidence" in low["warnings"]


def test_r9_uncertain_match_forces_low():
    strong_evidence = [msg("ai", tool_calls=[{"name": "find_relations", "args": {"target_id": "loc:tarawa"}, "id": "f"}]),
                       msg("tool", json.dumps([edge("org:usmc", "loc:tarawa", 5, "Battle of Tarawa")]), name="find_relations"),
                       msg("tool", json.dumps([{"entity": {"id": "loc:tarawa"}, "sources": [{"page": "Tarawa"}]}]), name="search_source_text")]
    turn_ = search_turn("What happened at Tara?", "Tara") + strong_evidence
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="x", context_ids=[], answer_ids=["loc:tarawa"], sources=["Battle of Tarawa", "Tarawa"]), turn_)
    assert out["confidence"]["level"] == "low" and out["confidence"]["basis"]["uncertain_match_used"] is True


def test_r9_clarification_reply_has_no_low_confidence_warning():
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Did you mean 1941 or 1944?", context_ids=[], answer_ids=[], sources=[]), search_turn("Who won the Battle of Guam?", "Battle of Guam"))
    assert out["confidence"]["level"] == "low" and "low_confidence" not in out["warnings"]


# ---------------------------------------------------------------------------
# Map drawing of non-spatial entities (spec R23)

def test_r23_org_drawn_only_at_relevant_places():
    """Observed: "Who attacked Corregidor in 1942?" with org:ija drew 9 places (Kiska, Chongqing, ...)."""
    turn_ = search_turn("Who attacked Corregidor in 1942?", "Corregidor") + [
        msg("ai", tool_calls=[{"name": "find_relations", "args": {"target_id": "loc:corregidor"}, "id": "f"}]),
        msg("tool", json.dumps(STORE.find_relations(target_id="loc:corregidor", relation_type="ATTACKED", date_from="1942", date_to="1942")), name="find_relations"),
    ]
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="The IJA.", context_ids=[], answer_ids=["loc:corregidor", "org:ija"], sources=[]), turn_)
    assert [f["properties"]["id"] for f in out["geojson"]["features"]] == ["loc:corregidor"]
    assert len(STORE.to_geojson(["loc:corregidor", "org:ija"])["features"]) == 9  # unrestricted call unchanged


def test_r23_event_drawn_at_its_locations_from_tool_output():
    turn_ = search_turn("Where was the Guadalcanal campaign?", "Guadalcanal campaign") + [
        msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:guadalcanal_campaign"}, "id": "g"}]),
        msg("tool", json.dumps(STORE.locations_of("event:guadalcanal_campaign")), name="get_locations"),
    ]
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Guadalcanal.", context_ids=[], answer_ids=["event:guadalcanal_campaign"], sources=[]), turn_)
    drawn = {f["properties"]["id"]: f["properties"].get("related_to") for f in out["geojson"]["features"]}
    assert drawn == {"loc:guadalcanal": "event:guadalcanal_campaign", "loc:tulagi": "event:guadalcanal_campaign",
                     "loc:henderson_field": "event:guadalcanal_campaign"}


def test_answer_schema_requires_ids_and_sources():
    """GLM omitted optional fields and returned answers with nothing on the map (eval: 6 of 76 answers)."""
    assert set(AgentAnswer.model_json_schema()["required"]) == {"answer", "answer_ids", "context_ids", "sources", "answer_sets"}


# ---------------------------------------------------------------------------
# Answer ids vs context ids (spec R24)

def corregidor_relations_turn():
    rels = STORE.find_relations(target_id="loc:corregidor", date_from="1942", date_to="1942")  # ATTACKED, CAPTURED, DEFENDED
    return search_turn("Who attacked Corregidor in 1942?", "Corregidor") + [
        msg("ai", tool_calls=[{"name": "find_relations", "args": {"target_id": "loc:corregidor"}, "id": "f"}]),
        msg("tool", json.dumps(rels), name="find_relations"),
    ]


def test_r24_answer_and_context_ids():
    parsed = AgentAnswer(answer_sets=[], answer="The Imperial Japanese Army; the US Army defended.", answer_ids=["org:ija"],
                         context_ids=["loc:corregidor", "org:us_army", "org:ija"], sources=[])
    out = finalize_answer(STORE, parsed, corregidor_relations_turn())
    assert out["answer_ids"] == ["org:ija"]
    assert out["context_ids"] == ["loc:corregidor", "org:us_army"]  # an id in both lists counts as answer
    assert out["entity_ids"] == ["org:ija", "loc:corregidor", "org:us_army"]  # union, answer first
    roles = {f["properties"]["id"]: f["properties"]["role"] for f in out["geojson"]["features"]}
    assert roles == {"loc:corregidor": "answer"}  # drawn once, on behalf of the attacker


def test_r24_feature_roles_split_answer_and_context_places():
    turn_ = search_turn("Where did the Guadalcanal campaign take place?", "Guadalcanal campaign") + [
        msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:guadalcanal_campaign"}, "id": "g"}]),
        msg("tool", json.dumps(STORE.locations_of("event:guadalcanal_campaign")), name="get_locations"),
    ]
    parsed = AgentAnswer(answer_sets=[], answer="Tulagi.", answer_ids=["loc:tulagi"], context_ids=["loc:guadalcanal", "event:guadalcanal_campaign"], sources=[])
    out = finalize_answer(STORE, parsed, turn_)
    roles = {f["properties"]["id"]: f["properties"]["role"] for f in out["geojson"]["features"]}
    assert roles["loc:tulagi"] == "answer" and roles["loc:guadalcanal"] == "context"


def test_r24_unsupported_ids_are_dropped_from_either_list():
    parsed = AgentAnswer(answer_sets=[], answer="x", answer_ids=["org:ija", "org:korean_army"], context_ids=["loc:tokyo"], sources=[])
    out = finalize_answer(STORE, parsed, corregidor_relations_turn())
    assert out["answer_ids"] == ["org:ija"] and out["context_ids"] == []
    assert {d["id"] for d in out["dropped_ids"]} == {"org:korean_army", "loc:tokyo"}


# ---------------------------------------------------------------------------
# Result sets in the final answer (spec R27)

from geo_agent.graph_store import ResultSets
from geo_agent.tools import build_tools


def set_turn(sets, question="Show me all locations connected to Admiral Isoroku Yamamoto."):
    t = {x.name: x for x in build_tools(STORE, sets)}
    payload = t["connected_locations"].invoke({"entity_id": "per:yamamoto"})
    turn_ = search_turn(question, "Isoroku Yamamoto") + [
        msg("ai", tool_calls=[{"name": "connected_locations", "args": {"entity_id": "per:yamamoto"}, "id": "c"}]),
        msg("tool", payload, name="connected_locations"),
    ]
    return turn_, json.loads(payload)["result_set"]


def test_r27_answer_set_expands_to_the_map():
    sets = ResultSets()
    turn_, handle = set_turn(sets)
    parsed = AgentAnswer(answer="Five places, via Midway and Pearl Harbor.", answer_ids=[], context_ids=["per:yamamoto"],
                         sources=[], answer_sets=[handle])
    out = finalize_answer(STORE, parsed, turn_, sets)
    expected = {"loc:pearl_harbor", "loc:hickam_field", "loc:midway_atoll", "loc:sand_island", "loc:eastern_island"}
    assert set(out["answer_ids"]) == expected
    features = {f["properties"]["id"]: f["properties"] for f in out["geojson"]["features"]}
    assert set(features) == expected and all(p["role"] == "answer" for p in features.values())
    assert features["loc:midway_atoll"]["via"].startswith("Isoroku Yamamoto -COMMANDED-> Battle of Midway")
    assert out["result_sets"] == [{"handle": handle, "label": "locations connected to Isoroku Yamamoto", "total": 5,
                                   "on_map": 5, "map_truncated": False}]
    assert out["confidence"]["basis"]["max_edge_count"] >= 3 and "result_set_unknown" not in out["warnings"]


def test_r27_unknown_or_unseen_handles_are_ignored():
    sets = ResultSets()
    turn_, handle = set_turn(sets)
    other = sets.add(["loc:tokyo"], "never shown to the model")
    parsed = AgentAnswer(answer="x", answer_ids=[], context_ids=[], sources=[], answer_sets=["rs-nope00", other])
    out = finalize_answer(STORE, parsed, turn_, sets)
    assert out["answer_ids"] == [] and out["result_sets"] == []
    assert out["dropped_sets"] == [{"handle": "rs-nope00", "reason": "unknown"}, {"handle": other, "reason": "not_in_tool_output"}]
    assert "result_set_unknown" in out["warnings"]


def test_r27_map_cap(monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_MAP_FEATURES", 3)
    sets = ResultSets()
    turn_, handle = set_turn(sets)
    out = finalize_answer(STORE, AgentAnswer(answer="x", answer_ids=[], context_ids=[], sources=[], answer_sets=[handle]), turn_, sets)
    assert len(out["geojson"]["features"]) == 3 and out["result_sets"][0]["map_truncated"] is True


# Approximations and bounded context (spec R29, R30)

def test_r30_finalize_flags_approximations_and_caps_text_based_confidence():
    sets = ResultSets()
    t = {x.name: x for x in build_tools(STORE, sets)}
    payload = t["entities_mentioning"].invoke({"phrase": "island hopping"})
    turn_ = [msg("human", "Show me all locations associated with the island-hopping campaign."),
             msg("ai", tool_calls=[{"name": "entities_mentioning", "args": {"phrase": "island hopping"}, "id": "m"}]),
             msg("tool", payload, name="entities_mentioning")]
    parsed = AgentAnswer(answer="Approximate: ...", answer_ids=[], context_ids=[], sources=[], answer_sets=[json.loads(payload)["result_set"]])
    out = finalize_answer(STORE, parsed, turn_, sets)
    assert "approximation_used" in out["warnings"]
    assert any(line.startswith("approximation: 'island hopping' is not a graph entity") for line in out["interpretation"])
    assert out["confidence"]["level"] == "low" and out["confidence"]["basis"]["text_based_approximation"] is True


def test_r29_prompt_states_the_set_pattern():
    assert "answer_sets" in agent_mod.SYSTEM_PROMPT and "never list every place" in agent_mod.SYSTEM_PROMPT


def test_r29_context_editing_keeps_set_tool_outputs(monkeypatch):
    captured = {}
    real = agent_mod.create_agent
    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(agent_mod, "create_agent", spy)
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    class Fake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kw): return self
    asyncio.run(GeoAgent.create(graph_path=GRAPH, llm=Fake(responses=[])))
    from langchain.agents.middleware import ContextEditingMiddleware
    cem = next(m for m in captured["middleware"] if isinstance(m, ContextEditingMiddleware))
    assert set(agent_mod.SET_TOOLS) <= set(cem.edits[0].exclude_tools)


def test_a_plan_is_not_accepted_as_the_answer():
    """Eval set-7 / set-9: GLM submitted its next step as the final answer."""
    import pydantic
    for plan in ["The island-hopping campaign is a clear match in the graph. Let me retrieve all locations associated with it.",
                 "I found the Manhattan Project. I will now look up its locations."]:
        with pytest.raises(pydantic.ValidationError):
            AgentAnswer(answer=plan, answer_ids=[], context_ids=[], sources=[], answer_sets=[])
    for ok in ["Let me know if you want the 1944 battle instead.", "Five places: Pearl Harbor, Hickam Field and three at Midway."]:
        assert AgentAnswer(answer=ok, answer_ids=[], context_ids=[], sources=[], answer_sets=[]).answer == ok


def test_plan_detection_covers_other_verbs():
    import pydantic
    with pytest.raises(pydantic.ValidationError):  # eval set-9, step 17b
        AgentAnswer(answer="I found the Manhattan Project. Let me pull up all locations connected to each.",
                    answer_ids=[], context_ids=[], sources=[], answer_sets=[])
    for ok in ["Let me clarify: there were two battles of Guam.", "Let me know which battle you mean."]:
        assert AgentAnswer(answer=ok, answer_ids=[], context_ids=[], sources=[], answer_sets=[]).answer == ok
