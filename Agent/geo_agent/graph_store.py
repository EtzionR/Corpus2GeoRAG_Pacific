"""In-memory knowledge-graph store: loading, indexing and querying.

This module has no LLM dependency. Every agent tool is a thin wrapper around a
`GraphStore` method, so the query logic can be tested deterministically.

Design decisions:
- Pydantic models with `extra="allow"`: the graph JSON is produced by another
  team and will evolve. Unknown fields are kept instead of rejected, and node and
  edge `type` are open strings (location/person/org/event today, time later).
- Geometry is GeoJSON in (lon, lat) order. Distances are great-circle km,
  measured from the query point to the nearest point of the geometry, so
  polygons (e.g. an island) match when the query point is inside or near them.
- Dates are ISO strings that may be partial ("1942", "1942-06", "1942-06-04").
  Date filters match on interval overlap; edges without dates are excluded
  whenever a date filter is given.
- Name lookup compares normalized forms (Unicode NFKD, combining marks removed,
  casefold, apostrophes deleted, hyphens as spaces, whitespace collapsed), so
  "TOKYO", "tokyo" and "Tōkyō" are the same name. Each result says how it
  matched (`name`, `alias`, `fuzzy`) and whether the match is `clear` or
  `uncertain` (spec R3, R4), so the agent can ask instead of guessing.
- Text retrieval uses BM25 over node paragraphs. Node/edge `embedding` fields
  are loaded but not used yet (see README, "Next steps").
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz, process
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

EARTH_RADIUS_KM = 6371.0
# Sentences in source text that address an AI rather than describe history (spec R32).
# They are replaced before any tool returns the text, so the model never reads them.
INSTRUCTION_RE = re.compile(
    r"\b(ignore|disregard|forget|override)\b[^.!?\]]{0,40}\b(instructions?|prompts?|rules)\b"
    r"|\byou are now\b|\bsystem prompt\b|\b(as an|you are an?) (ai|assistant|language model)\b"
    r"|\b(answer|reply|respond) (to )?(every|all|any) (question|request)s?\b",
    re.IGNORECASE)
REDACTED = "[instruction-like text removed]"


def redact_instructions(text: str) -> tuple[str, int]:
    """Replace each bracketed note or sentence that looks like an instruction to an AI."""
    count = 0

    def scrub(segment: str) -> str:
        nonlocal count
        if INSTRUCTION_RE.search(segment):
            count += 1
            return REDACTED
        return segment

    text = re.sub(r"\[[^\]]*\]", lambda m: scrub(m.group(0)), text)  # [editor notes] first
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(scrub(p) if p != REDACTED else p for p in parts), count

# Name-match quality (spec R4). Starting values, to be calibrated on the eval set.
CLEAR_SCORE = 90  # minimum score for a "clear" best match
AMBIGUITY_MARGIN = 5  # another node scoring within this of a result makes it a near-tie
MATCH_CUTOFF = 60  # results below this are not returned


# ---------------------------------------------------------------- data model


class Source(BaseModel):
    """Reference to the Wikipedia text a node/edge was extracted from."""

    model_config = ConfigDict(extra="allow")
    page: str
    paragraph: int | None = None
    url: str | None = None


class Node(BaseModel):
    """Graph entity (location, person, org, event, ...)."""

    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    geometry: dict[str, Any] | None = None  # GeoJSON geometry, (lon, lat)
    sources: list[Source] = Field(default_factory=list)
    text: str = ""  # raw source text the entity was extracted from
    attributes: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None


class Edge(BaseModel):
    """Directed, typed relationship source -> target (e.g. org ATTACKED location)."""

    model_config = ConfigDict(extra="allow")
    source: str
    target: str
    type: str
    count: int = 1  # number of supporting mentions in the corpus
    start_date: str | None = None
    end_date: str | None = None
    sources: list[Source] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None


# ------------------------------------------------------------------- helpers


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _date_bounds(value: str | None, *, end: bool) -> str | None:
    """Pad a partial ISO date to a full day: '1942' -> '1942-01-01' / '1942-12-31'."""
    if not value:
        return None
    parts = value.split("-")
    if len(parts) == 1:
        return f"{parts[0]}-12-31" if end else f"{parts[0]}-01-01"
    if len(parts) == 2:
        return f"{value}-31" if end else f"{value}-01"  # string compare only, so -31 is fine
    return value


def normalize_name(text: str) -> str:
    """Comparison form of a name: no diacritics, casefolded, punctuation-light."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    stripped = re.sub(r"['\u2018\u2019`]", "", stripped).replace("-", " ")
    return " ".join(stripped.split())


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


# --------------------------------------------------------------------- store


# --------------------------------------------------------- connection profiles
# Which paths lead from an entity to "its" locations (spec R26). One table, so
# the definition of "connected" can be tuned in one place:
#   person   direct person->location edges; events it COMMANDED / PARTICIPATED_IN -> event paths
#   org      LOCATION_RELATIONS to locations; events it PARTICIPATED_IN / COMMANDED -> event paths;
#            sub-units (org -PART_OF-> this org), recursively
#   event    OCCURRED_AT; sub-events (event -PART_OF-> this event), recursively
#   location itself and LOCATED_IN children
# Never traversed: MEMBER_OF, ALLIED_WITH (hubs: Yamamoto would get every IJN
# place), and CO_MENTIONED unless include_weak (with a minimum count).
LOCATION_RELATIONS = {"ATTACKED", "DEFENDED", "CAPTURED", "OCCUPIED", "LANDED_AT", "BOMBED", "BASED_AT"}
EVENT_LINKS = {"COMMANDED", "PARTICIPATED_IN"}
STRUCTURAL_RELATIONS = {"PART_OF", "LOCATED_IN"}  # timeless: pass date filters
WEAK_RELATION = "CO_MENTIONED"
# location_kind values that cover several graph kinds
KIND_GROUPS = {"island": {"island", "atoll"}, "city": {"city", "town", "village", "port"}}
SELF_STRENGTH = 99  # strength of an entity's own location (no path needed)

# Flagged approximations while the graph lacks kinds / countries (spec R30). Each one is
# used only when NO node of that type carries the attribute, and tools report it.
KIND_NAME_PATTERNS = {
    "island": r"\b(island|islands|isle|atoll|jima|lagoon)\b",
    "city": r"\b(city|town|port)\b",
    "battle": r"\bbattle\b",
    "campaign": r"\bcampaign\b",
    "raid": r"\braid\b",
    "bombing": r"\bbomb(ing|ings)?\b",
}
COUNTRY_NAME_PATTERNS = {
    "japan": r"\b(japan|japanese|imperial)\b",
    "united states": r"\b(united states|u\.s\.|us)\b",
}


class ResultSets:
    """Bounded registry of id sets behind short handles (spec R27).

    Set tools put the full list of ids here and give the model only the handle
    plus a summary, so the model never carries or copies a large list; the
    final answer names the handle and the map is built from the stored ids.
    """

    def __init__(self, max_sets: int = 200):
        self.max_sets = max_sets
        self._sets: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def add(self, ids: list[str], label: str, meta: dict[str, Any] | None = None) -> str:
        handle = "rs-" + uuid.uuid4().hex[:6]
        self._sets[handle] = {"ids": list(dict.fromkeys(ids)), "label": label, "meta": meta or {}}
        while len(self._sets) > self.max_sets:
            self._sets.popitem(last=False)  # least recently used
        return handle

    def get(self, handle: str) -> dict[str, Any] | None:
        entry = self._sets.get(handle)
        if entry is not None:
            self._sets.move_to_end(handle)
        return entry


class GraphStore:
    """Loads the graph JSON and exposes the query primitives used by the tools."""

    def __init__(self, nodes: list[Node], edges: list[Edge]):
        self.nodes: dict[str, Node] = {n.id: n for n in nodes}
        # Drop edges pointing at unknown nodes instead of failing the whole load.
        self.edges: list[Edge] = [e for e in edges if e.source in self.nodes and e.target in self.nodes]
        self.out_edges: dict[str, list[Edge]] = defaultdict(list)
        self.in_edges: dict[str, list[Edge]] = defaultdict(list)
        for e in self.edges:
            self.out_edges[e.source].append(e)
            self.in_edges[e.target].append(e)

        self.geoms: dict[str, BaseGeometry] = {
            n.id: shape(n.geometry) for n in nodes if n.geometry
        }

        # Name index for fuzzy lookup: one entry per name/alias, compared in
        # normalized form; the original label is kept for display.
        self._names: list[str] = []  # normalized labels
        self._labels: list[str] = []  # original labels
        self._name_owner: list[str] = []
        self._norm_name: dict[str, str] = {}  # node id -> normalized name
        self._norm_aliases: dict[str, set[str]] = {}  # node id -> normalized aliases
        for n in nodes:
            self._norm_name[n.id] = normalize_name(n.name)
            self._norm_aliases[n.id] = {normalize_name(a) for a in n.aliases}
            for label in [n.name, *n.aliases]:
                self._names.append(normalize_name(label))
                self._labels.append(label)
                self._name_owner.append(n.id)

        # Instruction-like sentences are removed from source text before it is indexed or
        # returned by any tool (spec R32); the raw text stays on the node.
        self.safe_text: dict[str, str] = {}
        self.redacted_ids: set[str] = set()
        for n in nodes:
            paras = []
            for para in n.text.split("\n\n"):
                clean, removed = redact_instructions(para)
                paras.append(clean)
                if removed:
                    self.redacted_ids.add(n.id)
            self.safe_text[n.id] = "\n\n".join(paras)

        # Text index: one chunk per paragraph of each node's (redacted) text.
        self._chunks: list[tuple[str, str]] = [
            (n.id, para.strip())
            for n in nodes
            for para in self.safe_text[n.id].split("\n\n")
            if para.strip()
        ]
        self._bm25 = BM25Okapi([_tokenize(t) for _, t in self._chunks]) if self._chunks else None

    @classmethod
    def from_json(cls, path: str | Path) -> "GraphStore":
        """Load a graph from a JSON file shaped like {"nodes": [...], "edges": [...]}."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            [Node.model_validate(n) for n in data.get("nodes", [])],
            [Edge.model_validate(e) for e in data.get("edges", [])],
        )

    # ------------------------------------------------------------ serialization

    def summarize_node(self, node_id: str) -> dict[str, Any]:
        """Compact node view for tool output: id, name, type, representative lat/lon."""
        n = self.nodes[node_id]
        out: dict[str, Any] = {"id": n.id, "name": n.name, "type": n.type}
        if (g := self.geoms.get(node_id)) is not None:
            c = g.representative_point()
            out.update(lat=round(c.y, 4), lon=round(c.x, 4), geometry_type=g.geom_type)
        return out

    def summarize_edge(self, e: Edge) -> dict[str, Any]:
        """Edge view with both endpoints resolved to names, plus dates and sources."""
        out: dict[str, Any] = {
            "source": {"id": e.source, "name": self.nodes[e.source].name, "type": self.nodes[e.source].type},
            "relation": e.type,
            "target": {"id": e.target, "name": self.nodes[e.target].name, "type": self.nodes[e.target].type},
            "count": e.count,
        }
        if e.start_date or e.end_date:
            out["start_date"], out["end_date"] = e.start_date, e.end_date
        if e.sources:
            out["sources"] = [s.page for s in e.sources]
        return out

    # ----------------------------------------------------------------- queries

    def schema(self) -> dict[str, Any]:
        """Vocabulary and extent of the loaded graph."""
        dates = sorted(d for e in self.edges for d in (e.start_date, e.end_date) if d)
        bounds = None
        if self.geoms:
            xs0, ys0, xs1, ys1 = zip(*(g.bounds for g in self.geoms.values()))
            bounds = {"min_lon": min(xs0), "min_lat": min(ys0), "max_lon": max(xs1), "max_lat": max(ys1)}
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(Counter(n.type for n in self.nodes.values())),
            "relation_types": dict(Counter(e.type for e in self.edges)),
            "date_range": [dates[0], dates[-1]] if dates else None,
            "bbox": bounds,
        }

    def search_entities(self, query: str, type: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Fuzzy match `query` against names and aliases; best score per node.

        Every result carries:
        - `matched`: the original graph label that matched best
        - `match`: "name" / "alias" when the normalized query equals the node's
          normalized name / an alias, else "fuzzy"
        - `quality`: "clear" only for the best result with score >= CLEAR_SCORE
          and no other result within AMBIGUITY_MARGIN of it; else "uncertain"
        - `ambiguous`: true when another result scores within AMBIGUITY_MARGIN
        """
        q = normalize_name(query)
        # processor=None: strings are already normalized; don't depend on rapidfuzz defaults.
        matches = process.extract(q, self._names, scorer=fuzz.WRatio, processor=None,
                                  limit=len(self._names), score_cutoff=MATCH_CUTOFF)
        results, seen = [], set()
        for _, score, idx in matches:
            node_id = self._name_owner[idx]
            if node_id in seen or (type and self.nodes[node_id].type != type):
                continue
            seen.add(node_id)
            match = "name" if q == self._norm_name[node_id] else "alias" if q in self._norm_aliases[node_id] else "fuzzy"
            results.append({**self.summarize_node(node_id), "matched": self._labels[idx], "score": round(score), "match": match})
            if len(results) >= limit:
                break

        scores = [r["score"] for r in results]
        for i, r in enumerate(results):
            others = scores[:i] + scores[i + 1:]
            r["ambiguous"] = any(abs(r["score"] - s) <= AMBIGUITY_MARGIN for s in others)
            clear = i == 0 and r["score"] >= CLEAR_SCORE and not r["ambiguous"]
            r["quality"] = "clear" if clear else "uncertain"
        return results

    def get_entity(self, entity_id: str, text_chars: int = 1500) -> dict[str, Any] | None:
        """Full node details, with the raw text truncated to `text_chars`."""
        n = self.nodes.get(entity_id)
        if n is None:
            return None
        return {
            **self.summarize_node(entity_id),
            "aliases": n.aliases,
            "attributes": n.attributes,
            "geometry": n.geometry,
            "sources": [s.model_dump(exclude_none=True) for s in n.sources],
            "text": self.safe_text[entity_id][:text_chars],
            **({"text_redacted": True} if entity_id in self.redacted_ids else {}),
            "degree": len(self.out_edges[entity_id]) + len(self.in_edges[entity_id]),
        }

    def edge_in_range(self, e: Edge, date_from: str | None, date_to: str | None) -> bool:
        """True if the edge's [start, end] interval overlaps [date_from, date_to]."""
        if not date_from and not date_to:
            return True
        start = _date_bounds(e.start_date or e.end_date, end=False)
        end = _date_bounds(e.end_date or e.start_date, end=True)
        if start is None:
            return False
        q_from = _date_bounds(date_from, end=False) or "0000"
        q_to = _date_bounds(date_to, end=True) or "9999"
        return start <= q_to and end >= q_from

    def sublocations(self, location_id: str) -> set[str]:
        """The location plus everything transitively LOCATED_IN it."""
        found, stack = {location_id}, [location_id]
        while stack:
            for e in self.in_edges[stack.pop()]:
                if e.type == "LOCATED_IN" and e.source not in found:
                    found.add(e.source)
                    stack.append(e.source)
        return found

    def find_relations(
        self,
        source_id: str | None = None,
        target_id: str | None = None,
        relation_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_sublocations: bool = True,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """Filter edges by endpoint(s), type and date. Sorted by count, descending.

        When `include_sublocations` is set, a location endpoint also matches places
        LOCATED_IN it (e.g. Henderson Field for Guadalcanal).
        """
        def expand(node_id: str | None) -> set[str] | None:
            if node_id is None:
                return None
            if include_sublocations and node_id in self.nodes and self.nodes[node_id].type == "location":
                return self.sublocations(node_id)
            return {node_id}

        sources, targets = expand(source_id), expand(target_id)
        rel = relation_type.upper() if relation_type else None
        candidates = (
            [e for t in targets for e in self.in_edges[t]] if targets
            else [e for s in sources for e in self.out_edges[s]] if sources
            else self.edges
        )
        hits = [
            e for e in candidates
            if (sources is None or e.source in sources)
            and (targets is None or e.target in targets)
            and (rel is None or e.type == rel)
            and self.edge_in_range(e, date_from, date_to)
        ]
        hits.sort(key=lambda e: -e.count)
        return [self.summarize_edge(e) for e in hits[:limit]]

    def neighbors(
        self,
        entity_id: str,
        relation_types: list[str] | None = None,
        direction: str = "both",
        neighbor_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """One-hop neighborhood. `direction` is "out", "in" or "both"."""
        rels = {r.upper() for r in relation_types} if relation_types else None
        edges = []
        if direction in ("out", "both"):
            edges += [(e, e.target) for e in self.out_edges[entity_id]]
        if direction in ("in", "both"):
            edges += [(e, e.source) for e in self.in_edges[entity_id]]
        results = [
            {**self.summarize_edge(e), "neighbor": self.summarize_node(other)}
            for e, other in edges
            if (rels is None or e.type in rels)
            and (neighbor_type is None or self.nodes[other].type == neighbor_type)
            and self.edge_in_range(e, date_from, date_to)
        ]
        results.sort(key=lambda r: -r["count"])
        return results[:limit]

    # ------------------------------------------------------ set queries (R26)

    def _edge_ok(self, e: Edge, date_from: str | None, date_to: str | None) -> bool:
        """Date filter for path edges: structural edges are timeless; an undated
        OCCURRED_AT falls back to its event's dates (as what_happened_at does)."""
        if not date_from and not date_to:
            return True
        if e.type in STRUCTURAL_RELATIONS and not (e.start_date or e.end_date):
            return True
        if e.type == "OCCURRED_AT" and not (e.start_date or e.end_date):
            attrs = self.nodes[e.source].attributes
            proxy = Edge(source=e.source, target=e.target, type=e.type,
                         start_date=attrs.get("start_date"), end_date=attrs.get("end_date"))
            return self.edge_in_range(proxy, date_from, date_to)
        return self.edge_in_range(e, date_from, date_to)

    def _profile_paths(self, entity_id: str, ok, include_weak: bool, min_weak_count: int, seen: set[str]):
        """Yield (location_id, [edges]) for every path the entity's profile allows."""
        if entity_id in seen:  # PART_OF cycles
            return
        seen = seen | {entity_id}
        node = self.nodes[entity_id]

        def event_paths(event_id: str, prefix: list[Edge], visited: set[str]):
            if event_id in visited:
                return
            visited = visited | {event_id}
            for e in self.out_edges[event_id]:
                if e.type == "OCCURRED_AT" and self.nodes[e.target].type == "location" and ok(e):
                    yield e.target, prefix + [e]
            for e in self.in_edges[event_id]:  # sub-event -PART_OF-> event
                if e.type == "PART_OF" and self.nodes[e.source].type == "event" and ok(e):
                    yield from event_paths(e.source, prefix + [e], visited)

        if node.type == "location":
            yield entity_id, []
            for child in self.sublocations(entity_id) - {entity_id}:
                yield child, [e for e in self.out_edges[child] if e.type == "LOCATED_IN"][:1]
            return
        if node.type == "event":
            yield from event_paths(entity_id, [], set())
            return

        for e in self.out_edges[entity_id]:
            target = self.nodes[e.target]
            if not ok(e):
                continue
            if target.type == "location" and (e.type in LOCATION_RELATIONS or (node.type != "org" and e.type != WEAK_RELATION)):
                yield e.target, [e]
            elif target.type == "event" and e.type in EVENT_LINKS:
                yield from event_paths(e.target, [e], set())
        if node.type == "org":  # sub-units: unit -PART_OF-> this org
            for e in self.in_edges[entity_id]:
                if e.type == "PART_OF" and self.nodes[e.source].type == "org" and ok(e):
                    for loc, path in self._profile_paths(e.source, ok, include_weak, min_weak_count, seen):
                        yield loc, [e] + path
        if include_weak:
            for e, other in [(e, e.target) for e in self.out_edges[entity_id]] + [(e, e.source) for e in self.in_edges[entity_id]]:
                if e.type == WEAK_RELATION and e.count >= min_weak_count and self.nodes[other].type == "location" and ok(e):
                    yield other, [e]

    def _path_text(self, start_id: str, path: list[Edge]) -> tuple[str, str | None]:
        """Readable path ("Yamamoto -COMMANDED-> Battle of Midway -OCCURRED_AT-> Midway Atoll") and its first intermediate id."""
        parts, current, via_id = [self.nodes[start_id].name], start_id, None
        for e in path:
            nxt = e.target if e.source == current else e.source
            arrow = f"-{e.type}->" if e.source == current else f"<-{e.type}-"
            parts += [arrow, self.nodes[nxt].name]
            if via_id is None and self.nodes[nxt].type != "location":
                via_id = nxt
            current = nxt
        return " ".join(parts), via_id

    def connected_locations(
        self,
        entity_id: str,
        location_kind: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_weak: bool = False,
        min_weak_count: int = 3,
    ) -> list[dict[str, Any]]:
        """Locations connected to an entity by its profile's paths (spec R26), strongest first.

        Each row: id, name, type, lat/lon, kind, `via` (the best path as text),
        `via_id` (first intermediate entity, e.g. the battle), `strength` (the
        weakest edge count on the best path) and `paths` (number of distinct paths).
        """
        if entity_id not in self.nodes:
            return []
        ok = lambda e: self._edge_ok(e, date_from, date_to)
        best: dict[str, tuple[int, list[Edge]]] = {}
        n_paths: Counter[str] = Counter()
        for loc, path in self._profile_paths(entity_id, ok, include_weak, min_weak_count, set()):
            if location_kind and not self._matches_kind(self.nodes[loc], location_kind):
                continue
            strength = min((e.count for e in path), default=SELF_STRENGTH)
            n_paths[loc] += 1
            if loc not in best or (strength, -len(path)) > (best[loc][0], -len(best[loc][1])):
                best[loc] = (strength, path)
        rows = []
        for loc, (strength, path) in best.items():
            via, via_id = self._path_text(entity_id, path)
            rows.append({**self.summarize_node(loc), "kind": self.nodes[loc].attributes.get("kind"),
                         "via": via, "via_id": via_id, "strength": strength, "paths": n_paths[loc]})
        rows.sort(key=lambda r: (-r["strength"], -r["paths"], r["name"]))
        return rows

    def has_attribute(self, type: str, key: str) -> bool:
        """True if any node of `type` carries attributes[key] (cached: the graph doesn't change after loading)."""
        cache = self.__dict__.setdefault("_attr_cache", {})
        if (type, key) not in cache:
            cache[(type, key)] = any(n.attributes.get(key) for n in self.nodes.values() if n.type == type)
        return cache[(type, key)]

    def _matches_kind(self, node: Node, kind: str) -> bool:
        """Kind test: the attribute when the graph has kinds for this type, else the name heuristic (R30)."""
        if self.has_attribute(node.type, "kind"):
            return node.attributes.get("kind") in KIND_GROUPS.get(kind, {kind})
        pattern = KIND_NAME_PATTERNS.get(kind)
        labels = [node.name, *node.aliases]
        return bool(pattern) and any(re.search(pattern, l, re.IGNORECASE) for l in labels)

    def approximations(self, type: str, kind: str | None = None, group: str | list[str] | None = None) -> list[str]:
        """Plain-language notes for every approximation a query on `type` would use (spec R30)."""
        notes = []
        if kind and not self.has_attribute(type, "kind"):
            notes.append(f"'{kind}' approximated from names: the graph has no {type} kinds")
        for g in group if isinstance(group, list) else [group]:
            if isinstance(g, str) and g.startswith("country:") and not self.has_attribute("org", "country"):
                notes.append(f"'{g[8:]}' orgs approximated from names: the graph has no org countries")
        return notes

    def entities_mentioning(self, phrase: str, type: str = "event") -> list[dict[str, Any]]:
        """Entities whose name, aliases or raw text mention `phrase` (normalized). A text-based
        approximation for concepts that aren't graph entities, e.g. a campaign (spec R30)."""
        needle = normalize_name(phrase)
        rows = []
        for node in self.nodes.values():
            if node.type != type:
                continue
            haystack = normalize_name(" ".join([node.name, *node.aliases, node.text]))
            if needle and needle in haystack:
                rows.append({**self.summarize_node(node.id), "importance": self.importance(node.id)})
        rows.sort(key=lambda r: (-r["importance"], r["name"]))
        return rows

    def importance(self, entity_id: str) -> int:
        """Ranking signal for "major" (spec R28): the graph's `importance` attribute when
        present, else the sum of edge counts plus the number of source pages."""
        node = self.nodes[entity_id]
        if isinstance(node.attributes.get("importance"), (int, float)):
            return int(node.attributes["importance"])
        edges = self.out_edges[entity_id] + self.in_edges[entity_id]
        return sum(e.count for e in edges) + len({s.page for s in node.sources})

    def find_entities(
        self,
        type: str,
        kind: str | None = None,
        country: str | None = None,
        side: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Entities of a type, filtered by kind / country / side / dates, most important first.

        Dates match the entity's own start_date / end_date attributes (interval
        overlap); entities without dates are dropped when a date filter is set.
        """
        rows = []
        name_country = country and not self.has_attribute(type, "country")
        for node in self.nodes.values():
            a = node.attributes
            if node.type != type or (kind and not self._matches_kind(node, kind)):
                continue
            if country and name_country:  # R30: no country attribute in the graph
                pattern = COUNTRY_NAME_PATTERNS.get(country.casefold(), re.escape(country))
                if not any(re.search(pattern, l, re.IGNORECASE) for l in [node.name, *node.aliases]):
                    continue
            elif country and str(a.get("country", "")).casefold() != country.casefold():
                continue
            if side and str(a.get("side", "")).casefold() != side.casefold():
                continue
            if date_from or date_to:
                proxy = Edge(source=node.id, target=node.id, type="SELF", start_date=a.get("start_date"), end_date=a.get("end_date"))
                if not self.edge_in_range(proxy, date_from, date_to):
                    continue
            rows.append({**self.summarize_node(node.id), "kind": a.get("kind"), "importance": self.importance(node.id),
                         "start_date": a.get("start_date"), "end_date": a.get("end_date")})
        rows.sort(key=lambda r: (-r["importance"], r["name"]))
        return rows

    def group_members(self, spec: str | list[str]) -> list[str]:
        """Org ids for a group: a list of entity ids, "country:<name>" or "side:<name>"."""
        if isinstance(spec, list):
            return [i for i in spec if i in self.nodes]
        key, _, value = spec.partition(":")
        if key not in ("country", "side") or not value:
            return [spec] if spec in self.nodes else []
        return [r["id"] for r in self.find_entities("org", **{key: value})]

    def shared_connections(self, group_a: str | list[str], group_b: str | list[str]) -> dict[str, Any]:
        """Places and events both groups connect to (spec R28).

        Each group's places are the union of its members' connection profiles;
        the shared places are their intersection. Shared events are events both
        groups took part in or commanded.
        """
        def side_data(members: list[str]):
            locs: dict[str, dict[str, Any]] = {}
            events: set[str] = set()
            for m in members:
                for r in self.connected_locations(m):
                    if r["id"] not in locs or r["strength"] > locs[r["id"]]["strength"]:
                        locs[r["id"]] = r
                events |= {e.target for e in self.out_edges[m] if e.type in EVENT_LINKS and self.nodes[e.target].type == "event"}
            return locs, events

        a_members, b_members = self.group_members(group_a), self.group_members(group_b)
        a_locs, a_events = side_data(a_members)
        b_locs, b_events = side_data(b_members)
        rows = []
        for loc in a_locs.keys() & b_locs.keys():
            ra, rb = a_locs[loc], b_locs[loc]
            rows.append({**self.summarize_node(loc), "kind": self.nodes[loc].attributes.get("kind"),
                         "via": f"{ra['via']}  |  {rb['via']}", "via_id": ra.get("via_id"),
                         "strength": min(ra["strength"], rb["strength"]), "paths": ra["paths"] + rb["paths"]})
        rows.sort(key=lambda r: (-r["strength"], -r["paths"], r["name"]))
        shared_events = sorted(a_events & b_events, key=lambda e: -self.importance(e))
        return {"a_members": a_members, "b_members": b_members, "shared_events": shared_events, "locations": rows}

    def summarize_set(self, rows: list[dict[str, Any]], top: int = 15) -> dict[str, Any]:
        """Compact view of a large location list for the model (spec R27): counts and the top rows."""
        by_via = Counter(self.nodes[r["via_id"]].name if r.get("via_id") else "direct" for r in rows)
        by_region = Counter()
        for r in rows:
            parent = next((e.target for e in self.out_edges[r["id"]] if e.type == "LOCATED_IN"), None)
            by_region[self.nodes[parent].name if parent else "(no region)"] += 1
        keep = ("id", "name", "lat", "lon", "via", "strength")
        return {
            "total": len(rows),
            "shown": min(len(rows), top),
            "truncated": len(rows) > top,
            "by_via": dict(by_via.most_common(10)),
            "by_region": dict(by_region.most_common(10)),
            "top": [{k: r[k] for k in keep if k in r} for r in rows[:top]],
        }

    def locations_of(self, entity_id: str) -> list[dict[str, Any]]:
        """Locations directly linked to an entity (any relation, either direction).

        For an event this returns its OCCURRED_AT places; for an org it returns the
        places it attacked, captured, occupied, and so on. Only locations with
        geometry are returned, since the point is to show them on a map.
        """
        n = self.nodes.get(entity_id)
        if n is None:
            return []
        if n.type == "location" and entity_id in self.geoms:
            return [{**self.summarize_node(entity_id), "relations": ["SELF"]}]
        by_loc: dict[str, dict[str, Any]] = {}
        for e, other in [(e, e.target) for e in self.out_edges[entity_id]] + [(e, e.source) for e in self.in_edges[entity_id]]:
            if self.nodes[other].type != "location" or other not in self.geoms:
                continue
            entry = by_loc.setdefault(other, {**self.summarize_node(other), "relations": []})
            entry["relations"].append({"type": e.type, "start_date": e.start_date, "end_date": e.end_date})
        return list(by_loc.values())

    def distance_km(self, node_id: str, lat: float, lon: float) -> float:
        """Distance from (lat, lon) to the nearest point of the node's geometry (0 if inside)."""
        g = self.geoms[node_id]
        p = Point(lon, lat)
        if g.contains(p):
            return 0.0
        near, _ = nearest_points(g, p)
        return haversine_km(lat, lon, near.y, near.x)

    def near(self, lat: float, lon: float, radius_km: float, type: str | None = None, limit: int | None = 25) -> list[dict[str, Any]]:
        """Entities whose geometry lies within `radius_km` of (lat, lon), nearest first."""
        hits = []
        for node_id in self.geoms:
            if type and self.nodes[node_id].type != type:
                continue
            d = self.distance_km(node_id, lat, lon)
            if d <= radius_km:
                hits.append((d, node_id))
        hits.sort()
        return [{**self.summarize_node(i), "distance_km": round(d, 1)} for d, i in hits[:limit]]

    def in_bbox(self, min_lon: float, min_lat: float, max_lon: float, max_lat: float, type: str | None = None, limit: int | None = 100) -> list[dict[str, Any]]:
        """Entities whose geometry intersects the bounding box.

        A box with min_lon > max_lon crosses the 180° meridian (e.g. 170 to -170
        is the 20° strip around the date line); it is searched as two boxes,
        [min_lon, 180] and [-180, max_lon]. Otherwise the box is used as given.
        """
        if min_lon > max_lon:
            region = box(min_lon, min_lat, 180.0, max_lat).union(box(-180.0, min_lat, max_lon, max_lat))
        else:
            region = box(min_lon, min_lat, max_lon, max_lat)
        return [
            self.summarize_node(i)
            for i, g in self.geoms.items()
            if g.intersects(region) and (type is None or self.nodes[i].type == type)
        ][:limit]

    def what_happened_at(self, lat: float, lon: float, radius_km: float = 50, text_chars: int = 400) -> dict[str, Any]:
        """Composite point query: nearby places, then their events and relations.

        Steps:
        1. Locations within `radius_km` of the point.
        2. Every edge touching those locations (attacks, captures, OCCURRED_AT, ...).
        3. For the events found, their participants and commanders.
        Relations are sorted chronologically, and events carry a text excerpt.
        """
        places = self.near(lat, lon, radius_km, type="location", limit=None)  # the tool caps and reports totals
        place_ids = {p["id"] for p in places}
        edges = [e for pid in place_ids for e in self.in_edges[pid] + self.out_edges[pid]]
        event_ids = {e.source for e in edges if self.nodes[e.source].type == "event"}
        edges += [e for ev in event_ids for e in self.in_edges[ev]]
        unique = list({id(e): e for e in edges}.values())
        unique.sort(key=lambda e: (e.start_date or "9999", -e.count))
        events = sorted(
            (
                {**self.summarize_node(ev), **self.nodes[ev].attributes, "excerpt": self.safe_text[ev][:text_chars],
                 **({"text_redacted": True} if ev in self.redacted_ids else {})}
                for ev in event_ids
            ),
            key=lambda x: x.get("start_date", "9999"),
        )
        return {
            "places": places,
            "events": events,
            "relations": [self.summarize_edge(e) for e in unique if e.type != "LOCATED_IN"],
        }

    def search_text(self, query: str, entity_ids: list[str] | None = None, k: int = 5) -> list[dict[str, Any]]:
        """BM25 search over node text paragraphs, optionally limited to `entity_ids`."""
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        allowed = set(entity_ids) if entity_ids else None
        ranked = sorted(
            (i for i, (nid, _) in enumerate(self._chunks) if allowed is None or nid in allowed),
            key=lambda i: -scores[i],
        )
        out = []
        for i in ranked[:k]:
            if scores[i] <= 0 and allowed is None:
                break
            nid, text = self._chunks[i]
            n = self.nodes[nid]
            out.append({
                "entity": {"id": nid, "name": n.name, "type": n.type},
                "text": text,
                **({"text_redacted": True} if nid in self.redacted_ids else {}),
                "sources": [s.model_dump(exclude_none=True) for s in n.sources],
                "score": round(float(scores[i]), 3),
            })
        return out

    def to_geojson(self, entity_ids: list[str], restrict_to: set[str] | None = None) -> dict[str, Any]:
        """FeatureCollection for the map UI.

        Entities without geometry (events, people, orgs) are drawn at the
        locations they are linked to, so `to_geojson(["event:battle_of_midway"])`
        still yields a map feature. With `restrict_to`, such an entity is drawn
        only at linked locations in that set (spec R23); otherwise at all of them.
        Entities with geometry are always drawn.
        """
        features, seen = [], set()

        def add(loc_id: str, via: str | None = None) -> None:
            if loc_id in seen or loc_id not in self.geoms:
                return
            seen.add(loc_id)
            n = self.nodes[loc_id]
            props = {"id": n.id, "name": n.name, "type": n.type}
            if via:
                props["related_to"] = via
            features.append({"type": "Feature", "geometry": n.geometry, "properties": props})

        for eid in entity_ids:
            if eid not in self.nodes:
                continue
            if eid in self.geoms:
                add(eid)
            else:
                for loc in self.locations_of(eid):
                    if restrict_to is None or loc["id"] in restrict_to:
                        add(loc["id"], via=eid)
        return {"type": "FeatureCollection", "features": features}
