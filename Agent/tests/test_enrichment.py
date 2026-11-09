"""Build-time Wikidata enrichment (spec R74): canned Wikidata JSON, no network."""

import copy
import json

import pytest

from geo_agent.graph_store import GraphStore, iso_date
from graph_enrichment import enrich as en


def _time(t, precision=11):
    return {"snaktype": "value", "datavalue": {"type": "time", "value": {"time": t, "precision": precision}}}


def _item(qid):
    return {"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": qid}}}


def _claim(prop, snak, sid, qualifiers=None, rank="normal"):
    return {"id": sid, "rank": rank, "mainsnak": {**snak, "property": prop}, **({"qualifiers": qualifiers} if qualifiers else {})}


# The Battle of Iwo Jima as Wikidata has it, cut down; Q30 United States, Q188712 Empire of Japan,
# Q1067 Iwo Jima (island), Q1 Pacific War, Q217645 Nimitz, Q9 a participant not in the graph.
CLAIMS = {
    "Q180182": {
        "P31": [_claim("P31", _item("Q178561"), "s-p31")],
        "P580": [_claim("P580", _time("+1945-02-19T00:00:00Z"), "s-start1"),
                 _claim("P580", _time("+1945-02-08T00:00:00Z"), "s-start2")],
        "P582": [_claim("P582", _time("+1945-03-26T00:00:00Z"), "s-end")],
        "P710": [
            _claim("P710", _item("Q30"), "Q180182$us", {"P4791": [_item("Q217645")], "P580": [_time("+1945-02-19T00:00:00Z")]}),
            _claim("P710", _item("Q188712"), "Q180182$jp", {"P1037": [_item("Q9")]}),   # commander not in the graph
            _claim("P710", _item("Q9"), "Q180182$other"),                                # participant not in the graph
            _claim("P710", _item("Q30"), "Q180182$dep", rank="deprecated")],
        "P276": [_claim("P276", _item("Q1067"), "Q180182$loc")],
        "P361": [_claim("P361", _item("Q1"), "Q180182$partof")],
        "P17": [_claim("P17", _item("Q30"), "Q180182$country")],      # already in the graph: not added again
    },
    "Q1": {"P580": [_claim("P580", _time("+1941-01-01T00:00:00Z", 9), "s-pw")]},   # year precision
    "Q1067": {"P585": [_claim("P585", _time("+1945-02-23T00:00:00Z"), "s-point")]},
    "Q217645": {"P607": [_claim("P607", _item("Q180182"), "Q217645$iwo")]},          # existing as an inverse view
}


def _graph():
    pt = {"type": "Point", "coordinates": [141.3, 24.8]}
    return {
        "metadata": {"scope": "test"},
        "relation_catalog": {"wd:P710": {"id": "wd:P710", "level1_families": ["PARTICIPATION_ROLE"]}},
        "nodes": [
            {"id": "Q180182::LOC", "wikidata_id": "Q180182", "name": "Battle of Iwo Jima", "type": "location", "geometry": pt},
            {"id": "Q180182::MISC", "wikidata_id": "Q180182", "name": "Battle of Iwo Jima", "type": "MISC",
             "attributes": {"end_date": "+1945-03-27T00:00:00Z"}},                       # a date the graph has: kept
            {"id": "Q30::LOC", "wikidata_id": "Q30", "name": "United States", "type": "location"},
            {"id": "Q188712::ORG", "wikidata_id": "Q188712", "name": "Empire of Japan", "type": "org"},
            {"id": "Q1067::LOC", "wikidata_id": "Q1067", "name": "Iwo Jima", "type": "location", "geometry": pt},
            {"id": "Q1::event", "wikidata_id": "Q1", "name": "Pacific War", "type": "event"},
            {"id": "Q217645::PER", "wikidata_id": "Q217645", "name": "Chester W. Nimitz", "type": "person"},
            {"id": "loc:plain", "name": "No QID", "type": "location"}],
        "edges": [
            {"id": "obs-1", "source": "Q180182", "target": "Q30", "type": "wd:P17", "count": 1},
            {"id": "obs-2::REVERSE", "source": "Q180182", "target": "Q217645", "type": "inverse:wd:P607", "derived": True,
             "source_edge_id": "obs-2"}],
    }


SNAPSHOT = {"claims_sha256": "abc", "fetched_at": "2026-11-03T00:00:00+00:00"}


@pytest.fixture
def enriched():
    return en.enrich(_graph(), CLAIMS, SNAPSHOT)


def _node(g, node_id):
    return next(n for n in g["nodes"] if n["id"] == node_id)


