"""Read-only date adapter for the approved model-backed graph contract.

Scalar graph values remain native Wikidata strings. Precision and calendar
stay in the model and are used only for comparisons. Population S2T section 8
defines possible overlap, coarse precision and the limited Julian shortcut.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from GG_Data.pipeline.poc import read_json
from GG_Data.pipeline.relation_selection import year_span
from GG_Data.pipeline.relations import date_parts

GREGORIAN = "http://www.wikidata.org/entity/Q1985727"
JULIAN = "http://www.wikidata.org/entity/Q1985786"
FIELDS = {"start_date": "from", "end_date": "to", "point_date": "point"}
PROPERTIES = {"from": "P580", "to": "P582", "point": "P585"}


def _metadata(value: str, precision: list[int], originals: list[dict]) -> dict:
    selected = [v for v in originals if v.get("time") == value]
    evidence = selected or originals
    calendars = {v.get("calendarmodel") for v in evidence if v.get("calendarmodel")}
    if len(precision) != 1 or len(calendars) > 1:
        raise ValueError(f"Ambiguous model date precision/calendar: {value}")
    return dict(
        time=value, precision=precision[0], calendarmodel=next(iter(calendars), None)
    )


def relation_dates(row: dict) -> dict:
    result = {}
    precision = row.get("time_precision", {})
    if isinstance(precision, str):
        precision = json.loads(precision)
    attrs = row.get("attributes", {})
    decisions = attrs.get("time_selection", {}).get("decisions", {})
    qualifiers = attrs.get("qualifiers", {})
    for field, key in FIELDS.items():
        value = row.get("time_" + key)
        if not value:
            continue
        decision = decisions.get(key, {})
        prop = decision.get("source_property", PROPERTIES[key])
        originals = [
            s.get("datavalue", {}).get("value", {}) for s in qualifiers.get(prop, [])
        ]
        result[field] = _metadata(
            value, decision.get("selected_precision", precision.get(key, [])), originals
        )
    return result


def event_dates(row: dict) -> dict:
    # Reuse the exporter's approved earliest-start/latest-end selection rather
    # than selecting or rewriting dates independently inside the Agent.
    from GG_Data.pipeline.graph_export import event_date_attributes

    selected = event_date_attributes(row)
    originals = [v for field in FIELDS for v in row.get(field, [])]
    result = {}
    for field, value in selected.items():
        matching = [v for v in originals if v.get("time") == value]
        precisions = sorted({v["precision"] for v in matching})
        result[field] = _metadata(value, precisions, matching)
    return result


def validate_cooccurrence_support(root: Path, edges: list) -> None:
    """Proposed adapter: verify page evidence; infer no native assertion dates."""
    weighted = [e for e in edges if e.type == "CO_MENTIONED" and e.strength is not None]
    if not weighted:
        return
    rows = read_json(root / "cooccurrence_support.json.gz")
    by_id = {r["id"]: r for r in rows}
    pairs = {(r["source"], r["target"]): r for r in rows}
    if len(by_id) != len(rows) or len(pairs) != len(rows):
        raise ValueError("Duplicate co-occurrence support")
    for edge in weighted:
        row = by_id.get(edge.id)
        if row is None or row["kind"] != "CO_MENTIONED" or (row["source"], row["target"]) != (edge.source, edge.target):
            raise ValueError("Missing or mismatched co-occurrence support")
        n, source, target = (row[k] for k in ("shared_page_count", "source_page_count", "target_page_count"))
        if any(type(v) is not int for v in (n, source, target)) or not 0 < n <= min(source, target):
            raise ValueError("Invalid co-occurrence page counts")
        reverse = pairs.get((edge.target, edge.source))
        if reverse is None or (reverse["shared_page_count"], reverse["source_page_count"], reverse["target_page_count"]) != (n, target, source):
            raise ValueError("Missing or mismatched reverse co-occurrence support")
        if edge.count != n or not math.isclose(edge.strength, n / source, rel_tol=1e-12) or not math.isclose(row["directional_strength"], n / source, rel_tol=1e-12):
            raise ValueError("Co-occurrence strength/count differs from evidence")
        if (edge.source_edge_id is not None and edge.source_edge_id != reverse["id"]) or any(getattr(edge, field) is not None for field in FIELDS):
            raise ValueError("Co-occurrence supplies no native dates or unrelated assertion identity")


def load_model_dates(root: Path, nodes: dict, edges: list) -> tuple[dict, dict]:
    """Join existing model IDs; reject a graph/model scalar disagreement."""
    relation_path, event_path = root / "relations.json.gz", root / "events.json.gz"
    edge_dates, node_dates = {}, {}
    validate_cooccurrence_support(root, edges)
    if relation_path.exists():
        rows = read_json(relation_path)
        by_id = {r["observation_id"]: r for r in rows}
        if len(by_id) != len(rows):
            raise ValueError("Duplicate Relations assertion IDs")
        for edge in edges:
            if edge.type == "CO_MENTIONED" and edge.strength is not None:
                continue  # Validated pair support supplies no native date.
            assertion_id = edge.source_edge_id or edge.id
            if assertion_id not in by_id:
                raise ValueError(f"Missing model assertion: {assertion_id}")
            metadata = relation_dates(by_id[assertion_id])
            for field in FIELDS:
                if (getattr(edge, field, None) or None) != (
                    metadata.get(field, {}).get("time") or None
                ):
                    raise ValueError(
                        f"Graph/model relation date differs: {edge.id}, {field}"
                    )
            edge_dates[id(edge)] = metadata
    if event_path.exists():
        rows = read_json(event_path)
        by_id = {r["event_id"]: r for r in rows}
        if len(by_id) != len(rows):
            raise ValueError("Duplicate Events IDs")
        for node in nodes.values():
            if node.type != "event":
                continue
            if node.wikidata_id not in by_id:
                raise ValueError(f"Missing model Event: {node.id}")
            metadata = event_dates(by_id[node.wikidata_id])
            for field in FIELDS:
                if (node.attributes.get(field) or None) != (
                    metadata.get(field, {}).get("time") or None
                ):
                    raise ValueError(
                        f"Graph/model Event date differs: {node.id}, {field}"
                    )
            node_dates[node.id] = metadata
    return edge_dates, node_dates


def _bounds(
    value: str, precision: int
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    year, month, day = date_parts(value)[:3]
    low_year, high_year = year_span(year, precision)
    count = max(1, min(3, precision - 8))
    if not month:
        count = min(count, 1)
    if not day:
        count = min(count, 2)
    return (
        (low_year, month if count >= 2 else 1, day if count >= 3 else 1),
        (high_year, month if count >= 2 else 12, day if count >= 3 else 31),
    )


def _query_bounds(value: str | None, *, end: bool) -> tuple[int, int, int]:
    if value is None:
        return (10**12, 12, 31) if end else (-(10**12), 1, 1)
    match = re.fullmatch(r"([+-]?\d+)(?:-(\d{2}))?(?:-(\d{2}))?", value)
    if not match:
        raise ValueError(f"Unsupported query date: {value}")
    year, month, day = match.groups()
    return (
        int(year),
        int(month) if month else (12 if end else 1),
        int(day) if day else (31 if end else 1),
    )


def in_range(metadata: dict, date_from: str | None, date_to: str | None) -> bool:
    """Possible overlap; unknown calendar-sensitive conversions fail visibly."""
    if not date_from and not date_to:
        return True
    if not metadata:
        return False
    q_low = _query_bounds(date_from, end=False)
    q_high = _query_bounds(date_to, end=True)
    bounds = {}
    for field, value in metadata.items():
        lower, upper = _bounds(value["time"], value["precision"])
        calendar = value.get("calendarmodel")
        if calendar not in (None, GREGORIAN):
            approved_year_comparison = (
                calendar == JULIAN
                and 1941 <= q_low[0] <= q_high[0] <= 1945
                and (upper[0] < 1941 or lower[0] > 1945)
            )
            if not approved_year_comparison:
                raise ValueError(
                    f"Calendar-sensitive date query requires conversion approval: {value['time']}"
                )
        bounds[field] = (lower, upper)
    if "point_date" in bounds and len(bounds) == 1:
        lower, upper = bounds["point_date"]
    else:
        lower = bounds.get("start_date", ((-(10**12), 1, 1), None))[0]
        upper = bounds.get("end_date", (None, (10**12, 12, 31)))[1]
    return lower <= q_high and upper >= q_low
