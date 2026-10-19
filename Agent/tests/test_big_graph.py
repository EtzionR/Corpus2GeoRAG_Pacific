"""Opt-in checks on the graph team's full export (contract update request, section 6).

Skipped unless GEO_AGENT_BIG_GRAPH points at the graph JSON, e.g.
    GEO_AGENT_BIG_GRAPH=/path/to/wwii_agent_graph_draft_v0.1.0.json uv run pytest tests/test_big_graph.py
Loading takes ~20 s and ~3 GB of memory. No LLM, no network.
Check 1 (the sample graph still loads and passes) is the default suite.
"""

import json
import os
import re
import time
from types import SimpleNamespace

import pytest

from geo_agent.agent import AgentAnswer, build_system_prompt, finalize_answer
from geo_agent.graph_store import GraphStore
from geo_agent.tools import build_tools

BIG = os.getenv("GEO_AGENT_BIG_GRAPH")
pytestmark = pytest.mark.skipif(not BIG, reason="set GEO_AGENT_BIG_GRAPH=<graph json> to run")

RECORD_PAGE = re.compile(r"^(Wikidata|EventKG|WarSampo):|^Q\d+(\$.*)?$|\$")
# Counts from the graph team's request (section 3) for this build; other builds only get the consistency checks.
REQUEST_COUNTS = {"wwii-agent-draft-v0.1.0": {"nodes_type_other": 110744, "nodes_name_is_id": 92307,
                                              "nodes_without_text": 94444, "locations_without_geometry": 663,
                                              "locations_with_geometry": 509, "non_location_with_geometry": 7096}}


@pytest.fixture(scope="module")
def store():
    t = time.perf_counter()
    s = GraphStore.from_json(BIG)
    s.load_seconds = time.perf_counter() - t
    return s


@pytest.fixture(scope="module")
def tools(store):
    return {t.name: t for t in build_tools(store)}


def call(tools, name, **args):
    return json.loads(tools[name].invoke(args))


def find_edge(store, pred):
    return next(e for e in store.edges if pred(e))


# 2. The draft loads without dropped nodes or edges.
def test_no_dropped_nodes_or_edges(store):
    assert len(store.nodes) == store.input_counts["nodes"]
    assert len(store.edges) == store.input_counts["edges"] and store.dropped_edges == 0
    assert all(e.id for e in store.edges) and len({e.id for e in store.edges}) == len(store.edges)
    assert store.load_seconds < 60


# 3. find_relations(relation_type="wd:P607") returns matching relations.
def test_exact_level2_relation(tools):
    out = call(tools, "find_relations", relation_type="wd:P607")
    assert out["total"] > 1000 and all(r["relation"] == "wd:P607" for r in out["results"])


# 4. A Level 1 family query returns all valid Level 2 relations in that family.
def test_family_query(store, tools):
    family = "CONFLICT_AFFILIATION"
    level2 = {t for t, c in store.catalog.items() if c["family"] == family}
    listed = {r["id"]: r for r in call(tools, "list_relations", family=family)["results"]}
    assert level2 <= set(listed)  # every catalog member of the family
    hits = store.find_relation_edges(relation_type=family)
    assert call(tools, "find_relations", relation_type=family)["total"] == len(hits) == len(store.by_family[family])
    assert {e.type for e in hits} <= set(listed)  # every relation the filter returns is listed
    extra = set(listed) - level2  # listed because their edges carry this family, not the catalog's
    assert all(family in listed[t]["edge_families"] for t in extra)


# 5. An edge with only point_date in 1942 matches a 1942 query.
def test_point_date_matches_year(store, tools):
    e = find_edge(store, lambda e: not (e.start_date or e.end_date) and (e.point_date or "").startswith("1942-06"))
    rows = call(tools, "find_relations", source_id=e.source, target_id=e.target, relation_type=e.type, year=1942)["results"]
    assert any(r["point_date"] == e.point_date for r in rows)
    assert call(tools, "find_relations", source_id=e.source, target_id=e.target, relation_type=e.type,
                year=1950).get("result", "").startswith("no ")


