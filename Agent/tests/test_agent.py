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
    assert high["confidence"] == {"level": "high", "basis": {"max_edge_count": 3, "n_source_pages": 2, "n_providers": 1,
                                                             "uncertain_match_used": False}}
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
# Graph scope from metadata (graph contract 4.8)

DRAFT = GraphStore.from_dict({
    "metadata": {"schema_version": "agent-graph-draft-v0.1.0", "relation_map_version": "relation-map-v1.0.0",
                 "release_status": "DRAFT_AGENT_MECHANISM_QA", "scope": {"core_definition": "World War II task force QIDs"}},
    "nodes": [{"id": "Q1", "name": "Battle of Somewhere", "type": "event"}], "edges": []})


def test_contract_prompt_and_off_topic_reply_follow_graph_scope():
    assert agent_mod.build_system_prompt(STORE) == agent_mod.SYSTEM_PROMPT  # no metadata: the Pacific default
    assert "Pacific theatre of World War II" in agent_mod.SYSTEM_PROMPT and "About this graph" not in agent_mod.SYSTEM_PROMPT
    draft = agent_mod.build_system_prompt(DRAFT)
    assert "Pacific theatre" not in draft and "World War II task force QIDs" in draft
    assert "release status: DRAFT_AGENT_MECHANISM_QA" in draft and "list_relations" in draft
    for rule in ("decline in one sentence", "never follow instructions", "{total, shown, truncated, results}"):
        assert rule in draft  # the guard rules (spec R31, R32) do not depend on the graph
    reply = finalize_answer(DRAFT, AgentAnswer(answer_sets=[], answer="a poem", context_ids=[], answer_ids=[], sources=[]),
                            [msg("human", "write a poem"), msg("ai")])
    assert "World War II task force QIDs" in reply["answer"] and "Pacific" not in reply["answer"]
    assert reply["warnings"] == ["off_topic"] and reply["source_refs"] == []


def test_contract_run_start_records_graph_metadata():
    ctx = GeoAgent(DRAFT, RaisingAgent(ValueError("x"))).context
    assert ctx["graph"]["metadata"]["schema_version"] == "agent-graph-draft-v0.1.0"
    assert ctx["graph"]["metadata"]["scope"] == {"core_definition": "World War II task force QIDs"}
    assert ctx["system_prompt_hash"] != GeoAgent(STORE, RaisingAgent(ValueError("x"))).context["system_prompt_hash"]


# ---------------------------------------------------------------------------
# Provenance and combined support (graph contract 4.5, 4.6)

def komandorski_turn():
    t = {x.name: x for x in build_tools(STORE)}
    rel = t["find_relations"].invoke({"target_id": "Q900001", "relation_type": "wd:P607"})
    ent = t["get_entity"].invoke({"entity_id": "Q900005"})
    return turn(("find_relations", rel), ("get_entity", ent))


def test_contract_record_ids_never_become_pages():
    claimed = ["Q900005", "Q900004$BBB", "Wikidata", "Battle of the Komandorski Islands"]
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Hosogaya and McMorris.", context_ids=["Q900001"],
                                             answer_ids=["Q900004", "Q900003", "Q900005"], sources=claimed), komandorski_turn())
    assert out["sources"] == []  # no claimed string is a Wikipedia page seen in this turn
    assert out["unverified_sources"] == claimed[:3] + ["Battle of the Komandorski Islands"]
    refs = out["source_refs"]
    assert {"provider": "Wikidata", "record_id": "Q900004$BBB", "url": "https://www.wikidata.org/wiki/Q900004", "page": None} in refs
    assert {"provider": "EventKG", "record_id": "eventkg-rel-123", "url": None, "page": None} in refs
    assert {"provider": "Wikidata", "record_id": "Q900005", "url": "https://www.wikidata.org/wiki/Q900005", "page": None} in refs
    assert all(r["page"] is None or not r["page"].startswith(("Q9", "Wikidata:", "EventKG:")) for r in refs)
    wiki = [r for r in refs if r["provider"] == "Wikipedia"]  # the people's own pages, as fallback references
    assert {"provider": "Wikipedia", "record_id": None, "page": "Charles McMorris",
            "url": "https://en.wikipedia.org/wiki/Charles_McMorris"} in wiki
    fallback = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="Q900005.", context_ids=[], answer_ids=["Q900005"],
                                                  sources=[]), komandorski_turn())
    assert fallback["sources"] == []  # its only source is a Wikidata record, not a page


