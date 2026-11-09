"""Self-fix build layer (spec R97): canned Wikipedia and Wikidata answers, no network."""

import copy
import json
from collections import Counter
import os

import pytest

from graph_enrichment import enrich as en
from graph_enrichment import impact as im
from graph_enrichment import selffix as sf
from graph_enrichment import wikipedia as wp


# ----------------------------------------------------------------- wikilinks (step 1)


def test_normalize_title_follows_mediawiki_rules():
    assert wp.normalize_title("battle_of  Midway") == "Battle of Midway"
    assert wp.normalize_title("William Halsey Jr.#Early life") == "William Halsey Jr."
    assert wp.normalize_title(":Battle of Leyte&nbsp;Gulf") == "Battle of Leyte Gulf"
    assert wp.normalize_title("Kwajalein%20Atoll") == "Kwajalein Atoll"
    assert wp.normalize_title("''Yamato''") == "Yamato"
    for non_article in ("File:X.jpg", "Category:Battles", ":Category:Battles", "ja:東京", "wikt:atoll", "W:ja:スールー諸島の戦い", "#Notes", "  "):
        assert wp.normalize_title(non_article) is None, non_article
    assert wp.normalize_title("Operation Ten-Go: the last sortie") == "Operation Ten-Go: the last sortie"  # a colon in a title


def test_parse_wikilinks_reads_titles_texts_and_offsets():
    text = ("At [[Shayang County|Shayang]] in [[hubei]], [[William Halsey Jr.#Early life|Halsey]]'s [[aircraft carrier]]s, "
            "[[Kongō (ship)|]], [[File:X.jpg|thumb|a [[Truk]] photo]], [[Category:Foo]], [[ja:東京]], [[#Notes|below]].")
    links = wp.parse_wikilinks(text, offset=100)
    assert [(l.title, l.text) for l in links] == [
        ("Shayang County", "Shayang"), ("Hubei", "hubei"), ("William Halsey Jr.", "Halsey"),
        ("Aircraft carrier", "aircraft carriers"), ("Kongō (ship)", "Kongō"), ("Truk", "Truk")]
    first = links[0]
    assert text[first.start - 100:first.end - 100] == "[[Shayang County|Shayang]]"
    assert links[2].target == "William Halsey Jr.#Early life"


def test_paragraph_bounds_is_the_block_between_blank_lines():
    text = "Intro.\n\nFirst [[A]] para\nstill first.\n\nSecond."
    start, end = wp.paragraph_bounds(text, text.index("[[A]]"))
    assert text[start:end] == "First [[A]] para\nstill first."
    assert wp.paragraph_bounds(text, 2) == (0, 6) and text[wp.paragraph_bounds(text, len(text) - 2)[0]:] == "Second."


# ----------------------------------------------------------------- title resolver (step 1)


QUERY = {"query": {
    "normalized": [{"from": "battle_of_midway", "to": "Battle of midway"}],
    "redirects": [{"from": "Battle of midway", "to": "Battle of Midway"},
                  {"from": "William F. Halsey", "to": "William Halsey Jr."},
                  {"from": "Truk", "to": "Chuuk"},
                  {"from": "Bombing of Foo", "to": "Foo", "tofragment": "World War II"}],
    "pages": [
        {"pageid": 60112, "ns": 0, "title": "Battle of Midway", "lastrevid": 11, "pageprops": {"wikibase_item": "Q173034"}},
        {"pageid": 206587, "ns": 0, "title": "William Halsey Jr.", "lastrevid": 12, "pageprops": {"wikibase_item": "Q439984"}},
        {"pageid": 230602, "ns": 0, "title": "Chuuk", "lastrevid": 13, "pageprops": {"disambiguation": "", "wikibase_item": "Q1090062"}},
        {"pageid": 5, "ns": 0, "title": "Foo", "lastrevid": 14, "pageprops": {"wikibase_item": "Q5"}},
        {"pageid": 6, "ns": 0, "title": "Mercury Island", "lastrevid": 15, "pageprops": {"wikibase_item": "Q6"}},
        {"pageid": 7, "ns": 0, "title": "No item page", "lastrevid": 16},
        {"pageid": 8, "ns": 4, "title": "Wikipedia:Style"},
        {"ns": 0, "title": "Nonexistent xyz", "missing": True},
        {"title": "Bad<title", "invalid": True}]}}
ASKED = ["battle_of_midway", "William F. Halsey", "Truk", "Bombing of Foo", "Mercury Island", "No item page",
         "Wikipedia:Style", "Nonexistent xyz", "Bad<title"]


def test_resolutions_follow_normalization_and_redirects_and_refuse_ambiguous_pages():
    r = wp.resolutions_from_query(ASKED, QUERY)
    assert (r["battle_of_midway"].qid, r["battle_of_midway"].redirected) == ("Q173034", True)
    assert (r["William F. Halsey"].qid, r["William F. Halsey"].title) == ("Q439984", "William Halsey Jr.")
    assert (r["Truk"].status, r["Truk"].qid, r["Truk"].page_qid) == ("disambiguation", None, "Q1090062")
    assert (r["Bombing of Foo"].status, r["Bombing of Foo"].qid) == ("section_redirect", None)  # a section of Foo is not Foo
    assert (r["Mercury Island"].status, r["Mercury Island"].redirected, r["Mercury Island"].lastrevid) == ("ok", False, 15)
    assert [r[t].status for t in ASKED[5:]] == ["no_item", "not_article", "missing", "invalid"]


def test_resolver_batches_by_50_caches_per_title_and_runs_offline(tmp_path):
    calls = []

    def get(url, params):
        calls.append(params)
        titles = params["titles"].split("|")
        assert url == wp.WIKIPEDIA_API and len(titles) <= 50 and params["redirects"] == "1"
        return {"query": {"pages": [{"pageid": i, "ns": 0, "title": t, "pageprops": {"wikibase_item": f"Q{i + 1}"}}
                                    for i, t in enumerate(titles)]}}

    titles = [f"Title {i}" for i in range(120)]
    got = wp.TitleResolver(tmp_path, get=get).resolve(titles + titles[:5])
    assert len(calls) == 3 and len(got) == 120 and all(r.status == "ok" for r in got.values())
    assert len(list((tmp_path / "titles-raw").glob("*.json"))) == 3  # raw answers kept for audit
    again = wp.TitleResolver(tmp_path, offline=True, get=lambda *a: pytest.fail("network")).resolve(titles)
    assert {t: r.qid for t, r in again.items()} == {t: r.qid for t, r in got.items()}
    with pytest.raises(SystemExit, match="--offline: 1 titles"):
        wp.TitleResolver(tmp_path, offline=True).resolve(["Not cached"])