def test_dates_are_added_only_where_missing_with_their_precision(enriched):
    loc, misc = _node(enriched, "Q180182::LOC"), _node(enriched, "Q180182::MISC")
    assert loc["attributes"]["start_date"] == "+1945-02-08T00:00:00Z" and loc["attributes"]["start_date_precision"] == 11
    assert loc["attributes"]["end_date"] == "+1945-03-26T00:00:00Z"
    assert misc["attributes"]["end_date"] == "+1945-03-27T00:00:00Z" and "end_date_precision" not in misc["attributes"]
    assert loc["attributes"]["enrichment_notes"] == ["P580 (start_date): 2 values on Wikidata, the earliest kept"]
    pw = _node(enriched, "Q1::event")["attributes"]
    assert pw["start_date"] == "+1941-00-00T00:00:00Z" and pw["start_date_precision"] == 9  # cut to the year
    assert _node(enriched, "Q1067::LOC")["attributes"]["point_date"] == "+1945-02-23T00:00:00Z"
    assert "attributes" not in _node(enriched, "loc:plain")


def test_the_agent_reads_the_enrichment_date_forms():
    """GraphStore.iso_date already reads Wikidata strings at every precision the tool writes."""
    assert iso_date("+1944-00-00T00:00:00Z", 9) == "1944"
    assert iso_date("+1944-06-00T00:00:00Z", 10) == "1944-06"
    assert iso_date("+1944-06-15T00:00:00Z", 11) == "1944-06-15"


def test_event_typing_from_instance_of(enriched):
    for rid in ("Q180182::LOC", "Q180182::MISC"):
        attrs = _node(enriched, rid)["attributes"]
        assert attrs["kind"] == "battle" and attrs["wikidata_instance_of"] == ["Q178561"]
    assert _node(enriched, "Q180182::LOC")["type"] == "location"  # the type is R73's decision, not this tool's


def test_r87_ships_and_countries_from_instance_of():
    assert en.entity_class({"P31": [_claim("P31", _item("Q214190"), "a")]}) == "ship"  # Akagi: a battlecruiser
    assert en.entity_class({"P31": [_claim("P31", _item("Q3024240"), "b"), _claim("P31", _item("Q48349"), "c")]}) == "country"
    assert en.entity_class({"P31": [_claim("P31", _item("Q23442"), "d")]}) is None  # an island
    assert en.entity_class({"P31": [_claim("P31", _item("Q82794"), "e")]}) == "region"  # Micronesia


def test_edges_only_between_entities_in_the_graph_and_never_twice(enriched):
    added = {(e["source"], e["target"], e["type"]): e for e in enriched["edges"] if e.get("provenance") == "wikidata"}
    assert set(added) == {
        ("Q180182", "Q30", "wd:P710"), ("Q180182", "Q188712", "wd:P710"),
        ("Q180182", "Q217645", "wd:P4791"),       # the commander qualifier on the US participant
        ("Q180182", "Q1067", "wd:P276"), ("Q180182", "Q1", "wd:P361")}
    # not added: P17 (exists), Nimitz P607 (exists as a reverse view), Q9 (not in the graph), the deprecated claim
    us = added[("Q180182", "Q30", "wd:P710")]
    assert us["id"] == "wdenrich:Q180182$us" and us["statement_id"] == "Q180182$us" and us["count"] == 1
    assert us["start_date"] == "+1945-02-19T00:00:00Z" and us["end_date"] is None and us["point_date"] is None
    assert us["relation_family"] == "PARTICIPATION_ROLE"
    assert "relation_family" not in added[("Q180182", "Q1067", "wd:P276")]  # not in the graph's catalog
    assert added[("Q180182", "Q217645", "wd:P4791")]["id"] == "wdenrich:Q180182$us#P4791:Q217645"
    again = en.enrich(copy.deepcopy(enriched), CLAIMS, SNAPSHOT)
    assert len(again["edges"]) == len(enriched["edges"])  # a second run adds nothing


def test_the_rest_of_the_graph_is_unchanged(enriched):
    before = _graph()
    assert [n["id"] for n in enriched["nodes"]] == [n["id"] for n in before["nodes"]]
    assert enriched["edges"][:2] == before["edges"]
    assert enriched["metadata"]["scope"] == "test" and enriched["relation_catalog"] == before["relation_catalog"]


def test_metadata_counts(enriched):
    meta = enriched["metadata"]["enrichment"]
    assert meta["tool_version"] == en.TOOL_VERSION and meta["snapshot_sha256"] == "abc"
    assert meta["counts"] == {"dates_added": 5, "entities_typed_as_event": 1, "entities_classed": 0,
                              "edges_added_by_type": {"wd:P276": 1, "wd:P361": 1, "wd:P4791": 1, "wd:P710": 2},
                              "life_dates_added": 0, "names_added": 0, "points_added": 0, "relation_dates_added": 0}


