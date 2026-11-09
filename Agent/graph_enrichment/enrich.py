"""Wikidata enrichment of a graph JSON, a build-time step (spec R74).

    uv run python -m graph_enrichment.enrich --graph IN.json --out OUT.json --cache CACHE_DIR [--offline]

Reads the claims of every Q-id in the graph from Wikidata (`wbgetentities`, cached per batch)
and writes a copy of the graph with:
- dates (P580 start, P582 end, P585 point) on records that have none, with their precision
- `kind` and `wikidata_instance_of` on military events (P31 in MILITARY_EVENT_CLASSES)
- `entity_class` (`ship`, `country`, `region`) from P31
- typed relations (RELATION_PROPS) between entities already in the graph, plus commanders
  read from the qualifiers of participant claims
- names for records named only by their Q-id (the English label, else the most used anchor
  text), with the other anchor texts as aliases (spec R93)
- a P625 point for places and events that have no geometry (spec R93)
- P580 / P582 / P585 qualifier dates on relations already in the graph that have none (spec R93)
- `birth_date` / `death_date` (P569 / P570) on people (P31 Q5) that have none (spec R100)
- `metadata.enrichment`: tool version, snapshot hash, fetch time, properties and counts

Nothing else changes: same nodes, same edges, same order; new edges are appended. The agent
(`geo_agent`) never imports this module and never calls Wikidata; it only loads the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TOOL_VERSION = "0.3.0"
API_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = "georag-enrichment/0.1"  # generic on purpose: no personal contact in the request
BATCH_SIZE = 50  # the wbgetentities maximum for ordinary clients
PAUSE_S = 1.0  # between calls
RETRIES = 5  # on 429 / 5xx, with exponential backoff
QID_RE = re.compile(r"^Q\d+$")

DATE_PROPS = {"start_date": "P580", "end_date": "P582", "point_date": "P585"}
LIFE_PROPS = {"birth_date": "P569", "death_date": "P570"}  # on people (spec R100)
HUMAN = "Q5"
RELATION_PROPS = ("P710", "P276", "P361", "P607", "P1344", "P4791", "P17")
COMMANDER_QUALIFIERS = ("P4791", "P1037")  # commanded by, director/manager: on a participant (P710) claim
PRECISION_DAY, PRECISION_MONTH, PRECISION_YEAR = 11, 10, 9
EARTH = "http://www.wikidata.org/entity/Q2"  # P625 on another globe (the Moon, Mars) is no place on the map
POINT_TYPES = ("location", "event")  # record types that get a P625 point (spec R93)
NO_POINT_CLASSES = ("ship", "country", "region")  # a ship moves; a country or region is no point
MAX_ALIASES = 5  # anchor texts kept as aliases of a newly named record
MIN_ALIAS_MENTIONS = 2  # an anchor text used once is as likely a wrong link as a name

# P31 (instance of) classes that make an entity a military event, with the `kind` they give.
# Built from the labels of the P31 values on cpu5747's battle-like entities (step 46) and
# chosen by hand; a class not listed here gives no kind. Left out on purpose: theater of war
# (Q718893, a region as much as an event), cancelled military operation (Q114350932, it never
# happened), military occupation (Q188686, a state), massacre (Q3199915), incident (Q11371169),
# D-Day (Q206106), nuclear test series (Q98391050), war front (Q842332), and ship classes.
MILITARY_EVENT_CLASSES: dict[str, str] = {
    "Q178561": "battle",      # battle
    "Q1261499": "battle",     # naval battle
    "Q830494": "battle",      # dogfight
    "Q831663": "campaign",    # military campaign
    "Q4698343": "campaign",   # air offensive
    "Q645883": "operation",   # military operation
    "Q467011": "operation",   # invasion
    "Q2001676": "operation",  # offensive
    "Q6982012": "operation",  # naval offensive
    "Q476807": "raid",        # military raid
    "Q188055": "siege",       # siege
    "Q198": "war",            # war
    "Q103495": "war",         # world war
    "Q180684": "war",         # conflict
    "Q2380335": "attack",     # airstrike
    "Q678146": "attack",      # bombardment
    "Q133868917": "attack",   # sneak attack
    "Q4688003": "attack",     # aerial bombing of a city
    "Q646740": "landing",     # landing operation
    "Q348120": "landing",     # amphibious warfare
}


# P31 classes that say what a location-typed record really is (spec R87), checked against Wikidata's labels.
SHIP_CLASSES = {
    "Q11446",    # ship
    "Q852190",   # shipwreck
    "Q17205",    # aircraft carrier
    "Q1185562",  # light aircraft carrier
    "Q253555",   # escort carrier
    "Q182531",   # battleship
    "Q214190",   # battlecruiser
    "Q104843",   # cruiser
    "Q898771",   # heavy cruiser
    "Q778129",   # light cruiser
    "Q174736",   # destroyer
    "Q2811",     # submarine
}
REGION_CLASSES = {  # broad regions: never the containing region of a place (spec R91)
    "Q9430",     # ocean
    "Q5107",     # continent
    "Q82794",    # geographic region (Micronesia, Southeast Asia)
}
COUNTRY_CLASSES = {
    "Q6256",     # country
    "Q3624078",  # sovereign state
    "Q3024240",  # historical country
    "Q48349",    # empire
    "Q133156",   # colony
}


# ----------------------------------------------------------------- fetching


def collect_qids(graph: dict[str, Any]) -> list[str]:
    """Every distinct Q-id named by a node's `wikidata_id` (top level or in attributes), sorted."""
    out = set()
    for n in graph.get("nodes") or []:
        wid = n.get("wikidata_id") or (n.get("attributes") or {}).get("wikidata_id")
        if isinstance(wid, str) and QID_RE.match(wid):
            out.add(wid)
    return sorted(out, key=lambda q: int(q[1:]))