def _entity(qid, p31=(), label=None, **claims):
    snak = lambda q: {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": q}}}}
    allc = {"P31": [snak(q) for q in p31], **{p: [snak(q) for q in v] for p, v in claims.items()},
            "P1082": [snak("Q1")]}  # a property the lite cache drops
    return {"id": qid, "lastrevid": 99, "labels": {"en": {"value": label or qid}}, "aliases": {"en": [{"value": "alias"}]},
            "sitelinks": {"enwiki": {"title": label or qid}}, "claims": allc}


def test_disambiguation_by_instance_of_and_the_lite_cache(tmp_path):
    def get(url, params):
        assert url == wp.WIKIDATA_API and params["action"] == "wbgetentities"
        ids = params["ids"].split("|")
        if "Q404" in ids:
            return {"error": {"code": "no-such-entity", "id": "Q404"}}
        return {"entities": {q: _entity(q, p31=["Q4167410"] if q == "Q2" else ["Q5"], P607=["Q362"]) for q in ids}}

    lite = wp.fetch_lite(["Q1", "Q2", "Q404"], tmp_path, get=get)
    assert set(lite) == {"Q1", "Q2", "Q404"} and lite["Q404"]["missing"]
    assert set(lite["Q1"]["claims"]) == {"P31", "P607"} and lite["Q1"]["lastrevid"] == 99 and lite["Q1"]["aliases"] == ["alias"]
    res = {"a": wp.Resolution("a", qid="Q1", page_qid="Q1"), "b": wp.Resolution("b", qid="Q2", page_qid="Q2")}
    assert wp.apply_instance_of(res, lite) == 1
    assert (res["a"].status, res["b"].status, res["b"].qid) == ("ok", "disambiguation", None)
    assert wp.fetch_lite(["Q1", "Q2"], tmp_path, offline=True, get=lambda *a: pytest.fail("network"))["Q2"]["label"] == "Q2"


def test_revisions_are_fetched_by_id_cached_and_re_asked_when_cut_off(tmp_path):
    calls = []

    def get(url, params):
        ids = [int(x) for x in params["revids"].split("|")]
        calls.append(ids)
        revs = [{"revid": r, "slots": {"main": {"content": f"text {r}"} if len(calls) > 1 or r == ids[0] else {}}} for r in ids
                if r != 3]
        return {"query": {"pages": [{"pageid": 7, "title": "Page", "revisions": revs}], "badrevids": {"3": {"revid": 3}}}}

    got = wp.fetch_revisions([1, 2, 3], tmp_path, get=get)
    assert calls == [[1, 2, 3], [2]]  # revision 2 came back without content: asked again
    assert got[1]["wikitext"] == "text 1" and got[2]["sha256"] and got[3]["missing"]
    assert wp.fetch_revisions([1, 2, 3], tmp_path, offline=True, get=lambda *a: pytest.fail("network"))[2]["title"] == "Page"
    with pytest.raises(SystemExit):
        wp.fetch_revisions([4], tmp_path, offline=True)


def test_corpus_paragraphs_come_from_the_revision_at_the_anchor_span():
    wikitext = "Lead.\n\nOn 29 August the [[Hubei|CACW]] bombed [[Shayang County|Shayang]]. Meng Shao-yi was killed.\n\nEnd."
    a = wikitext.index("Meng")
    src = lambda anchor, span: {"page_id": 7, "processing_revision_id": 70, "text": "cleaned",
                                "position": {"anchor_text": anchor, "raw_anchor_span": span}}
    graph = {"nodes": [{"id": "Q1::PER", "sources": [src("Meng Shao-yi", [a, a + 12]), src("Wrong", [0, 5])]},
                       {"id": "Q2::LOC", "sources": [src("Shayang", [wikitext.index("Shayang]]"), wikitext.index("Shayang]]") + 7])]}]}
    revs = {70: {"page_id": 7, "revid": 70, "title": "Aerial engagements", "wikitext": wikitext}}
    assert sf.corpus_revisions(graph) == {70: 7}
    paras, stats = sf.corpus_paragraphs(graph, revs)
    assert stats == {"sources": 3, "sources_aligned": 2, "sources_span_mismatch": 1, "paragraphs": 1}
    assert len(paras) == 1 and paras[0].mentions == 2 and paras[0].text.startswith("On 29 August")
    links = sf.corpus_links(paras)
    assert [(l.title, l.text, l.revid, l.page_title) for l in links] == [
        ("Hubei", "CACW", 70, "Aerial engagements"), ("Shayang County", "Shayang", 70, "Aerial engagements")]
    assert wikitext[links[1].start:links[1].end] == "[[Shayang County|Shayang]]"


def test_enrich_leaves_an_unchanged_snapshot_file_untouched(tmp_path):
    get = lambda params: {"entities": {q: {"claims": {}} for q in params["ids"].split("|")}}
    en.fetch_claims(["Q1", "Q2"], tmp_path, get=get, pause=0)
    snap = tmp_path / "snapshot.json"
    os.utime(snap, (1, 1))
    en.fetch_claims(["Q1", "Q2"], tmp_path, offline=True)
    assert snap.stat().st_mtime == 1


# ----------------------------------------------------------------- impact report


def _snap(**over):
    base = {"load_s": 1.0, "max_rss_mb": 10,
            "counts": {"nodes": 2, "node_types": {"event": 1, "location": 1}, "edges_by_type": {"wd:P276": 1}},
            "coverage": {"locations_with_geometry": 1, "nodes_type_other": 0},
            "nodes": {"Q1::event": {"qid": "Q1", "name": "Battle", "type": "event", "geom": None, "class": None, "in_geoms": False},
                      "Q2::LOC": {"qid": "Q2", "name": "Isle", "type": "location", "geom": "g", "class": None, "in_geoms": True}},
            "edge_keys": {"Q1|Q2|wd:P276": 1}, "duplicate_names": {}, "graph_qids": ["Q1", "Q2"],
            "probes": {"search_entities": {"Battle": [["Q1", "Battle"]]},
                       "connected_locations": {"Q9": [{"qid": "Q2", "name": "Isle", "lat": 0, "lon": 150, "via": "Q1", "via_in_scope": True}]},
                       "what_happened_at": {"Q2": {"events": ["Q1"], "nearby_events": [], "region_events": [], "names": {"Q1": "Battle"}}},
                       "locations_of": {"Q1": [["Q2", "Isle"]]}}}
    return json.loads(json.dumps({**base, **over}))


def test_impact_of_an_unchanged_graph_is_nothing():
    c = im.compare(_snap(), _snap(), heldout_rows=[{"id": "ho-x", "expected_entity_ids": ["Q1", "Q3"]}])
    assert not (c["lost_nodes"] or c["changed_nodes"] or c["lost_edges"] or c["coverage_changed"])
    assert all(not v for k, v in c["drift"].items())
    assert c["heldout"] == {"ho-x": {"before": ["Q3"], "after": ["Q3"]}}
    assert "nothing lost" in im.report_md(c, "1", {k: [] for k in ("search_entities", "connected_locations", "what_happened_at", "locations_of")})


def test_impact_reports_losses_drift_and_noise():
    after = _snap()
    after["nodes"]["Q2::LOC"]["name"] = "Renamed"
    after["nodes"]["Q3::LOC"] = {"qid": "Q3", "name": "HMS Ship", "type": "location", "geom": "h", "class": "ship", "in_geoms": True}
    after["edge_keys"] = {"Q3|Q2|wd:P361": 1}
    after["coverage"]["locations_with_geometry"] = 0
    after["duplicate_names"] = {"isle": ["Q2", "Q4"]}
    p = after["probes"]
    p["connected_locations"]["Q9"].append({"qid": "Q5", "name": "Paris", "lat": 48.8, "lon": 2.3, "via": "Q6", "via_in_scope": False})
    p["what_happened_at"]["Q2"] = {"events": [], "nearby_events": ["Q1"], "region_events": [], "names": {"Q1": "Battle"}}
    p["locations_of"]["Q1"] = []
    c = im.compare(_snap(), after)
    assert c["changed_nodes"] == [{"id": "Q2::LOC", "field": "name", "before": "Isle", "after": "Renamed"}]
    assert c["lost_edges"] == {"Q1|Q2|wd:P276": 1} and c["gained_edges_by_type"] == {"wd:P361": 1}
    assert c["coverage_worse"] == {"locations_with_geometry": (1, 0)}
    assert c["drift"]["what_happened_at"]["Q2"]["moved_between_lists"] == ["Q1"]
    assert c["drift"]["locations_of"]["Q1"] == {"gained": [], "lost": [["Q2", "Isle"]]}
    assert c["noise"]["ships_as_places"] == ["Q3::LOC"] and c["noise"]["new_duplicate_names"] == {"isle": ["Q2", "Q4"]}
    assert [g["qid"] for g in c["noise"]["connected_places_gained_out_of_theatre"]] == ["Q5"]
    assert [g["qid"] for g in c["noise"]["connected_places_gained_out_of_window"]] == ["Q5"]


def test_hub_probes_report_their_own_drift_and_noise():
    before = _snap()
    before["probes"]["hubs_connected_locations"] = {"Q30": [{"qid": "Q2", "name": "Isle", "lat": 0, "lon": 150, "via": "Q1", "via_in_scope": True}]}
    before["probes"]["hubs_locations_of"] = {"Q30": []}
    after = json.loads(json.dumps(before))
    after["probes"]["hubs_connected_locations"]["Q30"] = [
        {"qid": "Q7", "name": "Normandy", "lat": 49.2, "lon": -0.4, "via": "Q8", "via_in_scope": True},
        {"qid": "Q9", "name": "Burma Road", "lat": None, "lon": None, "via": "Q8", "via_in_scope": True}]
    c = im.compare(before, after)
    assert c["drift"]["hubs_connected_locations"]["Q30"]["lost"] == [["Q2", "Isle", "Q1"]]
    assert [g["qid"] for g in c["noise"]["hub_places_gained_out_of_theatre"]] == ["Q7"]  # a place without a point is not outside
    assert [g["qid"] for g in c["noise"]["places_gained_without_a_point"]] == ["Q9"]
    assert not c["noise"]["connected_places_gained_out_of_theatre"]  # the rule-chosen probes are kept apart
    hubs = {k: [] for k in ("search_entities", "connected_locations", "what_happened_at", "locations_of")} | {"hubs": im.HUBS}
    assert "something was lost" in im.report_md(c, "3", hubs)
    assert {h["qid"] for h in im.HUBS} == {"Q30", "Q188712", "Q219712", "Q11220", "Q184425", "Q11218"}


def test_asia_pacific_box_crosses_the_antimeridian():
    assert im.in_asia_pacific(177.6, 51.9) and im.in_asia_pacific(-176.6, 51.9) and im.in_asia_pacific(100.5, 13.7)
    assert not im.in_asia_pacific(2.35, 48.85) and not im.in_asia_pacific(-77.0, 38.9) and not im.in_asia_pacific(150, 65)
    assert not im.in_asia_pacific(None, 10)


# ----------------------------------------------------------------- step 3: class table and scope

from graph_enrichment import classes as cl  # noqa: E402


def _snak(q):
    return {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": q}}}}


def _tsnak(t, precision=11):
    return {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"value": {"time": t, "precision": precision}}}}


def _pt(lat, lon):
    return {"rank": "normal", "id": "s-p625", "mainsnak": {"snaktype": "value", "datavalue": {"value": {
        "latitude": lat, "longitude": lon, "precision": 0.01, "globe": "http://www.wikidata.org/entity/Q2"}}}}


def _claims(p31=(), **props):
    out = {"P31": [_snak(q) for q in p31]}
    for p, v in props.items():
        out[p] = v if isinstance(v, list) and v and isinstance(v[0], dict) else [_snak(q) for q in v]
    return out


