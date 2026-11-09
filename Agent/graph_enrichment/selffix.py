"""Self-fix build layer (spec R97): adds to the team's graph what its own sources state.

    uv run python -m graph_enrichment.selffix --graph RAW.json --out OUT/graph.selffix.json --cache OUT/cache \
        --wikidata-cache DIR --wikipedia-cache DIR [--offline] [--steps 1,3,5,6] [--keep-stages]

Stages, in order, each reading the previous one's graph:
- **enrich**: the R74 Wikidata enrichment (`graph_enrichment.enrich`) of the raw graph, always run.
- **1**: title -> QID resolution of every wikilink in the corpus paragraphs. The paragraphs are
  rebuilt from the processing revisions the sources cite (their `raw_anchor_span` indexes that
  revision's wikitext): the graph's own `sources[].text` has the links stripped. Changes no record.
- **3**: entities the corpus links at least MIN_LINKS times that the graph lacks, kept only when
  their Wikidata class (graph_enrichment.classes) and claims put them in scope; one record each,
  with a source per link occurrence (and per short-form mention of a person), then the R74
  enrichment over them so they connect.
- **5**: sides, commanders, units and places from the conflict infoboxes of the battles in the agent's
  window and the theatre, and battles from people's and units' infoboxes; entities those infoboxes name
  that the graph lacks are added.
- **6**: types from the class table where a record disagrees (old type in `attributes.ner_type`), held to
  the agent's window and the theatre; points for new places; unit and ship names on this build's records.

Every stage's graph can be written with `--keep-stages` (`graph.stage-<name>.json`), which the
impact report (`graph_enrichment.impact`) compares. No LLM, no query-time network: the agent
(`geo_agent`) only loads the result. Everything this layer adds says so: nodes and sources carry
`provenance`, nodes `attributes.selffix`, edges `selffix_step`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graph_enrichment import classes as cl
from graph_enrichment import enrich as wd
from graph_enrichment import infobox as ib
from graph_enrichment import wikipedia as wp
from graph_enrichment.impact import in_asia_pacific

TOOL_VERSION = "0.3.0"
LICENSE = "CC BY-SA 4.0 (English Wikipedia)"

MIN_LINKS = 3  # corpus link occurrences an entity needs to be added (step 3)
MIN_ALIAS_LINKS = 2  # a link text used this often becomes an alias
WARS = {"Q184425": "Pacific War", "Q362": "World War II", "Q48740": "Second Sino-Japanese War"}
THEATRE_WARS = {"Q184425", "Q48740"}  # an event part of these is in the theatre; part of World War II alone is not
SCOPE_YEARS = (1938, 1948)
LAST_WAR_YEAR = 1945  # a unit or ship raised after the war can't have fought in it
AGENT_WINDOW = (1938, 1948)  # the agent's R84 window (geo_agent.graph_store.SCOPE_START, SCOPE_END), checked in a test
PART_OF_DEPTH = 3
# Words that may stand before a surname in a short-form mention ("Admiral Halsey"); any other
# capitalized word before it names someone or something else ("John Halsey", "USS Halsey").
TITLE_WORDS = {
    "Admiral", "Vice", "Rear", "Fleet", "General", "Lieutenant", "Major", "Colonel", "Brigadier", "Captain",
    "Commander", "Commodore", "Marshal", "Field", "Air", "Chief", "Sergeant", "Corporal", "Private", "Ensign",
    "Sir", "Mr", "Mr.", "Mrs", "Dr", "Dr.", "President", "Premier", "Minister", "Prime", "Prince", "Emperor",
    "Generalissimo", "Gen.", "Adm.", "Lt.", "Col.", "Maj.", "Capt.", "Cmdr.", "Lt", "Col", "Maj", "Capt",
}
# English words that open a sentence before a surname ("Later Halsey ..."); grammar, not names.
OPENING_WORDS = {
    "later", "then", "when", "after", "before", "meanwhile", "however", "thus", "while", "although", "though",
    "on", "in", "at", "by", "with", "under", "as", "once", "since", "so", "but", "and", "yet", "there", "here",
    "eventually", "finally", "instead", "subsequently", "afterwards", "nevertheless", "consequently", "soon",
}


# ----------------------------------------------------------------- corpus


def corpus_revisions(graph: dict[str, Any]) -> dict[int, int]:
    """{processing revision id: page id} of every source record."""
    out = {}
    for n in graph.get("nodes") or []:
        for s in n.get("sources") or []:
            rev, page = s.get("processing_revision_id"), s.get("page_id")
            if isinstance(rev, int) and isinstance(page, int):
                out[rev] = page
    return out


@dataclass
class Paragraph:
    page_id: int
    revid: int
    title: str
    start: int
    end: int
    text: str          # raw wikitext of the paragraph
    mentions: int = 0  # graph source records anchored in it
    clean_texts: Counter = field(default_factory=Counter)  # the team's cleaned text of it, by use

    @property
    def clean_text(self) -> str | None:
        return self.clean_texts.most_common(1)[0][0] if self.clean_texts else None


def corpus_paragraphs(graph: dict[str, Any], revisions: dict[int, dict[str, Any]]) -> tuple[list[Paragraph], dict[str, int]]:
    """The raw wikitext paragraphs that hold at least one graph mention, one per (revision, span),
    and alignment counts: a source counts as aligned when its revision's text at `raw_anchor_span`
    is its anchor text."""
    paras: dict[tuple[int, int, int], Paragraph] = {}
    stats = Counter()
    for n in graph.get("nodes") or []:
        for s in n.get("sources") or []:
            stats["sources"] += 1
            rev = revisions.get(s.get("processing_revision_id"))
            pos = s.get("position") or {}
            span = pos.get("raw_anchor_span")
            if not rev or rev.get("missing") or not (isinstance(span, list) and len(span) == 2):
                stats["sources_without_revision_or_span"] += 1
                continue
            text = rev["wikitext"]
            a, b = span
            if text[a:b] != (pos.get("anchor_text") or ""):
                stats["sources_span_mismatch"] += 1
                continue
            stats["sources_aligned"] += 1
            start, end = wp.paragraph_bounds(text, a)
            key = (rev["revid"], start, end)
            if key not in paras:
                paras[key] = Paragraph(rev["page_id"], rev["revid"], rev["title"], start, end, text[start:end])
            paras[key].mentions += 1
            if s.get("text"):
                paras[key].clean_texts[s["text"]] += 1
    stats["paragraphs"] = len(paras)
    return sorted(paras.values(), key=lambda p: (p.revid, p.start)), dict(stats)


@dataclass
class LinkOccurrence:
    title: str
    text: str
    page_id: int
    revid: int
    page_title: str
    paragraph_start: int
    start: int
    end: int


REF_RE = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)


def non_prose_spans(text: str) -> list[tuple[int, int]]:
    """Spans of a wikitext paragraph that are not prose: citations (`<ref>...</ref>`) and templates
    (`{{...}}`, nested). A link there names a source or a template argument, not a mention."""
    spans = [(m.start(), m.end()) for m in REF_RE.finditer(text)]
    depth, start, i = 0, 0, 0
    while i < len(text) - 1:
        if text.startswith("{{", i):
            if depth == 0:
                start = i
            depth, i = depth + 1, i + 2
        elif text.startswith("}}", i) and depth:
            depth, i = depth - 1, i + 2
            if depth == 0:
                spans.append((start, i))
        else:
            i += 1
    if depth:
        spans.append((start, len(text)))
    return spans


def corpus_links(paragraphs: list[Paragraph]) -> list[LinkOccurrence]:
    """Every article wikilink in the prose of the paragraphs (not in citations or templates), with its
    page, revision and offsets in the revision."""
    out = []
    for p in paragraphs:
        skip = non_prose_spans(p.text)
        for link in wp.parse_wikilinks(p.text, offset=p.start):
            a, b = link.start - p.start, link.end - p.start
            if any(s <= a and b <= e for s, e in skip):
                continue
            out.append(LinkOccurrence(link.title, link.text, p.page_id, p.revid, p.title, p.start, link.start, link.end))
    return out


def graph_qids(graph: dict[str, Any]) -> set[str]:
    return set(wd.collect_qids(graph))


@dataclass
class Ctx:
    wikidata_cache: Path
    wikipedia_cache: Path
    cache: Path
    offline: bool = False
    out_dir: Path | None = None


@dataclass
class Corpus:
    paragraphs: list[Paragraph]
    alignment: dict[str, int]
    links: list[LinkOccurrence]
    res: dict[str, wp.Resolution]
    lite: dict[str, dict[str, Any]]
    p31_disambiguation: int


def load_corpus(graph: dict[str, Any], ctx: Ctx) -> Corpus:
    revisions = wp.fetch_revisions(corpus_revisions(graph), ctx.wikipedia_cache, ctx.offline)
    paragraphs, align = corpus_paragraphs(graph, revisions)
    links = corpus_links(paragraphs)
    res = wp.TitleResolver(ctx.wikipedia_cache, ctx.offline).resolve(sorted({l.title for l in links}))
    lite = wp.fetch_lite({r.qid for r in res.values() if r.qid}, ctx.cache, ctx.offline)
    n = wp.apply_instance_of(res, lite)
    return Corpus(paragraphs, align, links, res, lite, n)


# ----------------------------------------------------------------- step 1


def step1_resolve(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Resolve every corpus link title; return the step's report (the graph is not changed)."""
    c = load_corpus(graph, ctx)
    res, links = c.res, c.links
    in_graph = graph_qids(graph)
    occ_by_title = Counter(l.title for l in links)
    status_occ = Counter()
    occ_by_qid = Counter()
    for t, n in occ_by_title.items():
        status_occ[res[t].status] += n
        if res[t].qid:
            occ_by_qid[res[t].qid] += n
    qids = set(occ_by_qid)
    return {
        "revisions": len({p.revid for p in c.paragraphs}),
        "alignment": c.alignment, "link_occurrences": len(links), "distinct_titles": len(res),
        "titles_by_status": dict(Counter(r.status for r in res.values())), "occurrences_by_status": dict(status_occ),
        "titles_redirected": sum(1 for r in res.values() if r.redirected),
        "titles_redirected_ok": sum(1 for r in res.values() if r.redirected and r.status == "ok"),
        "disambiguation_by_p31_only": c.p31_disambiguation,
        "distinct_qids": len(qids), "qids_in_graph": len(qids & in_graph), "qids_not_in_graph": len(qids - in_graph),
        "occurrences_to_qids_in_graph": sum(n for q, n in occ_by_qid.items() if q in in_graph),
        "occurrences_to_qids_not_in_graph": sum(n for q, n in occ_by_qid.items() if q not in in_graph),
        "graph_qids": len(in_graph),
    }