def _api_get(params: dict[str, str]) -> dict[str, Any]:
    """One Wikidata API call with retry and backoff on 429 and 5xx."""
    url = f"{API_URL}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == RETRIES - 1:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else 2 ** (attempt + 1))
        except urllib.error.URLError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_entities(ids: list[str], props: str = "claims", get: Callable[[dict[str, str]], dict[str, Any]] = _api_get,
                   extra: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """wbgetentities for up to BATCH_SIZE ids: ({id: entity}, ids Wikidata doesn't know).
    An id Wikidata rejects fails the whole call, so it is set aside and the call repeated."""
    ids, missing = list(ids), []
    while ids:
        data = get({"action": "wbgetentities", "ids": "|".join(ids), "props": props, **(extra or {})})
        err = data.get("error")
        if err and err.get("code") == "no-such-entity" and err.get("id") in ids:
            ids.remove(err["id"])
            missing.append(err["id"])
            continue
        if err:
            raise RuntimeError(f"Wikidata error: {err.get('code')}: {err.get('info')}")
        entities = {}
        for key, ent in (data.get("entities") or {}).items():
            if "missing" in ent:
                missing.append(key)
            else:
                entities[key] = ent  # a redirected id stays under the id asked for
        return entities, missing
    return {}, missing


def _batch_path(cache_dir: Path, ids: list[str]) -> Path:
    digest = hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]
    return cache_dir / f"claims-{digest}.json"