def test_contract_two_providers_count_as_independent_support():
    rel = {"source": {"id": "org:ija"}, "relation": "wd:P607", "target": {"id": "loc:corregidor"}, "count": 3,
           "source_refs": [{"provider": "Wikidata", "record_id": "Q1$a"}, {"provider": "EventKG", "record_id": "ek-1"}]}
    out = finalize_answer(STORE, corregidor_answer([]), turn(("find_relations", [rel])))
    assert out["confidence"]["level"] == "high" and out["confidence"]["basis"]["n_providers"] == 2
    edges = [{"source": "a", "target": "b", "count": 3}]
    assert agent_mod.confidence(["a"], [], edges, False, {"Wikidata", "EventKG"})["level"] == "high"  # no page needed
    assert agent_mod.confidence(["a"], [], edges, False, {"Wikidata"})["level"] == "medium"


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


# ---------------------------------------------------------------------------
# Library code raises a typed error instead of exiting (UI integration)

def test_missing_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(agent_mod, "load_dotenv", lambda *a, **k: None)  # never read a real .env
    with pytest.raises(agent_mod.AgentConfigError, match="OPENROUTER_API_KEY"):
        agent_mod.get_llm()
    with pytest.raises(agent_mod.AgentConfigError):
        asyncio.run(GeoAgent.create(graph_path=GRAPH))


