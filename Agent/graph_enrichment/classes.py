"""Class table on Wikidata P31 / P279 (spec R97): what kind of thing an item is.

An item's class is read from its P31 (instance of) values, each followed up P279 (subclass of)
for at most MAX_DEPTH levels, against the root sets below; the first class in PRECEDENCE that
matches wins. An item that has P279 itself is a class (a ship type, a rank, a unit size), never
an individual, and gets no class. The roots were chosen from the P31 values of the corpus's
linked entities and checked against their Wikidata labels; broad roots are avoided on purpose:
"geographical feature" (Q618123) has "facility" under it, which would make hotels and
universities places, and "organization" would make newspapers units.
"""

from __future__ import annotations

from typing import Any, Callable

from graph_enrichment.enrich import MILITARY_EVENT_CLASSES, SHIP_CLASSES

MAX_DEPTH = 3  # P279 steps above a P31 value: "Fubuki-class destroyer" -> destroyer -> warship -> ship

ROOTS: dict[str, set[str]] = {
    "person": {"Q5"},  # human
    "ship": SHIP_CLASSES | {
        "Q11446",    # ship
        "Q3114762",  # warship
        "Q1229765",  # watercraft (boats such as PT-109)
    },
    "event": set(MILITARY_EVENT_CLASSES) | {
        "Q1190554",  # occurrence
        "Q350604",   # armed conflict
        "Q13418847", # historical event
        "Q1656682",  # planned event
    },
    "unit": {
        "Q176799",   # military unit
        "Q781132",   # military branch
        "Q15627509", # military organization
    },
    "place": {
        "Q486972",   # human settlement
        "Q56061",    # administrative territorial entity
        "Q271669",   # landform (islands, capes, peninsulas, mountains)
        "Q15324",    # body of water (seas, bays, straits, rivers, lagoons)
        "Q23442",    # island
        "Q33837",    # archipelago
        "Q245016",   # military base
        "Q62447",    # aerodrome (airfields, airbases, airports)
        "Q44782",    # port
        "Q1785071",  # fort
        "Q6256",     # country
        "Q82794",    # region
        "Q9430",     # ocean
    },
}
# Classes that make an item an institution, a company or a work, whatever else it is: never a place,
# unit or ship of the war (the Army Center of Military History is a "military unit" and a "book
# publisher"; the Australian War Memorial a "memorial" and a "museum"). Shipyards are left out of the
# place roots for the same reason: "shipyard" is a subclass of "organization".
EXCLUDE_ROOTS = {
    "Q4830453",  # business
    "Q783794",   # company
    "Q2085381",  # publishing house
    "Q1320047",  # book publisher
    "Q33506",    # museum
    "Q5003624",  # memorial
    "Q39614",    # cemetery
    "Q2385804",  # educational institution
    "Q917182",   # military academy
    "Q327333",   # government agency
    "Q31855",    # research institute
    "Q166118",   # archive
    "Q7075",     # library
}
# Roots that match only as a direct P31 value: "region" has memorials and museums a few P279 steps below.
ROOT_MAX_DEPTH = {"Q82794": 0}
PRECEDENCE = ("person", "ship", "event", "unit", "place")
# Places small enough to check an event's own point against (an atoll, a town, an airfield); seas,
# oceans, regions, provinces and archipelagos span hundreds of kilometres and say nothing about it.
POINT_LIKE_ROOTS = {"Q486972", "Q23442", "Q245016", "Q62447", "Q44782", "Q1785071"}
EXTENDED_ROOTS = {"Q15324", "Q9430", "Q82794", "Q33837", "Q6256", "Q5107", "Q10864048"}  # 10864048: first-level admin division


def is_point_like(claims: dict[str, Any], parents: Callable[[str], list[str]]) -> bool:
    up = ancestors(item_ids(claims, "P31"), parents)
    # "region" only as a direct class, as in the class table: "island" is a region two P279 steps up
    return any(r in up for r in POINT_LIKE_ROOTS) and not any(up.get(r, 99) <= ROOT_MAX_DEPTH.get(r, MAX_DEPTH) for r in EXTENDED_ROOTS)


