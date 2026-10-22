"""Draws the evaluation set into config/eval_set.yaml (labelled by a person in the app's Evaluation tab).
    python Processing/eval_sample.py
Measures the pipeline and is never used to tune it: no title here may become a calibration label, kNN neighbour or
Override, except precision_recovery which measures recovery Overrides (tests/test_corpus_output.py checks this).
Sections, each with a fixed seed:
  precision              Articles in the Corpus, by how they got in (rules / e5): how many are wrong?
  classification_recall  Articles left out although tied to an In-scope Battle or close to the e5 cut-off: missed?
  discovery_recall       Articles linked from Wikipedia list pages the crawl never starts from: found at all?
  precision_recovery     Recovery include-Overrides: --recovery appends 40 unlabelled titles (seed 3) without
                         changing existing sections or labels; refuses to redraw an existing section.
Refuses to overwrite a set that has labels. Needs network for discovery_recall (a few Wikipedia API calls)."""
import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml

from knn_neighbours import recovery_overrides

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "Processing" / "config"
INTERIM = ROOT / "Data" / "interim"
EVAL_PATH = CONFIG / "eval_set.yaml"
SOURCES_PATH = ROOT / "Processing" / "reports" / "eval_sources.tsv"
SEED = 1  # corpus_audit.yaml used seed 0
PRECISION = {"rules": 120, "e5": 30}
CLASSIFICATION_RECALL = 75
# Lists of people, ships and aircrews on both sides; not crawl seeds (the crawl starts from categories only).
DISCOVERY_LISTS = {
    "List of Medal of Honor recipients for World War II": 25,
    "List of United States Navy losses in World War II": 25,
    "List of Japanese Navy ships and war vessels in World War II": 25,
    "List of World War II aces from the United States": 25,
}
WP_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "Corpus2GeoRAG_Pacific/0.2 (https://github.com/NivGreenstein/Corpus2GeoRAG_Pacific)"

HEADER = """\
# Evaluation set: measures the pipeline, never tunes it (no title here may become a calibration label, kNN
# neighbour or Override). Labels are set by a person in the app's Evaluation tab; Claude's drafts and reasons are in
# reports/eval_log.tsv, so the agreement between the two is measured too.
#   keep:     about US–Japan fighting in WWII (took part in it, or is directly part of it)
#   marginal: tied to it, but only in passing; counts as correct (decision of 2026-10-17)
#   wrong:    not about US–Japan fighting
# precision_*: random Corpus Articles by how they got in · classification_recall: left out but tied or close to the
# e5 cut-off · discovery_recall: linked from the lists in DISCOVERY_LISTS (source per title in reports/eval_sources.tsv)
# Drawn by Processing/eval_sample.py (seed {seed}) from the pipeline output of {stamp}.
"""
RECOVERY_EXCEPTION = ("# Exception: precision_recovery titles are Overrides by construction (they measure Overrides), "
                      "but are never calibration labels or kNN neighbours.\n")


def rows(run_dir=INTERIM):
    out = {}
    for name in ("tagged", "excluded", "untagged"):
        for line in (run_dir / f"{name}.jsonl").open():
            r = json.loads(line)
            out[r["title"]] = r
    return out


def already_used():
    """Titles a person or a method has already seen: they cannot measure anything fresh."""
    def load(name):
        return yaml.safe_load((CONFIG / name).read_text()) or {}
    gray, overrides, seeds = load("gray_labels.yaml"), load("overrides.yaml"), load("knn_seeds.yaml")
    used = set(gray.get("calibration") or {}) | set(gray.get("holdout") or {})
    used |= set(overrides.get("include") or []) | set(overrides.get("exclude") or [])
    used |= set(seeds["unrelated"].get("close") or []) | set(seeds["related"].get("extra") or [])
    used |= {t for titles in load("gold_set.yaml").values() for t in titles}
    used |= {t for section in load("corpus_audit.yaml").values() if isinstance(section, dict) for t in section}
    return used


def recovery_includes():
    overrides = yaml.safe_load((CONFIG / "overrides.yaml").read_text()) or {}
    return recovery_overrides() & set(overrides.get("include") or [])


