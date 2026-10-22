"""Draws the Corpus audit sample into config/corpus_audit.yaml (labelled in the app's Audit tab).
    python Processing/audit_sample.py
Random Articles from the current Corpus, by how they got in (rules / e5), with a fixed seed. Overrides and the
Gold set are left out: a person already decided those. Refuses to overwrite a sample that has labels."""
import json
import random
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "Processing" / "config"
AUDIT_PATH = CONFIG / "corpus_audit.yaml"
SAMPLE = {"rules": 120, "e5": 30}  # about the share each has of the Corpus outside Overrides
SEED = 0

HEADER = """\
# Corpus audit: a random sample of Articles already in the Corpus, labelled by hand in the app's Audit tab.
# Measures how much of the Corpus is wrong or marginal, and tests any filter (e5, Laya) on Articles the
# rules let in. Never used to tune a method: only to test it once.
#   keep:     about US–Japan fighting in WWII (took part in it, or is directly part of it)
#   marginal: tied to it, but only in passing (e.g. a short Pacific posting in a long career elsewhere)
#   wrong:    not about US–Japan fighting
# Drawn by Processing/audit_sample.py (seed {seed}) from the pipeline output of {stamp}.
"""


def main():
    if AUDIT_PATH.exists():
        old = yaml.safe_load(AUDIT_PATH.read_text()) or {}
        if any(v for stratum in old.values() if isinstance(stratum, dict) for v in stratum.values()):
            raise SystemExit(f"{AUDIT_PATH.name} already has labels; not overwriting it.")
    gold = {t for titles in yaml.safe_load((CONFIG / "gold_set.yaml").read_text()).values() for t in titles}
    tagged = ROOT / "Data" / "interim" / "tagged.jsonl"
    rows = [json.loads(l) for l in tagged.open()]
    rng = random.Random(SEED)
    sample = {}
    for stratum, n in SAMPLE.items():
        pool = sorted(r["title"] for r in rows if r["decided_by"] == stratum and r["title"] not in gold)
        sample[stratum] = {t: None for t in sorted(rng.sample(pool, n))}
        print(f"{stratum}: {n} of {len(pool):,}")
    stamp = datetime.fromtimestamp(tagged.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    body = yaml.safe_dump(sample, allow_unicode=True, sort_keys=False)
    AUDIT_PATH.write_text(HEADER.format(seed=SEED, stamp=stamp) + body)
    print(f"wrote {AUDIT_PATH}")


if __name__ == "__main__":
    main()