NODE_TYPE = {"person": "person", "ship": "other", "event": "event", "unit": "org", "place": "location", "other": "other"}
# Things that are neither actors nor places (step 6 types them `other`): aircraft, weapons and vehicles,
# also when the item is itself a class (the Type 93 torpedo is a subclass of torpedo).
OTHER_ROOTS = {
    "Q11436",     # aircraft
    "Q15056995",  # aircraft model
    "Q15056993",  # aircraft family
    "Q728",       # weapon
    "Q124078422", # weapon type
    "Q42889",     # vehicle
}


def other_class(claims: dict[str, Any], parents: Callable[[str], list[str]]) -> bool:
    """True when the item's P31 or P279 values reach an OTHER_ROOTS class within MAX_DEPTH steps."""
    up = ancestors(item_ids(claims, "P31") + item_ids(claims, "P279"), parents)
    return any(r in up for r in OTHER_ROOTS)


def item_ids(claims: dict[str, Any], prop: str) -> list[str]:
    out = []
    for c in claims.get(prop) or []:
        snak = c.get("mainsnak") or {}
        if c.get("rank") == "deprecated" or snak.get("snaktype") != "value":
            continue
        v = (snak.get("datavalue") or {}).get("value")
        if isinstance(v, dict) and isinstance(v.get("id"), str):
            out.append(v["id"])
    return out


def ancestors(classes: list[str], parents: Callable[[str], list[str]], depth: int = MAX_DEPTH) -> dict[str, int]:
    """{class: steps above the P31 value} for the P31 values and their P279 ancestors up to `depth`."""
    seen = {c: 0 for c in classes}
    frontier = list(classes)
    for level in range(1, depth + 1):
        nxt = []
        for c in frontier:
            for p in parents(c):
                if p not in seen:
                    seen[p] = level
                    nxt.append(p)
        frontier = nxt
    return seen


def classify(claims: dict[str, Any], parents: Callable[[str], list[str]]) -> tuple[str | None, str | None]:
    """(class, the root that decided it) of an item from its claims; (None, reason) when it has none."""
    if item_ids(claims, "P279"):
        return None, "is a class (has P279)"
    p31 = item_ids(claims, "P31")
    if not p31:
        return None, "no P31"
    up = ancestors(p31, parents)
    excluded = sorted(r for r in EXCLUDE_ROOTS if r in up)
    if excluded:
        return None, f"an institution or company ({excluded[0]})"
    # an administrative territory or a country is a place even when it is also an occupation (Saharat Thai
    # Doem: "administrative divisions of Thailand", two P279 steps below administrative territorial entity)
    if "Q5" not in up and (terr := next((r for r in ("Q56061", "Q6256") if r in up), None)):
        return "place", terr
    # the nearest root wins, PRECEDENCE breaks ties: the China Expeditionary Army is a military unit two
    # P279 steps up and an "occurrence" three steps up (expeditionary force -> expedition)
    best = None
    for rank, cls in enumerate(PRECEDENCE):
        hits = sorted((d, r) for r in ROOTS[cls] if (d := up.get(r)) is not None and d <= ROOT_MAX_DEPTH.get(r, MAX_DEPTH))
        if hits and (best is None or (hits[0][0], rank) < best[0]):
            best = ((hits[0][0], rank), cls, hits[0][1])
    return (best[1], best[2]) if best else (None, "class not in the table")


def classes_to_fetch(p31_values: set[str], parents_known: dict[str, list[str]]) -> set[str]:
    """Class items whose P279 is still needed to walk MAX_DEPTH levels up from `p31_values`."""
    need, frontier = set(), set(p31_values)
    for _ in range(MAX_DEPTH):
        missing = {c for c in frontier if c not in parents_known}
        need |= missing
        frontier = {p for c in frontier - missing for p in parents_known[c]}
    return need
