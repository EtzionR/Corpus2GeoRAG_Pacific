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
  Date filters match on interval overlap; an edge with only a `point_date` is the
  interval [point, point]; edges without any date are excluded whenever a date
  filter is given.
- Name lookup compares normalized forms (Unicode NFKD, combining marks removed,
  casefold, apostrophes deleted, hyphens as spaces, whitespace collapsed), so
  "TOKYO", "tokyo" and "Tōkyō" are the same name. Each result says how it
  matched (`name`, `alias`, `fuzzy`) and whether the match is `clear` or
  `uncertain` (spec R3, R4), so the agent can ask instead of guessing.
- Text retrieval uses BM25 over node paragraphs. Node/edge `embedding` fields
  are loaded but not used yet (see README, "Next steps").
- Node ids are opaque strings: Wikidata QIDs ("Q362") and the sample's
  "<type>:<slug>" ids both work; nothing parses an id.
- Relations have two levels. `Edge.type` is the exact Level 2 id, case-preserved
  ("wd:P607", "ATTACKED"); `Edge.relation_family` is the Level 1 family
  ("CONFLICT_AFFILIATION"), read from the edge, its attributes, or the top-level
  `relation_catalog`. A relation filter matches either level (see `relation_filter`).
"""

from __future__ import annotations

import gc
import json
import math
import re
import sys
import unicodedata
import uuid
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
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
ALIAS_PART_SCORE = 97  # a person's nickname + surname, in order, inside one alias (spec R53)
ARTICLE_SCORE = 94  # exact except for a leading article: below a literal exact match by more than AMBIGUITY_MARGIN,
                    # so "Battle of Midway" stays clear of the film "The Battle of Midway"
AMBIGUITY_MARGIN = 5  # another node scoring within this of a result makes it a near-tie
MATCH_CUTOFF = 60  # results below this are not returned


# ---------------------------------------------------------------- data model


# Providers whose record references the draft exporter wrote into `page` as "<Provider>:<record>".
RECORD_PROVIDERS = ("Wikidata", "EventKG", "WarSampo")
QID_RE = re.compile(r"Q\d+")


def source_ref(provider: str | None = None, record_id: str | None = None, url: str | None = None,
               page: str | None = None, **_: Any) -> dict[str, Any] | None:
    """Answer-output provenance {provider, record_id, url, page}; None if nothing identifies it.

    Goes through Source, so a record id written as a page is moved to record_id;
    the url is derived when missing (Wikidata item, or the English Wikipedia page).
    """
    try:
        s = Source.model_validate({"provider": provider, "record_id": record_id, "url": url, "page": page})
    except ValueError:
        return None
    link = s.url or s.record_url()
    if link is None and s.page and s.provider == "Wikipedia":
        link = "https://en.wikipedia.org/wiki/" + quote(s.page.replace(" ", "_"))
    return {"provider": s.provider, "record_id": s.record_id, "url": link, "page": s.page}


def _record_id(provider: str, rest: str) -> str:
    """Record id from a "<Provider>:<rest>" reference; a Wikidata rest is JSON with a statement id."""
    if provider == "Wikidata" and rest.startswith("{"):
        try:
            return json.loads(rest).get("statement_id") or rest
        except ValueError:
            return rest
    return rest


_RECORD_PREFIXES = tuple(f"{p}:" for p in RECORD_PROVIDERS)


def _is_record_page(page: Any) -> bool:
    return page.__class__ is str and page.startswith(_RECORD_PREFIXES)


class Source(BaseModel):
    """Where a node or edge comes from: `{provider, record_id, url, page, paragraph}`.

    All fields are optional, but one of page / record_id / url must identify the
    source. `page` (and `paragraph`) are kept only for a real Wikipedia page title:
    a record reference written into `page` ("Wikidata:{...statement_id...}",
    "EventKG:file:line", or a bare QID with a wikidata.org url) is moved to
    `provider` + `record_id` and the original string kept in `raw_page`, so a
    record id is never shown as a page. The old `{page, paragraph, url}` form is
    still valid; its provider is inferred ("Wikipedia" for a Wikipedia page).
    """

    model_config = ConfigDict(extra="allow")
    provider: str | None = None
    record_id: str | None = None
    url: str | None = None
    page: str | None = None
    paragraph: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        # Runs once per source (~850k on the draft graph): kept to plain dict operations.
        if data.__class__ is not dict:
            return data
        page, url, provider = data.get("page"), data.get("url"), data.get("provider")
        if page.__class__ is str:
            if page.startswith(_RECORD_PREFIXES):
                prefix, _, rest = page.partition(":")
                data = {**data, "page": None, "raw_page": page, "provider": provider or prefix,
                        "record_id": data.get("record_id") or _record_id(prefix, rest)}
            elif page[:1] == "Q" and QID_RE.fullmatch(page) and (url is None or "wikidata.org" in url):
                data = {**data, "page": None, "provider": provider or "Wikidata", "record_id": data.get("record_id") or page}
            elif not provider and (url is None or "wikipedia.org" in url):
                data = {**data, "provider": "Wikipedia"}
        elif not provider and url.__class__ is str:
            host = "Wikipedia" if "wikipedia.org" in url else "Wikidata" if "wikidata.org" in url else None
            data = {**data, "provider": host}
        provider = data.get("provider")
        if provider.__class__ is str and provider is not (interned := sys.intern(provider)):
            data = {**data, "provider": interned}
        return data

    @model_validator(mode="after")
    def _identified(self) -> "Source":
        if not (self.page or self.record_id or self.url):
            raise ValueError("a source needs a page, record_id or url")
        return self

    def view(self) -> dict[str, Any]:
        """Output form: provider, record_id, url, page, paragraph (only those set)."""
        out = {"provider": self.provider, "record_id": self.record_id, "url": self.url or self.record_url(),
               "page": self.page, "paragraph": self.paragraph}
        return {k: v for k, v in out.items() if v is not None}

    def record_url(self) -> str | None:
        """Wikidata item URL for a Wikidata record id ("Q362$abc" -> .../wiki/Q362); else None."""
        if self.provider == "Wikidata" and self.record_id:
            qid = self.record_id.split("$")[0]
            if QID_RE.fullmatch(qid):
                return f"https://www.wikidata.org/wiki/{qid}"
        return None


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


# Edge attribute values repeated on every assertion of the draft graph; interning them
# keeps one string object per distinct value instead of one per edge.
_INTERNED_ATTRS = ("relation_label", "relation_family", "selected_source", "source_predicate", "review_status",
                   "relation_map_version")


class Edge(BaseModel):
    """Directed, typed relationship source -> target (e.g. org ATTACKED location).

    One edge is one source assertion. `type` is the exact Level 2 relation id
    (case-preserved), `relation_family` its Level 1 family. `relation_family` and
    `point_date` may also come in `attributes` (the draft exporter puts them there);
    they are lifted to the fields and stay in `attributes` unchanged.
    """

    model_config = ConfigDict(extra="allow")
    id: str | None = None  # unique; GraphStore assigns "e<n>" when missing
    source: str
    target: str
    type: str
    relation_family: str | None = None
    count: int = 1  # supporting mentions (sample) or 1 per source assertion (draft)
    start_date: str | None = None
    end_date: str | None = None
    point_date: str | None = None  # a single date; filters treat it as start == end
    sources: list[Source] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_attributes(cls, data: Any) -> Any:
        # Runs once per edge (700k on the draft graph): kept to plain dict operations.
        if data.__class__ is not dict:
            return data
        intern = sys.intern
        attrs = data.get("attributes")
        data = data.copy()
        if (t := data.get("type")).__class__ is str:
            data["type"] = intern(t)
        if attrs.__class__ is dict and attrs:
            a = attrs.copy()
            for k in _INTERNED_ATTRS:
                if (v := a.get(k)).__class__ is str:
                    a[k] = intern(v)
            data["attributes"] = a
            if not data.get("relation_family") and (fam := a.get("relation_family")):
                data["relation_family"] = fam
            if not data.get("point_date") and (point := a.get("point_date")):
                data["point_date"] = point
            # Draft exporter: provider and record id are edge attributes; the source's `page`
            # holds the raw record reference. Hand them to Source (saves parsing that page).
            provider, record = a.get("selected_source"), a.get("source_record_id")
            sources = data.get("sources")
            if (provider or record) and sources.__class__ is list:
                data["sources"] = [
                    {**src, "provider": src.get("provider") or provider, "record_id": record}
                    if src.__class__ is dict and not src.get("record_id") and _is_record_page(src.get("page")) else src
                    for src in sources]
        if (fam := data.get("relation_family")).__class__ is str:
            data["relation_family"] = intern(fam)
        return data


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


def edge_dates(e: Edge) -> tuple[str | None, str | None]:
    """(start, end) of an edge; a point date counts as start == end."""
    if e.start_date or e.end_date:
        return e.start_date, e.end_date
    return e.point_date, e.point_date


def dates_overlap(start: str | None, end: str | None, date_from: str | None, date_to: str | None) -> bool:
    """Interval overlap on partial ISO dates; undated never matches a set filter, and no filter matches all."""
    if not date_from and not date_to:
        return True
    lo = _date_bounds(start or end, end=False)
    hi = _date_bounds(end or start, end=True)
    if lo is None:
        return False
    return lo <= (_date_bounds(date_to, end=True) or "9999") and hi >= (_date_bounds(date_from, end=False) or "0000")


def normalize_name(text: str) -> str:
    """Comparison form of a name: no diacritics, casefolded, punctuation-light."""
    if text.isascii():  # fast path, same result: NFKD and combining marks change nothing in ASCII
        stripped = text.lower()
        if "'" in stripped or "`" in stripped:
            stripped = stripped.replace("'", "").replace("`", "")
        return " ".join(stripped.replace("-", " ").split())
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    stripped = re.sub(r"['\u2018\u2019`]", "", stripped).replace("-", " ")
    return " ".join(stripped.split())


def _in_order(needle: list[str], hay: list[str]) -> bool:
    """True if every word of `needle` appears in `hay` in the same order (gaps allowed)."""
    it = iter(hay)
    return all(w in it for w in needle)


def strip_article(norm: str) -> str:
    """A normalized label without a leading article: "the big e" -> "big e" (spec R53)."""
    for art in ("the ", "a ", "an "):
        if norm.startswith(art) and len(norm) > len(art):
            return norm[len(art):]
    return norm


# Nickname labels the graph itself holds (spec R53): the quoted part of an alias, and phrases in node text.
QUOTED_RE = re.compile(r'["\u201c\u201d]([^"\u201c\u201d]{2,40})["\u201c\u201d]')
TEXT_NICKNAME_RE = re.compile(
    r"\b(?:nicknamed|also known as|known as|dubbed|called the|codenamed|codename was|codename)\s+(?:the\s+)?"
    r"(?:[\"\u201c\u2018']([^\"\u201d\u2019']{2,40})[\"\u201d\u2019']"   # quoted
    r"|((?:[A-Z][\w\-.]*)(?:\s+[A-Z][\w\-.]*){0,4}))")          # or up to five capitalized words
NICKNAME_KINDS = {"nickname", "codename"}  # alias_details kinds matched like nicknames (exact, no partial credit)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def load_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize the top-level `relation_catalog` into {Level 2 id: entry}.

    Accepts the draft exporter's shape ({id, labels, level1_families, review_statuses,
    source_predicates: [{source, predicate, direction}]}) and a flat one ({id, label,
    family, direction, status}), as a list or as a dict keyed by id.
    """
    items = raw.values() if isinstance(raw, dict) else raw or []
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        labels = item.get("labels") or [item.get("label")]
        families = item.get("level1_families") or [item.get("family") or item.get("relation_family") or item.get("level1_family")]
        statuses = item.get("review_statuses") or [item.get("review_status") or item.get("status")]
        preds = [p for p in item.get("source_predicates") or [] if isinstance(p, dict)]
        directions = sorted({p.get("direction") for p in preds if p.get("direction")} or {item.get("direction")} - {None})
        out[item["id"]] = {
            "id": item["id"],
            "label": next((l for l in labels if l), item["id"]),
            "family": next((f for f in families if f), None),
            "direction": directions,
            "status": [s for s in statuses if s],
            "source_predicates": [f"{p.get('source')}: {p.get('predicate')}" for p in preds],
        }
    return out


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