# ----------------------------------------------------------------- step 3: scope


def _years(claims: dict[str, Any]) -> tuple[int | None, int | None]:
    """(first, last) year of an item from P580/P582/P585 (and P571/P576 when those are absent)."""
    dates, _ = wd.entity_dates(claims)
    ys = [int(t[:5].lstrip("+")) for t, _ in dates.values()]
    if not ys:
        for prop in ("P571", "P576"):
            ys += [int(v[0][:5].lstrip("+")) for c in wd._statements(claims, prop) if (v := wd._time(c["mainsnak"]))]
    return (min(ys), max(ys)) if ys else (None, None)


def _overlaps(first: int | None, last: int | None) -> bool:
    return first is not None and not (last < SCOPE_YEARS[0] or first > SCOPE_YEARS[1])


def part_of_war(qid: str, lite: dict[str, dict[str, Any]], depth: int = PART_OF_DEPTH, wars=WARS) -> str | None:
    """The war (in `wars`) reached from `qid` through P361 in at most `depth` steps, else None."""
    frontier, seen = [qid], {qid}
    for _ in range(depth):
        nxt = []
        for q in frontier:
            for p in cl.item_ids((lite.get(q) or {}).get("claims") or {}, "P361"):
                if p in wars:
                    return p
                if p not in seen:
                    seen.add(p)
                    nxt.append(p)
        frontier = nxt
    return None


def event_point(claims: dict[str, Any], lite: dict[str, dict[str, Any]]) -> tuple[float, float] | None:
    """(lon, lat) of an event: its own P625, else the first P276 (location) value with one, else the
    first P17 (country) value with one. Only used to tell the theatre, never written as geometry."""
    if (pt := wd.wikidata_point(claims)):
        return tuple(pt["coordinates"])
    for prop in ("P276", "P17"):
        for loc in cl.item_ids(claims, prop):
            if (pt := wd.wikidata_point((lite.get(loc) or {}).get("claims") or {})):
                return tuple(pt["coordinates"])
    return None


def conflict_in_scope(target: str, lite: dict[str, dict[str, Any]]) -> bool:
    """A P607 (conflict) value in scope: one of WARS, an event dated within 1938-1948, or part of a war."""
    if target in WARS:
        return True
    claims = (lite.get(target) or {}).get("claims") or {}
    return _overlaps(*_years(claims)) or part_of_war(target, lite) is not None


def in_theatre(q: str, lite: dict[str, dict[str, Any]]) -> bool:
    """A conflict in the theatre: the Pacific War or the Second Sino-Japanese War, part of one (P361, up to
    PART_OF_DEPTH steps), or an event of 1938-1948 (or of World War II) located in the Asia-Pacific box."""
    if q in THEATRE_WARS or part_of_war(q, lite, PART_OF_DEPTH, THEATRE_WARS):
        return True
    claims = (lite.get(q) or {}).get("claims") or {}
    where = event_point(claims, lite)
    return where is not None and in_asia_pacific(*where) and (_overlaps(*_years(claims)) or part_of_war(q, lite) is not None)


INFOBOX_ACTORS = ("Infobox military person", "Infobox military unit", "Infobox military person/sandbox")


def event_index(graph: dict[str, Any]) -> dict[str, list[tuple[str, tuple[int | None, int | None]]]]:
    """{normalized name or alias: [(QID, years)]} of the graph's events (records typed event or with a kind)."""
    from geo_agent.graph_store import normalize_name

    out: dict[str, dict[str, tuple[int | None, int | None]]] = defaultdict(dict)
    for n in graph.get("nodes") or []:
        q = n.get("wikidata_id")
        if isinstance(q, str) and _record_class(n) == "event":
            for name in [n.get("name"), *(n.get("aliases") or [])]:
                if isinstance(name, str) and name.strip():
                    years = _record_years(n)
                    prev = out[normalize_name(name)].get(q)
                    out[normalize_name(name)][q] = years if prev is None or prev[0] is None else prev
    return {k: list(v.items()) for k, v in out.items()}


def match_text_battles(items: list[tuple[str, tuple[int, int] | None]], index: dict[str, list]) -> list[str]:
    """QIDs of the graph events a plain-text battle item names: the exact name or an alias (normalized,
    never fuzzy), and, when the item states years, an event whose years overlap them."""
    from geo_agent.graph_store import normalize_name

    out = []
    for text, years in items:
        hits = [q for q, ys in index.get(normalize_name(text), [])
                if years is None or ys[0] is None or not (ys[1] < years[0] or years[1] < ys[0])]
        if len(hits) == 1:  # "Battle of Bataan" with no year names the 1942 or the 1945 battle: neither is linked
            out += hits
    return list(dict.fromkeys(out))


def infobox_battles_of(qids: list[str], full: dict[str, Any], lite: dict[str, dict[str, Any]], c: "Corpus", ctx: "Ctx",
                       events: dict[str, list] | None = None) -> dict[str, list[str]]:
    """{qid: QIDs of the battles in its own enwiki page's infobox} for people and units whose P607 names
    no conflict of the theatre: the `battles` field's links, and its plain-text items that name a graph
    event exactly (`events`, from event_index). The page is read at the revision the title resolution
    saw (`lastrevid`, cached); its links resolve like the corpus's, and their claims land in `lite`."""
    need = [q for q in qids if not any(in_theatre(t, lite) for t in cl.item_ids((full.get(q) or {}).get("claims") or {}, "P607"))]
    revid_of = {}
    for r in c.res.values():
        if r.qid in need and r.status == "ok" and r.lastrevid and not r.redirected:
            revid_of.setdefault(r.qid, r.lastrevid)
    for r in c.res.values():  # a page reached only through a redirect still has its own revision
        if r.qid in need and r.status == "ok" and r.lastrevid:
            revid_of.setdefault(r.qid, r.lastrevid)
    revs = wp.fetch_revisions(revid_of.values(), ctx.wikipedia_cache, ctx.offline)
    titles_of: dict[str, list[str]] = {}
    texts_of: dict[str, list[str]] = {}
    for q, rid in revid_of.items():
        rev = revs.get(rid) or {}
        box = wp.find_infobox(rev.get("wikitext") or "", INFOBOX_ACTORS) if not rev.get("missing") else None
        value = (box[1].get("battles") or ("", 0))[0] if box else ""
        titles_of[q], items = ib.battle_items(value)
        texts_of[q] = match_text_battles(items, events or {})
    res = wp.TitleResolver(ctx.wikipedia_cache, ctx.offline).resolve(sorted({t for ts in titles_of.values() for t in ts}))
    wp.apply_instance_of(res, lite)
    out = {q: list(dict.fromkeys([res[t].qid for t in ts if res[t].qid] + texts_of[q])) for q, ts in titles_of.items()}
    targets = {t for ts in out.values() for t in ts}
    for _ in range(PART_OF_DEPTH + 1):  # the battles, their part-of chains and their locations
        new = {t for t in targets if t not in lite}
        if new:
            lite.update(wp.fetch_lite(new, ctx.cache, ctx.offline))
        targets = {p for t in targets for prop in ("P361", "P276", "P17")
                   for p in cl.item_ids((lite.get(t) or {}).get("claims") or {}, prop)} - set(lite)
        if not targets:
            break
    return out


def in_scope(cls: str, claims: dict[str, Any], lite: dict[str, dict[str, Any]],
             infobox_battles: list[str] | None = None) -> tuple[bool, str]:
    """(kept, why) for an item of class `cls` (graph_enrichment.classes) from its full claims.
    `infobox_battles`: the QIDs its own enwiki infobox lists under `battles` (people and units)."""
    conflicts = [t for t in cl.item_ids(claims, "P607") if conflict_in_scope(t, lite)]
    if cls == "event":
        first, last = _years(claims)
        war = theatre = None
        for p in cl.item_ids(claims, "P361"):
            war = war or (p if p in WARS else part_of_war(p, lite, PART_OF_DEPTH - 1))
            theatre = p if p in THEATRE_WARS else part_of_war(p, lite, PART_OF_DEPTH - 1, THEATRE_WARS)
            if theatre:
                break
        where = event_point(claims, lite)
        if first is not None and not _overlaps(first, last):
            return False, f"dated {first}-{last}"
        if first is None and not war:
            return False, "event without dates or part of a war"
        when = f"dated {first}-{last}" if first is not None else f"undated, part of {WARS[war]}"
        if theatre:
            return True, (f"dated {first}-{last}, part of {WARS[theatre]}" if first is not None else f"undated, part of {WARS[theatre]}")
        if where is not None and in_asia_pacific(*where):
            return True, f"{when}, located {where[1]:.2f}, {where[0]:.2f}"
        return False, f"{when}, outside the theatre" + (f" (located {where[1]:.2f}, {where[0]:.2f})" if where else " (no location)")
    if cls in ("person", "unit"):
        # the theatre, not only the war: "World War II" or a service period also fits the Twelfth Air Force
        theatre = [t for t in cl.item_ids(claims, "P607") if in_theatre(t, lite)]
        if theatre:
            return True, f"fought in {theatre[0]} (P607)"
        boxed = [t for t in infobox_battles or [] if in_theatre(t, lite)]
        if boxed:
            return True, f"fought in {boxed[0]} (infobox battles)"
        seen = "P607 " + ", ".join(cl.item_ids(claims, "P607")[:3]) if cl.item_ids(claims, "P607") else "no P607"
        return False, f"{cls} without a conflict in the theatre ({seen}; infobox battles {len(infobox_battles or [])} links)"
    if cls == "ship" and not any(wd._statements(claims, p) for p in ("P571", "P729", "P730", "P793", "P576")):
        return False, "ship without a dated life (no inception, service or events): a class, not one ship"
    if cls == "ship":
        if conflicts:
            return True, f"fought in {conflicts[0]}"
        first = [int(v[0][:5].lstrip("+")) for c in wd._statements(claims, "P571") if (v := wd._time(c["mainsnak"]))]
        end = [int(v[0][:5].lstrip("+")) for c in wd._statements(claims, "P576") if (v := wd._time(c["mainsnak"]))]
        if first:
            s, e = min(first), max(end) if end else None
            ok = s <= LAST_WAR_YEAR and (e is None or e >= SCOPE_YEARS[0])
            return ok, f"in service {s}-{e or 'on'}"
        return False, f"{cls} without a conflict in scope or a service period"
    if cls == "place":
        pt = wd.wikidata_point(claims)
        if pt is None:
            return (True, "country or region without a point") if wd.entity_class(claims) in ("country", "region") \
                else (False, "place without coordinates")
        lon, lat = pt["coordinates"]
        return (in_asia_pacific(lon, lat), f"point {lat:.2f}, {lon:.2f}")
    return False, f"class {cls}"