def append_recovery():
    """Append only precision_recovery (40 of the recovery include-Overrides, seed 3), preserving the labelled set."""
    text = EVAL_PATH.read_text()
    sections = yaml.safe_load(text) or {}
    if "precision_recovery" in sections:
        raise SystemExit("precision_recovery already exists; not redrawing it.")
    population = sorted(recovery_includes())
    picked = sorted(random.Random(3).sample(population, 40))
    header = (f"# precision_recovery: 40 of {len(population)} recovery include-Overrides, seed 3, drawn by\n"
              "# Processing/eval_sample.py --recovery; labels null. Only this section may measure Overrides.\n")
    # Preserve the original header and labels; put recovery provenance after the main header.
    lines = text.splitlines(keepends=True)
    split = next((i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")), len(lines))
    EVAL_PATH.write_text("".join(lines[:split]) + RECOVERY_EXCEPTION + header
                         + "".join(lines[split:]).rstrip() + "\n"
                         + yaml.safe_dump({"precision_recovery": {t: None for t in picked}},
                                          allow_unicode=True, sort_keys=False))
    print(f"precision_recovery: 40 of {len(population)}; existing sections preserved")


def pools(data, thresholds):
    """The population each sampled section is drawn from: {section: [titles]} (before removing used titles)."""
    out = {f"precision_{s}": [t for t, r in data.items() if r["in_scope"] and r["decided_by"] == s] for s in PRECISION}
    out["classification_recall"] = [
        t for t, r in data.items() if not r["in_scope"] and r["decided_by"] != "override" and r["primary_tag"] != "Media"
        and (r.get("anchor_grade") not in (None, "none")
             or (r["relevance"] is not None and r["relevance"] >= thresholds["reject_below"]))]
    return out


def wp(session, **params):
    """All pages of a continued MediaWiki query."""
    base = {"action": "query", "format": "json", "formatversion": 2, **params}
    cont = {}
    while True:
        data = session.get(WP_API, params={**base, **cont}, timeout=60).json()
        yield data.get("query", {})
        if "continue" not in data:
            return
        cont = data["continue"]


def list_links(session, page):
    return sorted({l["title"] for q in wp(session, titles=page, prop="links", pllimit="max", plnamespace=0)
                   for p in q.get("pages", []) for l in p.get("links", [])})


def canonical(session, titles):
    """{requested title: canonical Article title}, without missing pages and disambiguation pages."""
    out = {}
    for i in range(0, len(titles), 50):
        aliases, ok = {}, set()
        for q in wp(session, titles="|".join(titles[i:i + 50]), redirects=1, prop="pageprops", ppprop="disambiguation"):
            for r in q.get("normalized", []) + q.get("redirects", []):
                aliases[r["from"]] = r["to"]
            ok |= {p["title"] for p in q.get("pages", []) if not p.get("missing") and "disambiguation" not in p.get("pageprops", {})}
        for t in titles[i:i + 50]:
            c = t
            while c in aliases:
                c = aliases[c]
            if c in ok:
                out[t] = c
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recovery", action="store_true", help="append only the recovery precision sample")
    args = ap.parse_args()
    if args.recovery:
        append_recovery()
        return
    if EVAL_PATH.exists():
        old = yaml.safe_load(EVAL_PATH.read_text()) or {}
        if any(v for section in old.values() if isinstance(section, dict) for v in section.values()):
            sys.exit(f"{EVAL_PATH.name} already has labels; not overwriting it.")
    data, used = rows(), already_used()
    thresholds = json.loads((INTERIM / "thresholds.json").read_text())
    rng = random.Random(SEED)
    sample, taken = {}, set(used)

    def draw(name, pool, n):
        pool = sorted(set(pool) - taken)
        picked = sorted(rng.sample(pool, min(n, len(pool))))
        taken.update(picked)
        sample[name] = {t: None for t in picked}
        print(f"{name}: {len(picked)} of {len(pool):,}")

    population = pools(data, thresholds)
    for stratum, n in PRECISION.items():
        draw(f"precision_{stratum}", population[f"precision_{stratum}"], n)
    draw("classification_recall", population["classification_recall"], CLASSIFICATION_RECALL)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    sources, discovery = [], {}
    for page, n in DISCOVERY_LISTS.items():
        links = [t for t in list_links(session, page) if not t.startswith("List of")]
        rng.shuffle(links)
        canon = canonical(session, links[:n * 3])
        picked = []
        for t in links[:n * 3]:
            c = canon.get(t)
            if c and c not in taken and c not in discovery and len(picked) < n:
                picked.append(c)
                discovery[c] = None
                sources.append((c, page))
        taken.update(picked)
        print(f"discovery_recall: {len(picked)} from {page} ({len(links):,} links)")
    sample["discovery_recall"] = dict(sorted(discovery.items()))

    stamp = datetime.fromtimestamp((INTERIM / "tagged.jsonl").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    EVAL_PATH.write_text(HEADER.format(seed=SEED, stamp=stamp)
                         + yaml.safe_dump(sample, allow_unicode=True, sort_keys=False))
    SOURCES_PATH.write_text("title\tlist\n" + "".join(f"{t}\t{p}\n" for t, p in sorted(sources)))
    print(f"wrote {EVAL_PATH} and {SOURCES_PATH}")


if __name__ == "__main__":
    main()