# P279 parents: a ship class under destroyer, a hotel under facility under geographical feature
PARENTS = {"Q-fubuki": ["Q174736"], "Q174736": ["Q3114762"], "Q3114762": ["Q11446"], "Q27686": ["Q13226383"],
           "Q13226383": ["Q618123"], "Q56019": ["Q4164871"], "Q695850": ["Q62447", "Q245016"], "Q-squadron": ["Q176799"]}
up = lambda q: PARENTS.get(q, [])


def test_class_table_reads_p31_up_the_p279_chain():
    assert cl.classify(_claims(["Q5"]), up) == ("person", "Q5")
    assert cl.classify(_claims(["Q-fubuki"]), up)[0] == "ship"            # Japanese destroyer Akebono
    assert cl.classify(_claims(["Q695850"]), up)[0] == "place"            # an airbase
    assert cl.classify(_claims(["Q-squadron"]), up)[0] == "unit"
    assert cl.classify(_claims(["Q178561"]), up)[0] == "event"            # battle
    assert cl.classify(_claims(["Q56019"]), up) == (None, "class not in the table")   # a military rank
    assert cl.classify(_claims(["Q27686"]), up) == (None, "class not in the table")   # a hotel is no place
    assert cl.classify(_claims(["Q2235308"], P279=["Q174736"]), up) == (None, "is a class (has P279)")  # PT boat
    assert cl.classify(_claims([]), up) == (None, "no P31")
    assert cl.classify(_claims(["Q5", "Q-fubuki"]), up)[0] == "person"    # precedence
    # the nearest root wins: an expeditionary army is a unit 2 steps up, an "occurrence" 3 steps up
    chain = {"Q-expeditionary": ["Q-field-army", "Q-force"], "Q-field-army": ["Q176799"], "Q-force": ["Q-expedition"],
             "Q-expedition": ["Q1190554"]}.get  # occurrence at depth 3, unit at depth 2
    assert cl.classify(_claims(["Q-expeditionary"]), lambda q: chain(q, []))[0] == "unit"
    # "region" counts only as a direct P31: the Australian War Memorial is a region a few P279 steps up
    assert cl.classify(_claims(["Q82794"]), up)[0] == "place"
    assert cl.classify(_claims(["Q-memorial"]), lambda q: {"Q-memorial": ["Q-monument"], "Q-monument": ["Q82794"]}.get(q, []))[0] is None


def test_scope_rules_per_class():
    lite = {"Q-cartwheel": {"claims": {"P361": [_snak("Q184425")]}},
            "Q-korea": {"claims": {"P580": [_tsnak("+1950-06-25T00:00:00Z")]}},
            "Q-leyte": {"claims": {"P580": [_tsnak("+1944-10-17T00:00:00Z")]}}}
    lite |= {"Q-kwajalein": {"claims": {"P625": [_pt(9.1, 167.3)]}}, "Q-france": {"claims": {"P625": [_pt(46.0, 2.0)]}}}
    sc = lambda cls, claims: sf.in_scope(cls, claims, lite)
    jan44 = [_tsnak("+1944-01-31T00:00:00Z")]
    # an event: within 1938-1948 and in the theatre (part of the Pacific War or Sino-Japanese War, or located in the box)
    assert sc("event", {"P580": jan44, "P276": [_snak("Q-kwajalein")]})[0]       # located through P276
    assert sc("event", {"P580": jan44, "P625": [_pt(9.1, 167.3)]})[0]           # its own point
    assert sc("event", {"P580": jan44, "P361": [_snak("Q-cartwheel")]})[0]      # part of the Pacific War, two steps up
    assert sc("event", {"P361": [_snak("Q-cartwheel")]}) == (True, "undated, part of Pacific War")
    assert sc("event", {"P580": jan44}) == (False, "dated 1944-1944, outside the theatre (no location)")
    lite |= {"Q-japan": {"claims": {"P625": [_pt(35.0, 136.0)]}}, "Q-usa": {"claims": {"P625": [_pt(39.8, -98.6)]}}}
    assert sc("event", {"P580": jan44, "P17": [_snak("Q-japan")]})[0]           # the country, without a location
    assert not sc("event", {"P580": jan44, "P17": [_snak("Q-usa")]})[0]         # the Infamy Speech: the US point is Kansas
    assert sc("event", {"P580": jan44, "P276": [_snak("Q-france")], "P17": [_snak("Q-japan")]})[0] is False  # P276 first
    battle_of_france = {"P580": [_tsnak("+1940-05-10T00:00:00Z")], "P361": [_snak("Q362")], "P276": [_snak("Q-france")]}
    assert sc("event", battle_of_france) == (False, "dated 1940-1940, outside the theatre (located 46.00, 2.00)")
    assert not sc("event", {"P580": [_tsnak("+1951-01-01T00:00:00Z")], "P625": [_pt(9.1, 167.3)]})[0]
    assert not sc("event", {})[0]
    # people and units: a conflict of the theatre, from P607 or from their own infobox's battles
    lite |= {"Q-leyte": {"claims": {"P580": [_tsnak("+1944-10-17T00:00:00Z")], "P276": [_snak("Q-kwajalein")]}},
             "Q-sicily": {"claims": {"P580": [_tsnak("+1943-07-09T00:00:00Z")], "P361": [_snak("Q362")],
                                     "P625": [_pt(37.5, 14.0)]}}}
    assert sc("person", {"P607": [_snak("Q-leyte")]}) == (True, "fought in Q-leyte (P607)")
    assert sc("person", {"P607": [_snak("Q184425")]})[0]
    assert not sc("person", {"P607": [_snak("Q362")]})[0]                      # World War II alone is not the theatre
    assert not sc("person", {"P607": [_snak("Q-korea")]})[0] and not sc("person", {})[0]
    assert not sc("unit", {"P571": [_tsnak("+1942-01-01T00:00:00Z", 9)]})[0]  # a service period alone: the Twelfth Air Force
    assert sf.in_scope("unit", {}, lite, ["Q-sicily", "Q-cartwheel"]) == (True, "fought in Q-cartwheel (infobox battles)")
    assert not sf.in_scope("unit", {"P607": [_snak("Q362")]}, lite, ["Q-sicily"])[0]
    # ships: a conflict in scope and a dated life; a ship raised after the war is out
    assert sc("ship", {"P607": [_snak("Q362")], "P571": [_tsnak("+1938-01-01T00:00:00Z", 9)]})[0]
    assert not sc("ship", {"P571": [_tsnak("+1948-01-01T00:00:00Z", 9)]})[0]
    assert not sc("ship", {})[0]
    assert sc("place", {"P625": [_pt(7.4, 151.8)]})[0]           # Chuuk Lagoon
    assert sc("place", {"P625": [_pt(51.9, 177.6)]})[0]          # Kiska, east of the antimeridian
    assert not sc("place", {"P625": [_pt(48.85, 2.35)]})[0]      # Paris
    assert not sc("place", {})[0]


# ----------------------------------------------------------------- step 3: short forms

PAGE = ("Admiral [[William Halsey Jr.|William F. Halsey]] led the fleet. Admiral Halsey then sailed north. "
        "Halsey's carriers struck. John Halsey stayed home. The Halsey Field crew cheered. USS Halsey sailed.")


def _short(page=PAGE, extra_links=(), team=((), ())):
    p = sf.Paragraph(1, 10, "Page", 0, len(page), page)
    links = sf.corpus_links([p])
    qids = {"William Halsey Jr.": "Q439984", **{t: q for t, q in extra_links}}
    return [page[a:b] + "@" + str(a) for _, a, b in sf.short_form_mentions(
        "Q439984", "Halsey", 10, [p], links, qids, {"Q999": ["USS Halsey"]}, list(team[0]), list(team[1]), set())]


def test_short_forms_count_later_plain_mentions_with_a_title_or_alone():
    got = _short()
    assert [g.split("@")[0] for g in got] == ["Halsey", "Halsey"]  # "Admiral Halsey", "Halsey's"
    assert all(int(g.split("@")[1]) > PAGE.index("]]") for g in got)


def test_short_forms_never_attach_another_name_or_class():
    # "John Halsey" (another person), "Halsey Field" (a place), "USS Halsey" (a ship) were all skipped above;
    # a page that also links a ship whose name holds the word gets no short forms at all
    page = PAGE + " The destroyer [[USS Halsey (DLG-23)|Halsey]] was named for him."
    assert _short(page, extra_links=[("USS Halsey (DLG-23)", "Q999")]) == []
    # nor a page where the team itself mentions another entity with the word in its name
    assert _short(team=((), ["Halsey Field"])) == []
    # nor a mention the team already anchored
    a = PAGE.index("Admiral Halsey then") + len("Admiral ")
    assert len(_short(team=([(a, a + 6)], []))) == 1
    # nor a word that names an entity of the graph
    p = sf.Paragraph(1, 10, "Page", 0, len(PAGE), PAGE)
    assert sf.short_form_mentions("Q439984", "Halsey", 10, [p], sf.corpus_links([p]), {"William Halsey Jr.": "Q439984"},
                                  {}, [], [], {"halsey"}) == []


# ----------------------------------------------------------------- step 3: end to end on canned data


