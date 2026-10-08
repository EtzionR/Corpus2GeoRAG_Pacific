"""LangChain tools exposed to the agent.

Each tool is a thin wrapper over a `GraphStore` method. List tools return
`{total, shown, truncated, results}`: when `truncated` is true there are more
rows than shown, so the model must not treat the list as complete (spec R25). The docstrings are sent to
the LLM as the tool descriptions, so they spell out argument meaning, units and
coordinate order. Tools return JSON strings: compact, deterministic, and easy to
cite from.

All tools are read-only and answer from the loaded graph only; they make no
network calls.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from geo_agent.graph_store import GraphStore, ResultSets


# Row caps for list tools (spec R25). The tool reports the full total, so a cut
# list is never mistaken for a complete one.
RELATIONS_CAP = 50
LOCATIONS_CAP = 50
NEAR_CAP = 25
BBOX_CAP = 100


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _approx(notes: list[str], text_based: bool = False) -> dict[str, Any]:
    """`approximation` field for a tool payload (spec R30); empty when nothing was approximated."""
    if not notes:
        return {}
    return {"approximation": "; ".join(notes), **({"text_based": True} if text_based else {})}


def _paged(rows: list[Any], cap: int, empty: str) -> Any:
    """{total, shown, truncated, results} for a list, or {"result": empty} when there is nothing."""
    if not rows:
        return {"result": empty}
    return {"total": len(rows), "shown": min(len(rows), cap), "truncated": len(rows) > cap, "results": rows[:cap]}


def _year_range(year: int | None, date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """`year` is shorthand for date_from=date_to=<year>; explicit dates win."""
    if year is not None:
        return date_from or str(year), date_to or str(year)
    return date_from, date_to


FIND_ENTITIES_DEFAULT = 10  # "major battles": the top N by importance


def build_tools(store: GraphStore, sets: ResultSets | None = None) -> list[BaseTool]:
    """Create the agent's tool list bound to a loaded graph.

    `sets` is the result-set registry shared with finalize_answer (spec R27);
    a private one is created when none is given (e.g. in tests).
    """
    sets = sets if sets is not None else ResultSets()

    def register(ids: list[str], label: str, rows: list[dict[str, Any]], draw: list[str] | None = None) -> str:
        """Store a set; `rows` give each map location's via/strength for the map features."""
        meta = {r["id"]: {"via": r.get("via"), "strength": r.get("strength")} for r in rows}
        return sets.add(ids, label, {"draw": draw if draw is not None else list(ids), "locations": meta})

    @tool
    def graph_schema() -> str:
        """Describe the knowledge graph: node types, relation types (with counts),
        date range and geographic bounding box. Call this first when unsure which
        entity or relation types exist."""
        return _json(store.schema())

    @tool
    def search_entities(query: str, type: str | None = None, limit: int = 5) -> str:
        """Find entities by (fuzzy) name or alias and return their ids.

        Use this to resolve names mentioned by the user (e.g. "Midway",
        "battle of Guadalcanal", "Japanese navy") into entity ids for the other tools.
        Case and accents are ignored ("TOKYO", "Tōkyō" and "tokyo" are the same).
        Args:
            query: name to look up.
            type: optional filter: "location", "person", "org", "event" (see graph_schema).
            limit: max results.

        Each result has `match` ("name", "alias" or "fuzzy"), `score` (0-100),
        `quality` and `ambiguous`:
        - quality "clear": the top result is a confident, unique match. Use it.
        - quality "uncertain": a weak match, or `ambiguous` (another result scores
          almost the same, e.g. "Battle of Guam (1941)" vs "Battle of Guam (1944)").
          If this entity matters to the answer, do NOT answer from it: ask the user
          one short question naming the candidates, unless the question already
          settles it (year, type or context).
        - no results: the graph has no such entity; say so.
        """
        return _json(store.search_entities(query, type, limit) or {"result": "no matching entity in graph"})

    @tool
    def get_entity(entity_id: str) -> str:
        """Get full details of one entity: aliases, attributes (e.g. dates),
        GeoJSON geometry, Wikipedia sources and a raw-text excerpt."""
        return _json(store.get_entity(entity_id) or {"error": f"unknown entity_id {entity_id}"})

    @tool
    def get_neighbors(
        entity_id: str,
        relation_types: list[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        neighbor_type: str | None = None,
        year: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """List entities directly connected to `entity_id`, with the relation, its
        dates, mention count and sources.

        Args:
            relation_types: optional filter, e.g. ["PARTICIPATED_IN", "COMMANDED"].
            direction: "out" = entity is the subject, "in" = entity is the object.
            neighbor_type: optional filter on the connected entity's type.
            year / date_from / date_to: keep relations whose dates overlap the range
                (ISO dates, partial allowed: "1942", "1942-06"). Undated relations
                are dropped when a date filter is set.
        """
        f, t = _year_range(year, date_from, date_to)
        return _json(_paged(store.neighbors(entity_id, relation_types, direction, neighbor_type, f, t, limit=None),
                            RELATIONS_CAP, "no matching neighbors in graph"))

    @tool
    def find_relations(
        source_id: str | None = None,
        target_id: str | None = None,
        relation_type: str | None = None,
        year: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_sublocations: bool = True,
    ) -> str:
        """Query relations (edges) as source -[relation_type]-> target, with optional
        date filtering. Leave an endpoint empty to use it as a wildcard.

        Example: who attacked Guadalcanal in 1942 ->
            find_relations(target_id="loc:guadalcanal", relation_type="ATTACKED", year=1942)
        With include_sublocations=True, a location also matches places inside it
        (e.g. Henderson Field is LOCATED_IN Guadalcanal).
        Results are sorted by mention count (higher = better supported).
        """
        f, t = _year_range(year, date_from, date_to)
        rows = store.find_relations(source_id, target_id, relation_type, f, t, include_sublocations, limit=None)
        return _json(_paged(rows, RELATIONS_CAP, "no matching relations in graph"))

    @tool
    def get_locations(entity_id: str) -> str:
        """Get the geolocated places linked to any entity, with coordinates.

        For a battle/event these are the places where it happened; for an org they
        are the places it attacked, captured, occupied, etc.; for a location it is
        the location itself.
        """
        return _json(_paged(store.locations_of(entity_id), LOCATIONS_CAP, "no geolocated places linked to this entity"))

    @tool
    def entities_near(lat: float, lon: float, radius_km: float = 50, type: str | None = None) -> str:
        """Find geolocated entities within `radius_km` kilometres of a point,
        nearest first. lat/lon are in decimal degrees (WGS84). Areas (polygons)
        match when the point is inside or within the radius of their edge."""
        return _json(_paged(store.near(lat, lon, radius_km, type, limit=None), NEAR_CAP, "nothing in graph within radius"))

    @tool
    def entities_in_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float, type: str | None = None) -> str:
        """Find geolocated entities intersecting a bounding box (decimal degrees),
        e.g. the area currently visible or selected on the map.

        Longitudes are -180..180. For a box that crosses the 180° meridian (the
        date line, e.g. Fiji or the Aleutians), pass min_lon > max_lon:
        min_lon=170, max_lon=-170 means 170°E eastward to 170°W.
        """
        return _json(_paged(store.in_bbox(min_lon, min_lat, max_lon, max_lat, type, limit=None), BBOX_CAP, "nothing in graph inside bbox"))

    @tool
    def what_happened_at(lat: float, lon: float, radius_km: float = 50) -> str:
        """Answer "what happened at this place/coordinate?" in one call.

        Returns the places within `radius_km` of (lat, lon), the events that took
        place there (with dates and excerpts), and every relation involving those
        places and events, sorted chronologically. Increase radius_km if empty.
        """
        data = store.what_happened_at(lat, lon, radius_km)
        for key, cap in (("places", NEAR_CAP), ("relations", RELATIONS_CAP)):  # spec R25: say when a list is cut
            data[f"{key}_total"] = len(data[key])
            data[f"{key}_truncated"] = len(data[key]) > cap
            data[key] = data[key][:cap]
        return _json(data)

    @tool
    def search_source_text(query: str, entity_ids: list[str] | None = None, k: int = 5) -> str:
        """Keyword search over the raw Wikipedia text behind the graph. Returns
        paragraphs with their entity and source page, which you can quote as
        evidence. Optionally restrict to specific entity_ids."""
        return _json(store.search_text(query, entity_ids, k) or {"result": "no matching text"})

    @tool
    def to_geojson(entity_ids: list[str]) -> str:
        """Build a GeoJSON FeatureCollection for the map from entity ids. Events,
        people and orgs are drawn at their linked locations. Use this to preview
        what will be shown on the map."""
        return _json(store.to_geojson(entity_ids))

    @tool
    def connected_locations(
        entity_id: str,
        location_kind: str | None = None,
        year: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_weak: bool = False,
    ) -> str:
        """ALL locations connected to a person, org, event or campaign, for "show me all locations
        connected to / associated with X" questions. One call returns the whole set.

        "Connected" follows fixed paths: a person -> the events they commanded or took part in ->
        where those happened; an org -> places it attacked/defended/captured/occupied/was based at,
        its events' places, and its sub-units (e.g. the Combined Fleet under the IJN); an event or
        campaign -> where it happened, including its sub-events. Membership (Yamamoto in the IJN) is
        not followed.
        Args:
            entity_id: from search_entities.
            location_kind: optional, e.g. "island" (includes atolls), "city", "airfield", "sea".
            year / date_from / date_to: optional period.
            include_weak: also follow plain co-mentions (noisier).
        Returns a result set: `result_set` (a handle), `total`, `by_via` (counts per battle/org),
        `by_region`, and the `top` 15 rows with their path (`via`). The full set is NOT listed:
        put the `result_set` handle in your final answer's answer_sets and the map shows every place.
        """
        f, t = _year_range(year, date_from, date_to)
        rows = store.connected_locations(entity_id, location_kind, f, t, include_weak)
        if not rows:
            return _json({"result": "no connected locations in graph"})
        name = store.nodes[entity_id].name if entity_id in store.nodes else entity_id
        label = f"locations connected to {name}" + (f" ({location_kind})" if location_kind else "")
        handle = register([r["id"] for r in rows], label, rows)
        return _json({"result_set": handle, "label": label, **_approx(store.approximations("location", location_kind)),
                      **store.summarize_set(rows)})

    @tool
    def find_entities(
        type: str,
        kind: str | None = None,
        country: str | None = None,
        side: str | None = None,
        year: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = FIND_ENTITIES_DEFAULT,
    ) -> str:
        """List entities by type and filters, most important first, e.g. "major battles":
        find_entities(type="event", kind="battle"). Importance = how much the graph says about
        the entity (relations and sources), or the graph's importance value when it has one.
        Args:
            type: "event", "org", "person" or "location".
            kind: e.g. "battle", "campaign", "raid", "bombing" for events; "island", "city" for locations.
            country / side: e.g. country="Japan", side="Allies" (orgs).
            year / date_from / date_to: events overlapping the period.
            limit: how many of the most important to keep (default 10).
        Returns a result set of the top entities (`result_set` handle, `total` candidates, `top`
        rows) whose places are drawn on the map when you put the handle in answer_sets.
        """
        f, t = _year_range(year, date_from, date_to)
        rows = store.find_entities(type, kind, country, side, f, t)
        if not rows:
            return _json({"result": "no matching entities in graph"})
        keep = rows[:max(1, limit)]
        draw_rows: dict[str, dict[str, Any]] = {}
        for r in keep:
            for loc in store.connected_locations(r["id"]):
                draw_rows.setdefault(loc["id"], loc)
        label = f"top {len(keep)} {kind or type}" + ("s" if len(keep) != 1 else "")
        notes = store.approximations(type, kind, f"country:{country}" if country else None)
        handle = register([r["id"] for r in keep], label, list(draw_rows.values()), draw=list(draw_rows))
        top = [{k: r[k] for k in ("id", "name", "kind", "importance", "start_date") if r.get(k) is not None} for r in keep]
        return _json({"result_set": handle, "label": label, **_approx(notes), "total": len(rows), "shown": len(keep),
                      "truncated": len(rows) > len(keep), "locations_on_map": len(draw_rows), "top": top})

    @tool
    def shared_connections(group_a: str | list[str], group_b: str | list[str]) -> str:
        """Places (and events) BOTH groups are connected to, e.g. "where are the United States and
        Japan both connected": shared_connections("country:United States", "country:Japan").
        Args:
            group_a, group_b: a list of entity ids, or "country:<name>" / "side:<name>" (orgs).
        Each group's places come from its members' connections (as connected_locations); the
        result is their intersection, with each side's path in `via`, plus the shared events.
        Returns a result set: put the `result_set` handle in answer_sets to draw every place.
        """
        data = store.shared_connections(group_a, group_b)
        if not data["a_members"] or not data["b_members"]:
            return _json({"result": "a group has no members in the graph", "a_members": data["a_members"], "b_members": data["b_members"]})
        rows = data["locations"]
        if not rows:
            return _json({"result": "no shared locations in graph", "shared_events": data["shared_events"]})
        label = f"places shared by {group_a} and {group_b}"
        handle = register([r["id"] for r in rows], label, rows)
        names = lambda ids: [store.nodes[i].name for i in ids]
        return _json({"result_set": handle, "label": label, **_approx(store.approximations("org", group=[group_a, group_b])),
                      "groups": {"a": names(data["a_members"]), "b": names(data["b_members"])},
                      "shared_events": names(data["shared_events"][:10]), **store.summarize_set(rows)})

    @tool
    def entities_mentioning(phrase: str, type: str = "event", limit: int = FIND_ENTITIES_DEFAULT) -> str:
        """FALLBACK for a concept that is NOT an entity in the graph (search_entities found no good
        match), e.g. a strategy or program: returns the events (or other entities) whose name or
        source text mention the phrase, as a result set whose places are drawn on the map.
        This is an approximation from text, not graph structure: say so in the answer.
        Args:
            phrase: e.g. "island hopping".
            type: entity type to return (default "event").
            limit: how many of the most important matches to keep.
        """
        rows = store.entities_mentioning(phrase, type)
        if not rows:
            return _json({"result": f"no {type} mentions '{phrase}' in the graph"})
        keep = rows[:max(1, limit)]
        draw_rows: dict[str, dict[str, Any]] = {}
        for r in keep:
            for loc in store.connected_locations(r["id"]):
                draw_rows.setdefault(loc["id"], loc)
        label = f"{type}s mentioning '{phrase}'"
        handle = register([r["id"] for r in keep], label, list(draw_rows.values()), draw=list(draw_rows))
        note = [f"'{phrase}' is not a graph entity; {type}s whose text mentions it are used instead"]
        return _json({"result_set": handle, "label": label, **_approx(note, text_based=True), "total": len(rows),
                      "shown": len(keep), "truncated": len(rows) > len(keep), "locations_on_map": len(draw_rows),
                      "top": [{"id": r["id"], "name": r["name"]} for r in keep]})

    return [
        graph_schema,
        search_entities,
        get_entity,
        get_neighbors,
        find_relations,
        get_locations,
        entities_near,
        entities_in_bbox,
        what_happened_at,
        search_source_text,
        to_geojson,
        connected_locations,
        find_entities,
        shared_connections,
        entities_mentioning,
    ]