# ----------------------------------------------------------------- step 3: short forms


def _word_re(word: str) -> re.Pattern:
    return re.compile(r"(?<![\w\-])" + re.escape(word) + r"(?![\w\-])")


def _intervals_contain(intervals: list[tuple[int, int]], a: int, b: int) -> bool:
    return any(s < b and a < e for s, e in intervals)


def short_form_mentions(person: str, surname: str, revid: int, paragraphs: list[Paragraph], links: list[LinkOccurrence],
                        qid_of_title: dict[str, str | None], names_of: dict[str, list[str]],
                        team_spans: list[tuple[int, int]], team_names: list[str], graph_names: set[str]) -> list[tuple[Paragraph, int, int]]:
    """Plain-text mentions of `surname` that stand for `person` on one page revision: after the first
    link to the person, outside links and the team's own mentions, not preceded by a capitalized word
    other than a title ("Admiral Halsey" yes, "John Halsey" or "USS Halsey" no), not followed by one
    ("Halsey Field"). None when the short form is ambiguous on the page: another entity linked there
    (any class) or mentioned by the team there has the word in its name or link text, or an entity
    of the graph is named by the word itself."""
    from geo_agent.graph_store import normalize_name

    page_links = [l for l in links if l.revid == revid]
    own = [l for l in page_links if qid_of_title.get(l.title) == person]
    if not own or len(surname) < 3 or not surname[0].isupper() or normalize_name(surname) in graph_names:
        return []
    word = _word_re(surname)
    for l in page_links:
        q = qid_of_title.get(l.title)
        if q != person and (word.search(l.text) or word.search(l.title) or any(word.search(n) for n in names_of.get(q or "", []))):
            return []
    if any(word.search(n) for n in team_names):
        return []
    first = min(l.start for l in own)
    link_spans = [(l.start, l.end) for l in page_links]
    out = []
    for p in paragraphs:
        if p.revid != revid or p.end <= first:
            continue
        for m in word.finditer(p.text):
            a, b = p.start + m.start(), p.start + m.end()
            if a <= first or _intervals_contain(link_spans, a, b) or _intervals_contain(team_spans, a, b):
                continue
            head = p.text[:m.start()]
            prev_m = re.search(r"([\w.\-']+)\s*$", head)
            after = re.match(r"\s+([\w\-']+)", p.text[m.end():m.end() + 40])
            if prev_m and head[prev_m.end(1):].strip() == "":
                prev = prev_m.group(1)
                before_prev = head[:prev_m.start(1)].rstrip()
                prev_opens_sentence = not before_prev or before_prev[-1] in ".!?\n"
                # a capitalized word right before the surname names someone else ("John Halsey",
                # "USS Halsey"), unless it is a title or an ordinary word opening the sentence ("Later Halsey ...")
                if prev[0].isupper() and prev not in TITLE_WORDS and not (prev_opens_sentence and prev.lower() in OPENING_WORDS):
                    continue
            if after and after.group(1)[0].isupper():
                continue
            out.append((p, a, b))
    return out


# ----------------------------------------------------------------- step 3: records


MAX_POINT_KM = 500  # an event's own point this far from every one of its stated locations is distrusted


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return 6371 * math.acos(max(-1.0, min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl))))


def check_event_points(events: list[dict[str, Any]], full: dict[str, Any], lite: dict[str, dict[str, Any]],
                       parents=lambda q: []) -> list[list[Any]]:
    """Drop the P625 point the enrichment gave a new event when it lies more than MAX_POINT_KM from every
    point-like P276 location the event states (the Makin Island raid's P625 says 172.8 W, its location,
    the atoll of Butaritari, is at 172.8 E); the event is then placed through its location relation.
    Seas, regions and archipelagos are not compared (graph_enrichment.classes.is_point_like): the sinking
    of Prince of Wales and Repulse is rightly 1,300 km from the South China Sea's centre point.
    Returns [qid, name, km] per drop."""
    out = []
    for n in events:
        if not n.get("geometry") or (n.get("geolocation_evidence") or {}).get("source_kind") != "WIKIDATA_P625_ENRICHMENT":
            continue
        claims = (full.get(n["wikidata_id"]) or {}).get("claims") or {}
        locs = [tuple(pt["coordinates"]) for loc in cl.item_ids(claims, "P276")
                if cl.is_point_like((lite.get(loc) or {}).get("claims") or {}, parents)
                and (pt := wd.wikidata_point((lite.get(loc) or {}).get("claims") or {}))]
        if not locs:
            continue
        own = tuple(n["geometry"]["coordinates"])
        km = min(_km(own, l) for l in locs)
        if km > MAX_POINT_KM:
            n.pop("geometry")
            ev = n.pop("geolocation_evidence")
            n["attributes"]["selffix"]["point_dropped"] = {"statement_id": ev.get("statement_id"), "coordinates": list(own),
                                                           "km_from_location": round(km)}
            out.append([n["wikidata_id"], n["name"], round(km)])
    return out


def _occurrence_id(*parts: Any) -> str:
    return "selffix-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def link_source(p: Paragraph, a: int, b: int, anchor: str, title: str, kind: str, of: str | None = None) -> dict[str, Any]:
    """A source record for one mention in a corpus paragraph, in the graph's own source form. Its text
    is the team's cleaned paragraph that holds the anchor (a raw block can hold several of them),
    else the raw paragraph cleaned the way the agent cleans wikitext (spec R82)."""
    from geo_agent.graph_store import clean_wikitext

    holding = [t for t, _ in p.clean_texts.most_common() if anchor in t]
    text, basis = (holding[0], "TEAM_PARAGRAPH_TEXT") if holding else (clean_wikitext(p.text) or p.text, "CLEANED_RAW_WIKITEXT")
    return {
        "provenance": "wikipedia_link", "license": LICENSE,
        "occurrence_id": _occurrence_id(p.revid, a, b, kind),
        "page": f"enwiki:{p.page_id}", "page_id": p.page_id, "page_title": p.title, "wiki_site": "enwiki",
        "processing_revision_id": p.revid, "processing_revision_url": f"https://en.wikipedia.org/w/index.php?oldid={p.revid}",
        "url": f"https://en.wikipedia.org/?curid={p.page_id}",
        "text": text, "text_basis": basis,
        "position": {"anchor_text": anchor, "raw_anchor_span": [a, b], "mention_kind": kind, "link_title": title,
                     **({"short_form_of": of} if of else {})},
    }


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def link_aliases(label: str, links: list[LinkOccurrence], res: dict[str, wp.Resolution], wikidata_aliases: list[str],
                 names: dict[str, set[str]], qid: str) -> list[str]:
    """The link texts of an entity that name it, as aliases: used at least MIN_ALIAS_LINKS times, a proper
    name (capitalized), not a fragment of the label ("Arawe" for the Battle of Arawe, "Thirteenth"), not
    the name of another entity of the graph, and, when every use comes through a redirect, an English
    Wikidata alias too ("200th Division" redirects to the 5th Corps page but is not the 5th Corps)."""
    from geo_agent.graph_store import normalize_name

    known = {normalize_name(a) for a in [label, *wikidata_aliases]}
    uses: dict[str, list[bool]] = defaultdict(list)
    for l in links:
        uses[l.text].append(res[l.title].redirected)
    out = []
    for t, redirected in sorted(uses.items(), key=lambda kv: -len(kv[1])):
        k = normalize_name(t)
        if len(redirected) < MIN_ALIAS_LINKS or not t[:1] or t[:1].islower() or k == normalize_name(label):
            continue
        if _words(t) <= _words(label) or names.get(k, set()) - {qid} or (all(redirected) and k not in known):
            continue
        out.append(t)
    return out


def name_index(graph: dict[str, Any]) -> dict[str, set[str]]:
    """{normalized name or alias: QIDs} over the graph's records."""
    from geo_agent.graph_store import normalize_name

    out: dict[str, set[str]] = defaultdict(set)
    for n in graph.get("nodes") or []:
        q = n.get("wikidata_id")
        for name in [n.get("name"), *(n.get("aliases") or [])]:
            if isinstance(name, str) and name.strip() and isinstance(q, str):
                out[normalize_name(name)].add(q)
    return out


def _record_class(n: dict[str, Any]) -> str:
    """The coarse class of a graph record, comparable with graph_enrichment.classes."""
    attrs = n.get("attributes") or {}
    if attrs.get("entity_class") == "ship":
        return "ship"
    if attrs.get("kind"):  # a military-event kind (R74): the team types many battles as locations
        return "event"
    return {"person": "person", "org": "unit", "location": "place", "event": "event"}.get(n.get("type"), "other")