# The same roles on the two-level vocabulary of the WWII draft (relation-map-v1.0.0).
#
# Level 1 families are NOT traversed wholesale: in the catalog and the data every
# family mixes relations a profile must follow with relations it must not, e.g.
#   SPATIAL               location (P276) but also place of birth, country of registry, sport, characters
#   PARTICIPATION_ROLE    commander, participant, but also occupation, rank, cast member, member of
#   PART_WHOLE_HIERARCHY  part of / has part(s), but also family name, see also, WikiProject
#   ORGANIZATION_HIERARCHY child/parent unit, but also patron, parent (of a person), structural engineer
#   CONFLICT_AFFILIATION  participated in conflict, but also military branch (a membership hub)
# so each role lists exact Level 2 ids, with the direction in which the edge states the
# role: FWD = edge source is the role's subject (person -participated in-> battle),
# INV = the edge points the other way (battle -commander-> person means person commanded battle).
# Families never followed at all (hubs or no geographic meaning): AFFILIATION_STATUS
# (citizenship, allegiance, party: Yamamoto -> Empire of Japan), AWARD_RECOGNITION (an
# award links thousands of people), EDUCATION_EMPLOYMENT, FAMILY_PERSONAL,
# CREATION_AUTHORSHIP, OWNERSHIP_CONTROL (operator: every ship -> its navy), CAUSAL,
# TEMPORAL_SUCCESSION, FORMATION_DISSOLUTION, and UNRESOLVED (instance of, described by, ...),
# apart from the UNRESOLVED-family relations named in the table (participant in, participated In).
FWD, INV = 1, -1
RELATION_ROLES: dict[str, dict[str, int]] = {
    # actor -> event it took part in or commanded (EVENT_LINKS)
    "event_link": {
        "COMMANDED": FWD, "PARTICIPATED_IN": FWD,
        "wd:P607": FWD,  # participated in conflict (CONFLICT_AFFILIATION); wars are hubs, see HUB_SUB_EVENTS
        "wd:P1344": FWD,  # participant in (catalog: UNRESOLVED family, meaning is participation)
        "http://yago-knowledge.org/resource/participatedIn": FWD,  # EventKG/YAGO participation (UNRESOLVED family)
        "wd:P598": FWD,  # commander of (deprecated in Wikidata, still in the data): person -> unit or event
        "wd:P710": INV,  # participant: event -> participant
        "http://dbpedia.org/ontology/commander": INV,  # event -> its commander (the main command relation in EventKG)
        "wd:P4791": INV,  # commanded by: event/unit -> person
        "wd:P8031": INV,  # perpetrator: event -> perpetrator
    },
    # event -> place where it happened (OCCURRED_AT); never a country-only relation (wd:P17): too coarse
    "occurred_at": {
        "OCCURRED_AT": FWD,
        "wd:P276": FWD,  # location
        "P276+OBJECT_TYPE_CITY": FWD,  # EventKG locationCity, mapped to location restricted to cities
        "wd:P131": FWD,  # located in the administrative territorial entity
        "wd:P706": FWD,  # located in/on physical feature
    },
    # actor -> place it attacked / defended / captured / occupied / landed at / bombed / was based at
    "acted_at": {
        **{r: FWD for r in LOCATION_RELATIONS},
        "wd:P159": FWD,  # headquarters location (BASED_AT)
        "http://dbpedia.org/ontology/garrison": FWD,  # unit -> garrison place
        "wd:P466": INV,  # occupant: place -> occupying org
    },
    # part -> whole: sub-event -> event, unit -> parent unit (PART_OF); timeless
    "part_of": {
        "PART_OF": FWD,
        "wd:P361": FWD,  # part of
        "wd:P527": INV,  # has part(s): whole -> part
        "wd:P749": FWD,  # parent organization or unit
        "wd:P355": INV,  # child organization or unit: parent -> child
        "http://dbpedia.org/ontology/commandStructure": FWD,  # unit -> higher command
        "http://dbpedia.org/ontology/parentOrganisation": FWD,
        "http://dbpedia.org/ontology/childOrganisation": INV,
    },
    # place -> containing place (LOCATED_IN); timeless. Not wd:P17 (country) nor EventKG's
    # containedInPlace (which folds P17 in): in the draft they mostly link ships, units and
    # artefacts to a country, so "places in the United States" would expand to every US ship.
    "located_in": {
        "LOCATED_IN": FWD,
        "wd:P131": FWD,  # located in the administrative territorial entity
        "wd:P706": FWD,  # located in/on physical feature
    },
    # person -> place of their life (the sample allows any direct person -> location edge)
    "person_place": {
        "wd:P19": FWD, "wd:P20": FWD, "wd:P119": FWD,  # birth, death, burial
        "wd:P551": FWD, "wd:P937": FWD, "wd:P2632": FWD,  # residence, work location, place of detention
        "http://dbpedia.org/ontology/birthPlace": FWD, "http://dbpedia.org/ontology/deathPlace": FWD,
    },
}
TIMELESS_ROLES = ("part_of", "located_in")  # undated edges of these roles pass date filters
EVENT_DATED_ROLES = ("event_link", "occurred_at")  # undated edges of these roles take their event's dates
# An event reached from a person or org through participation counts only if it has at
# most this many sub-events: being one of 46k participants of World War II (1710 parts)
# or 426 of the Pacific War (580 parts) does not connect Yamamoto to every battle, nor to
# the countries listed as their location. Campaigns stay below (Guadalcanal 10, New
# Guinea 42) and are followed into their battles. Starting value.
HUB_SUB_EVENTS = 50
OVERVIEW_TOP = 10  # items shown in an overview (spec R57); the result set holds all of them
OVERVIEW_THRESHOLD = 25  # a set larger than this answers a broad question: use overview (spec R57)
ACTOR_TYPES = ("person", "org", "other")  # `other`: unresolved draft nodes, e.g. the IJN
PLACE_TYPES = ("location", "other")


PLACE_ROLES = ("occurred_at", "acted_at", "located_in", "person_place")
ROLE_HAS = {r: {FWD: FWD in d.values(), INV: INV in d.values()} for r, d in RELATION_ROLES.items()}
PLACE_RELATIONS = {t for r in PLACE_ROLES for t in RELATION_ROLES[r]}


