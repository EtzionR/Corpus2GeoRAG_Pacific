"""Infobox fields as data (spec R97, self-fix step 5). Pure functions over wikitext strings; no network.

Wikitext is untrusted: it is read with regular expressions only. What each field gives:
- `combatant1/2/3` (and `combatant1a`...): the sides. Linked names, and the country a flag template
  names (`{{flag|X}}`, `{{flagcountry|X}}`, `{{flagicon|X}}`, `{{flagdeco|X}}`: the first argument is
  the country). A bare flag image (`[[File:Flag of X.svg]]`) names nothing. Everything after a
  "Supported by" / "support from" header is left out.
- `commander1/2/3`: linked names only (a flag there marks a commander's nationality, not a side).
- `units1/2/3`: linked names; "support from" sections left out like the combatants'.
- `place`: per line, the first linked name, with "off" / "near" kept as a qualifier.
- `battles` (people and units): linked names, and plain-text items with the years they state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from graph_enrichment import wikipedia as wp

CONFLICT_INFOBOXES = ("Infobox military conflict", "Infobox military operation", "Infobox Military Conflict")
ACTOR_INFOBOXES = ("Infobox military person", "Infobox military unit", "Infobox officeholder", "Infobox person")
SIDE_FIELD_RE = re.compile(r"^(combatant|commander|units)([123])([a-z]?)$")
REF_RE = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->", re.S | re.I)
SUPPORT_RE = re.compile(r"(?i)support(?:ed)?\s+(?:by|from)\b|\bin\s+support\b")
FLAG_RE = re.compile(r"\{\{\s*(?:flag|flagcountry|flagicon|flagdeco|flagu|flag\s+icon|flagcountry2)\s*\|\s*([^|{}]+?)\s*(?:\||\}\})", re.I)
LINE_SPLIT_RE = re.compile(r"<br\s*/?>|\n|;", re.I)
QUALIFIER_RE = re.compile(r"(?i)\b(off|near)\b")
YEAR_RE = re.compile(r"\b(19[0-9]{2})\b")


@dataclass
class Mention:
    title: str          # the title the mention names (a link target, or a flag template's country)
    text: str           # what the reader sees
    how: str            # "link" or "flag"
    start: int          # offset in the page's wikitext
    qualifier: str | None = None  # "off" / "near" (place field)


def strip_refs(value: str) -> str:
    """The value with citations and comments blanked out to spaces (offsets stay valid)."""
    return REF_RE.sub(lambda m: " " * len(m.group(0)), value)


def side_fields(fields: dict[str, tuple[str, int]], base: str) -> dict[int, list[tuple[str, int]]]:
    """{side: [(value, offset)]} of `base` fields (`combatant1`, `combatant1a`, ... -> side 1)."""
    out: dict[int, list[tuple[str, int]]] = {}
    for key, (value, offset) in fields.items():
        m = SIDE_FIELD_RE.match(key)
        if m and m.group(1) == base:
            out.setdefault(int(m.group(2)), []).append((value, offset))
    return out


def _cut_support(value: str) -> str:
    m = SUPPORT_RE.search(value)
    return value[:m.start()] if m else value


def mentions(value: str, offset: int, flags: bool, cut_support: bool = False) -> list[Mention]:
    """Linked names (and with `flags`, flag-template countries) of a field value, in order, offsets in the page."""
    v = strip_refs(value)
    if cut_support:
        v = _cut_support(v)
    out = [Mention(l.title, l.text, "link", offset + l.start) for l in wp.parse_wikilinks(v)]
    if flags:
        for m in FLAG_RE.finditer(v):
            name = m.group(1).strip()
            title = wp.normalize_title(name)
            if title and "=" not in name:
                out.append(Mention(title, name, "flag", offset + m.start()))
    return sorted(out, key=lambda x: x.start)


def place_mentions(value: str, offset: int) -> list[Mention]:
    """Per line of the `place` field, its first linked name, with an "off" / "near" qualifier written before it."""
    v = strip_refs(value)
    out, pos = [], 0
    for part in LINE_SPLIT_RE.split(v):
        start = v.find(part, pos)
        pos = start + len(part)
        links = wp.parse_wikilinks(part)
        if not links:
            continue
        before = part[:links[0].start]
        q = QUALIFIER_RE.search(before)
        # a link that ends a longer name or a direction ("Territory of [[New Guinea]]", "East of [[Samar]]",
        # "north of [[Santa Isabel Island]]") is not the place itself; an adjective before it is fine
        # ("Australian [[Territory of Papua|Papua]]")
        rest = re.sub(r"\{\{[^{}]*\}\}|'{2,}|[*#:;,.()\-–]", " ", before)
        if re.search(r"\b(?:of|the)\s*$", rest, flags=re.I):
            continue
        out.append(Mention(links[0].title, links[0].text, "link", offset + start + links[0].start,
                           q.group(1).lower() if q else None))
    return out


def battle_items(value: str) -> tuple[list[str], list[tuple[str, tuple[int, int] | None]]]:
    """(linked titles, plain-text items with the years they state) of a `battles` field. Templates give
    their positional arguments as text ("{{tree list}}" gives nothing, named arguments are dropped)."""
    v = strip_refs(value)
    titles = [l.title for l in wp.parse_wikilinks(v)]
    v = re.sub(r"\[\[[^\[\]]*\]\]", "\n", v)
    while True:  # innermost template first: keep its positional arguments as lines
        new = re.sub(r"\{\{([^{}]*)\}\}", lambda m: "\n".join(a for a in m.group(1).split("|")[1:] if "=" not in a), v)
        if new == v:
            break
        v = new
    v = re.sub(r"<[^>]+>", "\n", v).replace("&nbsp;", " ")
    items = []
    for raw in re.split(r"\n|;|\*|:|•|,(?![^()]*\))", v):
        years = [int(y) for y in YEAR_RE.findall(raw)]
        text = re.sub(r"\([^()]*\)", "", raw)
        text = re.sub(r"\s+", " ", text).strip(" -–—'\"")
        if len(text) >= 4 and not text.isdigit():
            items.append((text, (min(years), max(years)) if years else None))
    return titles, items
