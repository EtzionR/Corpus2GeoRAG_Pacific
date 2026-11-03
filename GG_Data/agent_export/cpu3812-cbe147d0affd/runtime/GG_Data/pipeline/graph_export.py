"""Exact existing consumer helper bodies; see accompanying Apache LICENSE."""
from .relations import date_parts

def event_date_attributes(event: dict) -> dict[str, str]:
    """S2T 4: native earliest start/latest end, without the relation 1900 floor.

    The model retains every original value and its precision/calendar. Existing
    P585 coverage ranges already supply bounds; do not mint a point. Explicit
    bounds with several point dates keep those bounds and omit the scalar point
    (owner approval, 23 October 2026); no new coverage range is derived here.
    This helper is shared with the Agent's model-backed date validation.
    """
    result = {}
    for field, choose in (("start_date", min), ("end_date", max), ("point_date", min)):
        values = event.get(field) or []
        if not isinstance(values, list) or any(
            not isinstance(v, dict) or not v.get("time") for v in values
        ):
            raise ValueError(f"Unsupported Event date: {event['event_id']} {field}")
        if not values:
            continue
        if len({v.get("calendarmodel") for v in values}) > 1:
            raise ValueError(f"Mixed Event calendars: {event['event_id']} {field}")
        times = {v["time"] for v in values}
        for value in times:
            date_parts(value)
        if field == "point_date" and len(times) > 1:
            if not (
                event.get("temporal_evidence", {}).get("derived_range")
                or event.get("start_date")
                or event.get("end_date")
            ):
                raise ValueError(f"Unbounded multiple points: {event['event_id']}")
            continue
        result[field] = choose(times, key=date_parts)
    return result