def role_dir(role: str, type: str) -> int | None:
    return RELATION_ROLES[role].get(type)
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
    # no bare "imperial": it matched the Second Australian Imperial Force (spec R59)
    "japan": r"\b(japan|japanese)\b",
    "united states": r"\b(united states|u\.s\.|us)\b",
}
# An org's country relations (spec R59): when an org has one, it decides membership before the name fallback.
COUNTRY_RELATIONS = ("wd:P17", "wd:P27")
COUNTRY_QIDS = {"japan": {"Q17", "Q188712"}, "united states": {"Q30"}}  # targets that may carry no label


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

    def __init__(
        self,
        nodes: list[Node],
        edges: list[Edge],
        metadata: dict[str, Any] | None = None,
        relation_catalog: Any = None,
    ):
        self.nodes: dict[str, Node] = {n.id: n for n in nodes}
        self.metadata: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
        self.catalog: dict[str, dict[str, Any]] = load_catalog(relation_catalog)
        # Drop edges pointing at unknown nodes instead of failing the whole load.
        self.edges: list[Edge] = [e for e in edges if e.source in self.nodes and e.target in self.nodes]
        self.dropped_edges = len(edges) - len(self.edges)
        self.input_counts = {"nodes": len(nodes), "edges": len(edges)}  # rows given, before dedup and drops
        # One edge per source assertion (contract 4.6). Assertions with the same subject,
        # object, Level 2 relation and dates are one relation at query time: the first is
        # its representative in the indexes below, `assertions` lists the whole group, and
        # `support()` adds up their counts. self.edges keeps every assertion.
        self.out_edges: dict[str, list[Edge]] = defaultdict(list)
        self.in_edges: dict[str, list[Edge]] = defaultdict(list)
        self.by_type: dict[str, list[Edge]] = defaultdict(list)
        self.by_family: dict[str, list[Edge]] = defaultdict(list)
        self.assertions: dict[str, list[Edge]] = {}  # representative id -> all its assertions (groups of 2+ only)
        self._support: dict[str, int] = {}
        reps: dict[tuple, Edge] = {}
        taken = {e.id for e in self.edges if e.id}
        for i, e in enumerate(self.edges):
            if not e.id:  # every edge gets a unique id (contract); the sample graph has none
                e.id = f"e{i}"
                while e.id in taken:
                    e.id += "_"
                taken.add(e.id)
            if not e.relation_family and e.type in self.catalog:
                e.relation_family = self.catalog[e.type]["family"]
            key = (e.source, e.target, e.type, e.start_date, e.end_date, e.point_date)
            rep = reps.setdefault(key, e)
            if rep is not e:
                self.assertions.setdefault(rep.id, [rep]).append(e)
                self._support[rep.id] = self._support.get(rep.id, rep.count) + e.count
                continue
            self.out_edges[e.source].append(e)
            self.in_edges[e.target].append(e)
            self.by_type[e.type].append(e)
            if e.relation_family:
                self.by_family[e.relation_family].append(e)
        del reps
        self.type_counts = Counter(e.type for e in self.edges)  # assertions per relation type
        self.family_counts = Counter(e.relation_family for e in self.edges if e.relation_family)
        # Types without a family (the sample's short vocabulary) also match case-insensitively.
        self._legacy_types: dict[str, str] = {}
        for t, es in self.by_type.items():
            if not any(e.relation_family for e in es):
                self._legacy_types.setdefault(t.casefold(), t)

        self.geoms: dict[str, BaseGeometry] = {
            n.id: shape(n.geometry) for n in nodes if n.geometry
        }

        # Name index for fuzzy lookup: one entry per name/alias, compared in
        # normalized form; the original label is kept for display.
        self._names: list[str] = []  # normalized labels
        self._labels: list[str] = []  # original labels
        self._name_owner: list[str] = []
        self._label_kind: list[str] = []  # name / alias / nickname / hull_number (spec R53)
        self._label_evidence: dict[int, str] = {}  # where a derived label comes from: the quoted alias or text
        self._norm_name: dict[str, str] = {}  # node id -> normalized name
        self._norm_aliases: dict[str, frozenset[str]] = {}  # node id -> normalized aliases
        no_aliases: frozenset[str] = frozenset()
        type_codes: dict[str, int] = {}
        label_types: list[int] = []
        for n in nodes:
            norm = normalize_name(n.name)
            aliases = [normalize_name(a) for a in n.aliases]
            self._norm_name[n.id] = norm
            self._norm_aliases[n.id] = frozenset(aliases) if aliases else no_aliases
            code = type_codes.setdefault(n.type, len(type_codes))

            def add_label(label: str, normalized: str, kind: str, evidence: str | None = None) -> None:
                if not normalized:
                    return
                if evidence:
                    self._label_evidence[len(self._names)] = evidence
                self._names.append(normalized)
                self._labels.append(label)
                self._name_owner.append(n.id)
                self._label_kind.append(kind)
                label_types.append(code)

            for label, normalized, kind in zip([n.name, *n.aliases], [norm, *aliases], ["name"] + ["alias"] * len(aliases)):
                add_label(label, normalized, kind)
            for a in n.aliases:  # 'Holland McTyeire "Howlin' Mad" Smith' -> "Howlin' Mad"
                for part in QUOTED_RE.findall(a):
                    add_label(part, normalize_name(part), "nickname", f"alias: {a}")
            for d in (n.model_extra or {}).get("alias_details") or []:  # spec R51: typed aliases
                if isinstance(d, dict) and isinstance(d.get("label"), str):
                    kind = "nickname" if d.get("kind") in NICKNAME_KINDS else "hull_number" if d.get("kind") == "hull_number" else "alias"
                    add_label(d["label"], normalize_name(d["label"]), kind, f"typed alias from {d.get('provider') or 'the graph'}")

        # Instruction-like sentences are removed from source text before it is indexed or
        # returned by any tool (spec R32); the raw text stays on the node.
        self.safe_text: dict[str, str] = {}
        self.redacted_ids: set[str] = set()
        for n in nodes:
            if not n.text:
                self.safe_text[n.id] = ""
                continue
            paras = []
            for para in n.text.split("\n\n"):
                clean, removed = redact_instructions(para)
                paras.append(clean)
                if removed:
                    self.redacted_ids.add(n.id)
            self.safe_text[n.id] = "\n\n".join(paras)

        # Nickname phrases in the (redacted) text: 'codename was "Cactus"', "nicknamed the Big E" (spec R53)
        for n in nodes:
            text = self.safe_text.get(n.id) or ""
            if not text:
                continue
            code = type_codes[n.type]
            for m in TEXT_NICKNAME_RE.finditer(text):
                label = (m.group(1) or m.group(2) or "").strip(" .,;:")
                if len(normalize_name(label)) < 3 or normalize_name(label) == self._norm_name[n.id]:
                    continue
                start = max(0, text.rfind(".", 0, m.start()) + 1)
                end = text.find(".", m.end())
                self._label_evidence[len(self._names)] = "text: " + text[start:end + 1 if end >= 0 else None].strip()[:200]
                self._names.append(normalize_name(label))
                self._labels.append(label)
                self._name_owner.append(n.id)
                self._label_kind.append("nickname")
                label_types.append(code)
        self._type_codes = type_codes
        self._label_types = np.array(label_types, dtype=np.int32)
        self._label_lens = np.array([len(x) for x in self._names], dtype=np.int32)
        self._nick_mask = np.array([k in ("nickname", "hull_number") for k in self._label_kind], dtype=bool)
        self._alias_mask = np.array([k == "alias" for k in self._label_kind], dtype=bool)
        self._label_ids: dict[str, list[int]] = {}  # node id -> its label indexes
        for i, owner in enumerate(self._name_owner):
            self._label_ids.setdefault(owner, []).append(i)
        self._exact: dict[str, list[int]] = {}  # label without a leading article -> label indexes
        for i, name in enumerate(self._names):
            self._exact.setdefault(strip_article(name), []).append(i)

        # Text index: one chunk per paragraph of each node's (redacted) text.
        self._chunks: list[tuple[str, str]] = [
            (n.id, para.strip())
            for n in nodes
            if n.text
            for para in self.safe_text[n.id].split("\n\n")
            if para.strip()
        ]
        self._bm25_index: BM25Okapi | None = None  # built on the first text search
        self._role_cache: dict[tuple[str, str, bool], tuple[tuple[Edge, str], ...]] = {}

        # Bounding boxes of all geometries, for a vectorized prefilter in near()
        self._geo_ids = list(self.geoms)
        self._geo_bounds = (np.array([g.bounds for g in self.geoms.values()], dtype=float).reshape(-1, 4)
                            if self.geoms else np.zeros((0, 4)))

    @property
    def _bm25(self) -> BM25Okapi | None:
        if self._bm25_index is None and self._chunks:
            self._bm25_index = BM25Okapi([_tokenize(t) for _, t in self._chunks])
        return self._bm25_index

    @classmethod
    def from_json(cls, path: str | Path) -> "GraphStore":
        """Load a graph from a JSON file shaped like
        {"metadata": {...}, "relation_catalog": [...], "nodes": [...], "edges": [...]}
        (metadata and relation_catalog are optional).

        The cyclic garbage collector is paused while loading: building ~1M objects
        otherwise triggers repeated full collections (measured: edge parsing 12.3 s
        with the collector, 4.6 s without, on the 700k-edge draft).
        """
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            store = cls.from_dict(data)
            del data
            # The graph lives as long as the process and holds no garbage cycles: move it out
            # of the collector's view, or every full collection walks ~10M objects (measured:
            # a BM25 build at query time took 3.4 s instead of 0.25 s).
            gc.collect()
            gc.freeze()
            return store
        finally:
            if was_enabled:
                gc.enable()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphStore":
        """Build a store from parsed graph JSON. Raw rows are released as they are converted."""
        raw_nodes, raw_edges = data.get("nodes") or [], data.get("edges") or []
        nodes = []
        for i, n in enumerate(raw_nodes):
            nodes.append(Node.model_validate(n))
            raw_nodes[i] = None
        edges = []
        for i, e in enumerate(raw_edges):
            edges.append(Edge.model_validate(e))
            raw_edges[i] = None
        store = cls(nodes, edges, data.get("metadata"), data.get("relation_catalog"))
        store.input_counts = {"nodes": len(raw_nodes), "edges": len(raw_edges)}  # to check nothing was dropped
        return store

    # ------------------------------------------------------------- relations

    def relation_filter(self, values: str | Iterable[str] | None) -> tuple[set[str], set[str]] | None:
        """Resolve relation filter values to (Level 2 types, Level 1 families); None = no filter.

        A value matches an exact Level 2 id ("wd:P607", case-preserved, never
        uppercased) or a Level 1 family ("CONFLICT_AFFILIATION", any case). Types
        without a family (the sample's ATTACKED, OCCURRED_AT, ...) also match
        case-insensitively, as before. Unknown values match nothing.
        """
        if values is None:
            return None
        types, families = set(), set()
        family_names = {f.casefold(): f for f in self.by_family} | {
            c["family"].casefold(): c["family"] for c in self.catalog.values() if c["family"]}
        for v in [values] if isinstance(values, str) else values:
            if v in self.by_type or v in self.catalog:
                types.add(v)
            if v.casefold() in family_names:
                families.add(family_names[v.casefold()])
            if v.casefold() in self._legacy_types:
                types.add(self._legacy_types[v.casefold()])
        return types, families

    @staticmethod
    def _relation_matcher(rel: tuple[set[str], set[str]] | None) -> Callable[[Edge], bool]:
        if rel is None:
            return lambda e: True
        types, families = rel
        return lambda e: e.type in types or (e.relation_family in families if e.relation_family else False)

    def relation_label(self, type: str) -> str:
        """Readable label of a relation id: the catalog label, else the id itself."""
        return self.catalog.get(type, {}).get("label") or type

    def unknown_relations(self, values: str | Iterable[str] | None) -> list[str]:
        """Filter values that name no relation type or family in this graph."""
        if values is None:
            return []
        return [v for v in ([values] if isinstance(values, str) else values) if self.relation_filter(v) == (set(), set())]

    def type_families(self) -> dict[str, Counter]:
        """Per relation type, the families its edges carry (assertion counts); cached."""
        if (cached := self.__dict__.get("_type_families")) is None:
            cached = defaultdict(Counter)
            for e in self.edges:
                cached[e.type][e.relation_family] += 1
            self.__dict__["_type_families"] = cached
        return cached

    def relation_catalog(self, family: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        """Level 2 relations of one family (or all), with label, family, direction, mapping
        status and the number of edges using it; most used first. `query` filters on
        id and label (case-insensitive). Relation types found on edges but missing from
        the catalog are listed with what the edges say (`in_catalog` false).

        An edge's own `relation_family` is what family filters match. Where edges carry a
        family other than the catalog's (the draft: 244k edges, e.g. wd:P793 edges say
        SPATIAL, the catalog UNRESOLVED), `edge_families` gives the edge counts per
        family, and the relation is listed under each of those families too.
        """
        counts, type_fams = self.type_counts, self.type_families()
        rows: dict[str, dict[str, Any]] = {
            i: {**{k: c[k] for k in ("id", "label", "family", "direction", "status", "source_predicates")}, "in_catalog": True}
            for i, c in self.catalog.items()}
        for t, es in self.by_type.items():
            if t not in rows:
                fam = next((e.relation_family for e in es if e.relation_family), None)
                rows[t] = {"id": t, "label": es[0].attributes.get("relation_label") or t, "family": fam,
                           "direction": [], "status": [], "source_predicates": [], "in_catalog": not self.catalog}
        fam = family.casefold() if family else None
        q = query.casefold() if query else None
        out = []
        for r in rows.values():
            on_edges = {f: n for f, n in type_fams.get(r["id"], {}).items() if f}
            families = {(r["family"] or "").casefold()} | {f.casefold() for f in on_edges}
            if fam is not None and fam not in families:
                continue
            if q is not None and q not in r["id"].casefold() and q not in str(r["label"]).casefold():
                continue
            row = {**r, "edges": counts.get(r["id"], 0)}
            if on_edges and set(on_edges) != {r["family"]}:
                row["edge_families"] = on_edges
            out.append(row)
        out.sort(key=lambda r: (-r["edges"], r["id"]))
        return out

    def family_summary(self) -> dict[str, dict[str, Any]]:
        """Short Level 1 summary: per family the number of Level 2 relations (in the
        catalog and in use), the number of edges, and its three most used relations."""
        fams: dict[str, dict[str, Any]] = {}
        for c in self.catalog.values():
            if c["family"]:
                fams.setdefault(c["family"], {"relations": 0, "relations_used": 0, "edges": 0, "top": []})["relations"] += 1
        for f, es in self.by_family.items():
            entry = fams.setdefault(f, {"relations": 0, "relations_used": 0, "edges": 0, "top": []})
            used = Counter({t: self.type_counts[t] for t in sorted({e.type for e in es})})
            entry.update(edges=self.family_counts[f], relations_used=len(used),
                         top=[f"{t} ({self.relation_label(t)})" for t, _ in used.most_common(3)])
            entry["relations"] = max(entry["relations"], len(used))
        return dict(sorted(fams.items(), key=lambda kv: -kv[1]["edges"]))

    def metadata_summary(self) -> dict[str, Any]:
        """Version, status and scope fields of the graph's top-level `metadata` ({} when absent)."""
        keys = ("schema_version", "build_version", "relation_map_version", "release_status", "scope")
        return {k: self.metadata[k] for k in keys if self.metadata.get(k) is not None}

    # ------------------------------------------------------------ serialization

    def summarize_node(self, node_id: str) -> dict[str, Any]:
        """Compact node view for tool output: id, name, type, representative lat/lon."""
        n = self.nodes[node_id]
        out: dict[str, Any] = {"id": n.id, "name": n.name, "type": n.type}
        if (g := self.geoms.get(node_id)) is not None:
            c = g.representative_point()
            out.update(lat=round(c.y, 4), lon=round(c.x, 4), geometry_type=g.geom_type)
        return out

    def group(self, e: Edge) -> list[Edge]:
        """All source assertions combined with this (representative) edge."""
        return self.assertions.get(e.id, [e])

    def support(self, e: Edge) -> int:
        """Combined support of a relation: the summed `count` of its assertions (mentions
        in the sample graph; one per source assertion in the draft). Used for ranking,
        path strength and confidence wherever a single edge's count was used before."""
        return self._support.get(e.id, e.count)

    def summarize_edge(self, e: Edge) -> dict[str, Any]:
        """Edge view with both endpoints resolved to names, plus dates and provenance.

        `count` is the combined support, `assertions` the number of source assertions
        combined into this row and `providers` their sources (Wikipedia, Wikidata,
        EventKG, ...). `sources` lists real Wikipedia page titles only, never a record
        id. Record ids are left out of tool output (EventKG ids run to 100+ characters
        and the model never needs them): finalize_answer looks them up for `source_refs`
        with `relation_groups`.
        """
        nodes = self.nodes
        out: dict[str, Any] = {
            "source": {"id": e.source, "name": nodes[e.source].name, "type": nodes[e.source].type},
            "relation": e.type,
            "target": {"id": e.target, "name": nodes[e.target].name, "type": nodes[e.target].type},
            "count": self.support(e),
        }
        if e.relation_family:
            out["relation_family"] = e.relation_family
            if (label := self.relation_label(e.type)) != e.type:
                out["relation_label"] = label
        if e.start_date or e.end_date:
            out["start_date"], out["end_date"] = e.start_date, e.end_date
        if e.point_date:  # kept as given: a point in time, not a period
            out["point_date"] = e.point_date
        group = self.group(e)
        srcs = [s for a in group for s in a.sources]
        if srcs:
            out["sources"] = list(dict.fromkeys(s.page for s in srcs if s.page))
            out["assertions"] = len(group)
            out["providers"] = sorted({s.provider for s in srcs if s.provider})
        return out

    def relation_groups(self, source: str, relation: str, target: str, start: str | None = None,
                        end: str | None = None, point: str | None = None) -> list[Edge]:
        """All source assertions behind a relation row (as summarize_edge prints it)."""
        return [a for e in self.out_edges.get(source, ()) if e.type == relation and e.target == target
                and (e.start_date, e.end_date, e.point_date) == (start, end, point) for a in self.group(e)]

    # ----------------------------------------------------------------- queries

    SCHEMA_MAX_TYPES = 40  # relation types listed in schema(); the rest are counted, see relation_catalog()

    def schema(self) -> dict[str, Any]:
        """Vocabulary and extent of the loaded graph (computed once; the graph is static).

        With many relation types, only the most used SCHEMA_MAX_TYPES are listed and
        `relation_types_total` gives the full number; families are summarized in
        `relation_families`, and `metadata` repeats the graph's version and scope.
        """
        if (cached := self.__dict__.get("_schema")) is not None:
            return cached
        dates = [d for e in self.edges for d in (e.start_date, e.end_date, e.point_date) if d]
        bounds = None
        if self.geoms:
            xs0, ys0, xs1, ys1 = zip(*(g.bounds for g in self.geoms.values()))
            bounds = {"min_lon": min(xs0), "min_lat": min(ys0), "max_lon": max(xs1), "max_lat": max(ys1)}
        types = self.type_counts
        out: dict[str, Any] = {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(Counter(n.type for n in self.nodes.values())),
            "relation_types": dict(types.most_common(self.SCHEMA_MAX_TYPES)),
            "date_range": [min(dates), max(dates)] if dates else None,
            "bbox": bounds,
        }
        if len(types) > self.SCHEMA_MAX_TYPES:
            out["relation_types_total"] = len(types)
            out["relation_types_truncated"] = True
        if self.by_family or self.catalog:
            out["relation_families"] = self.family_summary()
        if self.metadata:
            out["metadata"] = self.metadata_summary()
        out["coverage"] = self.coverage()
        self.__dict__["_schema"] = out
        return out

    def data_limits(self, node_id: str) -> list[str]:
        """Known gaps of one entity, for answers to name (contract section 5):
        name_is_id (no label, only its QID), no_text, type_unresolved (`other`),
        no_dates (an event without dates), no_geometry (a location without coordinates)."""
        n = self.nodes[node_id]
        limits = []
        if n.name == n.id or QID_RE.fullmatch(n.name):
            limits.append("name_is_id")
        if not n.text.strip():
            limits.append("no_text")
        if n.type == "other":
            limits.append("type_unresolved")
        if n.type == "event" and self.node_dates(node_id) == (None, None):
            limits.append("no_dates")
        if n.type == "location" and node_id not in self.geoms:
            limits.append("no_geometry")
        return limits

    def coverage(self) -> dict[str, Any]:
        """Data-quality counts over the whole graph (contract section 5), kept visible in graph_schema."""
        counts = Counter(lim for i in self.nodes for lim in self.data_limits(i))
        undated = point_only = 0
        for e in self.edges:
            if not (e.start_date or e.end_date):
                if e.point_date:
                    point_only += 1
                else:
                    undated += 1
        return {
            "nodes_name_is_id": counts["name_is_id"],
            "nodes_without_text": counts["no_text"],
            "nodes_type_other": counts["type_unresolved"],
            "events_without_dates": counts["no_dates"],
            "locations_without_geometry": counts["no_geometry"],
            "locations_with_geometry": sum(1 for n in self.nodes.values() if n.type == "location" and n.id in self.geoms),
            "non_location_nodes_with_geometry": dict(Counter(self.nodes[i].type for i in self.geoms if self.nodes[i].type != "location")),
            "edges_with_interval_dates": len(self.edges) - undated - point_only,
            "edges_with_point_date_only": point_only,
            "edges_without_dates": undated,
            "edges_family_unresolved": self.family_counts.get("UNRESOLVED", 0),
            "edges_family_differs_from_catalog": sum(
                n for t, fams in self.type_families().items() if t in self.catalog
                for f, n in fams.items() if f and f != self.catalog[t]["family"]) if self.catalog else 0,
            "relation_types_not_in_catalog": sum(1 for t in self.by_type if t not in self.catalog) if self.catalog else 0,
            "duplicate_assertions_combined": sum(len(g) - 1 for g in self.assertions.values()),
            "edges_dropped_unknown_endpoint": self.dropped_edges,
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
        best: dict[str, tuple[float, int, str]] = {}  # node id -> (score, label index, match)

        def offer(node_id: str, score: float, idx: int, match: str) -> None:
            if node_id not in best or score > best[node_id][0]:
                best[node_id] = (score, idx, match)

        code = self._type_codes.get(type, -1) if type is not None else None
        for idx in self._exact.get(strip_article(q), []) if q else []:  # exact, article-insensitive (R53)
            if code is None or self._label_types[idx] == code:
                kind = self._label_kind[idx]
                offer(self._name_owner[idx], 100.0 if self._names[idx] == q else ARTICLE_SCORE, idx,
                      "name" if kind == "name" else kind)
        q_tokens = _tokenize(q)
        for score, idx in self._name_candidates(q, type):
            node_id = self._name_owner[idx]
            label_tokens = _tokenize(self._names[idx]) if len(q_tokens) >= 2 and self._label_kind[idx] == "alias" else []
            if (label_tokens and self.nodes[node_id].type == "person" and q_tokens[-1] == label_tokens[-1]
                    and _in_order(q_tokens, label_tokens)):
                offer(node_id, ALIAS_PART_SCORE, idx, "alias_part")  # "howlin mad smith" in 'Holland McTyeire "Howlin' Mad" Smith'
            offer(node_id, score, idx, "fuzzy")
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:limit]
        results = []
        for node_id, (score, idx, match) in ranked:
            r = {**self.summarize_node(node_id), "matched": self._labels[idx], "score": round(score), "match": match}
            if idx in self._label_evidence:
                r["evidence"] = self._label_evidence[idx]
            results.append(r)

        scores = [r["score"] for r in results]
        for i, r in enumerate(results):
            others = scores[:i] + scores[i + 1:]
            r["ambiguous"] = any(abs(r["score"] - s) <= AMBIGUITY_MARGIN for s in others)
            clear = i == 0 and r["score"] >= CLEAR_SCORE and not r["ambiguous"]
            r["quality"] = "clear" if clear else "uncertain"
            if r["ambiguous"] and (hint := self.hint(r["id"])):
                r["hint"] = hint  # spec R54: what tells tied candidates apart
        return results

    def named_in(self, node_id: str, text: str) -> bool:
        """True if the text uses one of the node's labels (name, alias, graph nickname), as whole words and
        ignoring a leading article. Tells a graph-backed reading of the user's words from the model's own (Q18)."""
        words = f" {' '.join(_tokenize(normalize_name(text)))} "
        for i in self._label_ids.get(node_id, ()):
            label = " ".join(_tokenize(strip_article(self._names[i])))
            if len(label) >= 3 and f" {label} " in words:
                return True
        return False

    def hint(self, node_id: str) -> str:
        """Short distinguishing text for a tied search result (spec R54): hull numbers and
        years ("CV-6, 1938-1947"), else the first words of the description."""
        hulls = [self._labels[i] for i in self._label_ids.get(node_id, ()) if self._label_kind[i] == "hull_number"]
        start, end = self.node_dates(node_id)
        years = "-".join(dict.fromkeys(str(d)[:4] for d in (start, end) if d))
        parts = [*hulls, years] if (hulls or years) else [" ".join((self.nodes[node_id].text or "").split()[:8])]
        return ", ".join(p for p in parts if p)

    NAME_CANDIDATES = 200  # best-scoring labels examined per search (several labels can belong to one node)
    SHORT_LABEL = 3  # names up to this length get no partial-match credit
    SHORT_ALIAS = 5  # nor do aliases up to this length: "Bull", the Tu-4's alias, inside "Bull Halsey" (spec R53)

    def _name_candidates(self, q: str, type: str | None) -> list[tuple[float, int]]:
        """(score, label index) of labels scoring >= MATCH_CUTOFF, best first, ties in label order.

        All labels are scored with WRatio in one rapidfuzz call, with the labels as rows
        so it runs multi-threaded (rapidfuzz parallelizes over rows) and with the cutoff
        passed in (process.extract over ~180k draft labels took 0.6-0.9 s; this ~0.4 s). A label of at most
        SHORT_LABEL characters, shorter than half the query, is scored by plain ratio
        instead: WRatio's partial matching gives any such abbreviation found inside the
        query 90 ("DAL" for "Guadalcanl", "ga" for anything with "ga"), and the draft has
        thousands of them, which crowd out real near-misses.
        """
        if not self._names or not q:
            return []
        # processor=None: strings are already normalized; don't depend on rapidfuzz defaults.
        scores = process.cdist(self._names, [q], scorer=fuzz.WRatio, processor=None, dtype=np.float64,
                               workers=-1, score_cutoff=MATCH_CUTOFF)[:, 0]  # WRatio is symmetric
        mask = scores >= MATCH_CUTOFF
        if type is not None:
            code = self._type_codes.get(type)
            mask &= self._label_types == (code if code is not None else -1)
        short_len = (self._label_lens <= self.SHORT_LABEL) | (self._alias_mask & (self._label_lens <= self.SHORT_ALIAS))
        short = mask & short_len & (self._label_lens * 2 < len(q))
        short |= mask & self._nick_mask  # nickname labels: no partial credit ("Bull" in "Bull Halsey"; spec R53)
        for i in np.flatnonzero(short):
            scores[i] = fuzz.ratio(q, self._names[i])
        mask &= scores >= MATCH_CUTOFF
        idx = np.flatnonzero(mask)
        if len(idx) > self.NAME_CANDIDATES:  # keep the best, then order exactly
            idx = idx[np.argpartition(-scores[idx], self.NAME_CANDIDATES - 1)[:self.NAME_CANDIDATES]]
        order = sorted(idx.tolist(), key=lambda i: (-float(scores[i]), i))
        return [(float(scores[i]), i) for i in order]

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
            "sources": [s.view() for s in n.sources],
            "text": self.safe_text[entity_id][:text_chars],
            **({"text_redacted": True} if entity_id in self.redacted_ids else {}),
            **({"data_limits": lim} if (lim := self.data_limits(entity_id)) else {}),
            "degree": len(self.out_edges[entity_id]) + len(self.in_edges[entity_id]),
        }

    def edge_in_range(self, e: Edge, date_from: str | None, date_to: str | None) -> bool:
        """True if the edge's [start, end] interval overlaps [date_from, date_to].

        An edge with only a `point_date` is the interval [point, point]; start/end win
        when both kinds are present.
        """
        return dates_overlap(*edge_dates(e), date_from, date_to)

    def node_dates(self, node_id: str) -> tuple[str | None, str | None]:
        """(start, end) of an entity from its attributes: start_date / end_date; else
        point_date as [point, point]; else the span of point_date_candidates (the draft
        exporter's unselected point dates, e.g. the two atomic bombings)."""
        a = self.nodes[node_id].attributes
        start, end = a.get("start_date"), a.get("end_date")
        if start or end:
            return start, end
        if a.get("point_date"):
            return a["point_date"], a["point_date"]
        cands = sorted(str(c) for c in a.get("point_date_candidates") or [] if c)
        return (cands[0], cands[-1]) if cands else (None, None)

    # ----------------------------------------------------------- roles, places

    def _objects(self, node_id: str, role: str) -> tuple[tuple[Edge, str], ...]:
        """(edge, other) for every edge in which `node_id` is the subject of `role`."""
        return self._role_edges(node_id, role, True)

    def _subjects(self, node_id: str, role: str) -> tuple[tuple[Edge, str], ...]:
        """(edge, other) for every edge in which `node_id` is the object of `role`."""
        return self._role_edges(node_id, role, False)

    def _role_edges(self, node_id: str, role: str, as_subject: bool) -> tuple[tuple[Edge, str], ...]:
        """Role adjacency, cached per (node, role, side): a hub such as World War II has
        ~50k edges, and set tools visit it once per group member (shared_connections
        over 400 orgs took 12 s scanning them again each time)."""
        key = (node_id, role, as_subject)
        hit = self._role_cache.get(key)
        if hit is None:
            roles = RELATION_ROLES[role]
            fwd_side, inv_side = (self.out_edges, self.in_edges) if as_subject else (self.in_edges, self.out_edges)
            found = []
            if ROLE_HAS[role][FWD]:
                found += [(e, e.target if as_subject else e.source) for e in fwd_side[node_id] if roles.get(e.type) == FWD]
            if ROLE_HAS[role][INV]:
                found += [(e, e.source if as_subject else e.target) for e in inv_side[node_id] if roles.get(e.type) == INV]
            hit = self._role_cache[key] = tuple(found) if found else ()
        return hit

    def is_place_via(self, e: Edge, node_id: str) -> bool:
        """Is `node_id`, reached through edge `e`, a place? (geometry rule, see to_geojson)

        A `location` node always is. An unresolved `other` node is a place only if it has
        its own geometry AND the edge is a place relation (location, located in, attacked,
        place of birth, ...): in the draft most coordinates on `other` nodes belong to
        airfields, forts and towns, but also to ships and wrecks, which are not places of
        the battles they took part in. Events, orgs and people are never places.
        """
        n = self.nodes[node_id]
        return n.type == "location" or (n.type == "other" and node_id in self.geoms and e.type in PLACE_RELATIONS)

    def sub_event_count(self, event_id: str) -> int:
        cache = self.__dict__.setdefault("_sub_events", {})
        if event_id not in cache:
            cache[event_id] = sum(1 for _, sub in self._subjects(event_id, "part_of") if self.nodes[sub].type == "event")
        return cache[event_id]

    def sublocations(self, location_id: str) -> set[str]:
        """The location plus every place transitively located in it (LOCATED_IN, wd:P131, wd:P706)."""
        found, stack = {location_id}, [location_id]
        while stack:
            for e, child in self._subjects(stack.pop(), "located_in"):
                if child not in found and self.nodes[child].type in PLACE_TYPES:
                    found.add(child)
                    stack.append(child)
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
        hits = self.find_relation_edges(source_id, target_id, relation_type, date_from, date_to, include_sublocations)
        return [self.summarize_edge(e) for e in hits[:limit]]

    def find_relation_edges(
        self,
        source_id: str | None = None,
        target_id: str | None = None,
        relation_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_sublocations: bool = True,
    ) -> list[Edge]:
        """The edges behind find_relations, strongest first (tools summarize only the rows they show)."""
        def expand(node_id: str | None) -> set[str] | None:
            if node_id is None:
                return None
            if include_sublocations and node_id in self.nodes and self.nodes[node_id].type in PLACE_TYPES:
                return self.sublocations(node_id)
            return {node_id}

        sources, targets = expand(source_id), expand(target_id)
        rel = self.relation_filter(relation_type)
        matches = self._relation_matcher(rel)
        if targets:
            candidates = [e for t in targets for e in self.in_edges[t]]
        elif sources:
            candidates = [e for s in sources for e in self.out_edges[s]]
        elif rel is not None:  # wildcard endpoints: only the edges of the requested relations
            types, families = rel
            candidates = [e for t in types for e in self.by_type.get(t, [])]
            candidates += [e for f in families for e in self.by_family.get(f, []) if e.type not in types]
        else:
            candidates = self.edges
        hits = [
            e for e in candidates
            if (sources is None or e.source in sources)
            and (targets is None or e.target in targets)
            and matches(e)
            and self.edge_in_range(e, date_from, date_to)
        ]
        hits.sort(key=lambda e: -self.support(e))
        return hits

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
        hits = self.neighbor_edges(entity_id, relation_types, direction, neighbor_type, date_from, date_to)
        return [{**self.summarize_edge(e), "neighbor": self.summarize_node(other)} for e, other in hits[:limit]]

    def neighbor_edges(
        self,
        entity_id: str,
        relation_types: list[str] | None = None,
        direction: str = "both",
        neighbor_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[tuple[Edge, str]]:
        """(edge, neighbor id) pairs behind neighbors(), strongest first."""
        matches = self._relation_matcher(self.relation_filter(relation_types or None))
        edges = []
        if direction in ("out", "both"):
            edges += [(e, e.target) for e in self.out_edges[entity_id]]
        if direction in ("in", "both"):
            edges += [(e, e.source) for e in self.in_edges[entity_id]]
        hits = [
            (e, other) for e, other in edges
            if matches(e)
            and (neighbor_type is None or self.nodes[other].type == neighbor_type)
            and self.edge_in_range(e, date_from, date_to)
        ]
        hits.sort(key=lambda h: -self.support(h[0]))  # stable: equal support keeps edge order
        return hits

    # ------------------------------------------------------ set queries (R26)

    def _edge_ok(self, e: Edge, date_from: str | None, date_to: str | None) -> bool:
        """Date filter for path edges. A dated edge (point dates count) must overlap the
        period. An undated edge passes if its relation is timeless (part of, located in),
        and takes its event's dates if it links an event (OCCURRED_AT, participation,
        command), as what_happened_at does; any other undated edge fails."""
        if not date_from and not date_to:
            return True
        if edge_dates(e) != (None, None):
            return self.edge_in_range(e, date_from, date_to)
        if any(role_dir(r, e.type) for r in TIMELESS_ROLES):
            return True
        if any(role_dir(r, e.type) for r in EVENT_DATED_ROLES):
            event = next((i for i in (e.source, e.target) if self.nodes[i].type == "event"), None)
            return event is not None and dates_overlap(*self.node_dates(event), date_from, date_to)
        return False

    def _profile_paths(self, entity_id: str, ok, include_weak: bool, min_weak_count: int, seen: set[str]):
        """Yield (location_id, [edges]) for every path the entity's profile allows.

        Profiles (RELATION_ROLES names the relations of each role, both vocabularies):
          location  itself and the places located in it
          event     its own site if it has its own geometry, else its occurred_at places;
                    then its sub-events (part_of), recursively
          person, org, other (actors)
                    places it acted at (attacked, occupied, based at, ...); for a person also
                    places of their life; the events it took part in or commanded (event_link)
                    -> event paths, not into the sub-events of a hub (HUB_SUB_EVENTS);
                    for orgs and unresolved actors, sub-units (part_of), recursively
        `seen` is shared by the whole traversal, so cycles and shared sub-units end.
        """
        if entity_id in seen:  # PART_OF cycles
            return
        seen.add(entity_id)
        node = self.nodes[entity_id]

        def event_paths(event_id: str, prefix: list[Edge], visited: set[str], from_actor: bool):
            if event_id in visited:
                return
            visited.add(event_id)
            if from_actor and self.sub_event_count(event_id) > HUB_SUB_EVENTS:
                return  # a war or theatre the actor took part in: neither all its battles nor its countries
            if event_id in self.geoms:  # geometry rule: an event's own coordinates are its site
                yield event_id, prefix
            else:
                for e, place in self._objects(event_id, "occurred_at"):
                    if ok(e) and self.is_place_via(e, place):
                        yield place, prefix + [e]
            for e, sub in self._subjects(event_id, "part_of"):
                if self.nodes[sub].type == "event" and ok(e):
                    yield from event_paths(sub, prefix + [e], visited, from_actor)

        if node.type == "location":
            yield entity_id, []
            for child in self.sublocations(entity_id) - {entity_id}:
                yield child, [e for e, _ in self._objects(child, "located_in")][:1]
            return
        if node.type == "event":
            yield from event_paths(entity_id, [], set(), False)
            return

        for e, place in self._objects(entity_id, "acted_at"):
            if ok(e) and self.is_place_via(e, place):
                yield place, [e]
        if node.type == "person":
            for e, place in self._objects(entity_id, "person_place"):
                if ok(e) and self.is_place_via(e, place):
                    yield place, [e]
        if node.type != "org":  # sample vocabulary: any direct (non-weak) person -> location relation
            for e in self.out_edges[entity_id]:
                if (e.relation_family is None and e.type not in LOCATION_RELATIONS and e.type != WEAK_RELATION
                        and self.nodes[e.target].type == "location" and ok(e)):
                    yield e.target, [e]
        for e, event in self._objects(entity_id, "event_link"):
            if self.nodes[event].type == "event" and ok(e):
                yield from event_paths(event, [e], set(), True)
        if node.type in ("org", "other"):  # sub-units: unit -PART_OF-> this org
            for e, sub in self._subjects(entity_id, "part_of"):
                if self.nodes[sub].type in ("org", "other") and ok(e):
                    for loc, path in self._profile_paths(sub, ok, include_weak, min_weak_count, seen):
                        yield loc, [e] + path
        if include_weak:
            for e, other in [(e, e.target) for e in self.out_edges[entity_id]] + [(e, e.source) for e in self.in_edges[entity_id]]:
                if e.type == WEAK_RELATION and self.support(e) >= min_weak_count and self.nodes[other].type == "location" and ok(e):
                    yield other, [e]

    def _path_text(self, start_id: str, path: list[Edge]) -> tuple[str, str | None]:
        """Readable path ("Yamamoto -COMMANDED-> Battle of Midway -OCCURRED_AT-> Midway Atoll") and its first intermediate id."""
        parts, current, via_id = [self.nodes[start_id].name], start_id, None
        for e in path:
            nxt = e.target if e.source == current else e.source
            label = self.relation_label(e.type)
            arrow = f"-{label}->" if e.source == current else f"<-{label}-"
            parts += [arrow, self.nodes[nxt].name]
            if via_id is None and self.nodes[nxt].type not in PLACE_TYPES:
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
        weakest combined support on the best path) and `paths` (number of distinct paths).
        A row is a place (a `location`, or an `other` node with coordinates reached through
        a place relation) or, with `type` "event", the own site of an event that has
        coordinates (geometry rule). Ships, units and people with coordinates are never rows.
        """
        if entity_id not in self.nodes:
            return []
        ok = lambda e: self._edge_ok(e, date_from, date_to)
        best: dict[str, tuple[int, list[Edge]]] = {}
        n_paths: Counter[str] = Counter()
        for loc, path in self._profile_paths(entity_id, ok, include_weak, min_weak_count, set()):
            if location_kind and not self._matches_kind(self.nodes[loc], location_kind):
                continue
            strength = min((self.support(e) for e in path), default=SELF_STRENGTH)
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
                if self.has_country_relations():
                    notes.append(f"'{g[8:]}' orgs taken from their country relation (wd:P17/P27), or from their names "
                                 "when they have none: the graph has no org country attribute")
                else:
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

    GROUNDING_TYPES = ("event", "location", "other")  # entities whose naming in an answer states a fact
    GROUNDING_MIN_LEN = 5  # shorter names are too often ordinary words; short places ("Guam") go unchecked
    GROUNDING_MAX_DEGREE = 2000  # countries and navies (Empire of Japan 2,455 edges); battles stay well below
    GROUNDING_STOP = {"the", "a", "an", "of", "in", "on", "at", "and", "or", "to", "by", "for", "from", "with"}

    def names_in_text(self, text: str, max_words: int = 6) -> dict[str, list[str]]:
        """Graph entities named in a text: {name as written: [ids]}.

        Exact normalized names of events, places and unresolved entities, longest match first.
        A match must start with a capitalized word that isn't an article or preposition, so
        "escort carrier" or "the Battle" are not names. Hubs (wars with more than HUB_SUB_EVENTS
        parts, nodes with more than GROUNDING_MAX_DEGREE edges) are recognized but not returned:
        mentioning World War II is no specific claim, and "World War II" must not leave "World War".
        Used to check that an answer names only what this turn's tools returned.
        """
        index = self.__dict__.get("_grounding_index")
        if index is None:
            index = {}
            for n in self.nodes.values():
                norm = self._norm_name.get(n.id, "")
                if n.type not in self.GROUNDING_TYPES or n.name == n.id or len(norm) < self.GROUNDING_MIN_LEN:
                    continue
                hub = (len(self.out_edges[n.id]) + len(self.in_edges[n.id]) > self.GROUNDING_MAX_DEGREE
                       or (n.type == "event" and self.sub_event_count(n.id) > HUB_SUB_EVENTS))
                index.setdefault(norm, []).append((n.id, hub))
            self._grounding_index = index
        words = re.findall(r"[\w][\w'’.\-]*", text)
        found: dict[str, list[str]] = {}
        i = 0
        while i < len(words):
            if words[i].lower() in self.GROUNDING_STOP or not (words[i][0].isupper() or words[i][0].isdigit()):
                i += 1
                continue
            for n in range(min(max_words, len(words) - i), 0, -1):
                span = " ".join(words[i:i + n]).rstrip(".")
                hits = index.get(normalize_name(span))
                if hits:
                    ids = [node_id for node_id, hub in hits if not hub]
                    if ids:
                        found.setdefault(span, ids)
                    i += n
                    break
            else:
                i += 1
        return found

    def kind_of(self, node_id: str) -> str:
        """An entity's kind: its attribute when the graph has kinds for its type, else the first
        name pattern that matches (R30), else "other"."""
        node = self.nodes[node_id]
        if self.has_attribute(node.type, "kind"):
            return str(node.attributes.get("kind") or "other")
        labels = [node.name, *node.aliases]
        return next((k for k, p in KIND_NAME_PATTERNS.items() if any(re.search(p, l, re.IGNORECASE) for l in labels)), "other")

    def overview(self, scope: str | None = None, type: str = "event", kind: str | None = None,
                 date_from: str | None = None, date_to: str | None = None, top: int = OVERVIEW_TOP) -> dict[str, Any]:
        """Structure of a broad question's answer in one call (spec R57): counts, the most
        important items with their places, and ways to narrow the question.

        Items: with no scope, every entity of `type`; for an event scope (a war, theatre or
        campaign), its sub-events, recursively; for a place, the events there (and in the places
        inside it); for a person or org, the events it took part in or commanded, plus their
        sub-events unless the event is a hub (HUB_SUB_EVENTS), as in connected_locations.
        Each item is grouped by the scope's direct sub-event it belongs to (event scope), the
        place (place scope), the event the actor is linked to (actor scope), or its own parent
        (no scope).
        """
        groups: dict[str, str] = {}  # item id -> group label
        node = self.nodes.get(scope) if scope else None
        if scope and node is None:
            return {"total": 0, "error": f"unknown entity id {scope}"}

        def sub_events(event_id: str) -> list[str]:
            return [sub for _, sub in self._subjects(event_id, "part_of") if self.nodes[sub].type == "event"]

        def descend(event_id: str, label: str) -> None:
            stack = [event_id]
            while stack:
                cur = stack.pop()
                if cur in groups or cur == scope:
                    continue
                groups[cur] = label
                stack += sub_events(cur)

        if node is None:
            for n in self.nodes.values():
                if n.type == type:
                    parent = next((p for _, p in self._objects(n.id, "part_of")), None)
                    groups[n.id] = self.nodes[parent].name if parent else "(no parent)"
        elif node.type == "event":
            for child in sub_events(scope):
                descend(child, self.nodes[child].name)
        elif node.type == "location":
            for place in {scope} | self.sublocations(scope):
                for _, ev in self._subjects(place, "occurred_at"):
                    if self.nodes[ev].type == "event":
                        groups.setdefault(ev, self.nodes[place].name)
        else:
            for _, ev in self._objects(scope, "event_link"):
                if self.nodes[ev].type != "event" or ev in groups:
                    continue
                groups[ev] = self.nodes[ev].name
                if self.sub_event_count(ev) <= HUB_SUB_EVENTS:
                    for child in sub_events(ev):
                        descend(child, self.nodes[ev].name)

        rows = []
        for item, group in groups.items():
            if kind and not self._matches_kind(self.nodes[item], kind):
                continue
            start, end = self.node_dates(item)
            if (date_from or date_to) and not dates_overlap(start, end, date_from, date_to):
                continue
            if item in self.geoms:
                place, where = self.nodes[item].name, [item]
            else:
                where = [p for _, p in self._objects(item, "occurred_at") if p in self.geoms]
                place = self.nodes[where[0]].name if where else None
            first = self.summarize_node(where[0]) if where else {}
            rows.append({"id": item, "name": self.nodes[item].name, "kind": self.kind_of(item), "start_date": start,
                         "end_date": end, "place": place, "lat": first.get("lat"), "lon": first.get("lon"),
                         "group": group, "importance": self.importance(item), "draw": where})
        rows.sort(key=lambda r: (-r["importance"], r["name"]))
        by_kind = Counter(r["kind"] for r in rows)
        by_year = Counter(str(r["start_date"])[:4] if r["start_date"] else "undated" for r in rows)
        by_group = Counter(r["group"] for r in rows)
        narrow = []
        if len(by_group) > 1:
            narrow += [{"by": "part" if node is not None and node.type == "event" else "group", "value": g, "count": c}
                       for g, c in by_group.most_common(3) if g != "(no parent)"][:2]
        if len(by_year) > 1:
            narrow += [{"by": "year", "value": y, "count": c} for y, c in by_year.most_common(2) if y != "undated"][:1]
        if len(by_kind) > 1 and not kind:
            narrow += [{"by": "kind", "value": k, "count": c} for k, c in by_kind.most_common(1)]
        keep = ("id", "name", "kind", "start_date", "end_date", "place", "lat", "lon", "group")
        return {
            "scope": node.name if node is not None else f"all {kind or type}s",
            "total": len(rows),
            "by_kind": dict(by_kind.most_common(8)),
            "by_year": dict(sorted(by_year.items())[:15]),
            "by_group": dict(by_group.most_common(8)),
            "top": [{k: r[k] for k in keep if r.get(k) is not None} for r in rows[:top]],
            "narrow_by": narrow,
            "ids": [r["id"] for r in rows],
            "draw": list(dict.fromkeys(d for r in rows for d in r["draw"])),
            "kinds_from_names": not self.has_attribute(type, "kind"),
        }

    def importance(self, entity_id: str) -> int:
        """Ranking signal for "major" (spec R28): the graph's `importance` attribute when
        present, else the sum of edge counts plus the number of source pages."""
        node = self.nodes[entity_id]
        if isinstance(node.attributes.get("importance"), (int, float)):
            return int(node.attributes["importance"])
        edges = self.out_edges[entity_id] + self.in_edges[entity_id]
        return sum(self.support(e) for e in edges) + len({s.page for s in node.sources if s.page})

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

        Dates match the entity's own dates (`node_dates`: start/end attributes, else
        point dates; interval overlap); entities without dates are dropped when a date
        filter is set.
        """
        rows = []
        name_country = country and not self.has_attribute(type, "country")
        for node in self.nodes.values():
            a = node.attributes
            if node.type != type or (kind and not self._matches_kind(node, kind)):
                continue
            if country and name_country:  # R30/R59: no country attribute in the graph
                if not self._in_country(node, country):
                    continue
            elif country and str(a.get("country", "")).casefold() != country.casefold():
                continue
            if side and str(a.get("side", "")).casefold() != side.casefold():
                continue
            start, end = self.node_dates(node.id)
            if not dates_overlap(start, end, date_from, date_to):
                continue
            rows.append({**self.summarize_node(node.id), "kind": a.get("kind"), "importance": self.importance(node.id),
                         "start_date": start, "end_date": end})
        rows.sort(key=lambda r: (-r["importance"], r["name"]))
        return rows

    def has_country_relations(self) -> bool:
        """True if any org has a country relation (cached; spec R59)."""
        if "_has_country_rel" not in self.__dict__:
            self._has_country_rel = any(e.type in COUNTRY_RELATIONS for n in self.nodes.values() if n.type == "org"
                                        for e in self.out_edges.get(n.id, ()))
        return self._has_country_rel

    def _in_country(self, node: Node, country: str) -> bool:
        """Membership without a country attribute (spec R59): the org's country relation if it has one,
        else its name or aliases."""
        pattern = COUNTRY_NAME_PATTERNS.get(country.casefold(), re.escape(country))
        qids = COUNTRY_QIDS.get(country.casefold(), set())
        targets = [e.target for e in self.out_edges.get(node.id, ()) if e.type in COUNTRY_RELATIONS]
        if targets:
            return any(t in qids or any(re.search(pattern, l, re.IGNORECASE)
                                        for l in [self.nodes[t].name, *self.nodes[t].aliases]) for t in targets)
        return any(re.search(pattern, l, re.IGNORECASE) for l in [node.name, *node.aliases])

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
                events |= {ev for _, ev in self._objects(m, "event_link") if self.nodes[ev].type == "event"}
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
            parent = next((p for _, p in self._objects(r["id"], "located_in")), None)
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
        A non-location entity with its own coordinates (an event's site) comes first,
        with relations ["SELF"] and geometry_source "direct"; linked places have
        geometry_source "related". Places follow `is_place_via`.
        """
        n = self.nodes.get(entity_id)
        if n is None:
            return []
        if n.type == "location" and entity_id in self.geoms:
            return [{**self.summarize_node(entity_id), "relations": ["SELF"], "geometry_source": "direct"}]
        own = [{**self.summarize_node(entity_id), "relations": ["SELF"], "geometry_source": "direct"}] if entity_id in self.geoms else []
        by_loc: dict[str, dict[str, Any]] = {}
        for e, other in [(e, e.target) for e in self.out_edges[entity_id]] + [(e, e.source) for e in self.in_edges[entity_id]]:
            if other == entity_id or other not in self.geoms or not self.is_place_via(e, other):
                continue
            entry = by_loc.setdefault(other, {**self.summarize_node(other), "relations": [], "geometry_source": "related"})
            pages = list(dict.fromkeys(s.page for a in self.group(e) for s in a.sources if s.page))
            entry["relations"].append({"type": e.type, "start_date": e.start_date, "end_date": e.end_date,
                                       **({"point_date": e.point_date} if e.point_date else {}),
                                       "count": self.support(e), **({"sources": pages} if pages else {})})
        return own + list(by_loc.values())

    def distance_km(self, node_id: str, lat: float, lon: float) -> float:
        """Distance from (lat, lon) to the nearest point of the node's geometry (0 if inside)."""
        g = self.geoms[node_id]
        p = Point(lon, lat)
        if g.contains(p):
            return 0.0
        near, _ = nearest_points(g, p)
        return haversine_km(lat, lon, near.y, near.x)

    def _near_candidates(self, lat: float, lon: float, radius_km: float) -> list[str]:
        """Geometries whose bounding box could lie within radius_km of the point (a superset).

        Latitude: 1 degree is at least 110.5 km. Longitude: degrees per km grow toward the
        poles, so the widest latitude the box can reach is used; near a pole, or for very
        large radii, longitude is not filtered. Wrapping across 180 degrees is handled.
        """
        if not self._geo_ids:
            return []
        b = self._geo_bounds
        dlat = radius_km / 110.5 + 1e-9
        ok = (b[:, 1] - dlat <= lat) & (lat <= b[:, 3] + dlat)
        max_lat = min(abs(lat) + dlat, 90.0)
        cos = math.cos(math.radians(max_lat))
        dlon = radius_km / (111.32 * cos) if cos > 1e-6 else 360.0
        if dlon < 180:
            inside = (b[:, 0] <= lon) & (lon <= b[:, 2])
            gap_west = np.mod(b[:, 0] - lon, 360.0)  # eastward from the point to the box
            gap_east = np.mod(lon - b[:, 2], 360.0)  # westward from the point to the box
            ok &= inside | (gap_west <= dlon) | (gap_east <= dlon)
        return [self._geo_ids[i] for i in np.flatnonzero(ok)]

    def near(self, lat: float, lon: float, radius_km: float, type: str | None = None, limit: int | None = 25) -> list[dict[str, Any]]:
        """Entities whose geometry lies within `radius_km` of (lat, lon), nearest first."""
        hits = []
        for node_id in self._near_candidates(lat, lon, radius_km):
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

    EVENT_OUTPUT_ATTRS = ("kind", "start_date", "end_date", "point_date", "importance", "country", "side")

    def what_happened_at(
        self,
        lat: float,
        lon: float,
        radius_km: float = 50,
        text_chars: int = 400,
        date_from: str | None = None,
        date_to: str | None = None,
        relation_limit: int | None = None,
    ) -> dict[str, Any]:
        """Composite point query: nearby places, then their events and relations.

        Steps:
        1. Places within `radius_km` of the point: locations, and unresolved (`other`)
           entities with their own coordinates there.
        2. Every edge touching those places (attacks, captures, OCCURRED_AT, ...); the
           events found that way plus events whose own coordinates lie within the radius.
        3. For the events found, their participants and commanders.
        Relations are sorted chronologically, and events carry a text excerpt.

        With a date filter (spec R6) the places stay; a relation is kept only if its
        own dates overlap (`edge_in_range`: point dates count, undated relations are
        dropped); an event is kept if its own dates (`node_dates`) overlap, or, when it
        has no dates, if any of its relations is kept. Unlike find_relations, an event
        dated only in its attributes is found even when its OCCURRED_AT edge is undated.
        With `relation_limit`, only that many relations are summarized and
        `relations_total` gives the full number.
        """
        hits = self.near(lat, lon, radius_km, limit=None)  # the tool caps and reports totals
        places = [h for h in hits if h["type"] in PLACE_TYPES]
        place_ids = {p["id"] for p in places}
        edges = [e for pid in place_ids for e in self.in_edges[pid] + self.out_edges[pid]]
        event_ids = {e.source for e in edges if self.nodes[e.source].type == "event"}
        event_ids |= {h["id"] for h in hits if h["type"] == "event"}  # events with their own coordinates
        edges += [e for ev in event_ids for e in self.in_edges[ev]]
        unique = list({id(e): e for e in edges}.values())
        if date_from or date_to:
            unique = [e for e in unique if self.edge_in_range(e, date_from, date_to)]
            touched = {i for e in unique for i in (e.source, e.target)}

            def keep(ev: str) -> bool:
                start, end = self.node_dates(ev)
                return dates_overlap(start, end, date_from, date_to) if (start or end) else ev in touched

            event_ids = {ev for ev in event_ids if keep(ev)}
        unique.sort(key=lambda e: (edge_dates(e)[0] or "9999", -self.support(e)))

        def event_row(ev: str) -> dict[str, Any]:
            a = self.nodes[ev].attributes
            row = {**self.summarize_node(ev), **{k: a[k] for k in self.EVENT_OUTPUT_ATTRS if k in a}}
            start, end = self.node_dates(ev)
            if start and "start_date" not in row:  # point dates and candidates, as used by the filter
                row.update(start_date=start, end_date=end)
            row["excerpt"] = self.safe_text[ev][:text_chars]
            return {**row, **({"text_redacted": True} if ev in self.redacted_ids else {})}

        events = sorted((event_row(ev) for ev in event_ids), key=lambda x: x.get("start_date", "9999"))
        relations = [e for e in unique if not role_dir("located_in", e.type)]
        extra = {} if relation_limit is None else {"relations_total": len(relations)}
        return {
            **extra,
            "places": places,
            "events": events,
            "relations": [self.summarize_edge(e) for e in relations[:relation_limit]],
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
                "sources": [s.view() for s in n.sources],
                "score": round(float(scores[i]), 3),
            })
        return out

    def to_geojson(self, entity_ids: list[str], restrict_to: set[str] | None = None) -> dict[str, Any]:
        """FeatureCollection for the map UI.

        Geometry rule (contract 4.7, option 1): any node may carry geometry. Every
        feature says where its geometry comes from in `geometry_source`:
        - "direct": the entity's own geometry. An entity with geometry is always drawn
          at it, whatever its type (an event's coordinates, a place's shape).
        - "related": the geometry of a place linked to the entity, which has none of
          its own; `related_to` names the entity. So `to_geojson(["event:battle_of_midway"])`
          still yields a map feature. With `restrict_to`, such an entity is drawn only at
          linked places in that set (spec R23); otherwise at all of them. An event is always
          drawn at the places it occurred at: that is its site, not an incidental link.
        `type` is the drawn node's own type, so the UI can tell an event site from a place.
        """
        features, seen = [], set()

        def add(loc_id: str, via: str | None = None) -> None:
            if loc_id in seen or loc_id not in self.geoms:
                return
            seen.add(loc_id)
            n = self.nodes[loc_id]
            props = {"id": n.id, "name": n.name, "type": n.type, "geometry_source": "related" if via else "direct"}
            if via:
                props["related_to"] = via
            features.append({"type": "Feature", "geometry": n.geometry, "properties": props})

        for eid in entity_ids:
            if eid not in self.nodes:
                continue
            if eid in self.geoms:
                add(eid)
            else:
                is_event = self.nodes[eid].type == "event"
                for loc in self.locations_of(eid):
                    site = is_event and any(isinstance(r, dict) and r.get("type") in RELATION_ROLES["occurred_at"]
                                            for r in loc.get("relations") or [])
                    if loc["id"] != eid and (restrict_to is None or loc["id"] in restrict_to or site):
                        add(loc["id"], via=eid)
        return {"type": "FeatureCollection", "features": features}