def test_the_enriched_graph_loads_and_its_relations_survive(enriched):
    s = GraphStore.from_dict(enriched)
    battle = s.nodes["Q180182::LOC"]
    assert battle.type == "event" and battle.geometry  # R73: a military kind types it event, geometry kept
    assert s.node_dates("Q180182::LOC") == ("1945-02-08", "1945-03-26")  # the winning record's dates
    who = {r["target"]["id"] for r in s.find_relations(source_id="Q180182::LOC", relation_type="wd:P710")}
    assert who == {"Q30::LOC", "Q188712::ORG"}
    assert [r["neighbor"]["id"] for r in s.neighbors("Q217645::PER", ["wd:P4791"])] == ["Q180182::LOC"]
    assert "Q1067::LOC" in {r["id"] for r in s.locations_of("Q180182::LOC")}
    assert s.schema()["coverage"]["edges_dropped_unknown_endpoint"] == 0


# ------------------------------------------- names, points, relation dates (spec R93)


def _coord(lat, lon, globe="http://www.wikidata.org/entity/Q2"):
    return {"snaktype": "value", "datavalue": {"type": "globecoordinate",
            "value": {"latitude": lat, "longitude": lon, "precision": 0.0002, "globe": globe}}}


def _mention(anchor):
    return {"page": "enwiki:1", "page_id": 1, "text": f"... {anchor} ...", "position": {"anchor_text": anchor}}


# Lae (Q612250) is name-less in partof521; Chuuk Lagoon (Q3593555) has no English label here, so its
# anchor text names it; Akagi is a ship and Japan a country (no point); Q30 has a point on the Moon.
R93_CLAIMS = {
    "Q612250": {"P625": [_claim("P625", _coord(-6.0, 146.0), "Q612250$old"),
                         _claim("P625", _coord(-6.73, 147.0), "Q612250$pref", rank="preferred")]},
    "Q3593555": {"P625": [_claim("P625", _coord(7.42, 151.78), "Q3593555$c")]},
    "Q320967": {"P31": [_claim("P31", _item("Q17205"), "a")], "P625": [_claim("P625", _coord(30.5, 179.0), "s")]},
    "Q188712": {"P31": [_claim("P31", _item("Q3024240"), "b")], "P625": [_claim("P625", _coord(35.0, 139.0), "t")]},
    "Q30": {"P625": [_claim("P625", _coord(1.0, 1.0, "http://www.wikidata.org/entity/Q405"), "moon")]},
    "Q173034": {"P625": [_claim("P625", _coord(28.2, -177.35), "Q173034$c")],
                "P276": [_claim("P276", _item("Q47863"), "Q173034$loc", {"P585": [_time("+1942-06-04T00:00:00Z")]})]},
    "Q217645": {"P607": [_claim("P607", _item("Q173034"), "Q217645$mid", {"P580": [_time("+1942-06-04T00:00:00Z")]})]},
}


def _r93_graph():
    return {"metadata": {}, "nodes": [
        {"id": "Q612250::LOC", "wikidata_id": "Q612250", "name": "Q612250", "type": "location", "aliases": [],
         "sources": [_mention("Lae"), _mention("Lae"), _mention("lae"), _mention("Lae airfield"), _mention("Lae airfield"),
                     _mention("British Malaya")]},
        {"id": "Q3593555::LOC", "wikidata_id": "Q3593555", "name": "Q3593555", "type": "location", "aliases": [],
         "sources": [_mention("Truk"), _mention("Truk"), _mention("Truk Atoll")]},
        {"id": "Q320967::LOC", "wikidata_id": "Q320967", "name": "Akagi", "type": "location"},
        {"id": "Q188712::LOC", "wikidata_id": "Q188712", "name": "Empire of Japan", "type": "location"},
        {"id": "Q30::LOC", "wikidata_id": "Q30", "name": "United States", "type": "location"},
        {"id": "Q173034::event", "wikidata_id": "Q173034", "name": "Battle of Midway", "type": "event"},
        {"id": "Q47863::LOC", "wikidata_id": "Q47863", "name": "Midway Atoll", "type": "location",
         "geometry": {"type": "Point", "coordinates": [-177.37, 28.21]}},
        {"id": "Q217645::PER", "wikidata_id": "Q217645", "name": "Chester W. Nimitz", "type": "person"}],
        "edges": [
            {"id": "o1", "source": "Q173034", "target": "Q47863", "type": "wd:P276", "start_date": None, "end_date": None,
             "point_date": None},
            {"id": "o1::REVERSE", "source": "Q47863", "target": "Q173034", "type": "inverse:wd:P276", "derived": True},
            {"id": "o2::REVERSE", "source": "Q173034", "target": "Q217645", "type": "participant", "derived": True,
             "base_level2_relation_id": "wd:P607"},
            {"id": "c1", "source": "Q173034", "target": "Q47863", "type": "CO_MENTIONED", "count": 3}]}


