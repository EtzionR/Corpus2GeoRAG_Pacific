"""LangChain tools exposed to the agent.

Each tool is a thin wrapper over a `GraphStore` method. The docstrings are sent to
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

from geo_agent.graph_store import GraphStore


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _year_range(year: int | None, date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """`year` is shorthand for date_from=date_to=<year>; explicit dates win."""
    if year is not None:
        return date_from or str(year), date_to or str(year)
    return date_from, date_to


def build_tools(store: GraphStore) -> list[BaseTool]:
    """Create the agent's tool list bound to a loaded graph."""

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
        return _json(store.neighbors(entity_id, relation_types, direction, neighbor_type, f, t))

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
        rows = store.find_relations(source_id, target_id, relation_type, f, t, include_sublocations)
        return _json(rows or {"result": "no matching relations in graph"})

    @tool
    def get_locations(entity_id: str) -> str:
        """Get the geolocated places linked to any entity, with coordinates.

        For a battle/event these are the places where it happened; for an org they
        are the places it attacked, captured, occupied, etc.; for a location it is
        the location itself.
        """
        return _json(store.locations_of(entity_id) or {"result": "no geolocated places linked to this entity"})

    @tool
    def entities_near(lat: float, lon: float, radius_km: float = 50, type: str | None = None) -> str:
        """Find geolocated entities within `radius_km` kilometres of a point,
        nearest first. lat/lon are in decimal degrees (WGS84). Areas (polygons)
        match when the point is inside or within the radius of their edge."""
        return _json(store.near(lat, lon, radius_km, type) or {"result": "nothing in graph within radius"})

    @tool
    def entities_in_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float, type: str | None = None) -> str:
        """Find geolocated entities intersecting a bounding box (decimal degrees),
        e.g. the area currently visible or selected on the map.

        Longitudes are -180..180. For a box that crosses the 180° meridian (the
        date line, e.g. Fiji or the Aleutians), pass min_lon > max_lon:
        min_lon=170, max_lon=-170 means 170°E eastward to 170°W.
        """
        return _json(store.in_bbox(min_lon, min_lat, max_lon, max_lat, type) or {"result": "nothing in graph inside bbox"})

    @tool
    def what_happened_at(lat: float, lon: float, radius_km: float = 50) -> str:
        """Answer "what happened at this place/coordinate?" in one call.

        Returns the places within `radius_km` of (lat, lon), the events that took
        place there (with dates and excerpts), and every relation involving those
        places and events, sorted chronologically. Increase radius_km if empty.
        """
        return _json(store.what_happened_at(lat, lon, radius_km))

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
    ]
