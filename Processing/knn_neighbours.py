"""Freeze the Second key's comparison set; pass --rebuild only to change it on purpose."""
import argparse
import csv
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent / "config"
REVIEW_PATH = CONFIG.parent / "reports" / "review_log.tsv"
PATH = CONFIG / "knn_neighbours.yaml"
HEADER = """\
# Frozen kNN neighbours for the Second key. Changes only on purpose: run
# .venv/bin/python Processing/knn_neighbours.py --rebuild to rebuild from current sources.
# Built from knn_seeds.yaml unrelated.close, gray_labels.yaml calibration, gold_set.yaml and
# Overrides, in that order; recovery Overrides (review_log.tsv why starts with recovery:) are omitted.
# Holdout, eval_set.yaml and corpus_audit.yaml titles are never neighbours.
"""


def recovery_overrides():
    """Titles decided in the recovery review, including both include and exclude Overrides."""
    with REVIEW_PATH.open() as f:
        return {r["title"] for r in csv.DictReader(f, delimiter="\t") if r["why"].startswith("recovery:")}


def build():
    def load(name):
        return yaml.safe_load((CONFIG / name).read_text()) or {}
    seeds, gray, overrides = load("knn_seeds.yaml"), load("gray_labels.yaml"), load("overrides.yaml")
    labels = {t: False for t in seeds["unrelated"].get("close") or []}
    labels.update({t: bool(y) for t, y in (gray.get("calibration") or {}).items()})
    labels.update({t: True for titles in load("gold_set.yaml").values() for t in titles})
    recovery = recovery_overrides()
    labels.update({t: False for t in overrides.get("exclude") or [] if t not in recovery})
    labels.update({t: True for t in overrides.get("include") or [] if t not in recovery})
    never = set(gray.get("holdout") or {}) | recovery
    for name in ("eval_set.yaml", "corpus_audit.yaml"):
        never |= {t for s in load(name).values() if isinstance(s, dict) for t in s}
    return {side: sorted(t for t, y in labels.items() if y == value and t not in never)
            for side, value in (("in", True), ("out", False))}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    if PATH.exists() and not args.rebuild:
        raise SystemExit(f"{PATH.name} already exists; pass --rebuild to change it on purpose.")
    labels = build()
    PATH.write_text(HEADER + yaml.safe_dump(labels, allow_unicode=True, sort_keys=False))
    print(f"wrote {PATH}: {len(labels['in'])} in, {len(labels['out'])} out")


if __name__ == "__main__":
    main()
