"""Checks on the last pipeline run (Data/interim/). Skipped when there is no run."""
import json
from pathlib import Path

import pytest
import yaml
from knn_neighbours import recovery_overrides

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "Data" / "interim"
CONFIG = ROOT / "Processing" / "config"

pytestmark = pytest.mark.skipif(not (INTERIM / "tagged.jsonl").exists(), reason="no pipeline output")


def load(name):
    path = CONFIG / name
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


@pytest.fixture(scope="module")
def rows():
    out = {}
    for name in ("tagged", "excluded", "untagged"):
        for line in (INTERIM / f"{name}.jsonl").open():
            r = json.loads(line)
            out[r["title"]] = r
    return out


def test_gold_set_all_in_corpus(rows):
    gold = [t for titles in load("gold_set.yaml").values() for t in titles]
    missing = [t for t in gold if not rows.get(t, {}).get("in_scope")]
    assert not missing


def test_every_override_matches_a_discovered_article(rows):
    """An Article renamed on Wikipedia leaves its Override behind silently."""
    overrides = load("overrides.yaml")
    stale = [t for t in (overrides.get("include") or []) + (overrides.get("exclude") or []) if t not in rows]
    assert not stale, f"{len(stale)} overrides match no Article: {stale[:10]}"


def test_eval_set_never_used_for_tuning():
    """Evaluation titles never tune; precision_recovery alone may measure Overrides."""
    sections = load("eval_set.yaml")
    eval_titles = {t for section in sections.values() if isinstance(section, dict) for t in section}
    if not eval_titles:
        pytest.skip("no eval_set.yaml yet")
    gray = load("gray_labels.yaml")
    overrides = load("overrides.yaml")
    seeds = load("knn_seeds.yaml")
    used = (set(gray.get("calibration") or {}) | set(gray.get("holdout") or {})
            | set(seeds.get("unrelated", {}).get("close") or []) | set(seeds.get("related", {}).get("extra") or []))
    used |= {t for titles in load("knn_neighbours.yaml").values() for t in titles}
    assert not eval_titles & used, sorted(eval_titles & used)[:10]
    ordinary = {t for s, titles in sections.items() if s != "precision_recovery" for t in titles}
    recovery = set(sections.get("precision_recovery") or {})
    assert recovery <= recovery_overrides() & set(overrides.get("include") or [])
    assert not recovery & ordinary
    overrides = set(overrides.get("include") or []) | set(overrides.get("exclude") or [])
    assert not ordinary & overrides, sorted(ordinary & overrides)[:10]


def test_frozen_knn_neighbours_exclude_all_recovery_overrides():
    neighbours = {t for titles in load("knn_neighbours.yaml").values() for t in titles}
    assert not neighbours & recovery_overrides()