@pytest.fixture
def r93():
    return en.enrich(_r93_graph(), R93_CLAIMS, SNAPSHOT, {"Q612250": "Lae"}, "lab")


def test_r93_a_nameless_record_gets_its_label_or_its_most_used_anchor(r93):
    lae, truk = _node(r93, "Q612250::LOC"), _node(r93, "Q3593555::LOC")
    assert lae["name"] == "Lae" and lae["attributes"]["name_source"] == "wikidata_label"
    assert lae["aliases"] == ["Lae airfield"]  # "lae" is the same name; "British Malaya" is used once
    assert truk["name"] == "Truk" and truk["attributes"]["name_source"] == "anchor_text"
    assert truk["aliases"] == []  # "Truk Atoll" is used once
    assert _node(r93, "Q320967::LOC")["name"] == "Akagi"  # a named record keeps its name
    assert en.nameless_qids(_r93_graph()) == ["Q612250", "Q3593555"]  # numeric order


def test_r93_places_and_events_get_their_p625_point(r93):
    lae = _node(r93, "Q612250::LOC")
    assert lae["geometry"] == {"type": "Point", "coordinates": [147.0, -6.73]}  # the preferred statement
    assert lae["geolocation_evidence"]["statement_id"] == "Q612250$pref"
    assert lae["geolocation_evidence"]["source_kind"] == "WIKIDATA_P625_ENRICHMENT"
    assert _node(r93, "Q173034::event")["geometry"]["coordinates"] == [-177.35, 28.2]  # a battle's marker
    for rid in ("Q320967::LOC", "Q188712::LOC", "Q30::LOC", "Q217645::PER"):  # ship, country, the Moon, a person
        assert "geometry" not in _node(r93, rid)
    assert _node(r93, "Q47863::LOC")["geometry"]["coordinates"] == [-177.37, 28.21]  # the graph's own point kept


def test_r93_existing_relations_get_their_qualifier_dates(r93):
    e = {x["id"]: x for x in r93["edges"]}
    assert e["o1"]["point_date"] == "+1942-06-04T00:00:00Z" and e["o1"]["point_date_precision"] == 11
    assert e["o1::REVERSE"]["point_date"] == "+1942-06-04T00:00:00Z"  # an inverse: view
    assert e["o2::REVERSE"]["start_date"] == "+1942-06-04T00:00:00Z"  # a reverse view under its own label
    assert "point_date" not in e["c1"]  # never a co-mention
    counts = r93["metadata"]["enrichment"]["counts"]
    assert (counts["names_added"], counts["points_added"], counts["relation_dates_added"]) == (2, 3, 3)
    assert r93["metadata"]["enrichment"]["labels_sha256"] == "lab"


def test_r93_the_agent_finds_and_maps_the_named_places(r93):
    s = GraphStore.from_dict(r93)
    assert s.search_entities("Lae")[0]["id"] == "Q612250::LOC"
    assert s.search_entities("Truk")[0]["id"] == "Q3593555::LOC"
    assert s.nodes["Q612250::LOC"].geometry is not None


def test_r93_labels_are_fetched_once_and_cached(tmp_path):
    calls = []

    def get(params):
        calls.append(params)
        assert params["props"] == "labels" and params["languages"] == "en"
        return {"entities": {q: {"id": q, "labels": {"en": {"value": f"name {q}"}}} if q != "Q2" else {"id": q, "labels": {}}
                             for q in params["ids"].split("|")}}
    labels, digest = en.fetch_labels(["Q1", "Q2"], tmp_path, get=get, pause=0)
    assert labels == {"Q1": "name Q1"} and digest and len(calls) == 1
    assert en.fetch_labels(["Q1", "Q2"], tmp_path, offline=True, get=get) == (labels, digest) and len(calls) == 1
    assert en.fetch_labels([], tmp_path / "none", offline=True) == ({}, None)  # nothing name-less: no fetch
    with pytest.raises(SystemExit, match="label batches"):
        en.fetch_labels(["Q3"], tmp_path, offline=True, get=get)