def _step3_graph():
    para = ("At [[Chuuk Lagoon|Truk]] and [[Chuuk Lagoon|Truk]] the [[Kunio Nakagawa|Nakagawa]] force, "
            "[[Kunio Nakagawa]] and [[Kunio Nakagawa|Colonel Nakagawa]], met [[Vice admiral]]s, [[Vice admiral]]s, "
            "[[Vice admiral]]s and [[Peleliu]]. Later Nakagawa held. [[HMS Jupiter (F85)|Jupiter]] [[HMS Jupiter (F85)|Jupiter]] [[HMS Jupiter (F85)|Jupiter]]. [[Chuuk Lagoon|Truk]] [[Rare place]] [[Rare place]].")
    wikitext = "Lead.\n\n" + para
    a = wikitext.index("Peleliu]]") + len("Peleliu]]") - 9 + 2  # the anchor of the team's Peleliu mention
    a = wikitext.index("[[Peleliu]]") + 2
    graph = {"metadata": {"enrichment": {"tool_version": "r74"}}, "nodes": [
        {"id": "Q1090062::LOC", "wikidata_id": "Q1090062", "name": "Chuuk", "aliases": ["Truk"], "type": "location",
         "sources": [{"page_id": 7, "processing_revision_id": 70, "text": "cleaned: the Nakagawa force met Peleliu",
                      "position": {"anchor_text": "Peleliu", "raw_anchor_span": [a, a + 7]}}]},
        {"id": "Q497981::LOC", "wikidata_id": "Q497981", "name": "Peleliu", "type": "location"}], "edges": []}
    return graph, wikitext


def test_step3_adds_in_scope_entities_holds_duplicates_and_changes_no_record(tmp_path, monkeypatch):
    graph, wikitext = _step3_graph()
    original = copy.deepcopy(graph["nodes"])
    revs = {70: {"page_id": 7, "revid": 70, "title": "Truk page", "wikitext": wikitext}}
    titles = {"Chuuk Lagoon": "Q-lagoon", "Kunio Nakagawa": "Q6444743", "Vice admiral": "Q214443", "Peleliu": "Q497981",
              "Rare place": "Q-rare", "HMS Jupiter (F85)": "Q1564961"}
    lite = {"Q-lagoon": {"qid": "Q-lagoon", "label": "Chuuk Lagoon", "claims": _claims(["Q23442"])},
            "Q6444743": {"qid": "Q6444743", "label": "Kunio Nakagawa", "claims": _claims(["Q5"])},
            "Q214443": {"qid": "Q214443", "label": "vice admiral", "claims": _claims(["Q56019"])},
            "Q497981": {"qid": "Q497981", "label": "Peleliu", "claims": _claims(["Q23442"])},
            "Q-rare": {"qid": "Q-rare", "label": "Rare", "claims": _claims(["Q23442"])},
            "Q1564961": {"qid": "Q1564961", "label": "HMS Jupiter", "claims": _claims(["Q-sloop"])},
            "Q-sloop": {"qid": "Q-sloop", "claims": {"P279": [_snak("Q11446")]}},
            "Q23442": {"qid": "Q23442", "claims": {}}, "Q5": {"qid": "Q5", "claims": {}},
            "Q56019": {"qid": "Q56019", "label": "military rank", "claims": {}},
            "Q-nakagawa": {"qid": "Q-nakagawa", "label": "Nakagawa", "claims": {}}}
    full = {"Q-lagoon": {"claims": {"P625": [_pt(7.4, 151.8)]}, "lastrevid": 5},
            "Q1564961": {"claims": {"P607": [_snak("Q362")], "P571": [_tsnak("+1938-01-01T00:00:00Z", 9)]}, "lastrevid": 7},
            "Q6444743": {"claims": {"P607": [_snak("Q184425")], "P734": [_snak("Q-nakagawa")], "P710": []}, "lastrevid": 6}}

    def corpus(g, ctx):
        paras, align = sf.corpus_paragraphs(g, revs)
        links = sf.corpus_links(paras)
        res = {t: wp.Resolution(t, qid=titles[t], page_qid=titles[t]) for t in {l.title for l in links}}
        return sf.Corpus(paras, align, links, res, lite, 0)

    monkeypatch.setattr(sf, "load_corpus", corpus)
    monkeypatch.setattr(sf.wp, "fetch_lite", lambda qids, *a, **k: {q: lite.get(q, {"qid": q, "claims": {}}) for q in qids})
    monkeypatch.setattr(sf.wp, "fetch_full_claims", lambda qids, *a, **k: {q: full[q] for q in qids if q in full})
    monkeypatch.setattr(sf.wd, "fetch_claims", lambda qids, *a, **k: (
        {"Q497981": {"P361": [{"id": "s1", "rank": "normal", "mainsnak": {**_snak("Q6444743")["mainsnak"], "property": "P361"}}]}},
        {"claims_sha256": "x"}))
    ctx = sf.Ctx(tmp_path, tmp_path, tmp_path)
    report = sf.step3_add_entities(graph, ctx)

    assert report["candidates"] == 4 and report["below_min_links"] == 1  # "Rare place": 2 links
    ship = next(n for n in graph["nodes"] if n["wikidata_id"] == "Q1564961")
    assert (ship["type"], ship["attributes"]["entity_class"], ship.get("geometry")) == ("other", "ship", None)  # R87: no place
    assert report["added"] == 2 and report["held_as_possible_duplicates"] == 1  # Chuuk Lagoon: same name as Chuuk
    assert report["held_list"] == [["Q-lagoon", "Chuuk Lagoon", ["Q1090062"]]]
    assert [r[0] for r in report["rejected_list"]] == ["Q-lagoon", "Q214443"]
    assert "military rank" in report["rejected_list"][1][3]
    assert graph["nodes"][:2] == original                     # no team record changed
    new = next(n for n in graph["nodes"] if n["wikidata_id"] == "Q6444743")
    assert (new["id"], new["name"], new["type"], new["provenance"]) == ("Q6444743::selffix", "Kunio Nakagawa", "person", "wikipedia_link")
    kinds = Counter(s["position"]["mention_kind"] for s in new["sources"])
    assert kinds == {"wikilink": 3, "short_form": 1}          # "Later Nakagawa held"
    s = new["sources"][0]
    assert s["processing_revision_id"] == 70 and s["license"].startswith("CC BY-SA")
    assert (s["text"], s["text_basis"]) == ("cleaned: the Nakagawa force met Peleliu", "TEAM_PARAGRAPH_TEXT")
    other = next(x for x in new["sources"] if x["position"]["anchor_text"] == "Colonel Nakagawa")
    assert other["text_basis"] == "CLEANED_RAW_WIKITEXT" and "Colonel Nakagawa" in other["text"] and "[[" not in other["text"]
    assert all(x["position"]["anchor_text"] in x["text"] for x in new["sources"])
    assert wikitext[slice(*s["position"]["raw_anchor_span"])] == "[[Kunio Nakagawa|Nakagawa]]"
    assert new["aliases"] == [] and new["attributes"]["selffix"]["link_occurrences"] == 3
    added_edges = [e for e in graph["edges"] if e.get("selffix_step") == 3]
    assert [(e["source"], e["target"], e["type"]) for e in added_edges] == [("Q497981", "Q6444743", "wd:P361")]
    assert graph["metadata"]["enrichment"] == {"tool_version": "r74"}  # the R74 record is kept


# ----------------------------------------------------------------- step 3: rules from the supervisor's review


def test_links_in_citations_and_templates_are_not_mentions():
    text = ("The [[Battle of Arawe]] began.<ref>{{cite book |publisher=[[United States Army Center of Military History]]}}</ref>"
            " See {{main|[[Air raids on Japan]]}} and <ref name=x/> [[Lae]]{{efn|by [[Nadzab]] {{nested|[[X]]}}}}.")
    p = sf.Paragraph(1, 10, "Page", 0, len(text), text)
    assert [l.title for l in sf.corpus_links([p])] == ["Battle of Arawe", "Lae"]


def test_institutions_companies_and_works_are_never_entities_of_the_war():
    parents = {"Q-cmh-publisher": ["Q1320047"], "Q2772772": ["Q33506"], "Q917182": ["Q2385804"]}.get
    up2 = lambda q: parents(q, [])
    assert cl.classify(_claims(["Q1320047", "Q176799"]), up2)[0] is None   # Army Center of Military History
    assert cl.classify(_claims(["Q2772772", "Q5003624"]), up2)[0] is None  # Australian War Memorial
    assert cl.classify(_claims(["Q917182"]), up2)[0] is None               # Philippine Military Academy
    assert cl.classify(_claims(["Q190928"]), lambda q: [])[0] is None      # a shipyard (Uraga Dock Company)
    assert cl.classify(_claims(["Q176799"]), lambda q: [])[0] == "unit"


def test_an_administrative_territory_is_a_place_even_when_it_is_an_occupation():
    # Saharat Thai Doem: "administrative divisions of Thailand" -> "... of a single country" -> administrative territorial entity
    parents = {"Q-thai-div": ["Q-single"], "Q-single": ["Q56061"], "Q188686": ["Q645883"]}.get
    assert cl.classify(_claims(["Q-thai-div", "Q188686"]), lambda q: parents(q, [])) == ("place", "Q56061")
    assert cl.classify(_claims(["Q188686"]), lambda q: parents(q, []))[0] == "event"  # an occupation alone stays an event


