"""Impact of a self-fix step on the graph, measured through GraphStore only (spec R97). No LLM.

    # once (step 1): the fixed probe list, chosen by rule from the graph before any step
    uv run python -m graph_enrichment.impact probes --graph BEFORE.json --out graph_enrichment/probes.json \
        --exclude evals/gold_real.jsonl evals/gold_heldout.jsonl
    # per graph: counts, every node and edge key, probe outputs (one process each, for memory)
    uv run python -m graph_enrichment.impact snapshot --graph G.json --probes graph_enrichment/probes.json --out G.snap.json
    # per step: before vs after
    uv run python -m graph_enrichment.impact report --before A.snap.json --after B.snap.json --step 1 \
        --heldout evals/gold_heldout.jsonl --out impact_step1.md

The probe list is chosen by rule, not by hand: per class (event, person, unit, place, ship), the
most-mentioned entities plus a seeded random draw from the rest with at least 3 mentions, leaving
out every QID of the gold sets so the probes are a third, independent measure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
import resource
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

QID_RE = re.compile(r"^Q\d+$")
SEED = 97
# Hub probes for noise detection, chosen by the supervisor (2026-11-08): the entities with the most
# relations, where a new noisy place would show first. Kept apart from the rule-chosen probes.
HUBS = [{"qid": "Q30", "name": "United States"}, {"qid": "Q188712", "name": "Empire of Japan"},
        {"qid": "Q219712", "name": "Imperial Japanese Navy"}, {"qid": "Q11220", "name": "United States Navy"},
        {"qid": "Q184425", "name": "Pacific War"}, {"qid": "Q11218", "name": "United States Marine Corps"}]
SCOPE = ("1938", "1948")
COMMAND_TYPES = {"wd:P4791", "COMMANDED", "wd:P598", "http://dbpedia.org/ontology/commander"}
WORSE_IF_UP = {"nodes_name_is_id", "nodes_without_text", "nodes_type_other", "events_without_dates",
               "locations_without_geometry", "edges_family_unresolved", "edges_family_differs_from_catalog",
               "relation_types_not_in_catalog", "edges_dropped_unknown_endpoint", "edges_ambiguous_endpoint"}
WORSE_IF_DOWN = {"locations_with_geometry"}


def qid_of(node_id: str) -> str:
    head = str(node_id).split("::", 1)[0]
    return head if QID_RE.match(head) else str(node_id)


def in_asia_pacific(lon: float | None, lat: float | None) -> bool:
    """Inside the Asia-Pacific box: lon 60E eastward across the antimeridian to 130W, lat 50S to 60N.
    130W runs through open ocean off the whole North American coast south of the Alaska panhandle,
    so Hawaii, Alaska and the Aleutians are in and the US and Canadian home front (Seattle, San
    Diego) is out."""
    if lon is None or lat is None:
        return False
    return (lon >= 60 or lon <= -130) and -50 <= lat <= 60


def _overlaps_scope(start: str | None, end: str | None) -> bool | None:
    if not (start or end):
        return None
    s, e = (start or end)[:4], (end or start)[:4]
    return not (e < SCOPE[0] or s > SCOPE[1])


# ----------------------------------------------------------------- probes


def _classes(store) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for nid, n in store.nodes.items():
        cls = (n.attributes or {}).get("entity_class")
        if not QID_RE.match(qid_of(nid)):
            continue
        if cls == "ship":
            out["ship"].append(nid)
        elif n.type == "event" and not store.is_war_level(nid):
            out["event"].append(nid)
        elif n.type == "person":
            out["person"].append(nid)
        elif n.type == "org" and cls not in ("country", "region"):
            out["unit"].append(nid)
        elif n.type == "location" and cls not in ("country", "region") and nid in store.geoms \
                and store.geoms[nid].geom_type == "Point":
            out["place"].append(nid)
    return out


def _pick(store, ids: list[str], top: int, rand: int, exclude: set[str], rng: random.Random) -> list[str]:
    pool = sorted((i for i in ids if qid_of(i) not in exclude), key=lambda i: (-len(store.nodes[i].sources), i))
    head = pool[:top]
    rest = [i for i in pool[top:] if len(store.nodes[i].sources) >= 3]
    return head + sorted(rng.sample(rest, min(rand, len(rest))), key=lambda i: (-len(store.nodes[i].sources), i))


def select_probes(store, exclude: set[str]) -> dict[str, Any]:
    """The fixed probe list (QIDs and names), by the rule in the module docstring."""
    rng = random.Random(SEED)
    cls = _classes(store)
    entry = lambda i: {"qid": qid_of(i), "name": store.nodes[i].name, "mentions": len(store.nodes[i].sources)}
    search = [entry(i) | {"class": c} for c in ("event", "person", "unit", "place", "ship")
              for i in _pick(store, cls[c], 4, 4, exclude, rng)]
    actors = [entry(i) | {"class": c} for c in ("person", "unit") for i in _pick(store, cls[c], 5, 5, exclude, rng)]
    places = [entry(i) for i in _pick(store, cls["place"], 12, 13, exclude, rng)]
    battles = [entry(i) for i in _pick(store, cls["event"], 12, 13, exclude, rng)]
    return {"rule": __doc__.strip().splitlines()[-3:], "seed": SEED, "excluded_qids": len(exclude),
            "search_entities": search, "connected_locations": actors, "what_happened_at": places, "locations_of": battles,
            "hubs": HUBS}


def gold_qids(paths: list[Path]) -> set[str]:
    out = set()
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                for v in row.values():
                    if isinstance(v, list):
                        out |= {x for x in v if isinstance(x, str) and QID_RE.match(x)}
                        out |= {x for g in v if isinstance(g, list) for x in g if isinstance(x, str) and QID_RE.match(x)}
    return out


# ----------------------------------------------------------------- snapshot


def _geom_key(n) -> str | None:
    if not n.geometry:
        return None
    g = n.geometry if isinstance(n.geometry, dict) else getattr(n.geometry, "model_dump", lambda: n.geometry)()
    return hashlib.sha1(json.dumps(g, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _point(store, nid: str) -> tuple[float | None, float | None]:
    g = store.geoms.get(nid)
    if g is None:
        return None, None
    c = g if g.geom_type == "Point" else g.representative_point()
    return round(c.x, 4), round(c.y, 4)


def _connected_rows(store, qid: str) -> list[dict[str, Any]]:
    res = []
    for r in store.connected_locations(qid):
        via = r.get("via_id")
        vs, ve = store.node_dates(store.canonical_id(via)) if via and store.canonical_id(via) in store.nodes else (None, None)
        res.append({"qid": qid_of(r["id"]), "name": r["name"], "lat": r.get("lat"), "lon": r.get("lon"),
                    "via": qid_of(via) if via else None, "via_in_scope": _overlaps_scope(vs, ve)})
    return res


def run_probes(store, probes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"search_entities": {}, "connected_locations": {}, "what_happened_at": {}, "locations_of": {}}
    for p in probes["search_entities"]:
        out["search_entities"][p["name"]] = [[qid_of(r["id"]), r["name"]] for r in store.search_entities(p["name"], limit=3)]
    for p in probes["connected_locations"]:
        out["connected_locations"][p["qid"]] = _connected_rows(store, p["qid"])
    hubs = probes.get("hubs") or []
    if hubs:
        out["hubs_connected_locations"] = {p["qid"]: _connected_rows(store, p["qid"]) for p in hubs}
        out["hubs_locations_of"] = {p["qid"]: [[qid_of(r["id"]), r["name"]] for r in store.locations_of(p["qid"])] for p in hubs}
    for p in probes["what_happened_at"]:
        nid = store.canonical_id(p["qid"])
        if nid not in store.nodes:
            out["what_happened_at"][p["qid"]] = None
            continue
        lon, lat = _point(store, nid)
        if lat is None:
            out["what_happened_at"][p["qid"]] = None
            continue
        r = store.what_happened_at(lat, lon, place_id=nid)
        out["what_happened_at"][p["qid"]] = {
            "places": sorted(qid_of(x["id"]) for x in r.get("places") or []),
            "events": sorted(qid_of(e["id"]) for e in r["events"]),
            "nearby_events": sorted(qid_of(e["id"]) for e in r.get("nearby_events") or []),
            "region_events": sorted(qid_of(e["id"]) for e in r.get("region_events") or []),
            "names": {qid_of(e["id"]): e["name"] for k in ("events", "nearby_events", "region_events") for e in r.get(k) or []},
        }
    for p in probes["locations_of"]:
        out["locations_of"][p["qid"]] = [[qid_of(r["id"]), r["name"]] for r in store.locations_of(p["qid"])]
    return out


def snapshot(graph_path: Path, probes: dict[str, Any] | None, heldout: Path | None = None) -> dict[str, Any]:
    from geo_agent.graph_store import GraphStore, normalize_name

    t0 = time.time()
    store = GraphStore.from_json(graph_path)
    load_s = round(time.time() - t0, 1)
    rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 if sys.platform == "darwin" else 1 / 1024) / 1e6)
    nodes = {nid: {"qid": qid_of(nid), "name": n.name, "type": n.type, "geom": _geom_key(n),
                   "class": (n.attributes or {}).get("entity_class"), "in_geoms": nid in store.geoms}
             for nid, n in store.nodes.items()}
    edge_keys = Counter(f"{qid_of(e.source)}|{qid_of(e.target)}|{e.type}" for e in store.edges)
    events = [i for i, n in store.nodes.items() if n.type == "event"]
    ev = Counter()
    for i in events:
        ev["events"] += 1
        ev["with_dates"] += store.node_dates(i) != (None, None)
        has_place = any(store.is_place_via(e, o) for e, o in store._objects(i, "occurred_at"))
        ev["with_places"] += has_place
        actors = store._subjects(i, "event_link")
        ev["with_participants"] += any(e.type not in COMMAND_TYPES for e, _ in actors)
        ev["with_commanders"] += any(e.type in COMMAND_TYPES for e, _ in actors)
        if store.is_war_level(i):
            continue
        # battles (events below war level): the step 5 coverage counts
        sides = {(e.model_extra or {}).get("side") for e, _ in actors if e.type not in COMMAND_TYPES}
        ev["battles"] += 1
        ev["battles_with_both_sides"] += {1, 2} <= sides
        ev["battles_with_commanders"] += any(e.type in COMMAND_TYPES for e, _ in actors)
        ev["battles_with_units"] += any((e.model_extra or {}).get("participant_kind") == "unit"
                                        or (store.nodes[o].type == "org" and (store.nodes[o].attributes or {}).get("entity_class") != "country")
                                        for e, o in actors if e.type not in COMMAND_TYPES)
        ev["battles_with_place"] += has_place
    linked = Counter()
    for i, n in store.nodes.items():
        if n.type in ("person", "org") and (n.attributes or {}).get("entity_class") not in ("country", "region"):
            hit = any(store.nodes[o].type == "event" and not store.is_war_level(o) for _, o in store._objects(i, "event_link"))
            linked[f"{n.type}_linked_to_event"] += hit
            linked[f"{n.type}_total"] += 1
    names: dict[str, set[str]] = defaultdict(set)
    for nid, n in store.nodes.items():
        if QID_RE.match(qid_of(nid)) and n.name:
            names[normalize_name(n.name)].add(qid_of(nid))
    graph_qids = {qid_of(i) for i in store.nodes if QID_RE.match(qid_of(i))}
    out = {
        "graph": str(graph_path), "load_s": load_s, "max_rss_mb": rss_mb,
        "counts": {"nodes": len(store.nodes), "entities": len(graph_qids), "edges": len(store.edges),
                   "node_types": dict(Counter(n.type for n in store.nodes.values())),
                   "edges_by_type": dict(Counter(e.type for e in store.edges).most_common()),
                   "records_with_geometry": len(store.geoms), **dict(ev), **dict(linked)},
        "coverage": store.schema()["coverage"],
        "nodes": nodes, "edge_keys": dict(edge_keys),
        "duplicate_names": {k: sorted(v) for k, v in names.items() if len(v) > 1},
        "graph_qids": sorted(graph_qids, key=lambda q: int(q[1:])),
    }
    if probes:
        out["probes"] = run_probes(store, probes)
    return out


# ----------------------------------------------------------------- comparison


_QID = re.compile(r"^Q[1-9][0-9]*$")


def heldout_missing(rows: list[dict[str, Any]], qids: set[str]) -> dict[str, list[str]]:
    """Per question: the required QIDs the graph lacks, plus every member of an answer group none of
    whose members the graph has (the same rule as the agent developer's eval, `mark_missing`)."""
    out = {}
    for row in rows:
        required = [i for key in ("expected_entity_ids", "expected_answer_ids", "expected_location_set") for i in row.get(key) or []]
        for group in row.get("expected_answer_one_of") or []:
            if not set(group) & qids:
                required += group
        out[row["id"]] = sorted({i for i in required if _QID.match(i) and i not in qids})
    return out


def compare(a: dict[str, Any], b: dict[str, Any], declared_type_changes: "set[str] | dict[str, set[str]] | None" = None,
            heldout_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Before (a) vs after (b): counts, losses, coverage, probe drift, noise, held-out gaps."""
    # declared changes: a set of node ids whose type the step changes, or {field: ids} for type / name / geom
    declared = declared_type_changes or set()
    by_field = declared if isinstance(declared, dict) else {"type": set(declared)}
    by_field = {f: set(v) for f, v in by_field.items()}
    # a merged entity's type, name or geometry changes with any of its records: declared per QID too
    declared_qids = {f: {qid_of(i) for i in v} for f, v in by_field.items()}
    # an entity is lost when its QID is gone; a node id that changes while the QID stays is the agent naming
    # a merged entity after another of its records (its type changed), compared by QID instead
    b_by_qid = {}
    for nid, y in b["nodes"].items():
        b_by_qid.setdefault(y["qid"], (nid, y))
    gone = sorted(set(a["nodes"]) - set(b["nodes"]))
    lost_nodes = [nid for nid in gone if a["nodes"][nid]["qid"] not in b_by_qid]
    id_changes = [[nid, b_by_qid[a["nodes"][nid]["qid"]][0]] for nid in gone if a["nodes"][nid]["qid"] in b_by_qid]
    changed = []
    for nid, x in a["nodes"].items():
        y = b["nodes"].get(nid)
        if y is None and x["qid"] in b_by_qid:
            _, y = b_by_qid[x["qid"]]
        if y is None:
            continue
        for f in ("name", "type", "geom"):
            if x[f] != y[f] and nid not in by_field.get(f, set()) and x["qid"] not in declared_qids.get(f, set()):
                changed.append({"id": nid, "field": f, "before": x[f], "after": y[f]})
    lost_edges = {k: n - b["edge_keys"].get(k, 0) for k, n in a["edge_keys"].items() if b["edge_keys"].get(k, 0) < n}
    gained_edges = Counter()
    for k, n in b["edge_keys"].items():
        if n > a["edge_keys"].get(k, 0):
            gained_edges[k.rsplit("|", 1)[1]] += n - a["edge_keys"].get(k, 0)
    cov = {k: (a["coverage"].get(k), b["coverage"].get(k)) for k in sorted(set(a["coverage"]) | set(b["coverage"]))
           if a["coverage"].get(k) != b["coverage"].get(k)}
    worse = {k: v for k, v in cov.items() if isinstance(v[0], (int, float)) and isinstance(v[1], (int, float))
             and ((k in WORSE_IF_UP and v[1] > v[0]) or (k in WORSE_IF_DOWN and v[1] < v[0]))}
    drift = probe_drift(a.get("probes") or {}, b.get("probes") or {})
    new_nodes = set(b["nodes"]) - set(a["nodes"])
    loc_like = lambda n: n["type"] == "location" and n["in_geoms"]
    noise = {
        "ships_as_places": sorted(i for i, n in b["nodes"].items() if n["class"] == "ship" and loc_like(n)
                                  and not (i in a["nodes"] and a["nodes"][i]["class"] == "ship" and loc_like(a["nodes"][i]))),
        "countries_regions_newly_drawn": sorted(i for i, n in b["nodes"].items() if n["class"] in ("country", "region")
                                                and n["in_geoms"] and not (i in a["nodes"] and a["nodes"][i]["in_geoms"])),
        "new_duplicate_names": {k: v for k, v in b["duplicate_names"].items() if a["duplicate_names"].get(k) != v},
        "connected_places_gained_out_of_theatre": [g for g in drift["connected_locations_gained_rows"]
                                                   if g.get("lat") is not None and not in_asia_pacific(g.get("lon"), g.get("lat"))],
        "connected_places_gained_out_of_window": [g for g in drift["connected_locations_gained_rows"] if g.get("via_in_scope") is False],
        "hub_places_gained_out_of_theatre": [g for g in drift["hubs_gained_rows"] if g.get("lat") is not None and not in_asia_pacific(g.get("lon"), g.get("lat"))],
        "places_gained_without_a_point": [g for g in drift["connected_locations_gained_rows"] + drift["hubs_gained_rows"] if g.get("lat") is None],
        # a country or broad region must never become a place (as a participant, or otherwise)
        "countries_gained_as_places": sorted({
            f"{probe}: {q}" for probe, q in drift["places_gained"]
            if (b["nodes"].get(q) or {}).get("class") in ("country", "region")
            or any(n["qid"] == q and n["class"] in ("country", "region") for n in b["nodes"].values())}),
        "hub_places_gained_out_of_window": [g for g in drift["hubs_gained_rows"] if g.get("via_in_scope") is False],
    }
    out = {"counts": {"before": a["counts"], "after": b["counts"]}, "load": {"before": [a["load_s"], a["max_rss_mb"]],
           "after": [b["load_s"], b["max_rss_mb"]]},
           "lost_nodes": lost_nodes, "node_id_changes": id_changes, "changed_nodes": changed,
           "declared_type_changes": len(by_field.get("type", ())),
           "declared_changes": {f: len(v) for f, v in by_field.items()},
           "new_nodes": len(new_nodes), "lost_edges": lost_edges, "gained_edges_by_type": dict(gained_edges.most_common()),
           "coverage_changed": cov, "coverage_worse": worse, "drift": drift, "noise": noise}
    if heldout_rows is not None:
        before = heldout_missing(heldout_rows, set(a["graph_qids"]))
        after = heldout_missing(heldout_rows, set(b["graph_qids"]))
        out["heldout"] = {r["id"]: {"before": before[r["id"]], "after": after[r["id"]]} for r in heldout_rows}
    return out


def probe_drift(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"search_entities": {}, "connected_locations": {}, "what_happened_at": {}, "locations_of": {},
                           "hubs_connected_locations": {}, "hubs_locations_of": {}, "connected_locations_gained_rows": [],
                           "hubs_gained_rows": [], "places_gained": []}
    for name, top in (a.get("search_entities") or {}).items():
        new = (b.get("search_entities") or {}).get(name) or []
        if [x[0] for x in top] != [x[0] for x in new]:
            out["search_entities"][name] = {"before": top, "after": new}
    for key, rows_key in (("connected_locations", "connected_locations_gained_rows"), ("hubs_connected_locations", "hubs_gained_rows")):
        for q, rows in (a.get(key) or {}).items():
            new = (b.get(key) or {}).get(q) or []
            ka, kb = {r["qid"]: r for r in rows}, {r["qid"]: r for r in new}
            gained = sorted((kb[k] for k in kb.keys() - ka.keys()), key=lambda r: r["qid"])
            lost = sorted((ka[k] for k in ka.keys() - kb.keys()), key=lambda r: r["qid"])
            if gained or lost:
                out[key][q] = {"gained": [[r["qid"], r["name"], r.get("via")] for r in gained],
                               "lost": [[r["qid"], r["name"], r.get("via")] for r in lost],
                               "before": len(rows), "after": len(new)}
                out["places_gained"] += [(q, r["qid"]) for r in gained]
                out[rows_key] += [{**r, "probe": q} for r in gained]
    for q, r in (a.get("what_happened_at") or {}).items():
        s = (b.get("what_happened_at") or {}).get(q)
        if r is None and s is None:
            continue
        r, s = r or {}, s or {}
        names = {**(r.get("names") or {}), **(s.get("names") or {})}
        entry = {}
        out["places_gained"] += [(q, x) for x in set(s.get("places") or []) - set(r.get("places") or [])]
        for k in ("events", "nearby_events", "region_events"):
            x, y = set(r.get(k) or []), set(s.get(k) or [])
            if x != y:
                entry[k] = {"gained": [[i, names.get(i)] for i in sorted(y - x)], "lost": [[i, names.get(i)] for i in sorted(x - y)]}
        if entry:
            moved = {i for k in entry for i, _ in entry[k]["lost"]} & {i for k in entry for i, _ in entry[k]["gained"]}
            entry["moved_between_lists"] = sorted(moved)
            out["what_happened_at"][q] = entry
    for key in ("locations_of", "hubs_locations_of"):
        for q, rows in (a.get(key) or {}).items():
            new = (b.get(key) or {}).get(q) or []
            x, y = {r[0]: r[1] for r in rows}, {r[0]: r[1] for r in new}
            if x.keys() != y.keys():
                out["places_gained"] += [(q, k) for k in y.keys() - x.keys()]
                out[key][q] = {"gained": [[k, y[k]] for k in sorted(y.keys() - x.keys())],
                               "lost": [[k, x[k]] for k in sorted(x.keys() - y.keys())]}
    return out


# ----------------------------------------------------------------- report


def _table(rows: list[list[Any]], head: list[str]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def report_md(c: dict[str, Any], step: str, probes: dict[str, Any], notes: str = "") -> str:
    ca, cb = c["counts"]["before"], c["counts"]["after"]
    flat = lambda d: {k: v for k, v in d.items() if not isinstance(v, dict)}
    keys = list(dict.fromkeys([*flat(ca), *flat(cb)]))
    count_rows = [[k, ca.get(k), cb.get(k), (cb.get(k) or 0) - (ca.get(k) or 0)] for k in keys]
    types = sorted(set(ca["node_types"]) | set(cb["node_types"]))
    type_rows = [[t, ca["node_types"].get(t, 0), cb["node_types"].get(t, 0)] for t in types]
    etypes = list(dict.fromkeys([*list(ca["edges_by_type"])[:15], *c["gained_edges_by_type"]]))
    edge_rows = [[t, ca["edges_by_type"].get(t, 0), cb["edges_by_type"].get(t, 0)] for t in etypes]
    d = c["drift"]
    n = c["noise"]
    lost_any = c["lost_nodes"] or c["changed_nodes"] or c["lost_edges"] or c["coverage_worse"] or any(
        v.get("lost") for v in d["connected_locations"].values()) or any(
        v.get("lost") for v in d["locations_of"].values()) or any(
        v.get("lost") for k in ("hubs_connected_locations", "hubs_locations_of") for v in d[k].values()) or any(
        x.get("lost") for v in d["what_happened_at"].values() for k, x in v.items() if isinstance(x, dict)) or d["search_entities"]
    parts = [f"# Impact of self-fix step {step}", "",
             "Before vs after, measured through `geo_agent.graph_store.GraphStore` only (no LLM).", "", notes, "",
             "## 1. Counts", "", _table(count_rows, ["count", "before", "after", "change"]), "",
             f"Load time and peak memory: before {c['load']['before'][0]} s / {c['load']['before'][1]} MB, "
             f"after {c['load']['after'][0]} s / {c['load']['after'][1]} MB.", "",
             _table(type_rows, ["node type", "before", "after"]), "",
             _table(edge_rows, ["edge type (top 15 + gained)", "before", "after"]), "",
             "## 2. Nothing lost", "",
             f"- Entities lost (QID gone): {len(c['lost_nodes'])} {c['lost_nodes'][:20]}",
             f"- Node ids changed while the entity stays (the agent names a merged entity after another record): "
             f"{len(c.get('node_id_changes') or [])} {(c.get('node_id_changes') or [])[:10]}",
             f"- Nodes with a changed name, type or geometry: {len(c['changed_nodes'])} (declared changes excluded: {c.get('declared_changes') or c['declared_type_changes']})",
             *[f"  - `{x['id']}` {x['field']}: {x['before']!r} -> {x['after']!r}" for x in c["changed_nodes"][:30]],
             f"- Edges lost (source, target, type): {len(c['lost_edges'])}",
             *[f"  - `{k}` x{v}" for k, v in list(c["lost_edges"].items())[:30]],
             f"- New nodes: {c['new_nodes']}; gained edges by type: {c['gained_edges_by_type'] or 'none'}", "",
             "Coverage (`store.schema()['coverage']`) changed:", "",
             _table([[k, v[0], v[1], "WORSE" if k in c["coverage_worse"] else ""] for k, v in c["coverage_changed"].items()],
                    ["coverage", "before", "after", ""]) if c["coverage_changed"] else "No coverage count changed.", "",
             "## 3. Behaviour drift on the fixed probes", "",
             f"Probes: {len(probes['search_entities'])} searches, {len(probes['connected_locations'])} connected_locations, "
             f"{len(probes['what_happened_at'])} what_happened_at, {len(probes['locations_of'])} locations_of "
             "(list in `graph_enrichment/probes.json`).", "",
             f"- search_entities top-3 changed: {len(d['search_entities'])}",
             *[f"  - {k}: {[x[1] for x in v['before']]} -> {[x[1] for x in v['after']]}" for k, v in d["search_entities"].items()],
             f"- connected_locations changed: {len(d['connected_locations'])}",
             *[f"  - {k}: {v['before']} -> {v['after']} places; gained {v['gained'][:10]}; lost {v['lost']}" for k, v in d["connected_locations"].items()],
             f"- what_happened_at changed: {len(d['what_happened_at'])}",
             *[f"  - {k}: {v}" for k, v in d["what_happened_at"].items()],
             f"- locations_of changed: {len(d['locations_of'])}",
             *[f"  - {k}: gained {v['gained']}; lost {v['lost']}" for k, v in d["locations_of"].items()], "",
             f"Hub probes ({len(probes.get('hubs') or [])}, for noise detection: {', '.join(h['name'] for h in probes.get('hubs') or [])}):", "",
             f"- connected_locations changed: {len(d['hubs_connected_locations'])}",
             *[f"  - {k}: {v['before']} -> {v['after']} places; gained {len(v['gained'])} {v['gained'][:15]}; lost {v['lost']}"
               for k, v in d["hubs_connected_locations"].items()],
             f"- locations_of changed: {len(d['hubs_locations_of'])}",
             *[f"  - {k}: gained {v['gained']}; lost {v['lost']}" for k, v in d["hubs_locations_of"].items()], "",
             "## 4. New noise", "",
             f"- Ships newly drawn as places: {len(n['ships_as_places'])} {n['ships_as_places'][:20]}",
             f"- Countries or broad regions newly drawn as places: {len(n['countries_regions_newly_drawn'])} {n['countries_regions_newly_drawn'][:20]}",
             f"- New or changed duplicate names (one name, several QIDs): {len(n['new_duplicate_names'])}",
             *[f"  - {k}: {v}" for k, v in list(n["new_duplicate_names"].items())[:30]],
             f"- Places gained by probe people/units outside the Asia-Pacific box: {len(n['connected_places_gained_out_of_theatre'])}",
             *[f"  - {g['probe']}: {g['qid']} {g['name']} ({g.get('lat')}, {g.get('lon')}) via {g.get('via')}" for g in n["connected_places_gained_out_of_theatre"][:20]],
             f"- Places gained through an event outside 1938-1948: {len(n['connected_places_gained_out_of_window'])}",
             *[f"  - {g['probe']}: {g['qid']} {g['name']} via {g.get('via')}" for g in n["connected_places_gained_out_of_window"][:20]],
             f"- Places gained by hubs outside the Asia-Pacific box: {len(n['hub_places_gained_out_of_theatre'])}",
             *[f"  - {g['probe']}: {g['qid']} {g['name']} ({g.get('lat')}, {g.get('lon')}) via {g.get('via')}" for g in n["hub_places_gained_out_of_theatre"][:30]],
             f"- Places gained by hubs through an event outside 1938-1948: {len(n['hub_places_gained_out_of_window'])}",
             *[f"  - {g['probe']}: {g['qid']} {g['name']} via {g.get('via')}" for g in n["hub_places_gained_out_of_window"][:30]],
             f"- Countries or broad regions gained as a place by any probe (locations_of, what_happened_at places, connected_locations): "
             f"{len(n['countries_gained_as_places'])} {n['countries_gained_as_places'][:20]}",
             f"- Places gained (probes and hubs) that have no point, so draw nothing: {len(n['places_gained_without_a_point'])}",
             *[f"  - {g['probe']}: {g['qid']} {g['name']} via {g.get('via')}" for g in n["places_gained_without_a_point"][:30]], ""]
    if "heldout" in c:
        rows = [[k, ", ".join(v["before"]) or "-", ", ".join(v["after"]) or "-"] for k, v in c["heldout"].items()]
        nb = sum(1 for v in c["heldout"].values() if v["before"])
        na = sum(1 for v in c["heldout"].values() if v["after"])
        parts += ["## 5. Held-out rows: required QIDs missing from the graph (`mark_missing`)", "",
                  f"Rows with a gap: {nb} before, {na} after.", "", _table(rows, ["row", "missing before", "missing after"]), ""]
    parts += ["## 6. Verdict", "", "Automatic check: " + ("**something was lost or changed; see sections 2-3**" if lost_any
                                                       else "**nothing lost, no coverage count worse, no probe lost an item.**"), ""]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Impact of a self-fix step (spec R97).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("probes")
    a.add_argument("--graph", required=True, type=Path)
    a.add_argument("--out", required=True, type=Path)
    a.add_argument("--exclude", nargs="*", type=Path, default=[])
    b = sub.add_parser("snapshot")
    b.add_argument("--graph", required=True, type=Path)
    b.add_argument("--probes", type=Path)
    b.add_argument("--out", required=True, type=Path)
    r = sub.add_parser("report")
    r.add_argument("--before", required=True, type=Path)
    r.add_argument("--after", required=True, type=Path)
    r.add_argument("--step", required=True)
    r.add_argument("--probes", required=True, type=Path)
    r.add_argument("--heldout", type=Path)
    r.add_argument("--declared-type-changes", type=Path, help="JSON list of node ids whose type the step changes")
    r.add_argument("--notes", type=Path, help="markdown inserted at the top")
    r.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)
    if args.cmd == "probes":
        from geo_agent.graph_store import GraphStore
        store = GraphStore.from_json(args.graph)
        probes = select_probes(store, gold_qids(args.exclude))
        args.out.write_text(json.dumps(probes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    elif args.cmd == "snapshot":
        probes = json.loads(args.probes.read_text()) if args.probes else None
        args.out.write_text(json.dumps(snapshot(args.graph, probes), ensure_ascii=False), encoding="utf-8")
    else:
        before, after = json.loads(args.before.read_text()), json.loads(args.after.read_text())
        raw = json.loads(args.declared_type_changes.read_text()) if args.declared_type_changes else []
        declared = {f: set(v) for f, v in raw.items()} if isinstance(raw, dict) else set(raw)
        rows = [json.loads(l) for l in args.heldout.read_text().splitlines() if l.strip()] if args.heldout else None
        c = compare(before, after, declared, rows)
        notes = args.notes.read_text() if args.notes else ""
        args.out.write_text(report_md(c, args.step, json.loads(args.probes.read_text()), notes), encoding="utf-8")
        args.out.with_suffix(".json").write_text(json.dumps(c, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