def fetch_claims(qids: list[str], cache_dir: Path, offline: bool = False,
                 get: Callable[[dict[str, str]], dict[str, Any]] = _api_get, pause: float = PAUSE_S
                 ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Claims of every id ({qid: claims}), from the cache or Wikidata, and the snapshot record.

    One cache file per batch of BATCH_SIZE ids (sorted), so a rerun on the same graph reads
    the same files. With `offline`, a missing batch is an error. The snapshot names the ids,
    the fetch time and the SHA-256 of the combined claims; it is also written to snapshot.json."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    batches = [qids[i:i + BATCH_SIZE] for i in range(0, len(qids), BATCH_SIZE)]
    absent = [b for b in batches if not _batch_path(cache_dir, b).exists()]
    if offline and absent:
        raise SystemExit(f"--offline: {len(absent)} of {len(batches)} batches ({sum(map(len, absent))} ids) "
                         f"are not in the cache {cache_dir}; run once without --offline to fetch them")
    claims: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    fetched_at: list[str] = []
    for i, batch in enumerate(batches):
        path = _batch_path(cache_dir, batch)
        if not path.exists():
            if i and pause:
                time.sleep(pause)
            entities, gone = fetch_entities(batch, "claims", get)
            record = {"ids": batch, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "missing": gone, "entities": {q: {"claims": e.get("claims") or {}} for q, e in entities.items()}}
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            print(f"fetched batch {i + 1}/{len(batches)}", file=sys.stderr)
        record = json.loads(path.read_text(encoding="utf-8"))
        claims.update({q: e["claims"] for q, e in record["entities"].items()})
        missing += record.get("missing") or []
        fetched_at.append(record["fetched_at"])
    snapshot = {
        "ids": qids,
        "missing": sorted(missing),
        "fetched_at": max(fetched_at) if fetched_at else None,
        "claims_sha256": hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "batch_size": BATCH_SIZE,
    }
    path, text = cache_dir / "snapshot.json", json.dumps(snapshot, indent=1)
    if not path.exists() or path.read_text(encoding="utf-8") != text:  # a rerun leaves a shared cache untouched
        path.write_text(text, encoding="utf-8")
    return claims, snapshot


def nameless_qids(graph: dict[str, Any]) -> list[str]:
    """Q-ids with a record whose name is a bare Q-id (spec R93), sorted: the only labels fetched."""
    out = {n["wikidata_id"] for n in graph.get("nodes") or []
           if isinstance(n.get("wikidata_id"), str) and QID_RE.match(n["wikidata_id"])
           and isinstance(n.get("name"), str) and QID_RE.match(n["name"].strip())}
    return sorted(out, key=lambda q: int(q[1:]))


def fetch_labels(qids: list[str], cache_dir: Path, offline: bool = False,
                 get: Callable[[dict[str, str]], dict[str, Any]] = _api_get, pause: float = PAUSE_S
                 ) -> tuple[dict[str, str], str | None]:
    """English labels ({qid: label}, ids without one left out) and their SHA-256, cached per batch
    in `labels-<digest>.json` like the claims. With `offline`, a missing batch is an error."""
    if not qids:
        return {}, None
    cache_dir.mkdir(parents=True, exist_ok=True)
    batches = [qids[i:i + BATCH_SIZE] for i in range(0, len(qids), BATCH_SIZE)]
    path_of = lambda b: cache_dir / f"labels-{hashlib.sha256('|'.join(b).encode()).hexdigest()[:16]}.json"
    absent = [b for b in batches if not path_of(b).exists()]
    if offline and absent:
        raise SystemExit(f"--offline: {len(absent)} of {len(batches)} label batches are not in the cache {cache_dir}; "
                         "run once without --offline to fetch them")
    labels: dict[str, str] = {}
    for i, batch in enumerate(batches):
        path = path_of(batch)
        if not path.exists():
            if i and pause:
                time.sleep(pause)
            entities, gone = fetch_entities(batch, "labels", get, {"languages": "en"})
            found = {q: ((e.get("labels") or {}).get("en") or {}).get("value") for q, e in entities.items()}
            record = {"ids": batch, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "missing": gone, "labels": {q: v for q, v in found.items() if v}}
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            print(f"fetched label batch {i + 1}/{len(batches)}", file=sys.stderr)
        labels.update(json.loads(path.read_text(encoding="utf-8"))["labels"])
    return labels, hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------ claim reading


def _statements(claims: dict[str, Any], prop: str) -> list[dict[str, Any]]:
    """Claims of `prop` with a value, deprecated rank left out."""
    return [c for c in claims.get(prop) or []
            if c.get("rank") != "deprecated" and (c.get("mainsnak") or {}).get("snaktype") == "value"]


def _item_id(snak: dict[str, Any]) -> str | None:
    value = ((snak.get("datavalue") or {}).get("value")) or {}
    qid = value.get("id") if isinstance(value, dict) else None
    return qid if isinstance(qid, str) and QID_RE.match(qid) else None


def _time(snak: dict[str, Any]) -> tuple[str, int] | None:
    """(Wikidata time string cut to its precision, precision) of a time snak; None below a year.
    "+1944-01-01T00:00:00Z" at year precision becomes "+1944-00-00T00:00:00Z", the form the agent
    already reads as "1944" (it doesn't read the integer precision)."""
    if snak.get("snaktype") != "value":
        return None
    value = (snak.get("datavalue") or {}).get("value") or {}
    t, precision = value.get("time"), value.get("precision")
    if not isinstance(t, str) or not isinstance(precision, int) or precision < PRECISION_YEAR:
        return None
    m = re.match(r"^([+-]\d+)-(\d{2})-(\d{2})T", t)
    if m is None:
        return None
    year, month, day = m.groups()
    if precision == PRECISION_YEAR:
        month, day = "00", "00"
    elif precision == PRECISION_MONTH:
        day = "00"
    return f"{year}-{month}-{day}T00:00:00Z", min(precision, PRECISION_DAY)


def _time_key(t: str) -> tuple[int, int, int]:
    m = re.match(r"^([+-]\d+)-(\d{2})-(\d{2})", t)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def entity_dates(claims: dict[str, Any]) -> tuple[dict[str, tuple[str, int]], list[str]]:
    """{field: (time, precision)} from P580 / P582 / P585 and notes on fields with several values:
    the earliest start, the latest end, the earliest point."""
    out, notes = {}, []
    for field, prop in DATE_PROPS.items():
        values = [v for c in _statements(claims, prop) if (v := _time(c["mainsnak"]))]
        if not values:
            continue
        values.sort(key=lambda v: _time_key(v[0]))
        out[field] = values[-1] if field == "end_date" else values[0]
        if len({v[0] for v in values}) > 1:
            which = "latest" if field == "end_date" else "earliest"
            notes.append(f"{prop} ({field}): {len(values)} values on Wikidata, the {which} kept")
    return out, notes


def life_dates(claims: dict[str, Any]) -> dict[str, tuple[str, int]]:
    """{birth_date / death_date: (time, precision)} of a human (P31 Q5) from P569 / P570, preferred rank first,
    then the earliest value (spec R100). {} for anything that isn't a human."""
    if HUMAN not in {_item_id(c["mainsnak"]) for c in _statements(claims, "P31")}:
        return {}
    out = {}
    for field, prop in LIFE_PROPS.items():
        pairs = [(c.get("rank"), v) for c in _statements(claims, prop) if (v := _time(c["mainsnak"]))]
        preferred = [v for rank, v in pairs if rank == "preferred"]
        if pairs:
            out[field] = preferred[0] if preferred else min((v for _, v in pairs), key=lambda v: _time_key(v[0]))
    return out


def qualifier_dates(claim: dict[str, Any]) -> dict[str, Any]:
    """start_date / end_date / point_date of a statement from its P580 / P582 / P585 qualifiers."""
    out: dict[str, Any] = {}
    quals = claim.get("qualifiers") or {}
    for field, prop in DATE_PROPS.items():
        values = [v for snak in quals.get(prop) or [] if (v := _time(snak))]
        if values:
            values.sort(key=lambda v: _time_key(v[0]))
            t, precision = values[-1] if field == "end_date" else values[0]
            out[field], out[f"{field}_precision"] = t, precision
        else:
            out[field] = None
    return out


def entity_class(claims: dict[str, Any]) -> str | None:
    """`ship`, `country` or `region` from P31 (spec R87, R91), else None."""
    classes = {q for c in _statements(claims, "P31") if (q := _item_id(c["mainsnak"]))}
    if classes & SHIP_CLASSES:
        return "ship"
    if classes & COUNTRY_CLASSES:
        return "country"
    return "region" if classes & REGION_CLASSES else None


def wikidata_point(claims: dict[str, Any]) -> dict[str, Any] | None:
    """{coordinates: [lon, lat], precision, statement_id} of the P625 statement (preferred rank first,
    then the first normal one), on Earth only; None without one."""
    statements = sorted(_statements(claims, "P625"), key=lambda c: c.get("rank") != "preferred")
    for c in statements:
        value = (c["mainsnak"].get("datavalue") or {}).get("value") or {}
        lat, lon = value.get("latitude"), value.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if value.get("globe") not in (None, EARTH) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        return {"coordinates": [lon, lat], "precision": value.get("precision"), "statement_id": c.get("id")}
    return None


def anchor_names(records: list[dict[str, Any]]) -> list[str]:
    """The anchor texts of an entity's mentions, most used first, one per spelling ignoring case."""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for n in records:
        for s in n.get("sources") or []:
            text = ((s.get("position") or {}).get("anchor_text") or "").strip()
            if text and not QID_RE.match(text):
                key = text.casefold()
                first.setdefault(key, text)
                counts[key] = counts.get(key, 0) + 1
    order = sorted(counts, key=lambda k: -counts[k])
    return [first[k] for k in order if counts[k] >= MIN_ALIAS_MENTIONS or k == order[0]]


def military_kind(claims: dict[str, Any]) -> tuple[list[str], str | None]:
    """(P31 values, the kind of the first one that is a military-event class, else None)."""
    classes = [q for c in _statements(claims, "P31") if (q := _item_id(c["mainsnak"]))]
    return classes, next((MILITARY_EVENT_CLASSES[q] for q in classes if q in MILITARY_EVENT_CLASSES), None)


# --------------------------------------------------------------- enrichment


def _catalog_family(catalog: Any, relation: str) -> str | None:
    """Level 1 family of a relation in the graph's relation_catalog (list or dict, as the agent reads it)."""
    items = catalog.values() if isinstance(catalog, dict) else catalog or []
    for item in items:
        if isinstance(item, dict) and item.get("id") == relation:
            families = item.get("level1_families") or [item.get("family") or item.get("relation_family")]
            return next((f for f in families if f), None)
    return None


def enrich(graph: dict[str, Any], claims: dict[str, dict[str, Any]], snapshot: dict[str, Any],
           labels: dict[str, str] | None = None, labels_sha256: str | None = None) -> dict[str, Any]:
    """The enriched graph (the input dict, changed in place and returned). Pure: no network.
    `labels` ({qid: English label}) names the records that have only a Q-id as their name (spec R93)."""
    labels = labels or {}
    nodes, edges = graph.setdefault("nodes", []), graph.setdefault("edges", [])
    qid_of: dict[str, str] = {}  # node id -> its QID
    records: dict[str, list[dict[str, Any]]] = {}  # QID -> its records
    for n in nodes:
        wid = n.get("wikidata_id") or (n.get("attributes") or {}).get("wikidata_id")
        if isinstance(wid, str) and QID_RE.match(wid):
            qid_of[n["id"]] = wid
            records.setdefault(wid, []).append(n)

    dates_added = typed_as_event = classed = names_added = points_added = life_added = 0
    for qid, recs in records.items():  # names first: they need no claims (spec R93)
        nameless = [n for n in recs if isinstance(n.get("name"), str) and QID_RE.match(n["name"].strip())]
        if not nameless:
            continue
        anchors = anchor_names(recs)
        name, source = (labels[qid], "wikidata_label") if labels.get(qid) else (anchors[0], "anchor_text") if anchors else (None, None)
        if name is None:
            continue
        for n in nameless:
            n["name"] = name
            known = {a.casefold() for a in n.get("aliases") or []} | {name.casefold()}
            new = [a for a in anchors if a.casefold() not in known][:MAX_ALIASES]
            n["aliases"] = [*(n.get("aliases") or []), *new]
            attrs = n.get("attributes") if isinstance(n.get("attributes"), dict) else {}
            attrs["name_source"] = source
            n["attributes"] = attrs
            names_added += 1

    for qid, recs in records.items():
        c = claims.get(qid)
        if not c:
            continue
        dates, notes = entity_dates(c)
        life = life_dates(c)
        classes, kind = military_kind(c)
        cls = entity_class(c)
        classed += bool(cls)
        if kind:
            typed_as_event += 1
        for n in recs:
            attrs = n.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}
            added = False
            for field, (t, precision) in dates.items():
                if field not in attrs:
                    attrs[field], attrs[f"{field}_precision"] = t, precision
                    dates_added += 1
                    added = True
            if added and notes:
                attrs["enrichment_notes"] = [*attrs.get("enrichment_notes", []), *notes]
            for field, (t, precision) in life.items():  # spec R100
                if field not in attrs:
                    attrs[field], attrs[f"{field}_precision"] = t, precision
                    life_added += 1
            if kind:
                attrs["wikidata_instance_of"] = classes
                attrs.setdefault("kind", kind)
            if cls:
                attrs["entity_class"] = cls
            if attrs:
                n["attributes"] = attrs
        if cls in NO_POINT_CLASSES or (point := wikidata_point(c)) is None:
            continue
        for n in recs:  # a P625 point where the record has no geometry (spec R93)
            if n.get("geometry") or n.get("geometry_wkt") or (n.get("type") not in POINT_TYPES and not kind):
                continue
            n["geometry"] = {"type": "Point", "coordinates": point["coordinates"]}
            n["geolocation_evidence"] = {"source_kind": "WIKIDATA_P625_ENRICHMENT", "record_id": qid, "crs": "EPSG:4326",
                                         "statement_id": point["statement_id"],
                                         "coordinate_precision_degrees": point["precision"]}
            points_added += 1

    def endpoint_qid(ref: Any) -> Any:
        return qid_of.get(ref, ref)
    existing = set()
    for e in edges:
        s, t, typ = endpoint_qid(e.get("source")), endpoint_qid(e.get("target")), e.get("type")
        if isinstance(typ, str) and typ.startswith("inverse:"):  # a reverse view states the base relation
            s, t, typ = t, s, typ[len("inverse:"):]
        existing.add((s, t, typ))

    catalog = graph.get("relation_catalog")
    families: dict[str, str | None] = {}
    added_by_type: dict[str, int] = {}

    def add(edge_id: str, statement_id: str, source: str, target: str, prop: str, dated: dict[str, Any]) -> None:
        typ = f"wd:{prop}"
        if source == target or (source, target, typ) in existing:
            return
        existing.add((source, target, typ))
        if typ not in families:
            families[typ] = _catalog_family(catalog, typ)
        edge = {"id": edge_id, "source": source, "target": target, "type": typ}
        if families[typ]:
            edge["relation_family"] = families[typ]
        edges.append({**edge, **dated, "count": 1, "provenance": "wikidata", "statement_id": statement_id})
        added_by_type[typ] = added_by_type.get(typ, 0) + 1

    relation_dates = 0  # qualifier dates on relations the graph already has (spec R93)
    stated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for qid in records:
        for prop in RELATION_PROPS:
            for claim in _statements(claims.get(qid) or {}, prop):
                target, dated = _item_id(claim["mainsnak"]), qualifier_dates(claim)
                if target and any(dated.get(f) for f in DATE_PROPS):
                    stated.setdefault((qid, target, f"wd:{prop}"), dated)
    for e in edges:
        if e.get("type") == "CO_MENTIONED" or any(e.get(f) for f in DATE_PROPS):
            continue
        s, t, typ = endpoint_qid(e.get("source")), endpoint_qid(e.get("target")), e.get("type")
        if isinstance(typ, str) and typ.startswith("inverse:"):
            s, t, typ = t, s, typ[len("inverse:"):]
        elif e.get("derived") and e.get("base_level2_relation_id"):  # a reverse view under its own label
            s, t, typ = t, s, e["base_level2_relation_id"]
        if (dated := stated.get((s, t, typ))):
            e.update(dated)
            relation_dates += 1

    for qid in records:
        c = claims.get(qid) or {}
        for prop in RELATION_PROPS:
            for claim in _statements(c, prop):
                target = _item_id(claim["mainsnak"])
                if target not in records:
                    continue
                sid = claim.get("id") or f"{qid}${prop}${target}"
                add(f"wdenrich:{sid}", sid, qid, target, prop, qualifier_dates(claim))
                if prop != "P710":
                    continue
                for qual in COMMANDER_QUALIFIERS:  # who led this participant at the event
                    for snak in (claim.get("qualifiers") or {}).get(qual) or []:
                        if (commander := _item_id(snak)) in records:
                            add(f"wdenrich:{sid}#{qual}:{commander}", sid, qid, commander, "P4791", qualifier_dates(claim))

    graph.setdefault("metadata", {})
    if not isinstance(graph["metadata"], dict):
        graph["metadata"] = {}
    graph["metadata"]["enrichment"] = {
        "tool_version": TOOL_VERSION,
        "source": "Wikidata wbgetentities (claims; English labels for name-less records)",
        "snapshot_sha256": snapshot.get("claims_sha256"),
        "labels_sha256": labels_sha256,
        "fetched_at": snapshot.get("fetched_at"),
        "properties": {"dates": list(DATE_PROPS.values()), "relations": list(RELATION_PROPS),
                       "commander_qualifiers": list(COMMANDER_QUALIFIERS), "event_typing": "P31", "points": "P625",
                       "life": list(LIFE_PROPS.values())},
        "counts": {"dates_added": dates_added, "edges_added_by_type": dict(sorted(added_by_type.items())),
                   "entities_typed_as_event": typed_as_event, "entities_classed": classed,
                   "life_dates_added": life_added, "names_added": names_added, "points_added": points_added,
                   "relation_dates_added": relation_dates},
    }
    return graph


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Enrich a graph JSON with Wikidata dates, event kinds and typed relations.")
    p.add_argument("--graph", required=True, type=Path, help="input graph JSON")
    p.add_argument("--out", required=True, type=Path, help="enriched graph JSON to write")
    p.add_argument("--cache", required=True, type=Path, help="folder for the Wikidata batches and snapshot.json")
    p.add_argument("--offline", action="store_true", help="use only the cache; fail if a batch is missing")
    args = p.parse_args(argv)
    if args.out.resolve() == args.graph.resolve():
        raise SystemExit("--out must differ from --graph")
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    qids = collect_qids(graph)
    claims, snapshot = fetch_claims(qids, args.cache, args.offline)
    labels, labels_sha256 = fetch_labels(nameless_qids(graph), args.cache, args.offline)
    enrich(graph, claims, snapshot, labels, labels_sha256)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False)
    print(json.dumps(graph["metadata"]["enrichment"], indent=1))


if __name__ == "__main__":
    main()