# 6. Source output separates provider, record id, URL, Wikipedia page and paragraph.
def test_provenance_fields(store):
    wd = find_edge(store, lambda e: e.attributes.get("selected_source") == "Wikidata")
    row = store.summarize_edge(wd)
    assert row["providers"] == ["Wikidata"] and row["sources"] == []
    view = wd.sources[0].view()
    assert view["provider"] == "Wikidata" and view["record_id"] == wd.attributes["source_record_id"] and "page" not in view
    assert view["url"].startswith("https://www.wikidata.org/wiki/Q")
    ek = find_edge(store, lambda e: e.attributes.get("selected_source") == "EventKG")
    assert ek.sources[0].provider == "EventKG" and ek.sources[0].record_id and ek.sources[0].page is None
    qid_only = next(n for n in store.nodes.values() if n.name == n.id)
    assert [s.view() for s in qid_only.sources] == [
        {"provider": "Wikidata", "record_id": qid_only.id, "url": f"https://www.wikidata.org/wiki/{qid_only.id}"}]
    named = store.nodes["Q173034"]  # Battle of Midway
    assert named.sources[0].view()["page"] == "Battle of Midway" and named.sources[0].provider == "Wikipedia"


# 7. A Wikidata statement id is never called a Wikipedia page.
def test_record_ids_never_pages(store):
    for e in store.edges[::50]:
        for s in e.sources:
            assert s.page is None or not RECORD_PAGE.search(s.page), (e.id, s.page)
        assert not any(RECORD_PAGE.search(p) for p in store.summarize_edge(e).get("sources", []))
    for n in list(store.nodes.values())[::20]:
        assert all(s.page is None or not RECORD_PAGE.search(s.page) for s in n.sources), n.id
    t = {x.name: x for x in build_tools(store)}
    rel = t["find_relations"].invoke({"source_id": "Q152325", "relation_type": "wd:P607"})
    msgs = [SimpleNamespace(type="human", content="q", tool_calls=[], name=None),
            SimpleNamespace(type="ai", content="", tool_calls=[{"name": "find_relations", "args": {}, "id": "c"}], name=None),
            SimpleNamespace(type="tool", content=rel, tool_calls=[], name="find_relations")]
    first = json.loads(rel)["results"][0]
    statement = next(s.record_id for a in store.relation_groups(first["source"]["id"], first["relation"], first["target"]["id"])
                     for s in a.sources if s.provider == "Wikidata")
    out = finalize_answer(store, AgentAnswer(answer="x", answer_ids=["Q152325"], context_ids=[], sources=[statement], answer_sets=[]), msgs)
    assert out["sources"] == [] and out["unverified_sources"] == [statement]
    assert any(r["record_id"] == statement and r["page"] is None for r in out["source_refs"])


# 8. graph_schema() reports scope, schema version, relation-map version and Level 1 families.
def test_schema_reports_metadata_and_families(store, tools):
    s = call(tools, "graph_schema")
    meta = s["metadata"]
    assert {"schema_version", "relation_map_version", "release_status", "scope"} <= set(meta)
    assert set(s["relation_families"]) >= {"SPATIAL", "CONFLICT_AFFILIATION", "PARTICIPATION_ROLE"}
    assert len(s["relation_types"]) <= 40 and s["relation_types_total"] > 800
    assert len(json.dumps(s)) < 12000  # a short summary, not 810 definitions
    prompt = build_system_prompt(store)
    assert "Pacific theatre" not in prompt and str(meta["scope"]["core_definition"]) in prompt