def test_a_ship_needs_a_dated_life():
    oiler = {"P31": [_snak("Q11446")], "P607": [_snak("Q362")]}       # Kawasaki-type oiler: a class modelled as a ship
    assert sf.in_scope("ship", oiler, {}) == (False, "ship without a dated life (no inception, service or events): a class, not one ship")
    assert sf.in_scope("ship", {**oiler, "P729": [_tsnak("+1938-01-01T00:00:00Z")]}, {})[0]


def test_the_box_keeps_the_pacific_and_leaves_the_us_west_coast_out():
    assert im.in_asia_pacific(-157.9, 21.3) and im.in_asia_pacific(-134.4, 58.3)   # Honolulu, Juneau
    assert not im.in_asia_pacific(-122.33, 47.6) and not im.in_asia_pacific(-122.68, 45.52)  # Seattle, Portland
    assert not im.in_asia_pacific(-117.16, 32.72)                                  # San Diego


def test_aliases_are_proper_names_of_the_entity_only():
    L = lambda title, text: sf.LinkOccurrence(title, text, 1, 10, "Page", 0, 0, 0)
    res = {"Battle of Arawe": wp.Resolution("a"), "Arawe landing": wp.Resolution("b", redirected=True),
           "200th Division (National Revolutionary Army)": wp.Resolution("c", redirected=True),
           "13th Air Force": wp.Resolution("d", redirected=True)}
    links = [L("Battle of Arawe", t) for t in ["Arawe"] * 3 + ["landed at Arawe"] * 3 + ["Landing at Arawe"] * 2 + ["Battle of Arawe"]]
    links += [L("Arawe landing", "Arawe landing")] * 2 + [L("200th Division (National Revolutionary Army)", "200th Division")] * 3
    links += [L("13th Air Force", "13th Air Force")] * 2
    names = {"lae": {"Q2"}, "landing at arawe": {"Q-other"}}
    got = sf.link_aliases("Battle of Arawe", links, res, ["13th Air Force"], names, "Q1")
    # "Arawe" is a fragment of the label, "landed at Arawe" no name, "Landing at Arawe" another entity's name,
    # "Arawe landing" and "200th Division" come only through redirects and are no Wikidata alias
    assert got == ["13th Air Force"]


UNIT_PAGE = """'''15th Operations Group'''
{{Infobox military unit
| unit_name = 15th Operations Group
| image = [[File:15og.png|200px|alt=a|emblem]]
| dates = 1940–1946; 1955–present
| battles = {{plainlist|
* World War II – [[Asiatic-Pacific Theater|Asiatic-Pacific]]
* [[Attack on Pearl Harbor|Pearl Harbor]]<ref>{{cite web|title=[[Not a battle]]}}</ref>
}}
| decorations = [[Distinguished Unit Citation]]
}}
The group ...
"""


def test_find_infobox_splits_fields_at_top_level_pipes_only():
    name, fields = wp.find_infobox(UNIT_PAGE, ["Infobox military unit"])
    assert name == "Infobox military unit" and set(fields) == {"unit_name", "image", "dates", "battles", "decorations"}
    value, offset = fields["battles"]
    assert UNIT_PAGE[offset:offset + len(value)] == value and "[[Attack on Pearl Harbor|Pearl Harbor]]" in value
    assert wp.find_infobox(UNIT_PAGE, ["Infobox military conflict"]) is None


def test_units_get_their_theatre_from_their_infobox_battles(tmp_path, monkeypatch):
    res = {"15th Operations Group": wp.Resolution("15th Operations Group", qid="Q15og", page_qid="Q15og", lastrevid=77)}
    corpus = sf.Corpus([], {}, [], res, {}, 0)
    monkeypatch.setattr(sf.wp, "fetch_revisions", lambda revids, *a, **k: {
        r: {"revid": r, "wikitext": UNIT_PAGE} for r in revids})
    resolved = {"Asiatic-Pacific Theater": "Q-apt", "Attack on Pearl Harbor": "Q52418"}

    class Resolver:
        def __init__(self, *a, **k):
            pass

        def resolve(self, titles):
            assert "Not a battle" not in titles  # a link inside a citation is no battle
            return {t: wp.Resolution(t, qid=resolved.get(t), page_qid=resolved.get(t)) for t in titles}

    monkeypatch.setattr(sf.wp, "TitleResolver", Resolver)
    lite = {"Q52418": {"claims": {"P361": [_snak("Q184425")]}}, "Q-apt": {"claims": {}}}
    monkeypatch.setattr(sf.wp, "fetch_lite", lambda qids, *a, **k: {q: lite.get(q, {"claims": {}}) for q in qids})
    got = sf.infobox_battles_of(["Q15og"], {"Q15og": {"claims": {}}}, lite, corpus, sf.Ctx(tmp_path, tmp_path, tmp_path))
    assert got == {"Q15og": ["Q-apt", "Q52418"]}
    assert sf.in_scope("unit", {}, lite, got["Q15og"]) == (True, "fought in Q52418 (infobox battles)")


def test_an_event_point_far_from_its_stated_location_is_dropped():
    makin = {"id": "Q660232::selffix", "wikidata_id": "Q660232", "name": "Makin Island raid", "type": "event",
             "geometry": {"type": "Point", "coordinates": [-172.833, 3.15]}, "attributes": {"selffix": {}},
             "geolocation_evidence": {"source_kind": "WIKIDATA_P625_ENRICHMENT", "statement_id": "s1"}}
    near = json.loads(json.dumps(makin)) | {"wikidata_id": "Q-near", "geometry": {"type": "Point", "coordinates": [172.9, 3.2]}}
    full = {q: {"claims": {"P276": [_snak("Q867325")]}} for q in ("Q660232", "Q-near")}
    lite = {"Q867325": {"claims": {"P625": [_pt(3.1678, 172.8258)], "P31": [_snak("Q42523")]}},  # Butaritari, an atoll
            "Q37660": {"claims": {"P625": [_pt(12.0, 113.0)], "P31": [_snak("Q165")]}}}         # South China Sea, a sea
    parents = {"Q42523": ["Q23442"], "Q23442": ["Q-landmass"], "Q-landmass": ["Q82794"],  # an island is a region 2 steps up
               "Q165": ["Q-water"], "Q-water": ["Q15324"]}.get
    sinking = json.loads(json.dumps(makin)) | {"wikidata_id": "Q700569", "geometry": {"type": "Point", "coordinates": [104.5, 3.6]}}
    full["Q700569"] = {"claims": {"P276": [_snak("Q37660")]}}
    dropped = sf.check_event_points([makin, near, sinking], full, lite, lambda q: parents(q, []))
    assert [d[0] for d in dropped] == ["Q660232"] and 1500 < dropped[0][2] < 1700
    assert sinking["geometry"]["coordinates"] == [104.5, 3.6]  # 1,300 km from the sea's centre, but a sea says nothing
    assert "geometry" not in makin and makin["attributes"]["selffix"]["point_dropped"]["coordinates"] == [-172.833, 3.15]
    assert near["geometry"]["coordinates"] == [172.9, 3.2]


def test_same_name_events_are_one_only_when_their_years_can_be_the_same():
    assert not sf.same_event_time((1942, 1942), (1939, 1939))   # the battles of Changsha
    assert sf.same_event_time((1944, 1945), (1945, 1945)) and sf.same_event_time((1944, 1944), (None, None))
    battle = {"type": "location", "attributes": {"kind": "battle", "start_date": "+1944-05-27T00:00:00Z"}}
    assert sf._record_class(battle) == "event" and sf._record_years(battle) == (1944, 1944)


# ----------------------------------------------------------------- step 5: infobox facts

from graph_enrichment import infobox as ib  # noqa: E402

MILNE_BAY = """{{Infobox military conflict
| conflict = Battle of Milne Bay
| place = Off [[Milne Bay]], [[Territory of Papua]]<br>Near [[Rabi, Papua New Guinea|Rabi]]
| combatant1 = {{plainlist |
* {{flagcountry|Australia}}
* {{flagicon|United States|1912}} [[United States]]
* [[File:Flag of the Netherlands.svg|20px]] Netherlands
* {{flagcountry|Dominion of New Zealand}}
'''Supported by:'''
* {{flag|United Kingdom}}
}}
| combatant2 = {{flagcountry|Empire of Japan}}<ref>{{cite web|title=[[Empire of Japan]] [[Germany]]}}</ref>
| commander1 = {{flagicon|Australia}} [[Cyril Clowes]]
| commander2 = {{flagicon|Empire of Japan|naval}} [[Shojiro Hayashi]]{{KIA}}<br>[[Cyril Clowes]]
| units1 = {{tree list}}
* [[7th Brigade (Australia)|7th Infantry Brigade]]
<br />''Naval support from:''<br />* [[HMAS Arunta]]
{{tree list/end}}
| strength1 = 9,458
| casualties1 = [[Killed in action|167 killed]]
}}
Body text."""


def test_a_place_link_inside_a_longer_name_is_no_place():
    value = (" [[Rabaul]], [[New Britain]]<br/>Territory of [[New Guinea]]<br>{{flagicon|Japan}} ''[[Truk]]''<br>Off [[Savo Island]]"
             "<br>East of [[Samar]]<br>Australian [[Territory of Papua|Papua]]")
    got = [(m.title, m.qualifier) for m in ib.place_mentions(value, 0)]
    # "Territory of [[New Guinea]]" names the territory, "East of [[Samar]]" a direction; an adjective is fine
    assert got == [("Rabaul", None), ("Truk", None), ("Savo Island", "off"), ("Territory of Papua", None)]


