"""English Wikipedia at build time (spec R97): revision wikitext, wikilinks, title -> QID.

Everything downloaded here is untrusted data: wikitext is parsed as text with regular
expressions, never evaluated. Every response is cached on disk so a rerun can be offline:
- `revisions/<revid>.json`: one page revision (page id, title, revision id, wikitext, SHA-256)
- `titles.jsonl`: one line per asked title, with how it resolved (append-only)
- `titles-raw/<digest>.json`: the raw API answer of each title batch, for audit
- `wikidata-lite.jsonl`: per QID, the English label/aliases, the enwiki title, the Wikidata
  revision id and the claims of the properties in LITE_PROPS (append-only)

Title -> QID uses two calls, both batched by 50:
1. enwiki `action=query&titles=...&redirects=1&prop=pageprops|info`: the explicit normalization
   and redirect mapping, the page id, the Wikidata item and the `disambiguation` page property.
   (`wbgetentities?sites=enwiki&titles=...` follows no redirects for more than one title, so it
   can't say which asked title became which item.)
2. Wikidata `wbgetentities?ids=...`: P31, so an item that is a disambiguation page
   (P31 Q4167410) resolves to nothing even when the page property is missing.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

USER_AGENT = "georag-enrichment/0.2"  # generic on purpose: no personal contact in the request
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
BATCH = 50  # titles per query, ids per wbgetentities
REVISION_BATCH = 10  # revisions with content per call (keeps a response under the API's size cap)
PAUSE_S = 1.0
RETRIES = 6
QID_RE = re.compile(r"^Q\d+$")

DISAMBIGUATION_CLASSES = {
    "Q4167410",   # Wikimedia disambiguation page
    "Q22808320",  # Wikimedia human name disambiguation page
}
# Claims kept per linked entity: enough to judge scope (step 3) and type (step 6); the full
# claims of the entities finally added are read by graph_enrichment.enrich.
LITE_PROPS = ("P31", "P279", "P361", "P580", "P582", "P585", "P571", "P576", "P569", "P570",
              "P607", "P625", "P17", "P276", "P241", "P1344", "P710")

# Namespaces and interwiki prefixes whose links are not articles.
NON_ARTICLE_PREFIXES = {
    "file", "image", "media", "category", "template", "help", "portal", "special", "talk", "user", "wikipedia",
    "wp", "project", "draft", "module", "mediawiki", "timedtext", "book", "education program", "gadget",
    "w", "wikipedia", "wikt", "wiktionary", "s", "wikisource", "commons", "c", "d", "wikidata", "q", "wikiquote", "n", "wikinews",
    "b", "wikibooks", "v", "wikiversity", "voy", "wikivoyage", "species", "meta", "m", "mw", "phab", "foundation",
}
LANG_PREFIX_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,8})?$")  # ja:, zh-yue:, simple: is caught below
LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]([a-z]*)")  # innermost links; the trail ("[[ship]]s") joins the text


# ----------------------------------------------------------------- HTTP


_last_call = [0.0]


def http_get_json(url: str, params: dict[str, str], pause: float = PAUSE_S) -> dict[str, Any]:
    """GET a JSON API with a pause between calls and retry with backoff on 429, 5xx and network errors."""
    full = f"{url}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRIES):
        wait = _last_call[0] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == RETRIES - 1:
                raise
            after = e.headers.get("Retry-After")
            time.sleep(float(after) if after and after.isdigit() else 5 * 2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(5 * 2 ** attempt)
    raise RuntimeError("unreachable")


Getter = Callable[[str, dict[str, str]], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _offline_error(what: str, n: int, cache: Path) -> SystemExit:
    return SystemExit(f"--offline: {n} {what} are not in the cache {cache}; run once without --offline to fetch them")


# ----------------------------------------------------------------- wikitext


def normalize_title(raw: str) -> str | None:
    """The article title a link target names, MediaWiki style, or None for a non-article link.

    Drops the section (`Title#Section`), decodes entities (`&ndash;`), turns underscores into
    spaces, collapses whitespace and upper-cases the first letter (enwiki titles are case
    sensitive except for it). A same-page link (`#Section`), a namespace (File:, Category:)
    or an interwiki/interlanguage prefix (wikt:, ja:) gives None. `:Title` is an article."""
    t = re.sub(r"'{2,}", "", html.unescape(raw)).replace("_", " ")  # \s below also folds no-break spaces
    t = t.split("#", 1)[0]
    t = re.sub(r"\s+", " ", t).strip()
    if t.startswith(":"):
        t = t[1:].strip()
    if not t:
        return None
    if ":" in t:
        prefix = t.split(":", 1)[0].strip().lower()
        if prefix in NON_ARTICLE_PREFIXES or prefix == "simple" or LANG_PREFIX_RE.match(prefix):
            return None
    try:
        t = urllib.parse.unquote(t) if "%" in t else t
    except Exception:  # noqa: BLE001 - an odd escape stays as written
        pass
    return t[0].upper() + t[1:]


@dataclass
class Wikilink:
    title: str          # normalized article title (no section)
    text: str           # the text a reader sees
    start: int          # offset of "[[" in the text searched
    end: int            # offset after "]]" and the link trail
    target: str         # the target as written, section included


def parse_wikilinks(text: str, offset: int = 0) -> list[Wikilink]:
    """Article links in wikitext, in order: `[[Title]]`, `[[Title|text]]`, `[[Title#Section|text]]`,
    with a link trail joined to the text (`[[ship]]s` -> "ships"). Non-article links (files,
    categories, interwiki, same-page sections) are left out. Offsets are relative to `text`
    plus `offset`."""
    out = []
    for m in LINK_RE.finditer(text):
        inner, trail = m.group(1), m.group(2)
        target, pipe, label = inner.partition("|")
        title = normalize_title(target)
        if title is None:
            continue
        if label.strip():
            shown = label
        elif pipe:  # the pipe trick: [[Kongō (ship)|]] shows "Kongō"
            shown = re.sub(r"\s*\([^()]*\)\s*$", "", target.split("#", 1)[0])
        else:
            shown = target.replace("_", " ")
        shown = re.sub(r"\s+", " ", re.sub(r"'{2,}", "", html.unescape(shown)).replace("\u00a0", " ")).strip() + trail
        out.append(Wikilink(title=title, text=shown, start=offset + m.start(), end=offset + m.end(), target=target.strip()))
    return out


def paragraph_bounds(text: str, pos: int) -> tuple[int, int]:
    """(start, end) of the wikitext paragraph (block between blank lines) holding offset `pos`."""
    start = text.rfind("\n\n", 0, pos)
    end = text.find("\n\n", pos)
    return (0 if start < 0 else start + 2), (len(text) if end < 0 else end)


def _template_end(text: str, start: int) -> int:
    """Offset after the `}}` that closes the template opening at `start` (nested templates and links counted)."""
    depth, i = 0, start
    while i < len(text) - 1:
        two = text[i:i + 2]
        if two in ("{{", "[["):
            depth, i = depth + 1, i + 2
        elif two in ("}}", "]]"):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


def find_infobox(text: str, names: Iterable[str]) -> tuple[str, dict[str, tuple[str, int]]] | None:
    """(template name, {field: (raw value, offset of the value in `text`)}) of the first infobox whose name is
    one of `names` (case-insensitive, `Infobox military conflict` or `infobox military conflict`), else None.
    Fields are split at top-level pipes only, so `[[A|b]]` and nested `{{flag|X}}` stay inside a value."""
    wanted = {n.casefold() for n in names}
    for m in re.finditer(r"\{\{\s*([Ii]nfobox[^|{}\n]*)", text):
        name = re.sub(r"\s+", " ", m.group(1).replace("_", " ")).strip()
        if name.casefold() not in wanted:
            continue
        end = _template_end(text, m.start())
        body_start = m.end()
        fields: dict[str, tuple[str, int]] = {}
        depth, i, seg = 0, body_start, body_start
        while i < end - 2:
            two = text[i:i + 2]
            if two in ("{{", "[["):
                depth, i = depth + 1, i + 2
                continue
            if two in ("}}", "]]"):
                depth, i = depth - 1, i + 2
                continue
            if text[i] == "|" and depth == 0:
                _add_field(fields, text, seg, i)
                seg = i + 1
            i += 1
        _add_field(fields, text, seg, end - 2)
        return name, fields
    return None


def _add_field(fields: dict[str, tuple[str, int]], text: str, a: int, b: int) -> None:
    part = text[a:b]
    key, eq, value = part.partition("=")
    if eq and key.strip() and "\n" not in key.strip():
        offset = a + len(key) + 1
        fields[key.strip().casefold().replace(" ", "_")] = (value, offset)


# ----------------------------------------------------------------- revisions


def fetch_revisions(revids: Iterable[int], cache_dir: Path, offline: bool = False, get: Getter = http_get_json
                    ) -> dict[int, dict[str, Any]]:
    """{revid: {page_id, title, revid, wikitext, sha256, fetched_at}} for page revisions, by revision id,
    cached one file per revision under `cache_dir/revisions/`. A deleted or hidden revision is
    cached with `missing: true` and no text."""
    folder = cache_dir / "revisions"
    folder.mkdir(parents=True, exist_ok=True)
    wanted = sorted({int(r) for r in revids})
    absent = [r for r in wanted if not (folder / f"{r}.json").exists()]
    if absent and offline:
        raise _offline_error("revisions", len(absent), folder)
    for i in range(0, len(absent), REVISION_BATCH):
        batch = absent[i:i + REVISION_BATCH]
        todo = list(batch)
        while todo:  # the API may stop before the end of a batch (size cap): ask again for the rest
            data = get(WIKIPEDIA_API, {"action": "query", "revids": "|".join(map(str, todo)), "prop": "revisions",
                                       "rvprop": "content|ids|sha1", "rvslots": "main", "formatversion": "2"})
            got = set()
            for page in (data.get("query") or {}).get("pages") or []:
                for rev in page.get("revisions") or []:
                    slot = ((rev.get("slots") or {}).get("main") or {})
                    content = slot.get("content")
                    if content is None and not (slot.get("texthidden") or rev.get("texthidden")):
                        continue  # cut off by the size cap: asked again
                    rec = {"page_id": page.get("pageid"), "title": page.get("title"), "revid": rev["revid"],
                           "fetched_at": _now(), "missing": content is None,
                           "sha256": hashlib.sha256(content.encode()).hexdigest() if content is not None else None,
                           "wikitext": content}
                    (folder / f"{rev['revid']}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
                    got.add(rev["revid"])
            for bad in (data.get("query") or {}).get("badrevids") or {}:
                rid = int(bad)
                (folder / f"{rid}.json").write_text(json.dumps({"revid": rid, "missing": True, "fetched_at": _now()}))
                got.add(rid)
            if not got:
                raise RuntimeError(f"no revision content returned for {todo[:3]}...")
            todo = [r for r in todo if r not in got]
        print(f"fetched revisions {min(i + REVISION_BATCH, len(absent))}/{len(absent)}", file=sys.stderr)
    return {r: json.loads((folder / f"{r}.json").read_text(encoding="utf-8")) for r in wanted}


# ----------------------------------------------------------------- titles


@dataclass
class Resolution:
    asked: str
    status: str = "ok"           # ok, missing, invalid, not_article, no_item, disambiguation, section_redirect
    title: str | None = None     # the page the title leads to, after normalization and redirect
    page_id: int | None = None
    lastrevid: int | None = None
    qid: str | None = None       # the Wikidata item; None unless status is "ok"
    page_qid: str | None = None  # the item of the page reached, whatever the status (for reports)
    redirected: bool = False
    fragment: str | None = None  # a redirect into a section of another page
    fetched_at: str | None = None
    notes: list[str] = field(default_factory=list)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resolutions_from_query(asked: list[str], data: dict[str, Any], fetched_at: str | None = None) -> dict[str, Resolution]:
    """How each asked title resolved in one enwiki `action=query` answer (formatversion 2)."""
    q = data.get("query") or {}
    norm = {n["from"]: n["to"] for n in q.get("normalized") or []}
    redirects = {r["from"]: r for r in q.get("redirects") or []}
    pages = {p["title"]: p for p in q.get("pages") or [] if "title" in p}
    out = {}
    for a in asked:
        t = norm.get(a, a)
        r = Resolution(asked=a, fetched_at=fetched_at)
        seen = set()
        while t in redirects and t not in seen:  # the API resolves one hop; follow what it reports
            seen.add(t)
            r.redirected = True
            r.fragment = redirects[t].get("tofragment") or r.fragment
            t = redirects[t]["to"]
        page = pages.get(t)
        r.title = t
        if page is None or page.get("invalid"):
            r.status = "invalid"
        elif page.get("missing"):
            r.status = "missing"
        elif page.get("ns", 0) != 0:
            r.status = "not_article"
        else:
            pp = page.get("pageprops") or {}
            r.page_id, r.lastrevid = page.get("pageid"), page.get("lastrevid")
            r.page_qid = pp.get("wikibase_item")
            if "disambiguation" in pp:
                r.status = "disambiguation"
            elif r.fragment:
                r.status = "section_redirect"  # "X" -> "Y#Section": a part of Y, not Y itself
            elif not r.page_qid:
                r.status = "no_item"
            else:
                r.qid = r.page_qid
        out[a] = r
    return out


class TitleResolver:
    """Wikipedia title -> QID with a per-title cache (`titles.jsonl`), so reruns can be offline."""

    def __init__(self, cache_dir: Path, offline: bool = False, get: Getter = http_get_json):
        self.cache_dir, self.offline, self.get = cache_dir, offline, get
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / "titles.jsonl"
        self.known: dict[str, Resolution] = {}
        for rec in _read_jsonl(self.path):
            self.known[rec["asked"]] = Resolution(**rec)

    def resolve(self, titles: Iterable[str]) -> dict[str, Resolution]:
        wanted = list(dict.fromkeys(t for t in titles if t))
        absent = [t for t in wanted if t not in self.known]
        if absent and self.offline:
            raise _offline_error("titles", len(absent), self.path)
        raw = self.cache_dir / "titles-raw"
        raw.mkdir(exist_ok=True)
        for i in range(0, len(absent), BATCH):
            batch = absent[i:i + BATCH]
            data = self.get(WIKIPEDIA_API, {"action": "query", "titles": "|".join(batch), "redirects": "1",
                                            "prop": "pageprops|info", "ppprop": "wikibase_item|disambiguation",
                                            "formatversion": "2"})
            when = _now()
            digest = hashlib.sha256("|".join(batch).encode()).hexdigest()[:16]
            (raw / f"{digest}.json").write_text(json.dumps({"asked": batch, "fetched_at": when, "response": data},
                                                           ensure_ascii=False), encoding="utf-8")
            found = resolutions_from_query(batch, data, when)
            with self.path.open("a", encoding="utf-8") as f:
                for t in batch:
                    f.write(json.dumps(asdict(found[t]), ensure_ascii=False) + "\n")
            self.known.update(found)
            if (i // BATCH) % 20 == 0:
                print(f"resolved titles {min(i + BATCH, len(absent))}/{len(absent)}", file=sys.stderr)
        return {t: self.known[t] for t in wanted}


def apply_instance_of(resolutions: dict[str, Resolution], lite: dict[str, dict[str, Any]]) -> int:
    """Turn `ok` resolutions whose item is a disambiguation page by P31 into `disambiguation`; the count."""
    n = 0
    for r in resolutions.values():
        if r.status == "ok" and DISAMBIGUATION_CLASSES & set(instance_of(lite.get(r.qid) or {})):
            r.status, r.qid = "disambiguation", None
            r.notes.append("P31 disambiguation page")
            n += 1
    return n


# ----------------------------------------------------------------- Wikidata (lite)


def _item_ids(claims: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in claims or []:
        snak = c.get("mainsnak") or {}
        if c.get("rank") == "deprecated" or snak.get("snaktype") != "value":
            continue
        v = (snak.get("datavalue") or {}).get("value")
        if isinstance(v, dict) and isinstance(v.get("id"), str):
            out.append(v["id"])
    return out


def instance_of(rec: dict[str, Any]) -> list[str]:
    return _item_ids((rec.get("claims") or {}).get("P31") or [])


def lite_record(qid: str, entity: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    """The cached part of one Wikidata entity: label, aliases, enwiki title, revision, LITE_PROPS claims."""
    claims = entity.get("claims") or {}
    return {
        "qid": qid, "fetched_at": fetched_at, "lastrevid": entity.get("lastrevid"),
        "redirected_to": entity.get("id") if entity.get("id") and entity.get("id") != qid else None,
        "label": ((entity.get("labels") or {}).get("en") or {}).get("value"),
        "aliases": [a["value"] for a in (entity.get("aliases") or {}).get("en") or []],
        "enwiki": (((entity.get("sitelinks") or {}).get("enwiki")) or {}).get("title"),
        "claims": {p: claims[p] for p in LITE_PROPS if p in claims},
    }


def fetch_lite(qids: Iterable[str], cache_dir: Path, offline: bool = False, get: Getter = http_get_json
               ) -> dict[str, dict[str, Any]]:
    """{qid: lite record} for Wikidata items, cached per QID in `wikidata-lite.jsonl`. An unknown
    id is cached with `missing: true`."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "wikidata-lite.jsonl"
    known = {rec["qid"]: rec for rec in _read_jsonl(path)}
    wanted = sorted({q for q in qids if q and QID_RE.match(q)}, key=lambda q: int(q[1:]))
    absent = [q for q in wanted if q not in known]
    if absent and offline:
        raise _offline_error("Wikidata items", len(absent), path)
    for i in range(0, len(absent), BATCH):
        todo = absent[i:i + BATCH]
        while todo:
            data = get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(todo),
                                      "props": "claims|labels|aliases|sitelinks|info", "languages": "en",
                                      "sitefilter": "enwiki"})
            err = data.get("error")
            if err and err.get("code") == "no-such-entity" and err.get("id") in todo:
                bad = err["id"]
                todo.remove(bad)
                known[bad] = {"qid": bad, "missing": True, "fetched_at": _now()}
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(known[bad]) + "\n")
                continue
            if err:
                raise RuntimeError(f"Wikidata error: {err.get('code')}: {err.get('info')}")
            when = _now()
            with path.open("a", encoding="utf-8") as f:
                for key, ent in (data.get("entities") or {}).items():
                    rec = {"qid": key, "missing": True, "fetched_at": when} if "missing" in ent else lite_record(key, ent, when)
                    known[key] = rec
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            todo = []
        if (i // BATCH) % 20 == 0:
            print(f"fetched Wikidata items {min(i + BATCH, len(absent))}/{len(absent)}", file=sys.stderr)
    return {q: known[q] for q in wanted if q in known}


def fetch_full_claims(qids: Iterable[str], cache_dir: Path, offline: bool = False, get: Getter = http_get_json
                      ) -> dict[str, dict[str, Any]]:
    """{qid: {qid, lastrevid, fetched_at, claims}} with every claim of each item, cached one file per
    QID in `cache_dir/claims/`, so a changed candidate set reuses what it has. An unknown id is
    cached with `missing: true` and gives no entry."""
    folder = cache_dir / "claims"
    folder.mkdir(parents=True, exist_ok=True)
    wanted = sorted({q for q in qids if q and QID_RE.match(q)}, key=lambda q: int(q[1:]))
    absent = [q for q in wanted if not (folder / f"{q}.json").exists()]
    if absent and offline:
        raise _offline_error("Wikidata claim sets", len(absent), folder)
    for i in range(0, len(absent), BATCH):
        todo = absent[i:i + BATCH]
        while todo:
            data = get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(todo), "props": "claims|info"})
            err = data.get("error")
            if err and err.get("code") == "no-such-entity" and err.get("id") in todo:
                bad = err["id"]
                todo.remove(bad)
                (folder / f"{bad}.json").write_text(json.dumps({"qid": bad, "missing": True, "fetched_at": _now()}))
                continue
            if err:
                raise RuntimeError(f"Wikidata error: {err.get('code')}: {err.get('info')}")
            when = _now()
            for key, ent in (data.get("entities") or {}).items():
                rec = ({"qid": key, "missing": True, "fetched_at": when} if "missing" in ent else
                       {"qid": key, "lastrevid": ent.get("lastrevid"), "fetched_at": when, "claims": ent.get("claims") or {}})
                (folder / f"{key}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            todo = []
    out = {}
    for q in wanted:
        rec = json.loads((folder / f"{q}.json").read_text(encoding="utf-8"))
        if not rec.get("missing"):
            out[q] = rec
    return out