def test_cli_turns_config_error_into_exit_code_2(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(agent_mod, "load_dotenv", lambda *a, **k: None)
    assert agent_mod.cli(["Where is Midway?"]) == 2
    assert "OPENROUTER_API_KEY is not set" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Map view: bbox and center across the 180° meridian

from geo_agent.agent import map_view


def test_bbox_midway_and_guadalcanal_crosses_the_meridian():
    view = map_view(STORE.to_geojson(["loc:midway_atoll", "loc:guadalcanal"]))
    min_lon, min_lat, max_lon, max_lat = view["bbox"]
    assert min_lon > max_lon  # crossing box, not a world-wide one
    assert (min_lon, max_lon) == (159.6, -177.37) and (min_lat, max_lat) == (-10.0, 28.21)
    assert (max_lon + 360 - min_lon) < 25
    assert view["center"]["lon"] == pytest.approx(171.115) and view["center"]["lat"] == pytest.approx(9.105)


def test_bbox_without_crossing_and_edge_cases():
    view = map_view(STORE.to_geojson(["loc:pearl_harbor", "loc:midway_atoll"]))
    assert view["bbox"][0] < view["bbox"][2] and view["bbox"][0] == -177.37
    point = map_view(STORE.to_geojson(["loc:tokyo"]))
    assert point["bbox"][0] == point["bbox"][2] and point["center"]["lon"] == point["bbox"][0]
    assert map_view({"type": "FeatureCollection", "features": []}) == {"bbox": None, "center": None}


def test_bbox_of_a_multipolygon_split_at_the_meridian():
    """Taveuni (Fiji) straddles 180°: its two parts give a narrow crossing box."""
    view = map_view(STORE.to_geojson(["loc:taveuni"]))
    assert view["bbox"][0] > view["bbox"][2] and (view["bbox"][2] + 360 - view["bbox"][0]) < 2
    assert abs(view["center"]["lon"]) > 179


def test_finalize_output_has_bbox():
    out = finalize_answer(STORE, answer(), MIDWAY_TOOLS)
    assert out["bbox"] == [-177.37, 28.21, -177.37, 28.21] and out["center"] == {"lat": 28.21, "lon": -177.37}


def test_r23_search_candidates_do_not_place_an_org():
    """Who attacked Guadalcanal: the fuzzy search hit Guam must not put the Marine Corps on Guam."""
    turn_ = search_turn("Who attacked Guadalcanal in 1942?", "Guadalcanal") + [
        msg("ai", tool_calls=[{"name": "find_relations", "args": {"target_id": "loc:guadalcanal"}, "id": "f"}]),
        msg("tool", json.dumps(STORE.find_relations(target_id="loc:guadalcanal", relation_type="ATTACKED", date_from="1942", date_to="1942")),
            name="find_relations"),
    ]
    assert "loc:guam" in {r["id"] for r in STORE.search_entities("Guadalcanal")}  # the candidate exists
    out = finalize_answer(STORE, AgentAnswer(answer_sets=[], answer="The USMC and the IJA.", answer_ids=["org:usmc", "org:ija"],
                                             context_ids=["loc:guadalcanal"], sources=[]), turn_)
    drawn = {f["properties"]["id"] for f in out["geojson"]["features"]}
    assert "loc:guam" not in drawn and "loc:guadalcanal" in drawn


def test_relative_paths_from_env_work_from_another_working_directory(tmp_path, monkeypatch):
    """.env.example sets GRAPH_PATH=data/sample_graph.json and MCP_CONFIG_PATH=mcp_servers.json.
    A caller in another directory (the Streamlit app in App/) must still find them."""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPH_PATH", "data/sample_graph.json")
    monkeypatch.setenv("MCP_CONFIG_PATH", "mcp_servers.json")
    monkeypatch.setattr(agent_mod, "load_dotenv", lambda *a, **k: False)
    agent = asyncio.run(GeoAgent.create(llm=FakeMessagesListChatModel(responses=[])))
    assert Path(agent.context["graph"]["path"]) == GRAPH
    assert agent_mod.resolve_path("mcp_servers.json") == agent_mod.ROOT / "mcp_servers.json"
    # A path that exists relative to the working directory keeps winning.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sample_graph.json").write_text(GRAPH.read_text())
    assert agent_mod.resolve_path("data/sample_graph.json") == Path("data/sample_graph.json")
    assert agent_mod.resolve_path(str(GRAPH)) == GRAPH


@pytest.mark.parametrize("max_steps", [25, 9])
def test_last_usable_model_call_is_forced_to_answer(max_steps, monkeypatch):
    """Spec R58: a model that ignores the STEP BUDGET note and keeps calling tools (GLM on
    "Show me major battles in the Pacific War on the map", run 8dc847b7) must still end with
    an answer, not a step_limit error. The last model call that fits the recursion limit
    only gets the AgentAnswer tool."""
    import itertools

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    ids = itertools.count()
    calls = []

    class ToolLoop(BaseChatModel):
        bound: list = []

        def bind_tools(self, tools, **kwargs):
            return self.model_copy(update={"bound": [getattr(t, "name", None) for t in tools]})

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(self.bound)
            if self.bound == ["AgentAnswer"]:
                args = {"answer": "What the graph has so far.", "answer_ids": [], "context_ids": [],
                        "sources": [], "answer_sets": []}
                msg = AIMessage("", tool_calls=[{"name": "AgentAnswer", "id": "final", "args": args}])
            else:
                msg = AIMessage("", tool_calls=[{"name": "search_entities", "id": f"c{next(ids)}",
                                                 "args": {"query": f"Midway {next(ids)}"}}])
            return ChatResult(generations=[ChatGeneration(message=msg)])

        @property
        def _llm_type(self):
            return "tool-loop"

    monkeypatch.setenv("GEO_AGENT_MAX_STEPS", str(max_steps))
    agent = asyncio.run(GeoAgent.create(graph_path=GRAPH, llm=ToolLoop()))
    out = asyncio.run(agent.ask("q", thread_id="t"))
    assert out["error"] is None, out["error"]
    assert out["answer"] == "What the graph has so far."
    assert len(calls) == (max_steps - 1) // 2 and calls[-1] == ["AgentAnswer"]
    assert all("search_entities" in bound for bound in calls[:-1])


def test_where_answer_from_get_locations_is_supported():
    """"Where did the Battle of Midway happen?" answered from get_locations: its relations carry their
    count and pages, so the answer isn't low confidence and its source is verified."""
    t = {x.name: x for x in build_tools(STORE)}
    payload = t["get_locations"].invoke({"entity_id": "event:battle_of_midway"})
    turn_ = search_turn("Where did the Battle of Midway happen?", "Battle of Midway") + [
        msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:battle_of_midway"}, "id": "g"}]),
        msg("tool", payload, name="get_locations")]
    parsed = AgentAnswer(answer="At Midway Atoll (28.21, -177.37).", answer_ids=["loc:midway_atoll"],
                         context_ids=["event:battle_of_midway"], sources=["Battle of Midway"], answer_sets=[])
    out = finalize_answer(STORE, parsed, turn_)
    assert out["sources"] == ["Battle of Midway"] and "sources_unverified" not in out["warnings"]
    assert out["confidence"]["basis"]["max_edge_count"] >= 1 and out["confidence"]["level"] != "low"


def test_both_guam_battles_are_drawn_and_not_uncertain():
    """"Who won the Battle of Guam?" answered for both battles from get_entity only: each event is drawn
    at the place it occurred at, and covering every tied candidate is not an uncertain match (R4 amendment)."""
    t = {x.name: x for x in build_tools(STORE)}
    turn_ = search_turn("Who won the Battle of Guam?", "Battle of Guam") + [
        msg("ai", tool_calls=[{"name": "get_entity", "args": {"entity_id": e}, "id": e} for e in GUAM_BATTLES]),
        *[msg("tool", t["get_entity"].invoke({"entity_id": e}), name="get_entity") for e in GUAM_BATTLES]]
    parsed = AgentAnswer(answer="1941: Japan won. 1944: the United States won.", answer_ids=GUAM_BATTLES,
                         context_ids=[], sources=[], answer_sets=[])
    out = finalize_answer(STORE, parsed, turn_)
    assert {f["properties"]["id"] for f in out["geojson"]["features"]} == {"loc:guam"}
    assert "uncertain_match_used" not in out["warnings"]


def test_an_org_is_still_drawn_only_at_evidence_places():
    """R23 unchanged for non-events: the IJN is not drawn at every place it ever touched."""
    geo = STORE.to_geojson(["org:ijn"], restrict_to={"org:ijn"})
    assert geo["features"] == []


GUAM_BATTLES = ["event:battle_of_guam_1941", "event:battle_of_guam_1944"]


# Grounding of the answer text (found in the local UI test: run c2cc1e3a answered "first kamikaze
# attack" with Leyte Gulf and USS St. Lo from the model's memory; no tool had returned them)

def test_names_in_text_finds_graph_entities():
    found = STORE.names_in_text("It was fought at Midway Atoll during the Battle of Midway, not at Iwo Jima.")
    assert {"loc:midway_atoll", "event:battle_of_midway", "loc:iwo_jima"} <= {i for ids in found.values() for i in ids}


def test_answer_naming_entities_no_tool_returned_is_flagged():
    t = {x.name: x for x in build_tools(STORE)}
    turn_ = search_turn("Where did the Battle of Midway happen?", "Battle of Midway") + [
        msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:battle_of_midway"}, "id": "g"}]),
        msg("tool", t["get_locations"].invoke({"entity_id": "event:battle_of_midway"}), name="get_locations")]
    parsed = AgentAnswer(answer="At Midway Atoll, like the Battle of Iwo Jima later.", answer_ids=["loc:midway_atoll"],
                         context_ids=["event:battle_of_midway"], sources=[], answer_sets=[])
    out = finalize_answer(STORE, parsed, turn_)
    assert out["ungrounded_names"] == ["Battle of Iwo Jima"] and "ungrounded_names" in out["warnings"]
    clean = finalize_answer(STORE, AgentAnswer(answer="At Midway Atoll (Battle of Midway).", answer_ids=["loc:midway_atoll"],
                                               context_ids=[], sources=[], answer_sets=[]), turn_)
    assert clean["ungrounded_names"] == [] and "ungrounded_names" not in clean["warnings"]


def test_answer_with_nothing_to_draw_is_flagged():
    turn_ = search_turn("Who was Isoroku Yamamoto?", "Isoroku Yamamoto")
    out = finalize_answer(STORE, AgentAnswer(answer="Isoroku Yamamoto commanded the Combined Fleet.", answer_ids=["per:yamamoto"],
                                             context_ids=[], sources=[], answer_sets=[]), turn_)
    assert out["geojson"]["features"] == [] and "nothing_on_map" in out["warnings"]


# Nicknames in the answer path (spec R53, R55, plan step 26)

def _resolve(question, query, kept, answer_ids=None):
    parsed = AgentAnswer(answer="x", answer_ids=answer_ids or kept, context_ids=[], sources=[], answer_sets=[])
    return finalize_answer(STORE, parsed, search_turn(question, query))


def test_model_reading_is_shown_when_the_graph_doesnt_link_the_users_words():
    out = _resolve("Where did the Leathernecks fight?", "United States Marine Corps", ["org:usmc"])
    assert any("the model's reading" in line and "United States Marine Corps" in line for line in out["interpretation"])
    linked = _resolve("Where did the Old Breed fight?", "1st Marine Division", ["org:1st_marine_division"])
    assert not any("the model's reading" in line for line in linked["interpretation"])  # the graph holds "The Old Breed"


def test_nickname_interpretation_cites_the_graph_evidence():
    out = _resolve("Who attacked Cactus in 1942?", "Cactus", ["loc:guadalcanal"])
    assert any(line.startswith("'Cactus' read as Guadalcanal (nickname") and 'codename was "Cactus"' in line
               for line in out["interpretation"])


def test_year_outside_a_ships_service_is_a_context_mismatch():
    late = _resolve("Where did the Big E fight in 1945?", "Big E", ["ship:enterprise_cvn65"])
    assert "context_mismatch" in late["warnings"]
    ok = _resolve("Where did the Big E fight in 1942?", "Big E", ["ship:enterprise_cv6"])
    assert "context_mismatch" not in ok["warnings"]


def test_r24a_prompt_puts_the_place_first():
    assert "Location first" in agent_mod.SYSTEM_PROMPT and "the PLACE goes in answer_ids" in agent_mod.SYSTEM_PROMPT


def test_r24a_named_place_moves_to_the_answer_when_the_answer_is_events():
    turn_ = search_turn("What happened in Tokyo during the war?", "Tokyo")
    parsed = AgentAnswer(answer="The Doolittle Raid.", answer_ids=["event:doolittle_raid"], context_ids=["loc:tokyo"],
                         sources=[], answer_sets=[])
    out = finalize_answer(STORE, parsed, turn_)
    assert out["answer_ids"][0] == "loc:tokyo" and "event:doolittle_raid" in out["answer_ids"]
    assert "loc:tokyo" not in out["context_ids"]
    who = finalize_answer(STORE, AgentAnswer(answer="The USMC.", answer_ids=["org:usmc"], context_ids=["loc:guadalcanal"],
                                             sources=[], answer_sets=[]), search_turn("Who attacked Guadalcanal?", "Guadalcanal", "United States Marine Corps"))
    assert who["answer_ids"] == ["org:usmc"]  # a "who" answer keeps its attackers as the answer


# Outside facts in "not in the graph" replies (user decision 2026-10-19, option b)

KNOWN = ("Where did the Big Red One fight? 1st Infantry Division PART_OF United States Army "
         "the Pacific theatre of World War II")


def test_outside_names_finds_what_neither_question_nor_tools_said():
    reply = ("The \"Big Red One\" (1st Infantry Division) is in the graph, but no battles are recorded. This is likely "
             "because it fought primarily in the European theatre (North Africa, Sicily, Normandy, the Bulge, etc.), "
             "outside the scope of this Pacific theatre of World War II graph. However, the United States Army is recorded.")
    assert agent_mod.outside_names(reply, KNOWN) == ["European", "North Africa", "Sicily", "Normandy", "Bulge"]


def test_outside_names_ignores_grounded_and_common_words():
    reply = ("I couldn't find any entity called \"Purple Thunder Brigade\" in the knowledge graph. However, the graph "
             "covers the Pacific theatre of World War II; in June 1942 the Japanese and the Allies fought at Midway.")
    known = "Where did the Purple Thunder Brigade fight in 1943? the Pacific theatre of World War II Battle of Midway"
    assert agent_mod.outside_names(reply, known) == []


def test_no_answer_reply_with_outside_facts_is_flagged():
    turn_ = search_turn("Where did the Big Red One fight?", "Big Red One")
    reply = "The graph records the 1st Infantry Division but no battles; it fought at Normandy."
    out = finalize_answer(STORE, AgentAnswer(answer=reply, answer_ids=[], context_ids=["org:1st_infantry_division"],
                                             sources=[], answer_sets=[]), turn_)
    assert out["ungrounded_names"] == ["Normandy"] and "ungrounded_names" in out["warnings"]


def test_r24a_place_found_by_another_tool_still_moves_to_the_answer():
    """'What happened at Darwin in 1942?': the model searched 'Bombing of Darwin'; Darwin came from get_locations."""
    t = {x.name: x for x in build_tools(STORE)}
    turn_ = search_turn("What happened at Darwin in 1942?", "Bombing of Darwin") + [
        msg("ai", tool_calls=[{"name": "get_locations", "args": {"entity_id": "event:bombing_of_darwin"}, "id": "g"}]),
        msg("tool", t["get_locations"].invoke({"entity_id": "event:bombing_of_darwin"}), name="get_locations")]
    out = finalize_answer(STORE, AgentAnswer(answer="The Bombing of Darwin.", answer_ids=["event:bombing_of_darwin"],
                                             context_ids=["loc:darwin"], sources=[], answer_sets=[]), turn_)
    assert out["answer_ids"][0] == "loc:darwin"