# ------------------------------------------------------------------ fetching


def _stub(entities, calls):
    def get(params):
        calls.append(params)
        ids = params["ids"].split("|")
        if "Q404" in ids:
            return {"error": {"code": "no-such-entity", "id": "Q404", "info": "no such entity"}}
        return {"entities": {q: ({"id": q, "claims": entities[q]} if q in entities else {"id": q, "missing": ""}) for q in ids}}
    return get


def test_fetch_caches_per_batch_and_writes_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(en, "BATCH_SIZE", 2)
    calls = []
    qids = ["Q1", "Q404", "Q1067", "Q5"]
    claims, snap = en.fetch_claims(qids, tmp_path, get=_stub(CLAIMS, calls), pause=0)
    assert set(claims) == {"Q1", "Q1067"} and snap["missing"] == ["Q404", "Q5"]
    assert len(list(tmp_path.glob("claims-*.json"))) == 2 and json.loads((tmp_path / "snapshot.json").read_text()) == snap
    n = len(calls)
    again, snap2 = en.fetch_claims(qids, tmp_path, offline=True, get=_stub(CLAIMS, calls))
    assert len(calls) == n and again == claims and snap2["claims_sha256"] == snap["claims_sha256"]  # from the cache


def test_offline_with_a_missing_cache_fails(tmp_path):
    with pytest.raises(SystemExit, match="not in the cache"):
        en.fetch_claims(["Q1"], tmp_path / "empty", offline=True, get=lambda p: pytest.fail("network used"))


def test_cli_offline_end_to_end(tmp_path):
    graph = _graph()
    (tmp_path / "in.json").write_text(json.dumps(graph))
    cache = tmp_path / "cache"
    en.fetch_claims(en.collect_qids(graph), cache, get=_stub(CLAIMS, []), pause=0)
    en.main(["--graph", str(tmp_path / "in.json"), "--out", str(tmp_path / "out.json"), "--cache", str(cache), "--offline"])
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["metadata"]["enrichment"]["counts"]["edges_added_by_type"]["wd:P710"] == 2
    assert out["metadata"]["enrichment"]["snapshot_sha256"] == json.loads((cache / "snapshot.json").read_text())["claims_sha256"]


def test_the_agent_does_not_import_the_enrichment_tool():
    import pathlib
    for path in pathlib.Path(en.__file__).parents[1].joinpath("geo_agent").glob("*.py"):
        assert "graph_enrichment" not in path.read_text(encoding="utf-8"), path
    assert "geo_agent" not in pathlib.Path(en.__file__).read_text(encoding="utf-8").replace("`geo_agent`", "")


# R100: birth and death dates on people

def test_r100_people_get_birth_and_death_dates_and_nothing_else_does():
    graph = {"nodes": [
        {"id": "Q152325::PER", "wikidata_id": "Q152325", "name": "Isoroku Yamamoto", "type": "person"},
        {"id": "Q697037::ORG", "wikidata_id": "Q697037", "name": "Combined Fleet", "type": "org"},
        {"id": "Q1::PER", "wikidata_id": "Q1", "name": "Known", "type": "person", "attributes": {"death_date": "1950"}}],
        "edges": []}
    claims = {
        "Q152325": {"P31": [_claim("P31", _item("Q5"), "y-p31")],
                    "P569": [_claim("P569", _time("+1884-04-04T00:00:00Z"), "y-b")],
                    "P570": [_claim("P570", _time("+1943-04-19T00:00:00Z"), "y-d1"),
                             _claim("P570", _time("+1943-04-18T00:00:00Z"), "y-d2", rank="preferred")]},
        "Q697037": {"P31": [_claim("P31", _item("Q176799"), "c-p31")],
                    "P570": [_claim("P570", _time("+1945-10-10T00:00:00Z"), "c-d")]},   # not a human: ignored
        "Q1": {"P31": [_claim("P31", _item("Q5"), "k-p31")], "P570": [_claim("P570", _time("+1944-01-01T00:00:00Z"), "k-d")]}}
    out = en.enrich(graph, claims, {"sha256": "x"})
    a = out["nodes"][0]["attributes"]
    assert (a["birth_date"], a["death_date"], a["death_date_precision"]) == ("+1884-04-04T00:00:00Z", "+1943-04-18T00:00:00Z", 11)
    assert "death_date" not in out["nodes"][1].get("attributes", {})
    assert out["nodes"][2]["attributes"]["death_date"] == "1950"   # the graph's own value wins
    assert out["metadata"]["enrichment"]["counts"]["life_dates_added"] == 2