def test_the_window_is_the_agents():
    from geo_agent.graph_store import SCOPE_END, SCOPE_START
    assert sf.AGENT_WINDOW == (int(SCOPE_START), int(SCOPE_END))


def test_the_location_field_counts_like_place():
    page = MILNE_BAY.replace("| place = ", "| location = ")
    got = [(kind, field, m.title) for kind, field, _, m in sf.conflict_mentions({"wikitext": page}) if kind == "place"]
    assert got == [("place", "location", "Milne Bay"), ("place", "location", "Rabi, Papua New Guinea")]


def test_conflict_fields_give_sides_commanders_units_and_places():
    got = [(kind, field, side, m.title, m.how, m.qualifier) for kind, field, side, m in sf.conflict_mentions({"wikitext": MILNE_BAY})]
    assert ("combatant", "combatant1", 1, "Australia", "flag", None) in got
    assert ("combatant", "combatant1", 1, "United States", "flag", None) in got and ("combatant", "combatant1", 1, "United States", "link", None) in got
    assert not any(t in ("Netherlands", "United Kingdom", "Germany") for *_, t, _, _ in got)  # a bare flag image, "Supported by", a citation
    assert ("combatant", "combatant2", 2, "Empire of Japan", "flag", None) in got
    commanders = [(side, t, how) for kind, _, side, t, how, _ in got if kind == "commander"]
    assert commanders == [(1, "Cyril Clowes", "link"), (2, "Shojiro Hayashi", "link"), (2, "Cyril Clowes", "link")]  # no flag countries
    assert [t for kind, *_, t, _, _ in got if kind == "units"] == ["7th Brigade (Australia)"]  # "Naval support from" left out
    assert [(t, q) for kind, *_, t, _, q in got if kind == "place"] == [("Milne Bay", "off"), ("Rabi, Papua New Guinea", "near")]
    assert not any(kind in ("strength", "casualties") for kind, *_ in got)
    m = next(x for k, _, _, x in sf.conflict_mentions({"wikitext": MILNE_BAY}) if x.title == "Cyril Clowes")
    assert MILNE_BAY[m.start:].startswith("[[Cyril Clowes]]")


def test_battle_items_and_exact_text_matching():
    value = """{{tree list}}
* [[World War II]]
** [[Guadalcanal campaign]]
*World War II: American Theater (1942–1944); Asiatic-Pacific Campaign (1941–1942, 1944–1945)
* {{hidden|See list|Battle of Bataan}}<ref>{{cite web|title=Battle of Midway}}</ref>
{{tree list/end}}"""
    titles, items = ib.battle_items(value)
    assert titles == ["World War II", "Guadalcanal campaign"]
    texts = dict(items)
    assert texts["American Theater"] == (1942, 1944) and texts["Asiatic-Pacific Campaign"] == (1941, 1945)
    assert "Battle of Bataan" in texts and "Battle of Midway" not in texts  # a citation is no battle
    from geo_agent.graph_store import normalize_name
    index = {normalize_name("Asiatic-Pacific Campaign"): [("Q-apc", (1941, 1945))],
             normalize_name("Battle of Bataan"): [("Q-bataan1", (1942, 1942)), ("Q-bataan2", (1945, 1945))],
             normalize_name("American Theater"): [("Q-at", (1950, 1951))]}
    assert sf.match_text_battles(items, index) == ["Q-apc"]  # "Battle of Bataan" with no year is ambiguous: nothing
    assert sf.match_text_battles([("Battle of Bataan", (1945, 1945))], index) == ["Q-bataan2"]
    assert sf.match_text_battles([("Bataan", None), ("Battle of Bataan Peninsula", None)], index) == []  # never fuzzy


def test_step5_writes_sided_facts_and_never_touches_team_edges(tmp_path, monkeypatch):
    pt = {"type": "Point", "coordinates": [150.4, -10.3]}
    graph = {"metadata": {}, "relation_catalog": {"wd:P710": {"id": "wd:P710", "level1_families": ["PARTICIPATION_ROLE"]}},
             "nodes": [
                 {"id": "Q113621::LOC", "wikidata_id": "Q113621", "name": "Battle of Milne Bay", "type": "location",
                  "attributes": {"kind": "battle", "start_date": "+1942-08-25T00:00:00Z"}},
                 {"id": "Q408::LOC", "wikidata_id": "Q408", "name": "Australia", "type": "location", "attributes": {"entity_class": "country"}},
                 {"id": "Q30::ORG", "wikidata_id": "Q30", "name": "United States", "type": "org", "attributes": {"entity_class": "country"}},
                 {"id": "Q188712::LOC", "wikidata_id": "Q188712", "name": "Empire of Japan", "type": "location", "attributes": {"entity_class": "country"}},
                 {"id": "Q11700921::PER", "wikidata_id": "Q11700921", "name": "Cyril Clowes", "type": "person"},
                 {"id": "Q1781066::LOC", "wikidata_id": "Q1781066", "name": "Milne Bay", "type": "location", "geometry": pt},
                 {"id": "Q-territory::LOC", "wikidata_id": "Q7000008", "name": "Territory of Papua", "type": "location"},
                 {"id": "Q-7th::ORG", "wikidata_id": "Q7000007", "name": "7th Brigade", "type": "org"},
                 {"id": "Q-guadalcanal::event", "wikidata_id": "Q7000009", "name": "Guadalcanal campaign", "type": "event",
                  "attributes": {"start_date": "+1942-08-07T00:00:00Z"}},
                 {"id": "Q483039::event", "wikidata_id": "Q483039", "name": "Battle of Inchon", "type": "event",
                  "geometry": {"type": "Point", "coordinates": [126.6, 37.5]},
                  "attributes": {"kind": "battle", "start_date": "+1950-09-15T00:00:00Z"}},  # in the box, out of the window
                 {"id": "Q16470::event", "wikidata_id": "Q16470", "name": "Normandy landings", "type": "event",
                  "attributes": {"kind": "landing", "start_date": "+1944-06-06T00:00:00Z"}},
                 {"id": "Q184425::event", "wikidata_id": "Q184425", "name": "Pacific War", "type": "event",
                  "attributes": {"start_date": "+1941-12-07T00:00:00Z"}},  # no kind: war-level by its name
                 {"id": "Q362::event", "wikidata_id": "Q362", "name": "World War II", "type": "event", "attributes": {"kind": "war",
                  "start_date": "+1939-09-01T00:00:00Z"}}],
             "edges": [
                 {"id": "team-1", "source": "Q113621", "target": "Q408", "type": "wd:P710", "count": 1},
                 {"id": "wdenrich:x", "source": "Q113621", "target": "Q188712", "type": "wd:P710", "count": 1, "provenance": "wikidata"}]}
    team_edge = copy.deepcopy(graph["edges"][0])
    team_nodes = copy.deepcopy(graph["nodes"])
    clowes_page = """{{Infobox military person
| name = Cyril Clowes
| battles = [[World War II]]
* [[Pacific War]]
* [[Normandy landings]]
* [[Guadalcanal campaign]]
* Battle of Milne Bay (1942)
}}"""
    pages = {"Q113621": {"revid": 501, "page_id": 9, "title": "Battle of Milne Bay", "wikitext": MILNE_BAY},
             "Q483039": {"revid": 503, "page_id": 11, "title": "Battle of Inchon", "wikitext": MILNE_BAY},
             "Q11700921": {"revid": 502, "page_id": 10, "title": "Cyril Clowes", "wikitext": clowes_page}}
    monkeypatch.setattr(sf, "pages_of", lambda qids, lite, ctx: {q: p for q, p in pages.items() if q in qids})
    titles = {"Australia": "Q408", "United States": "Q30", "Empire of Japan": "Q188712", "Cyril Clowes": "Q11700921",
              "Shojiro Hayashi": "Q6782410", "7th Brigade (Australia)": "Q7000007", "Milne Bay": "Q1781066",
              "Rabi, Papua New Guinea": "Q7000010", "Dominion of New Zealand": "Q2594990", "World War II": "Q362", "Pacific War": "Q184425", "Normandy landings": "Q16470", "Guadalcanal campaign": "Q7000009"}

    class Resolver:
        def __init__(self, *a, **k):
            pass

        def resolve(self, ts):
            return {t: wp.Resolution(t, qid=titles.get(t), page_qid=titles.get(t), status="ok" if t in titles else "missing") for t in ts}

    monkeypatch.setattr(sf.wp, "TitleResolver", Resolver)
    country = {"P31": [_snak("Q6256")]}
    lite = {"Q408": {"claims": country}, "Q30": {"claims": country}, "Q188712": {"claims": country},
            "Q11700921": {"claims": _claims(["Q5"])},
            "Q6782410": {"label": "Masajiro Hayashi", "claims": _claims(["Q5"]), "lastrevid": 77},
            "Q7000007": {"claims": _claims(["Q176799"])}, "Q1781066": {"claims": {"P31": [_snak("Q39594")], "P625": [_pt(-10.3, 150.4)]}},
            "Q7000010": {"label": "Rabi", "claims": {"P31": [_snak("Q486972")], "P625": [_pt(-10.35, 150.5)]}},
            "Q113621": {"claims": {"P361": [_snak("Q184425")]}},
            "Q7000009": {"claims": {"P361": [_snak("Q184425")]}},  # Guadalcanal campaign: part of the Pacific War
            "Q16470": {"claims": {"P361": [_snak("Q362")], "P625": [_pt(49.3, -0.6)]}},  # Normandy: outside the theatre
            "Q2594990": {"label": "Dominion of New Zealand", "claims": {"P31": [_snak("Q7000011")]}},
            "Q7000011": {"claims": {"P279": [_snak("Q1048835")]}},  # dominion -> political territorial entity
            "Q39594": {"claims": {"P279": [_snak("Q15324")]}}}
    monkeypatch.setattr(sf.wp, "fetch_lite", lambda qids, *a, **k: {q: lite.get(q, {"claims": {}}) for q in qids})
    report = sf.step5_infoboxes(graph, sf.Ctx(tmp_path, tmp_path, tmp_path))
    assert report["events_outside_window"] == 1
    assert not any(e.get("selffix_step") == 5 and e["source"] == "Q483039" for e in graph["edges"])  # Inchon (1950): not read

    new = {(e["source"], e["type"], e["target"]): e for e in graph["edges"] if e.get("selffix_step") == 5}
    assert new[("Q113621", "wd:P710", "Q30")]["side"] == 1 and new[("Q113621", "wd:P710", "Q30")]["provenance"] == "wikipedia_infobox"
    assert new[("Q113621", "wd:P710", "Q7000007")]["participant_kind"] == "unit"
    assert new[("Q113621", "wd:P276", "Q1781066")]["place_qualifier"] == "off"
    assert new[("Q113621", "wd:P4791", "Q6782410")]["side"] == 2                       # Hayashi, added from the infobox
    assert ("Q113621", "wd:P4791", "Q11700921") not in new                             # Clowes listed on both sides: unclear, dropped
    assert not any(t == "Q7000008" for _, typ, t in new if typ == "wd:P276")         # only the first link of each place line
    assert not any(typ == "wd:P276" and t in ("Q408", "Q30", "Q188712") for _, typ, t in new)  # a country is never a place
    assert ("Q113621", "wd:P710", "Q408") not in new and graph["edges"][0] == team_edge  # existing team fact: confirmed, untouched
    assert graph["edges"][1]["side"] == 2                                               # this build's own R74 edge takes the side
    assert new[("Q11700921", "wd:P607", "Q7000009")]["mention"] == "link"         # people's battles, to graph battles
    assert new[("Q11700921", "wd:P607", "Q113621")]["mention"] == "text"                # "Battle of Milne Bay (1942)", exact
    assert ("Q11700921", "wd:P607", "Q362") not in new                                 # never to a war
    assert ("Q11700921", "wd:P607", "Q184425") not in new                              # nor to one only its name says is a war
    assert ("Q11700921", "wd:P607", "Q16470") not in new                               # nor to a battle outside the theatre
    hayashi = next(n for n in graph["nodes"] if n["wikidata_id"] == "Q6782410")
    assert (hayashi["name"], hayashi["type"], hayashi["provenance"]) == ("Masajiro Hayashi", "person", "wikipedia_infobox")
    s0 = hayashi["sources"][0]
    assert (s0["revision_id"], s0["position"]["field"], s0["position"]["side"]) == (501, "commander2", 2) and "CC BY-SA" in s0["license"]
    rabi = next(n for n in graph["nodes"] if n["wikidata_id"] == "Q7000010")
    assert rabi["type"] == "location" and rabi["geometry"]["coordinates"] == [150.5, -10.35]
    nz = next(n for n in graph["nodes"] if n["wikidata_id"] == "Q2594990")  # a polity side: an org, never a place
    assert (nz["type"], nz["attributes"]["entity_class"], nz.get("geometry")) == ("org", "country", None)
    assert new[("Q113621", "wd:P710", "Q2594990")]["side"] == 1
    assert graph["nodes"][:len(team_nodes)] == team_nodes
    assert report["confirmed_existing"]["wd:P710"] == 2 and report["r74_edges_given_a_side"] == 1