# 9. Direct geometry and relation-derived geometry follow the geometry rule.
def test_geometry_rule(store):
    fc = store.to_geojson(["Q173034"])  # Battle of Midway: an event with its own coordinates
    assert [f["properties"]["geometry_source"] for f in fc["features"]] == ["direct"]
    person = next(n.id for n in store.nodes.values() if n.type == "person" and n.id not in store.geoms
                  and any(store.nodes[o].type == "location" and o in store.geoms for o in (e.target for e in store.out_edges[n.id])))
    related = store.to_geojson([person])["features"]
    assert related and all(f["properties"]["geometry_source"] == "related" and f["properties"]["related_to"] == person for f in related)
    for entity in ("Q152325", "Q127417", "Q184425", "Q219712"):  # Yamamoto, MacArthur, Pacific War, IJN
        rows = store.connected_locations(entity)
        assert rows and all(r["type"] in ("location", "other", "event") for r in rows)
        assert all(r["id"] in store.geoms or r["type"] == "location" for r in rows)
        assert all(r["type"] != "event" or r["id"] in store.geoms for r in rows)  # an event row is its own site


# 10. Missing names, text, types, dates and geometry are reported as data limitations.
def test_data_limits_reported(store, tools):
    cov = call(tools, "graph_schema")["coverage"]
    assert cov["edges_family_differs_from_catalog"] > 0 and cov["relation_types_not_in_catalog"] > 0  # reported upstream
    expected = REQUEST_COUNTS.get(store.metadata.get("build_version"))
    if expected:
        for key in ("nodes_type_other", "nodes_name_is_id", "nodes_without_text", "locations_without_geometry",
                    "locations_with_geometry"):
            assert cov[key] == expected[key], key
        assert sum(cov["non_location_nodes_with_geometry"].values()) == expected["non_location_with_geometry"]
    assert cov["events_without_dates"] > 0 and cov["edges_with_point_date_only"] > 40000
    assert cov["edges_with_interval_dates"] + cov["edges_with_point_date_only"] + cov["edges_without_dates"] == len(store.edges)
    qid_only = next(n.id for n in store.nodes.values() if n.name == n.id and n.type == "other")
    assert {"name_is_id", "no_text", "type_unresolved"} <= set(call(tools, "get_entity", entity_id=qid_only)["data_limits"])


def test_typical_tool_calls_are_fast(tools):
    calls = [("search_entities", {"query": "Battle of Midway"}), ("find_relations", {"relation_type": "wd:P607"}),
             ("get_neighbors", {"entity_id": "Q362"}), ("what_happened_at", {"lat": 28.2, "lon": -177.37, "radius_km": 300}),
             ("connected_locations", {"entity_id": "Q152325"}), ("entities_near", {"lat": 0, "lon": 150, "radius_km": 2000})]
    for name, args in calls:
        t = time.perf_counter()
        tools[name].invoke(args)
        assert time.perf_counter() - t < 2.0, name


# Nicknames the draft already holds (spec R51-R54): checked on the real data, where the article and
# partial-match rules matter most.
@pytest.mark.parametrize("query,name,match", [
    ("Mighty Mo", "USS Missouri", "alias"),
    ("Big E", "USS Enterprise", "alias"),              # the graph's alias is "The Big E"
    ("Howlin' Mad", "Holland Smith", "nickname"),      # quoted part of 'Holland McTyeire "Howlin' Mad" Smith'
    ("Howlin' Mad Smith", "Holland Smith", "alias_part"),
    ("Long Lance", "Type 93", "alias"),
])
def test_draft_nicknames_resolve_clearly(store, query, name, match):
    top = store.search_entities(query)[0]
    assert (top["name"], top["match"], top["quality"]) == (name, match, "clear")


def test_draft_nickname_rules_dont_mislead(store):
    assert store.search_entities("Bull Halsey")[0]["name"] != "Tupolev Tu-4"  # the Tu-4's alias "Bull": no partial credit
    assert store.search_entities("Old Breed")[0]["quality"] == "uncertain"     # only a book title holds it
    midway = store.search_entities("Battle of Midway")
    assert midway[0]["name"] == "Battle of Midway" and midway[0]["score"] == 100  # the article rule doesn't tie the film