def _record_years(n: dict[str, Any]) -> tuple[int | None, int | None]:
    attrs = n.get("attributes") or {}
    ys = [int(str(attrs[k])[:5].lstrip("+")) for k in ("start_date", "end_date", "point_date")
          if attrs.get(k) and str(attrs[k])[:5].lstrip("+").lstrip("-").isdigit()]
    return (min(ys), max(ys)) if ys else (None, None)


def same_event_time(a: tuple[int | None, int | None], b: tuple[int | None, int | None]) -> bool:
    """Two same-name events may be one only when their years overlap or one is undated: the battles of
    Changsha in 1939, 1942 and 1944 are three events (R92)."""
    if a[0] is None or b[0] is None:
        return True
    return not (a[1] < b[0] or b[1] < a[0])


def step3_add_entities(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Add the in-scope entities the corpus links at least MIN_LINKS times; return the step's report."""
    from geo_agent.graph_store import normalize_name

    c = load_corpus(graph, ctx)
    orig_qids = wd.collect_qids(graph)
    in_graph = set(orig_qids)
    qid_of_title = {t: r.qid for t, r in c.res.items()}
    occ: dict[str, list[LinkOccurrence]] = defaultdict(list)
    for l in c.links:
        q = qid_of_title.get(l.title)
        if q and q not in in_graph:
            occ[q].append(l)
    below = {q for q, ls in occ.items() if len(ls) < MIN_LINKS}
    cands = sorted((q for q in occ if q not in below), key=lambda q: (-len(occ[q]), q))
    lite = dict(c.lite)

    # the class of every candidate, walking P279 up from its P31 values
    parents: dict[str, list[str]] = {}
    p31 = {v for q in cands for v in cl.item_ids((lite.get(q) or {}).get("claims") or {}, "P31")}
    for _ in range(cl.MAX_DEPTH + 1):
        need = cl.classes_to_fetch(p31, parents)
        if not need:
            break
        got = wp.fetch_lite(need, ctx.cache, ctx.offline)
        for q in need:
            parents[q] = cl.item_ids((got.get(q) or {}).get("claims") or {}, "P279")
        lite.update(got)
    up = lambda q: parents.get(q, [])
    decided: dict[str, dict[str, Any]] = {}
    classed = []
    for q in cands:
        rec = lite.get(q) or {}
        if rec.get("missing"):
            decided[q] = {"kept": False, "reason": "no Wikidata item"}
            continue
        if rec.get("redirected_to") and rec["redirected_to"] in in_graph:
            decided[q] = {"kept": False, "reason": f"merged into {rec['redirected_to']}, in the graph"}
            continue
        cls, why = cl.classify(rec.get("claims") or {}, up)
        if cls is None:
            labels = [lite.get(v, {}).get("label") or v for v in cl.item_ids(rec.get("claims") or {}, "P31")]
            decided[q] = {"kept": False, "reason": f"{why}: P31 {', '.join(labels[:3]) or '-'}"}
            continue
        decided[q] = {"class": cls, "root": why}
        classed.append(q)

    # scope from the full claims; conflicts and part-of chains need their targets' claims
    full = wp.fetch_full_claims(classed, ctx.cache, ctx.offline)
    targets = {t for q in classed for p in ("P607", "P361", "P276", "P17") for t in cl.item_ids((full.get(q) or {}).get("claims") or {}, p)}
    for _ in range(PART_OF_DEPTH):
        new = {t for t in targets if t not in lite}
        if not new:
            break
        lite.update(wp.fetch_lite(new, ctx.cache, ctx.offline))
        targets = {p for t in new for p in cl.item_ids((lite.get(t) or {}).get("claims") or {}, "P361")}
    battles = infobox_battles_of([q for q in classed if decided[q]["class"] in ("person", "unit")], full, lite, c, ctx,
                                 event_index(graph))
    for q in classed:
        claims = (full.get(q) or {}).get("claims") or {}
        kept, why = in_scope(decided[q]["class"], claims, lite, battles.get(q))
        decided[q].update(kept=kept, reason=why)

    # duplicates by name and alias against the graph's records (supervisor check a)
    names = name_index(graph)
    rec_class: dict[str, str] = {}
    rec_years: dict[str, tuple[int | None, int | None]] = {}
    for n in graph.get("nodes") or []:
        if isinstance(n.get("wikidata_id"), str):
            q0, k = n["wikidata_id"], _record_class(n)
            if q0 not in rec_class or k == "event":  # the first record decides, unless another has an event kind
                rec_class[q0] = k
            if _record_years(n)[0] is not None:
                rec_years[q0] = _record_years(n)
    held = {}
    new_names: dict[str, set[str]] = defaultdict(set)
    for q in [q for q in classed if decided[q]["kept"]]:
        label = lite[q].get("label") or Counter(l.text for l in occ[q]).most_common(1)[0][0]
        aliases = link_aliases(label, occ[q], c.res, lite[q].get("aliases") or [], names, q)
        decided[q].update(label=label, aliases=aliases)
        # the duplicate check reads every name the corpus uses for it (link texts used twice or more),
        # not only the aliases kept: "Truk" for Chuuk Lagoon finds a graph record already called Truk
        used = Counter(l.text for l in occ[q])
        keys = list(dict.fromkeys(normalize_name(nm) for nm in [label, *aliases, *(t for t, n in used.items()
                                                                                    if n >= MIN_ALIAS_LINKS and t[:1] and not t[:1].islower())]))
        clash = sorted({o for k in keys for o in names.get(k, ()) if o != q})
        mine = _years((full.get(q) or {}).get("claims") or {})
        same = [o for o in clash if rec_class.get(o) == decided[q]["class"]
                and (decided[q]["class"] != "event" or same_event_time(mine, rec_years.get(o, (None, None))))]
        # an earlier (more linked) new entity of the same class with this name: the same check among additions
        same += sorted({o for k in keys for o in new_names.get(k, ()) if decided[o]["class"] == decided[q]["class"]})
        if not same:
            for k in keys:
                new_names[k].add(q)
        if same:
            decided[q].update(kept=False, reason=f"held: same name as {', '.join(same)} of the same class (possible duplicate)")
            held[q] = same
        elif clash:
            decided[q]["name_shared_with"] = clash

    added = [q for q in classed if decided[q]["kept"]]
    # short forms of added people (supervisor check b: only people, never another class's mention)
    family = {q: cl.item_ids((full.get(q) or {}).get("claims") or {}, "P734") for q in added if decided[q]["class"] == "person"}
    fam_lite = wp.fetch_lite({f for fs in family.values() for f in fs}, ctx.cache, ctx.offline)
    names_of = defaultdict(list)
    for q, rec in lite.items():
        if rec.get("label"):
            names_of[q].append(rec["label"])
    for q in added:
        names_of[q] = [decided[q]["label"], *decided[q]["aliases"]]
    team_spans: dict[int, list[tuple[int, int]]] = defaultdict(list)
    team_names: dict[int, list[str]] = defaultdict(list)
    for n in graph.get("nodes") or []:
        for s in n.get("sources") or []:
            span = (s.get("position") or {}).get("raw_anchor_span")
            rev = s.get("processing_revision_id")
            if isinstance(span, list) and len(span) == 2 and isinstance(rev, int):
                team_spans[rev].append(tuple(span))
                team_names[rev] += [n.get("name") or "", (s.get("position") or {}).get("anchor_text") or ""]
    graph_names = set(names)

    para_at = {(pp.revid, pp.start): pp for pp in c.paragraphs}
    nodes = graph.setdefault("nodes", [])
    by_class = Counter()
    short_total = 0
    for q in added:
        d = decided[q]
        cls = d["class"]
        sources = []
        for l in occ[q]:
            p = para_at[(l.revid, l.paragraph_start)]
            sources.append(link_source(p, l.start, l.end, l.text, l.title, "wikilink"))
        shorts = []
        for fam in family.get(q, []):
            surname = re.sub(r"\s*\(.*\)$", "", (fam_lite.get(fam) or {}).get("label") or "")
            for rev in sorted({l.revid for l in occ[q]}):
                for p, a, b in short_form_mentions(q, surname, rev, c.paragraphs, c.links, qid_of_title, names_of,
                                                   team_spans[rev], team_names[rev], graph_names):
                    shorts.append(link_source(p, a, b, surname, occ[q][0].title, "short_form", of=q))
        short_total += len(shorts)
        claims = (full.get(q) or {}).get("claims") or {}
        node: dict[str, Any] = {
            "id": f"{q}::selffix", "wikidata_id": q, "name": d["label"], "aliases": d["aliases"],
            "type": cl.NODE_TYPE[cls], "provenance": "wikipedia_link", "sources": sources + shorts,
            "attributes": {
                "wikidata_type": cls, "name_source": "wikidata_label" if lite[q].get("label") else "anchor_text",
                "selffix": {"step": 3, "rule": "corpus links", "link_occurrences": len(occ[q]), "short_form_mentions": len(shorts),
                            "class_root": d["root"], "scope": d["reason"], "wikidata_lastrevid": (full.get(q) or {}).get("lastrevid"),
                            **({"name_shared_with": d["name_shared_with"]} if d.get("name_shared_with") else {})},
                # a ship of the class table is a ship even when R74's short ship list lacks its class (a sloop)
                **({"entity_class": "ship"} if cls == "ship" else {}),
            },
        }
        if cls == "place" and wd.entity_class(claims) not in wd.NO_POINT_CLASSES and (pt := wd.wikidata_point(claims)):
            node["geometry"] = {"type": "Point", "coordinates": pt["coordinates"]}
            node["geolocation_evidence"] = {"source_kind": "WIKIDATA_P625_ENRICHMENT", "record_id": q, "crs": "EPSG:4326",
                                            "statement_id": pt["statement_id"], "coordinate_precision_degrees": pt["precision"]}
        nodes.append(node)
        by_class[cls] += 1

    # the R74 enrichment over the new entities, so they get dates, kinds and typed relations
    edges_before = len(graph.get("edges") or [])
    old_claims, snapshot = wd.fetch_claims(orig_qids, ctx.wikidata_cache, ctx.offline)
    all_claims = {**old_claims, **{q: (full.get(q) or {}).get("claims") or {} for q in added}}
    saved = graph.get("metadata", {}).get("enrichment")
    wd.enrich(graph, all_claims, snapshot)
    second = graph["metadata"].pop("enrichment")
    if saved is not None:
        graph["metadata"]["enrichment"] = saved
    for e in graph["edges"][edges_before:]:
        e["selffix_step"] = 3
    new_events = [n for n in nodes if n.get("id", "").endswith("::selffix") and n.get("type") == "event"]
    loc_classes = {v for n in new_events for loc in cl.item_ids((full.get(n["wikidata_id"]) or {}).get("claims") or {}, "P276")
                   for v in cl.item_ids((lite.get(loc) or {}).get("claims") or {}, "P31")}
    for _ in range(cl.MAX_DEPTH + 1):  # the P279 chains of the locations' classes
        need = cl.classes_to_fetch(loc_classes, parents)
        if not need:
            break
        got = wp.fetch_lite(need, ctx.cache, ctx.offline)
        for q in need:
            parents[q] = cl.item_ids((got.get(q) or {}).get("claims") or {}, "P279")
    dropped_points = check_event_points(new_events, full, lite, up)

    rejected = [(q, decided[q]["reason"], len(occ[q])) for q in cands if not decided[q].get("kept")]
    report = {
        "min_links": MIN_LINKS, "qids_not_in_graph": len(occ), "below_min_links": len(below),
        "candidates": len(cands), "class_decided": len(classed), "added": len(added), "added_by_class": dict(by_class),
        "held_as_possible_duplicates": len(held), "short_form_mentions": short_total,
        "event_points_dropped": dropped_points,
        "link_sources_added": sum(len(occ[q]) for q in added),
        "edges_added_by_enrichment": second["counts"]["edges_added_by_type"],
        "dates_added": second["counts"]["dates_added"], "points_added_by_enrichment": second["counts"]["points_added"],
        "rejection_reasons": dict(Counter(re.sub(r":.*|\d{4}-.*|point .*", "", r) for _, r, _ in rejected).most_common()),
        "added_list": [[q, decided[q]["label"], decided[q]["class"], len(occ[q]), decided[q]["reason"]] for q in added],
        "rejected_list": [[q, (lite.get(q) or {}).get("label"), n, r] for q, r, n in rejected],
        "held_list": [[q, decided[q]["label"], same] for q, same in held.items()],
    }
    return report


# ----------------------------------------------------------------- step 5: infobox facts

ALLIANCE_CLASSES = {"Q1127126"}  # military alliance: "Allies of World War II" names a side
# A side can be a polity that is no "country" in the class table: a dominion, colony or protectorate
# (Dominion of New Zealand, British Solomon Islands), a government-in-exile or provisional government
# (Dutch government-in-exile, Azad Hind), a party (Kuomintang). Checked against Wikidata labels.
POLITY_ROOTS = {
    "Q6256",     # country
    "Q1048835",  # political territorial entity
    "Q7275",     # state
    "Q133156",   # colony
    "Q161243",   # dependent territory
    "Q7188",     # government
    "Q7278",     # political party
}
FORCES_ROOTS = {"Q772547", "Q8473"}  # armed forces, military: a side's army (National Revolutionary Army)
PROP_OF = {"combatant": "P710", "units": "P710", "commander": "P4791", "place": "P276"}


def pages_of(qids: list[str], lite: dict[str, dict[str, Any]], ctx: Ctx) -> dict[str, dict[str, Any]]:
    """{qid: revision record} of each item's enwiki page (its sitelink title), at the revision the title
    resolution sees, cached by revision id. A title that resolves to another item is left out."""
    titles = {q: lite[q]["enwiki"] for q in qids if (lite.get(q) or {}).get("enwiki")}
    res = wp.TitleResolver(ctx.wikipedia_cache, ctx.offline).resolve(sorted(set(titles.values())))
    revid = {q: res[t].lastrevid for q, t in titles.items() if res[t].page_qid == q and res[t].lastrevid}
    revs = wp.fetch_revisions(revid.values(), ctx.wikipedia_cache, ctx.offline)
    return {q: revs[r] for q, r in revid.items() if not revs[r].get("missing")}


def infobox_source(rev: dict[str, Any], field: str, side: int | None, m: "ib.Mention") -> dict[str, Any]:
    """A source record for one infobox mention (CC BY-SA attribution: page, revision, field)."""
    return {
        "provenance": "wikipedia_infobox", "license": LICENSE,
        "occurrence_id": _occurrence_id(rev["revid"], m.start, field, m.title),
        "page": f"enwiki:{rev['page_id']}", "page_id": rev["page_id"], "page_title": rev["title"], "wiki_site": "enwiki",
        "revision_id": rev["revid"], "revision_url": f"https://en.wikipedia.org/w/index.php?oldid={rev['revid']}",
        "url": f"https://en.wikipedia.org/?curid={rev['page_id']}",
        "text": f"{rev['title']}, infobox {field}" + (f" (side {side})" if side else "") + f": {m.text}",
        "text_basis": "INFOBOX_FIELD",
        "position": {"anchor_text": m.text, "raw_anchor_span": [m.start, m.start + len(m.text)], "field": field,
                     "mention_kind": m.how, **({"side": side} if side else {})},
    }


ORDINAL_WORDS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7",
                 "eighth": "8", "ninth": "9", "tenth": "10", "eleventh": "11", "twelfth": "12", "thirteenth": "13",
                 "fourteenth": "14", "fifteenth": "15", "sixteenth": "16", "seventeenth": "17", "eighteenth": "18",
                 "nineteenth": "19", "twentieth": "20"}