def test_a_redirect_counts_only_when_it_names_its_target():
    lite = {"Q-halsey": {"label": "William Halsey Jr."}, "Q-8th": {"label": "8th Division"},
            "Q-108th": {"label": "108th Cavalry Regiment"}, "Q-23rd": {"label": "23rd Air Army"},
            "Q-3rd": {"label": "United States Third Fleet"}, "Q-truk": {"label": "Chuuk Lagoon"},
            "Q-wil": {"label": "Theodore Stark Wilkinson", "aliases": ["Theodore S. Wilkinson"]}}
    yes = lambda title, q: sf.names_the_target(ib.Mention(title, title, "link", 0), q, lite)
    assert yes("William Halsey, Jr.", "Q-halsey") and yes("William F. Halsey", "Q-halsey")
    assert yes("IJA 8th Division", "Q-8th") and yes("United States 3rd Fleet", "Q-3rd") and yes("Truk Lagoon", "Q-truk")
    assert yes("Theodore S. Wilkinson", "Q-wil")                          # a Wikidata alias
    lite["Q-503"] = {"label": "503rd Infantry Regiment"}
    assert yes("503d Infantry Regiment (United States)", "Q-503")         # "503d" is 503
    assert not yes("101st Coast Artillery Battalion", "Q-108th")         # another unit's number
    assert not yes("12th Air Army", "Q-23rd")


# ----------------------------------------------------------------- step 6: types and names

def test_decide_class_keeps_countries_and_finds_things():
    parents = {"Q-kongo": ["Q182531"], "Q-torpedo-model": [], "Q47217": ["Q728"]}.get
    up6 = lambda q: parents(q, [])
    assert sf.decide_class({"P31": [_snak("Q6256")]}, up6, False) == "country"            # kept as it is
    assert sf.decide_class({"P31": [_snak("Q-kongo")]}, up6, False) == "ship"             # Kirishima
    assert sf.decide_class({"P31": [_snak("Q515")], }, lambda q: {"Q515": ["Q486972"]}.get(q, []), False) == "place"
    assert sf.decide_class({"P279": [_snak("Q47217")]}, up6, False) == "other"            # a torpedo model is a class
    assert sf.decide_class({}, up6, True) == "event"                                      # an R74 military kind
    assert sf.decide_class({}, up6, False) is None


def test_step6_types_points_and_names(tmp_path, monkeypatch):
    graph = {"metadata": {}, "edges": [], "nodes": [
        {"id": "Q34664::ORG", "wikidata_id": "Q34664", "name": "Hiroshima", "type": "org"},               # a city typed org
        {"id": "Q34664::LOC", "wikidata_id": "Q34664", "name": "Hiroshima", "type": "location",
         "geometry": {"type": "Point", "coordinates": [132.45, 34.39]}},
        {"id": "Q-kiri::LOC", "wikidata_id": "Q7000020", "name": "Kirishima", "type": "location"},       # a ship typed location
        {"id": "Q30::ORG", "wikidata_id": "Q30", "name": "United States", "type": "org",
         "attributes": {"entity_class": "country"}},
        {"id": "Q-battle::LOC", "wikidata_id": "Q7000021", "name": "Battle of X", "type": "location",
         "attributes": {"kind": "battle"}},
        {"id": "Q7000022::selffix", "wikidata_id": "Q7000022", "name": "26th Space Aggressor Squadron", "type": "org",
         "attributes": {"name_source": "wikidata_label"}},
        {"id": "Q7000023::ORG", "wikidata_id": "Q7000023", "name": "50th Education Squadron", "type": "org"},  # a team name
        {"id": "Q7000026::selffix", "wikidata_id": "Q7000026", "name": "5th Corps", "type": "org",
         "attributes": {"name_source": "wikidata_label"}},
        {"id": "Q7000024::selffix", "wikidata_id": "Q7000024", "name": "Eritrea", "type": "other",
         "attributes": {"name_source": "wikidata_label"}},
        {"id": "Q986::LOC", "wikidata_id": "Q986", "name": "Eritrea", "type": "location", "attributes": {"entity_class": "country"}},
        {"id": "Q7000025::MISC", "wikidata_id": "Q7000025", "name": "Type 93", "type": "MISC"},
        {"id": "Q208127::LOC", "wikidata_id": "Q208127", "name": "Battle of Tsushima", "type": "location"},   # 1905
        {"id": "Q151187::MISC", "wikidata_id": "Q151187", "name": "Potsdam Conference", "type": "MISC"},     # Europe
        {"id": "Q1726::ORG", "wikidata_id": "Q1726", "name": "Munich", "type": "org"},                       # Europe
        {"id": "Q953903::selffix", "wikidata_id": "Q953903", "name": "Northern Rhodesia", "type": "org",
         "attributes": {"entity_class": "country"}}]}
    lite = {"Q34664": {"claims": {"P31": [_snak("Q515")], "P625": [_pt(34.39, 132.45)]}, "label": "Hiroshima"},
            "Q7000020": {"claims": {"P31": [_snak("Q7000030")]}, "label": "Kirishima"},
            "Q30": {"claims": {"P31": [_snak("Q6256")]}, "label": "United States"},
            "Q7000021": {"claims": {"P361": [_snak("Q184425")]}, "label": "Battle of X"},
            "Q208127": {"claims": {"P31": [_snak("Q178561")], "P585": [_tsnak("+1905-05-27T00:00:00Z")]}, "label": "Battle of Tsushima"},
            "Q151187": {"claims": {"P31": [_snak("Q1190554")], "P585": [_tsnak("+1945-07-17T00:00:00Z")],
                                   "P625": [_pt(52.42, 13.07)]}, "label": "Potsdam Conference"},
            "Q1726": {"claims": {"P31": [_snak("Q515")], "P625": [_pt(48.14, 11.58)]}, "label": "Munich"},
            "Q7000022": {"claims": {"P31": [_snak("Q176799")]}, "label": "26th Space Aggressor Squadron"},
            "Q7000023": {"claims": {"P31": [_snak("Q176799")]}, "label": "50th Education Squadron"},
            "Q7000026": {"claims": {"P31": [_snak("Q176799")]}, "label": "5th Corps"},
            "Q7000024": {"claims": {"P31": [_snak("Q11446")]}, "label": "Eritrea", "enwiki": "Italian sloop Eritrea"},
            "Q986": {"claims": {"P31": [_snak("Q6256")]}, "label": "Eritrea"},
            "Q7000025": {"claims": {"P279": [_snak("Q7000031")]}, "label": "Type 93"},
            "Q515": {"claims": {"P279": [_snak("Q486972")]}}, "Q7000030": {"claims": {"P279": [_snak("Q182531")]}},
            "Q7000031": {"claims": {"P279": [_snak("Q728")]}},
            "Q953903": {"claims": {"P31": [_snak("Q7000032")], "P625": [_pt(-15.0, 28.0)]}, "label": "Northern Rhodesia"},
            "Q7000032": {"claims": {"P279": [_snak("Q56061")]}}}  # protectorate -> administrative territorial entity
    monkeypatch.setattr(sf.wp, "fetch_lite", lambda qids, *a, **k: {q: lite.get(q, {"claims": {}}) for q in qids})
    L = lambda title, text: sf.LinkOccurrence(title, text, 1, 10, "Page", 0, 0, 0)
    links = [L("26th Space Aggressor Squadron", "26th Bombardment Squadron")] * 3 + [L("50th Education Squadron", "431st Bombardment Squadron")] * 3
    links += [L("200th Division (National Revolutionary Army)", "200th Division")] * 3
    links += [L("5th Corps", "200th Division")]  # a piped direct link: still a sub-unit's name
    res = {"26th Space Aggressor Squadron": wp.Resolution("a", qid="Q7000022"), "50th Education Squadron": wp.Resolution("b", qid="Q7000023"),
           "200th Division (National Revolutionary Army)": wp.Resolution("c", qid="Q7000026", redirected=True),
           "5th Corps": wp.Resolution("d", qid="Q7000026")}
    monkeypatch.setattr(sf, "load_corpus", lambda g, ctx: sf.Corpus([], {}, links, res, {}, 0))
    report = sf.step6_types(graph, sf.Ctx(tmp_path, tmp_path, tmp_path, out_dir=tmp_path))
    node = {n["id"]: n for n in graph["nodes"]}
    h = node["Q34664::ORG"]
    assert (h["type"], h["attributes"]["ner_type"], h["geometry"]["coordinates"]) == ("location", "org", [132.45, 34.39])
    k = node["Q-kiri::LOC"]
    assert (k["type"], k["attributes"]["entity_class"], k["attributes"]["ner_type"]) == ("other", "ship", "location")
    assert node["Q30::ORG"]["type"] == "org" and node["Q30::ORG"]["attributes"]["entity_class"] == "country"   # kept
    nr = node["Q953903::selffix"]  # a polity step 5 added: stays an org, no point, though its class reads as a place
    assert (nr["type"], nr.get("geometry"), nr["attributes"]["wikidata_type"]) == ("org", None, "country")
    assert node["Q-battle::LOC"]["type"] == "event"
    # not retyped: R84 would drop a 1905 event at load, and a European event or place would reach Pacific maps
    assert node["Q208127::LOC"]["type"] == "location" and node["Q208127::LOC"]["attributes"]["wikidata_type"] == "event"
    assert node["Q151187::MISC"]["type"] == "MISC" and node["Q1726::ORG"]["type"] == "org"
    assert report["kept_outside_window_or_theatre"] == {"event outside the agent's window": 1, "event outside the theatre": 1,
                                                        "place outside the theatre": 1}
    assert node["Q7000025::MISC"]["type"] == "MISC" and node["Q7000025::MISC"]["attributes"]["wikidata_type"] == "other"
    sq = node["Q7000022::selffix"]
    assert (sq["name"], sq["aliases"], sq["attributes"]["name_source"]) == ("26th Bombardment Squadron", ["26th Space Aggressor Squadron"], "corpus_link_text")
    assert node["Q7000023::ORG"]["name"] == "50th Education Squadron"                    # a team record keeps its name
    assert node["Q7000026::selffix"]["name"] == "5th Corps"  # "200th Division" only redirects to it: a sub-unit, not its name
    assert (node["Q7000024::selffix"]["name"], node["Q7000024::selffix"]["attributes"]["name_source"]) == ("Italian sloop Eritrea", "enwiki_title")
    assert node["Q986::LOC"]["name"] == "Eritrea"
    assert report["check_ships_typed_location"] == 0 and report["check_battle_like_records_not_event"] == 0
    declared = json.loads((tmp_path / "step6_declared.json").read_text())
    assert set(declared["type"]) == {"Q34664::ORG", "Q-kiri::LOC", "Q-battle::LOC"}
    assert declared["geom"] == ["Q34664::ORG"] and set(declared["name"]) == {"Q7000022::selffix", "Q7000024::selffix"}


def test_impact_takes_declared_changes_per_field():
    after = _snap()
    after["nodes"]["Q2::LOC"]["name"] = "Isle Renamed"
    after["nodes"]["Q1::event"]["type"] = "location"
    c = im.compare(_snap(), after, {"name": {"Q2::LOC"}, "type": {"Q1::event"}})
    assert c["changed_nodes"] == [] and c["declared_changes"] == {"name": 1, "type": 1}
    assert im.compare(_snap(), after, {"name": {"Q2::LOC"}})["changed_nodes"] == [
        {"id": "Q1::event", "field": "type", "before": "event", "after": "location"}]


def test_an_entity_named_after_another_record_is_no_loss():
    after = _snap()
    after["nodes"]["Q1::MISC"] = after["nodes"].pop("Q1::event")  # the merged entity now carries another record's id
    c = im.compare(_snap(), after)
    assert c["lost_nodes"] == [] and c["node_id_changes"] == [["Q1::event", "Q1::MISC"]] and c["changed_nodes"] == []
    del after["nodes"]["Q1::MISC"]
    assert im.compare(_snap(), after)["lost_nodes"] == ["Q1::event"]  # the QID is gone: a loss


def test_r100_people_added_by_the_build_get_their_life_dates():
    from graph_enrichment import selffix as sf
    t = lambda v: {"snaktype": "value", "datavalue": {"value": {"time": v, "precision": 11}}}
    c = lambda prop, snak: [{"rank": "normal", "mainsnak": {**snak, "property": prop}}]
    human = {"snaktype": "value", "datavalue": {"value": {"id": "Q5"}}}
    graph = {"nodes": [{"id": "Q1::selffix", "wikidata_id": "Q1", "name": "A", "type": "person"},
                       {"id": "Q2::selffix", "wikidata_id": "Q2", "name": "B", "type": "org"}]}
    lite = {"Q1": {"claims": {"P31": c("P31", human), "P570": c("P570", t("+1943-04-18T00:00:00Z"))}},
            "Q2": {"claims": {"P31": c("P31", human), "P570": c("P570", t("+1950-01-01T00:00:00Z"))}}}
    assert sf.apply_life_dates(graph, lite) == 1
    assert graph["nodes"][0]["attributes"]["death_date"] == "+1943-04-18T00:00:00Z"
    assert "attributes" not in graph["nodes"][1]   # not a person record


def test_r103_page_titles_pass_names_the_pages_the_graph_gives_by_id_only(tmp_path, monkeypatch):
    graph = {"nodes": [{"id": "a", "sources": [{"page": "enwiki:1", "page_title": "Battle of Leyte Gulf"}]},
                       {"id": "b", "sources": [{"page": "enwiki:2"}, {"page": "enwiki:3"}, {"page": "Midway Atoll"}]}],
             "edges": [{"source": "a", "target": "b", "sources": [{"page": "enwiki:2"}]}]}
    assert sf.untitled_pages(graph) == {2, 3}
    asked = []

    def get(url, params):
        asked.append(params["pageids"])
        return {"query": {"pages": [{"pageid": 2, "title": "William Halsey Jr."}, {"pageid": 3, "missing": True}]}}

    real = wp.fetch_page_titles
    monkeypatch.setattr(wp, "fetch_page_titles", lambda ids, cache, offline: real(ids, cache, offline, get=get))
    report = sf.stage_titles(graph, sf.Ctx(tmp_path, tmp_path, tmp_path))
    assert graph["metadata"]["page_titles"] == {"enwiki:2": "William Halsey Jr."}  # a deleted page is left out
    assert report == {"pages_without_title": 2, "titles_added": 1} and asked == ["2|3"]
    assert real([2, 3], tmp_path, offline=True) == {2: "William Halsey Jr.", 3: None}  # cached