STOP_WORDS = {"the", "of", "and", "for", "jr", "sr"}


def _designation(text: str) -> tuple[set[str], set[str]]:
    """(unit numbers, other words of 3+ letters) of a name: "United States Third Fleet" -> ({3}, {united, states, fleet})."""
    from geo_agent.graph_store import normalize_name

    words = re.findall(r"\w+", normalize_name(text))
    nums = {re.sub(r"(st|nd|rd|th|d)$", "", w) for w in words if re.match(r"^\d+(st|nd|rd|th|d)?$", w)}  # "503d": US Army style
    nums |= {ORDINAL_WORDS[w] for w in words if w in ORDINAL_WORDS}
    rest = {w for w in words if len(w) >= 3 and w not in STOP_WORDS and w not in ORDINAL_WORDS and not w[0].isdigit()}
    return nums, rest


def names_the_target(m: "ib.Mention", target: str, lite: dict[str, dict[str, Any]]) -> bool:
    """Whether a mention reached through a redirect names its target and not a broader or other body.
    Yes when its link title (without the bracketed qualifier) or its text is the target's English label
    or a Wikidata alias, or when the title and the label carry the same unit numbers and share a word
    ("William Halsey, Jr." -> William Halsey Jr.; "IJA 8th Division" -> 8th Division). No when the numbers
    differ: "101st Coast Artillery Battalion" -> 108th Cavalry Regiment is another unit."""
    from geo_agent.graph_store import normalize_name

    rec = lite.get(target) or {}
    names = [x for x in [rec.get("label") or "", *(rec.get("aliases") or [])] if x]
    known = {normalize_name(x) for x in names}
    asked = re.sub(r"\s*\([^()]*\)\s*$", "", m.title)
    if {normalize_name(asked), normalize_name(m.text)} & known:
        return True
    nums, words = _designation(asked)
    for name in names:
        n2, w2 = _designation(name)
        if nums == n2 and words & w2:
            return True
    return False


def conflict_mentions(rev: dict[str, Any]) -> list[tuple[str, str, int | None, "ib.Mention"]]:
    """(field kind, field name, side, mention) of every fact candidate in a page's conflict infobox."""
    box = wp.find_infobox(rev.get("wikitext") or "", ib.CONFLICT_INFOBOXES)
    if not box:
        return []
    fields = box[1]
    out = []
    for kind, flags, cut in (("combatant", True, True), ("commander", False, False), ("units", False, True)):
        for key, (value, offset) in sorted(fields.items()):
            m = ib.SIDE_FIELD_RE.match(key)
            if m and m.group(1) == kind:
                out += [(kind, key, int(m.group(2)), x) for x in ib.mentions(value, offset, flags, cut)]
    for name in ("place", "location"):  # the template's parameter is `location`; older pages say `place`
        if name in fields:
            out += [("place", name, None, x) for x in ib.place_mentions(*fields[name])]
            break
    return out


def step5_infoboxes(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Sides, commanders, units and places from the battles' infoboxes, and battles from people's and
    units' infoboxes; return the step's report. Adds edges (and entities the infoboxes name that the
    graph lacks, in scope); changes no team record."""
    from geo_agent.graph_store import WAR_LEVEL_NAME_RE, normalize_name

    nodes, edges = graph.setdefault("nodes", []), graph.setdefault("edges", [])
    qids = wd.collect_qids(graph)
    in_graph = set(qids)
    rec_class: dict[str, str] = {}
    for n in nodes:
        q0 = n.get("wikidata_id")
        if isinstance(q0, str):
            k = _record_class(n)
            if q0 not in rec_class or k == "event":
                rec_class[q0] = k
    country_like = lambda q: (lite.get(q) or {}).get("claims") is not None and \
        wd.entity_class((lite.get(q) or {}).get("claims") or {}) in ("country", "region")
    lite = wp.fetch_lite(qids, ctx.cache, ctx.offline)
    events = [q for q in qids if rec_class.get(q) == "event"]
    actors = [q for q in qids if rec_class.get(q) in ("person", "unit") and not country_like(q)]
    pages = pages_of(events + actors, lite, ctx)
    stats = Counter()
    skipped: Counter = Counter()  # (field kind, reason, QID, label): what the infoboxes name that no rule takes
    redirects_skipped: Counter = Counter()
    event_point_in_box = {n["wikidata_id"] for n in nodes if n.get("wikidata_id") in set(events)
                          and (n.get("geometry") or {}).get("type") == "Point"
                          and in_asia_pacific(*n["geometry"]["coordinates"][:2])}
    not_theatre: Counter = Counter()  # battles whose new people/units were not added: not in the theatre
    seen = frontier = set(events)
    for _ in range(PART_OF_DEPTH + 1):  # the events' part-of chains and locations, for in_theatre
        frontier = {p for t in frontier for prop in ("P361", "P276", "P17")
                    for p in cl.item_ids((lite.get(t) or {}).get("claims") or {}, prop)} - seen
        if not frontier:
            break
        lite.update(wp.fetch_lite(frontier - set(lite), ctx.cache, ctx.offline))
        seen = seen | frontier

    # --- the battles' own infoboxes
    found = []
    event_years: dict[str, tuple[int | None, int | None]] = {}
    for n in nodes:
        q0, ys = n.get("wikidata_id"), _record_years(n)
        if isinstance(q0, str) and ys[0] is not None:
            prev = event_years.get(q0)
            event_years[q0] = ys if prev is None else (min(prev[0], ys[0]), max(prev[1], ys[1]))
    # an event dated outside the agent's window (R84) is never read: the Battle of Inchon (1950) is in the
    # theatre but not in the war; an undated event keeps the theatre rule alone
    in_window = lambda ev: ev not in event_years or not (event_years[ev][1] < AGENT_WINDOW[0] or event_years[ev][0] > AGENT_WINDOW[1])
    theatre_ok = lambda ev: in_window(ev) and (in_theatre(ev, lite) or ev in event_point_in_box)
    for ev in events:
        if ev not in pages:
            stats["events_without_page"] += 1
            continue
        if not in_window(ev):
            stats["events_outside_window"] += 1
            continue
        if not theatre_ok(ev):  # a battle of Europe or Africa would put its places on Pacific maps (Overlord)
            stats["events_outside_theatre"] += 1
            continue
        ms = conflict_mentions(pages[ev])
        stats["events_with_conflict_infobox" if ms else "events_without_conflict_infobox"] += 1
        found += [(ev, *x) for x in ms]
    res = wp.TitleResolver(ctx.wikipedia_cache, ctx.offline).resolve(sorted({m.title for *_, m in found}))
    targets = {r.qid for r in res.values() if r.qid}
    lite.update(wp.fetch_lite(targets - set(lite), ctx.cache, ctx.offline))
    wp.apply_instance_of(res, lite)
    parents: dict[str, list[str]] = {}
    p31 = {v for t in targets for v in cl.item_ids((lite.get(t) or {}).get("claims") or {}, "P31")}
    for _ in range(cl.MAX_DEPTH + 1):
        need = cl.classes_to_fetch(p31, parents)
        if not need:
            break
        got = wp.fetch_lite(need, ctx.cache, ctx.offline)
        for q in need:
            parents[q] = cl.item_ids((got.get(q) or {}).get("claims") or {}, "P279")
        lite.update(got)
    up = lambda q: parents.get(q, [])
    cls_of: dict[str, str | None] = {}
    for t in targets:
        claims = (lite.get(t) or {}).get("claims") or {}
        c0, _ = cl.classify(claims, up)
        anc = cl.ancestors(cl.item_ids(claims, "P31"), up)
        if wd.entity_class(claims) == "country" or (c0 in (None, "place") and POLITY_ROOTS & set(anc)):
            c0 = "country"  # a polity: a side, never a place
        elif set(cl.item_ids(claims, "P31")) & ALLIANCE_CLASSES:
            c0 = "alliance"
        elif c0 is None and FORCES_ROOTS & set(anc):
            c0 = "unit"
        cls_of[t] = c0
    allowed = {"combatant": {"country", "unit", "alliance", "ship"}, "commander": {"person"},
               "units": {"unit", "ship"}, "place": {"place"}}

    # existing assertions, reverse views counted as their base relation (as the R74 enrichment does)
    def key_of(e: dict[str, Any]) -> tuple[str, str, str]:
        s0, t0, typ = qid_of_ref(e.get("source")), qid_of_ref(e.get("target")), e.get("type")
        if isinstance(typ, str) and typ.startswith("inverse:"):
            s0, t0, typ = t0, s0, typ[len("inverse:"):]
        return s0, t0, typ
    qid_of_node = {n["id"]: n.get("wikidata_id") for n in nodes if n.get("wikidata_id")}
    qid_of_ref = lambda ref: qid_of_node.get(ref, ref)
    existing: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        existing[key_of(e)].append(e)

    # one fact per (event, target, relation); a target on two sides of one battle is dropped as unclear
    facts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ev, kind, field, side, m in found:
        r = res[m.title]
        t = r.qid
        if not t:
            stats[f"skip_{kind}_unresolved_{r.status}"] += 1
            continue
        if t == ev or cls_of.get(t) not in allowed[kind]:
            stats[f"skip_{kind}_class_{cls_of.get(t)}"] += 1
            skipped[(kind, f"class {cls_of.get(t)}", t, (lite.get(t) or {}).get("label") or m.title)] += 1
            continue
        if kind == "place" and (wd.entity_class((lite.get(t) or {}).get("claims") or {}) in ("country", "region")):
            stats["skip_place_country_or_region"] += 1
            continue
        if r.redirected and m.how == "link" and not names_the_target(m, t, lite):
            # "[[121st Infantry Regiment|...]]" redirects to the Georgia Army National Guard: a broader body
            stats[f"skip_{kind}_redirect_to_another_name"] += 1
            redirects_skipped[(kind, m.title, (lite.get(t) or {}).get("label"))] += 1
            continue
        prop = PROP_OF[kind]
        f = facts.setdefault((ev, t, f"wd:{prop}"), {"kind": kind, "sides": set(), "mentions": []})
        if side:
            f["sides"].add(side)
        f["mentions"].append((field, side, m))
    for k, f in list(facts.items()):
        if len(f["sides"]) > 1:
            stats[f"skip_{f['kind']}_on_two_sides"] += 1
            del facts[k]

    # entities the infoboxes name that the graph lacks: added when in scope (the battle is in the theatre)
    names = name_index(graph)
    new_entities: dict[str, dict[str, Any]] = {}
    for (ev, t, typ), f in sorted(facts.items()):
        if t in in_graph or t in new_entities:
            continue
        cls = cls_of[t]
        claims = (lite.get(t) or {}).get("claims") or {}
        # only a battle of the theatre brings in new people, units and places: by Wikidata, or by the
        # graph's own point for it (Wikidata gives Cape Torokina or Nassau Bay no theatre at all)
        ok = in_theatre(ev, lite) or (ev in event_point_in_box)
        if cls == "place":
            pt = wd.wikidata_point(claims)
            ok = ok and pt is not None and in_asia_pacific(*pt["coordinates"])
        label = (lite.get(t) or {}).get("label")
        if not ok or not label:
            stats[f"skip_new_{cls}_out_of_scope_or_unnamed"] += 1
            not_theatre[ev] += 1
            f["drop"] = True
            continue
        node_cls = {"country": "place", "alliance": "unit"}.get(cls, cls)
        clash = [o for o in names.get(normalize_name(label), ()) if o != t and rec_class.get(o) == node_cls]
        if clash:
            stats["held_new_possible_duplicate"] += 1
            f["drop"] = True
            continue
        new_entities[t] = {"cls": cls, "label": label, "facts": []}
    for (ev, t, typ), f in facts.items():
        if t in new_entities and not f.get("drop"):
            new_entities[t]["facts"].append((ev, f))
    for t, d in new_entities.items():
        cls, claims = d["cls"], (lite.get(t) or {}).get("claims") or {}
        node_type = {"country": "org", "alliance": "org"}.get(cls) or cl.NODE_TYPE[cls]  # a polity is never a place
        sources = [infobox_source(pages[ev], field, side, m) for ev, f in d["facts"] for field, side, m in f["mentions"]]
        node = {"id": f"{t}::selffix", "wikidata_id": t, "name": d["label"], "aliases": [], "type": node_type,
                "provenance": "wikipedia_infobox", "sources": sources,
                "attributes": {"wikidata_type": cls, "name_source": "wikidata_label",
                               **({"entity_class": "ship"} if cls == "ship" else {}),
                               **({"entity_class": "country"} if cls == "country" else {}),
                               "selffix": {"step": 5, "rule": "infobox", "infobox_mentions": len(sources),
                                           "battles": sorted({ev for ev, _ in d["facts"]}),
                                           "wikidata_lastrevid": (lite.get(t) or {}).get("lastrevid")}}}
        if cls == "place" and (pt := wd.wikidata_point(claims)):
            node["geometry"] = {"type": "Point", "coordinates": pt["coordinates"]}
            node["geolocation_evidence"] = {"source_kind": "WIKIDATA_P625_ENRICHMENT", "record_id": t, "crs": "EPSG:4326",
                                            "statement_id": pt["statement_id"], "coordinate_precision_degrees": pt["precision"]}
        nodes.append(node)
    stats_new = Counter(d["cls"] for d in new_entities.values())

    catalog = graph.get("relation_catalog")
    family: dict[str, str | None] = {}
    added: list[dict[str, Any]] = []
    confirmed = Counter()
    sided_r74 = 0

    def add_edge(source: str, target: str, typ: str, extra: dict[str, Any], rev: dict[str, Any], field: str) -> None:
        nonlocal sided_r74
        k = (source, target, typ)
        if k in existing:
            confirmed[typ] += 1
            for e in existing[k]:  # the R74 enrichment's own edges (this build's) take the side; team edges stay as they are
                if e.get("provenance") == "wikidata" and "side" in extra and "side" not in e:
                    e["side"], e["side_source"] = extra["side"], f"wikipedia_infobox:{rev['revid']}:{field}"
                    sided_r74 += 1
            return
        if typ not in family:
            family[typ] = wd._catalog_family(catalog, typ)
        e = {"id": f"selffix5:{source}:{typ}:{target}", "source": source, "target": target, "type": typ,
             **({"relation_family": family[typ]} if family[typ] else {}),
             "start_date": None, "end_date": None, "point_date": None, "count": 1, **extra,
             "provenance": "wikipedia_infobox", "license": LICENSE, "page_title": rev["title"], "page_id": rev["page_id"],
             "revision_id": rev["revid"], "field": field, "selffix_step": 5}
        edges.append(e)
        existing[k].append(e)
        added.append(e)

    for (ev, t, typ), f in sorted(facts.items()):
        if f.get("drop") or (t not in in_graph and t not in new_entities):
            continue
        field, side, m = f["mentions"][0]
        extra: dict[str, Any] = {"mention": m.how}
        if f["sides"]:
            extra["side"] = next(iter(f["sides"]))
        if f["kind"] == "units":
            extra["participant_kind"] = "unit"
        if f["kind"] == "place" and m.qualifier:
            extra["place_qualifier"] = m.qualifier
        add_edge(ev, t, typ, extra, pages[ev], field)

    # --- people's and units' infoboxes: their battles (wd:P607), only to graph battles of 1938-1948
    index = event_index(graph)
    years_of = {}
    for n in nodes:
        q0 = n.get("wikidata_id")
        if isinstance(q0, str) and _record_years(n)[0] is not None:
            years_of.setdefault(q0, _record_years(n))
    kind_of = {n.get("wikidata_id"): (n.get("attributes") or {}).get("kind") for n in nodes if (n.get("attributes") or {}).get("kind")}
    name_of = {}
    for n in nodes:
        name_of.setdefault(n.get("wikidata_id"), n.get("name"))
    battle_links: list[tuple[str, list[str], list[str], dict[str, Any]]] = []
    for a in actors:
        rev = pages.get(a)
        box = wp.find_infobox(rev.get("wikitext") or "", ib.ACTOR_INFOBOXES) if rev else None
        if not box or "battles" not in box[1]:
            continue
        titles, items = ib.battle_items(box[1]["battles"][0])
        battle_links.append((a, titles, match_text_battles(items, index), rev))
    res2 = wp.TitleResolver(ctx.wikipedia_cache, ctx.offline).resolve(sorted({t for _, ts, _, _ in battle_links for t in ts}))
    p607 = Counter()
    for a, titles, text_hits, rev in battle_links:
        linked = [res2[t].qid for t in titles if res2[t].qid]
        for q, how in [(q, "link") for q in linked] + [(q, "text") for q in text_hits if q not in linked]:
            if q not in in_graph or rec_class.get(q) != "event" or q == a:
                continue
            if not theatre_ok(q):
                p607["skip_outside_theatre"] += 1
                continue
            if kind_of.get(q) == "war" or WAR_LEVEL_NAME_RE.search(name_of.get(q) or ""):  # the agent's own war-level test (R83)
                p607["skip_war"] += 1
                continue
            ys = years_of.get(q)
            if not ys or not _overlaps(*ys):
                p607["skip_undated_or_out_of_window"] += 1
                continue
            before = len(added)
            add_edge(a, q, "wd:P607", {"mention": how}, rev, "battles")
            p607["added_" + how if len(added) > before else "confirmed_" + how] += 1

    by_type = Counter(e["type"] + (f" side" if "side" in e else "") for e in added)
    return {
        "events": len(events), "actors": len(actors), "pages": len(pages), **dict(stats),
        "facts_found": len(facts), "edges_added": len(added), "edges_added_by_type": dict(by_type),
        "confirmed_existing": dict(confirmed), "r74_edges_given_a_side": sided_r74,
        "new_entities": len(new_entities), "new_entities_by_class": dict(stats_new),
        "p607_from_actor_infoboxes": dict(p607),
        "added_list": [[e["source"], e["type"], e["target"], e.get("side"), e.get("participant_kind"), e.get("place_qualifier"),
                        e["field"], e["page_title"], e["revision_id"], e.get("mention")] for e in added],
        "new_entity_list": [[t, d["label"], d["cls"], sorted({ev for ev, _ in d["facts"]})] for t, d in new_entities.items()],
        "skipped_top": [[*k, n] for k, n in skipped.most_common(80)],
        "redirects_skipped": [[*k, n] for k, n in redirects_skipped.most_common()],
        "battles_not_in_theatre": [[ev, (lite.get(ev) or {}).get("label"), n] for ev, n in not_theatre.most_common()],
    }


# ----------------------------------------------------------------- step 6: types and names

MIN_CORPUS_NAME_LINKS = 3  # a unit takes the corpus's name when the corpus links it so this often


def _norm_type(t: Any) -> str:
    return "other" if str(t).upper() == "MISC" else str(t)


def decide_class(claims: dict[str, Any], parents, has_kind: bool, known_class: str | None = None) -> str | None:
    """The class step 6 types a QID by: country / region (kept as they are), ship, person, event, unit,
    place or other; None when the class table says nothing. `known_class` is an `entity_class` a record
    already has (R74, R87, step 5): a country or region stays one."""
    ec = wd.entity_class(claims)
    if ec in ("country", "region"):
        return ec
    if known_class in ("country", "region"):
        return known_class
    c, _ = cl.classify(claims, parents)
    # (step 5's POLITY_ROOTS are not used here: "political territorial entity" is above every province and
    # city, so it would stop Hunan or George Town from being typed as places)
    if c is None and has_kind:  # an R74 military-event kind: the event table already decided
        return "event"
    if c is None and cl.other_class(claims, parents):
        return "other"
    return c


def step6_types(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Types from the class table (spec R97); returns the report. Changes a record's `type` only where it
    disagrees with the class of its QID, keeping the old one in `attributes.ner_type`; writes
    `attributes.wikidata_type`; gives a record newly typed as a place or event its P625 point; renames
    only records whose name this build set. The declared changes go to `step6_declared.json`."""
    from geo_agent.graph_store import normalize_name

    nodes = graph.setdefault("nodes", [])
    qids = wd.collect_qids(graph)
    lite = wp.fetch_lite(qids, ctx.cache, ctx.offline)
    parents: dict[str, list[str]] = {}
    seeds = {v for q in qids for p in ("P31", "P279") for v in cl.item_ids((lite.get(q) or {}).get("claims") or {}, p)}
    for _ in range(cl.MAX_DEPTH + 1):
        need = cl.classes_to_fetch(seeds, parents)
        if not need:
            break
        got = wp.fetch_lite(need, ctx.cache, ctx.offline)
        for q in need:
            parents[q] = cl.item_ids((got.get(q) or {}).get("claims") or {}, "P279")
    up = lambda q: parents.get(q, [])
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        if isinstance(n.get("wikidata_id"), str):
            records[n["wikidata_id"]].append(n)

    # the theatre test of an event or a place needs its part-of chain and stated locations
    seen = frontier = set(qids)
    for _ in range(PART_OF_DEPTH + 1):
        frontier = {p for t in frontier for prop in ("P361", "P276", "P17")
                    for p in cl.item_ids((lite.get(t) or {}).get("claims") or {}, prop)} - seen
        if not frontier:
            break
        lite.update(wp.fetch_lite(frontier - set(lite), ctx.cache, ctx.offline))
        seen = seen | frontier

    def own_point(recs: list[dict[str, Any]], claims: dict[str, Any]) -> tuple[float, float] | None:
        for n in recs:
            g = n.get("geometry") or {}
            if g.get("type") == "Point":
                return tuple(g["coordinates"][:2])
        pt = wd.wikidata_point(claims)
        return tuple(pt["coordinates"]) if pt else None

    declared: dict[str, list[str]] = {"type": [], "name": [], "geom": []}
    changes = Counter()
    change_list = []
    decided_by = Counter()
    kept = Counter()
    kept_list = []
    for q, recs in records.items():
        claims = (lite.get(q) or {}).get("claims") or {}
        has_kind = any((n.get("attributes") or {}).get("kind") for n in recs)
        known = next((x for n in recs if (x := (n.get("attributes") or {}).get("entity_class")) in ("country", "region")), None)
        c = decide_class(claims, up, has_kind, known)
        if c is None:
            decided_by["unclassed"] += 1
            continue
        decided_by[c] += 1
        # a record becomes an event or a place only inside the agent's window and the theatre: R84 drops an
        # event of 1904 or 1950 at load (the Battle of Tsushima, the Vietnam War), and a European event or
        # place typed as such would put its point on Pacific maps (the Potsdam Conference)
        hold = None
        if c == "event":
            ys = [y for n in recs for y in _record_years(n) if y is not None] or [y for y in _years(claims) if y is not None]
            if ys and (max(ys) < AGENT_WINDOW[0] or min(ys) > AGENT_WINDOW[1]):
                hold = "event outside the agent's window"
            elif not (in_theatre(q, lite) or ((p := own_point(recs, claims)) and in_asia_pacific(*p))):
                hold = "event outside the theatre"
        elif c == "place" and (p := own_point(recs, claims)) and not in_asia_pacific(*p):
            hold = "place outside the theatre"
        for n in recs:
            attrs = n.get("attributes") if isinstance(n.get("attributes"), dict) else {}
            attrs["wikidata_type"] = c
            n["attributes"] = attrs
            if c in ("country", "region"):
                continue  # countries and broad regions keep their type and entity_class
            target = cl.NODE_TYPE[c]
            if hold and _norm_type(n.get("type")) != target:
                kept[hold] += 1
                kept_list.append([n["id"], n.get("name"), _norm_type(n.get("type")), target, hold])
                continue
            current = _norm_type(n.get("type"))
            if current != target:
                attrs.setdefault("ner_type", n.get("type"))
                n["type"] = target
                declared["type"].append(n["id"])
                changes[(current, target)] += 1
                change_list.append([n["id"], n.get("name"), current, target, c])
            if c == "ship":
                attrs["entity_class"] = "ship"
            if target in ("location", "event") and not (n.get("geometry") or n.get("geometry_wkt")) and \
                    current != target and (pt := wd.wikidata_point(claims)):
                n["geometry"] = {"type": "Point", "coordinates": pt["coordinates"]}
                n["geolocation_evidence"] = {"source_kind": "WIKIDATA_P625_ENRICHMENT", "record_id": q, "crs": "EPSG:4326",
                                             "statement_id": pt["statement_id"], "coordinate_precision_degrees": pt["precision"],
                                             "added_by": "selffix step 6"}
                declared["geom"].append(n["id"])

    # names, only on records this build named (R74 labels, step 3 and step 5 records)
    names = name_index(graph)
    c_links = load_corpus(graph, ctx)
    texts: dict[str, Counter] = defaultdict(Counter)
    for l in c_links.links:
        q = c_links.res[l.title].qid if l.title in c_links.res else None
        if q:
            texts[q][l.text] += 1

    def names_it(q: str, text: str) -> bool:
        """A link text names its entity only when it passes step 5's target test itself: a Wikidata alias,
        or the same unit numbers and a shared word. A piped link "[[5th Corps (NRA)|200th Division]]" or a
        redirect "200th Division" names a sub-unit; "16th Bombardment Squadron" on the 522d's page names
        a renumbered predecessor, which nothing in the data proves to be the same unit."""
        return names_the_target(ib.Mention(text, text, "link", 0), q, lite)
    renamed = []
    for q, recs in records.items():
        c = ((recs[0].get("attributes") or {}).get("wikidata_type"))
        label = (lite.get(q) or {}).get("label")
        for n in recs:
            attrs = n.get("attributes") or {}
            ours = n["id"].endswith("::selffix") or attrs.get("name_source") == "wikidata_label"
            if not ours or not label or n.get("name") != label:
                continue
            new_name, source = None, None
            others = lambda nm: names.get(normalize_name(nm), set()) - {q}
            if c == "unit":
                for text, k in texts[q].most_common():
                    if k < MIN_CORPUS_NAME_LINKS:
                        break
                    if text[:1] and not text[:1].islower() and normalize_name(text) != normalize_name(label) and not others(text) \
                            and not (_words(text) <= _words(label)) and names_it(q, text):
                        new_name, source = text, "corpus_link_text"
                    break  # only the most used text: the corpus's own name for it
            elif c == "ship" and others(label):
                title = (lite.get(q) or {}).get("enwiki")
                if title and normalize_name(title) != normalize_name(label) and not others(title):
                    new_name, source = title, "enwiki_title"
            if new_name:
                n["aliases"] = list(dict.fromkeys([*(n.get("aliases") or []), label]))
                n["name"] = new_name
                attrs["name_source"] = source
                n["attributes"] = attrs
                declared["name"].append(n["id"])
                renamed.append([n["id"], label, new_name, source])

    # the checks the step promises
    ships_as_places = [n["id"] for n in nodes if (n.get("attributes") or {}).get("entity_class") == "ship" and n.get("type") == "location"]
    battle_like = {q for q, recs in records.items() if any((n.get("attributes") or {}).get("kind") for n in recs)
                   or (recs[0].get("attributes") or {}).get("wikidata_type") == "event"}
    held_ids = {k[0] for k in kept_list}
    not_event = [n["id"] for q in battle_like for n in records[q] if _norm_type(n.get("type")) != "event" and n["id"] not in held_ids]
    if ctx.out_dir:
        (ctx.out_dir / "step6_declared.json").write_text(json.dumps(declared, indent=0), encoding="utf-8")
    return {
        "qids": len(records), "classes": dict(decided_by),
        "type_changes": len(declared["type"]), "type_changes_by_from_to": {f"{a} -> {b}": k for (a, b), k in changes.most_common()},
        "kept_outside_window_or_theatre": dict(kept), "kept_list": kept_list,
        "points_added": len(declared["geom"]), "renamed": len(renamed),
        "renamed_by_source": dict(Counter(r[3] for r in renamed)),
        "check_ships_typed_location": len(ships_as_places), "check_battle_like_entities": len(battle_like),
        "check_battle_like_records_not_event": len(not_event),
        "change_list": change_list, "renamed_list": renamed, "ships_typed_location_list": ships_as_places,
        "battle_like_not_event_list": not_event,
    }


# ----------------------------------------------------------------- CLI


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_graph(graph: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False)


def stage_enrich(graph: dict[str, Any], wikidata_cache: Path, offline: bool) -> dict[str, Any]:
    """The R74 enrichment, from the given Wikidata cache."""
    claims, snapshot = wd.fetch_claims(wd.collect_qids(graph), wikidata_cache, offline)
    labels, labels_sha = wd.fetch_labels(wd.nameless_qids(graph), wikidata_cache, offline)
    return wd.enrich(graph, claims, snapshot, labels, labels_sha)


def apply_life_dates(graph: dict[str, Any], lite: dict[str, dict[str, Any]]) -> int:
    """Birth and death dates (P569 / P570) on person records that have none, from lite claims (spec R100).
    The R74 enrichment gives them to the team's people; the people steps 3 and 5 add need them too."""
    added = 0
    for n in graph.get("nodes") or []:
        qid = n.get("wikidata_id")
        if n.get("type") != "person" or not qid or qid not in lite:
            continue
        attrs = n.setdefault("attributes", {})
        for name, (t, precision) in wd.life_dates(lite[qid].get("claims") or {}).items():
            if name not in attrs:
                attrs[name], attrs[f"{name}_precision"] = t, precision
                added += 1
    return added


def stage_life(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """The life-dates pass after the steps (spec R100)."""
    people = {n["wikidata_id"] for n in graph.get("nodes") or [] if n.get("type") == "person" and n.get("wikidata_id")
              and not {"birth_date", "death_date"} <= set((n.get("attributes") or {}))}
    lite = wp.fetch_lite(people, ctx.cache, ctx.offline)
    return {"people_checked": len(people), "life_dates_added": apply_life_dates(graph, lite)}


def untitled_pages(graph: dict[str, Any]) -> set[int]:
    """Page ids of `enwiki:<id>` source pages that no source of the graph gives a `page_title` for (spec R103)."""
    pages, titled = set(), set()
    for rec in (graph.get("nodes") or []) + (graph.get("edges") or []):
        for s in rec.get("sources") or []:
            page = s.get("page") if isinstance(s, dict) else None
            if isinstance(page, str) and page.startswith("enwiki:") and page[7:].isdigit():
                pages.add(int(page[7:]))
                if s.get("page_title"):
                    titled.add(int(page[7:]))
    return pages - titled


def stage_titles(graph: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """The page-title pass (spec R103): `metadata.page_titles` {"enwiki:<id>": title} for the corpus pages
    the graph names by id only, so the agent can show and verify titles. Deleted pages are left out."""
    need = untitled_pages(graph)
    got = wp.fetch_page_titles(need, ctx.wikipedia_cache, ctx.offline)
    titles = {f"enwiki:{p}": t for p, t in sorted(got.items()) if t}
    graph.setdefault("metadata", {})["page_titles"] = titles
    return {"pages_without_title": len(need), "titles_added": len(titles)}


STEPS = {"1": step1_resolve, "3": step3_add_entities, "5": step5_infoboxes, "6": step6_types}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Self-fix build layer over a graph JSON (spec R97).")
    p.add_argument("--graph", required=True, type=Path, help="raw graph JSON (the team's delivery)")
    p.add_argument("--out", required=True, type=Path, help="graph JSON to write")
    p.add_argument("--cache", required=True, type=Path, help="folder for this layer's own caches")
    p.add_argument("--wikidata-cache", required=True, type=Path, help="R74 claims cache (graph_enrichment.enrich)")
    p.add_argument("--wikipedia-cache", required=True, type=Path, help="revisions and titles cache")
    p.add_argument("--offline", action="store_true", help="use only the caches; fail if anything is missing")
    p.add_argument("--steps", default="1,3,5,6", help=f"comma-separated steps after the enrichment (implemented: {', '.join(STEPS)})")
    p.add_argument("--keep-stages", action="store_true", help="also write graph.stage-<name>.json for each stage")
    args = p.parse_args(argv)
    if args.out.resolve() == args.graph.resolve():
        raise SystemExit("--out must differ from --graph")
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise SystemExit(f"unknown or unimplemented steps: {unknown}; implemented: {sorted(STEPS)}")
    out_dir = args.out.parent
    ctx = Ctx(args.wikidata_cache, args.wikipedia_cache, args.cache, args.offline, out_dir)
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    graph = stage_enrich(graph, args.wikidata_cache, args.offline)
    if args.keep_stages:
        write_graph(graph, out_dir / "graph.stage-enrich.json")
    reports: dict[str, Any] = {}
    for step in steps:
        reports[step] = STEPS[step](graph, ctx)
        (out_dir / f"step{step}_report.json").write_text(json.dumps(reports[step], indent=1, ensure_ascii=False))
        if args.keep_stages:
            write_graph(graph, out_dir / f"graph.stage-{step}.json")
    reports["life"] = stage_life(graph, ctx)
    reports["titles"] = stage_titles(graph, ctx)
    graph.setdefault("metadata", {})["selffix"] = {
        "tool_version": TOOL_VERSION, "steps": steps, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_sha256": sha256_file(args.graph), "license_note": "Facts from Wikipedia links and infoboxes: " + LICENSE,
        "reports": {k: {kk: v for kk, v in r.items() if not isinstance(v, list)} for k, r in reports.items()},
    }
    write_graph(graph, args.out)
    print(json.dumps(graph["metadata"]["selffix"], indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
