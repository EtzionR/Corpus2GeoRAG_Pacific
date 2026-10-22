"""Measures a pipeline run against the evaluation set (config/eval_set.yaml).
    python Processing/evaluate.py                                  # the last run (Data/interim/)
    python Processing/evaluate.py --compare Data/interim/runs/baseline
    python Processing/evaluate.py --drafts                         # fill unverified labels with Claude's drafts
    python Processing/evaluate.py --labels majority               # correct-vs-wrong majority of available labellers
Labels: keep and marginal count as correct, wrong as an error. Every sampled Article stands for its share of the
population it was drawn from (eval_sample.pools on the run the set was drawn from, Data/interim/runs/baseline/), so
the numbers describe the whole Corpus, not just the sample. Intervals are 95% bootstrap intervals over the sample.
Writes Data/interim/eval_results.json."""
import argparse
import collections
import csv
import json
import random
from itertools import combinations
from pathlib import Path

import yaml

import eval_sample

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "Data" / "interim"
BASELINE = INTERIM / "runs" / "baseline"
DRAFTS_PATH = ROOT / "Processing" / "reports" / "eval_log.tsv"
GPT_PATH = ROOT / "Processing" / "reports" / "eval_log_gpt.tsv"
RESULTS_PATH = INTERIM / "eval_results.json"
BOOTSTRAP = 2000


def majority(votes, set_label=None):
    """Vote on correct vs wrong; ties use eval_set.yaml (unlabelled ties stay unlabelled)."""
    votes = [v for v in votes if v]
    correct = sum(v != "wrong" for v in votes)
    if correct * 2 == len(votes):
        return set_label
    if correct * 2 < len(votes):
        return "wrong"
    return set_label if set_label and set_label != "wrong" else next(v for v in votes if v != "wrong")


def labels(use_drafts=False, source="set"):
    """Selected labels and pairwise correct-vs-wrong agreement; --drafts keeps its set-first fallback."""
    sections = yaml.safe_load(eval_sample.EVAL_PATH.read_text()) or {}
    def read(path, column):
        if not path.exists():
            print(f"note: {path.name} missing; skipping that labeller")
            return {}
        with path.open() as f:
            return {r["title"]: r[column] for r in csv.DictReader(f, delimiter="\t")
                    if r[column] in ("keep", "marginal", "wrong")}
    set_labels = {t: l for titles in sections.values() for t, l in titles.items() if l}
    drafts, gpt = read(DRAFTS_PATH, "draft"), read(GPT_PATH, "label")
    titles = {t for titles in sections.values() for t in titles}
    labellers = {"set": set_labels, "drafts": drafts, "gpt": gpt}
    pairwise = {}
    for a, b in combinations(labellers, 2):
        both = titles & labellers[a].keys() & labellers[b].keys()
        pairwise[f"{a}/{b}"] = {"total": len(both), "agree": sum(
            (labellers[a][t] != "wrong") == (labellers[b][t] != "wrong") for t in both)}
    voted = {t: majority([ls.get(t) for ls in labellers.values()], set_labels.get(t)) for t in titles}
    differs = sum((voted[t] != "wrong") != (l != "wrong") for t, l in set_labels.items())
    out = {}
    for section, titles in sections.items():
        for title, label in titles.items():
            label = voted.get(title) if source == "majority" else labellers[source].get(title)
            if use_drafts and source == "set" and not label:
                label = drafts.get(title)
            if label:
                out[title] = (section, label)
    return out, {"verified": len(set_labels), "total": sum(len(t) for t in sections.values()),
                 "agree_with_drafts": sum(l == drafts.get(t) for t, l in set_labels.items()),
                 "pairwise": pairwise, "majority_differs": differs}


def weights():
    """{section: Articles each sampled title stands for}, from the run the set was drawn from."""
    sections = yaml.safe_load(eval_sample.EVAL_PATH.read_text()) or {}
    data = eval_sample.rows(BASELINE)
    population = eval_sample.pools(data, json.loads((BASELINE / "thresholds.json").read_text()))
    used = eval_sample.already_used()
    return {s: len(set(population[s]) - used) / len(sections[s]) for s in population}


def measure(run, sample, w, n_override_in):
    """Corpus wrong rate and classification recall for one run, from (title, section, label) triples."""
    wrong = total = rel_in = rel = 0.0
    for title, section, label in sample:
        if section == "discovery_recall":
            continue
        r = run.get(title)
        inside = bool(r and r["in_scope"])
        correct = label != "wrong"
        if inside and section.startswith("precision"):
            total += w[section]
            wrong += w[section] * (not correct)
        if correct:
            rel += w[section]
            rel_in += w[section] * inside
    total += n_override_in  # Original review Overrides are correct by definition
    rel += n_override_in
    rel_in += n_override_in
    return {"wrong_rate": wrong / total if total else float("nan"), "classification_recall": rel_in / rel if rel else float("nan")}


def discovery(run, sample):
    relevant = [t for t, s, l in sample if s == "discovery_recall" and l != "wrong"]
    found = [t for t in relevant if t in run]
    included = [t for t in found if run[t]["in_scope"]]
    return {"relevant": len(relevant), "discovered": len(found), "included": len(included)}


def interval(values):
    values = sorted(values)
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def evaluate(run_dir, lab, w):
    run = eval_sample.rows(run_dir)
    n_override_in = sum(1 for r in run.values() if r["in_scope"] and r["decided_by"] == "override")
    sample = [(t, s, l) for t, (s, l) in lab.items()]
    recovery = eval_sample.recovery_includes()
    n_recovery = sum(bool(run.get(t, {}).get("in_scope")) and run[t]["decided_by"] == "override"
                     for t in recovery)
    n_labelled = sum(s == "precision_recovery" for _, s, _ in sample)
    w = dict(w)
    if n_labelled:
        # The same population must count toward recall even before its recovery.
        w["precision_recovery"] = len(recovery) / n_labelled
        n_override_in -= n_recovery
    point = measure(run, sample, w, n_override_in)
    by_section = collections.defaultdict(list)
    for item in sample:
        by_section[item[1]].append(item)
    rng, boot = random.Random(0), collections.defaultdict(list)
    for _ in range(BOOTSTRAP):  # resample within each section, as the sample was drawn
        resampled = [rng.choice(items) for items in by_section.values() for _ in items]
        for k, v in measure(run, resampled, w, n_override_in).items():
            boot[k].append(v)
    disc = discovery(run, sample)
    in_corpus = sum(r["in_scope"] for r in run.values())
    return {"run": str(run_dir), "corpus": in_corpus,
            "wrong_rate": point["wrong_rate"], "wrong_rate_ci": interval(boot["wrong_rate"]),
            "wrong_articles": round(point["wrong_rate"] * in_corpus),
            "classification_recall": point["classification_recall"],
            "classification_recall_ci": interval(boot["classification_recall"]),
            "discovery": disc,
            "review_band": sum(r["decided_by"] == "review" for r in run.values()),
            "recovery_candidates": sum(r["decided_by"] == "recovery" for r in run.values()),
            "recovery_includes": n_recovery, "recovery_labelled": n_labelled}


def flips(old_dir, new_dir, lab):
    """Sampled Articles whose inclusion changed between two runs, by label: the paired comparison."""
    old, new = eval_sample.rows(old_dir), eval_sample.rows(new_dir)
    out = collections.Counter()
    for title, (section, label) in lab.items():
        was = bool(old.get(title, {}).get("in_scope"))
        now = bool(new.get(title, {}).get("in_scope"))
        if was != now:
            out[f"{'removed' if was else 'added'} {'wrong' if label == 'wrong' else 'correct'}"] += 1
    return dict(out)


def show(res):
    lo, hi = res["wrong_rate_ci"]
    rlo, rhi = res["classification_recall_ci"]
    d = res["discovery"]
    print(f"{res['run']}: {res['corpus']:,} Articles in the Corpus · review band {res['review_band']:,} · "
          f"recovery candidates {res['recovery_candidates']:,}")
    print(f"  recovery include-Overrides: {res['recovery_includes']} in this run, "
          f"{res['recovery_labelled']} labelled in precision_recovery")
    if not res["recovery_labelled"]:
        print("  WARNING: precision_recovery has no labels yet; recovery Overrides still count as correct by definition. "
              "The wrong rate is optimistic.")
    print(f"  wrong in the Corpus      {res['wrong_rate']:6.1%}  (95% {lo:.1%}–{hi:.1%}) ≈ {res['wrong_articles']:,} Articles")
    print(f"  classification recall    {res['classification_recall']:6.1%}  (95% {rlo:.1%}–{rhi:.1%})")
    if d["relevant"]:
        print(f"  discovery recall         {d['discovered'] / d['relevant']:6.1%}  ({d['discovered']} of {d['relevant']} relevant "
              f"list Articles found; {d['included']} included)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", default=str(INTERIM))
    ap.add_argument("--compare", help="an earlier run directory (e.g. Data/interim/runs/baseline)")
    ap.add_argument("--drafts", action="store_true", help="use Claude's draft where no person has labelled yet")
    ap.add_argument("--labels", choices=("set", "drafts", "gpt", "majority"), default="set",
                    help="label source (drafts selects drafts alone; --drafts fills gaps in set)")
    args = ap.parse_args()
    if args.drafts and args.labels != "set":
        ap.error("--drafts fills gaps in --labels set; choose either it or another --labels source")
    lab, agreement = labels(args.drafts, args.labels)
    print(f"labels: {agreement['verified']} of {agreement['total']} labelled in eval_set.yaml"
          + (f", {agreement['agree_with_drafts'] / agreement['verified']:.0%} agree with the drafts in eval_log.tsv" if agreement["verified"] else "")
          + (f"; {len(lab) - agreement['verified']} filled from Claude's drafts (provisional)" if args.drafts else ""))
    print(f"  using {args.labels}" + (" with draft fallback" if args.drafts else "") + f": {len(lab)} labels")
    for pair, counts in agreement["pairwise"].items():
        n, agree = counts["total"], counts["agree"]
        print(f"  agreement {pair} (correct vs wrong): " + (f"{agree}/{n} ({agree / n:.1%})" if n else "no shared labels"))
    print(f"  majority differs from eval_set.yaml (correct vs wrong): {agreement['majority_differs']} titles")
    if not lab:
        raise SystemExit("no labels yet: verify them in the app's Evaluation tab, or pass --drafts")
    w = weights()
    n_unmeasured = len(eval_sample.recovery_overrides() - eval_sample.recovery_includes())
    print(f"  note: classification recall excludes {n_unmeasured} unmeasured recovery exclude-Overrides.")
    results = {"labels": agreement, "label_source": args.labels, "drafts_used": args.drafts, "runs": []}
    if args.compare:
        results["runs"].append(evaluate(Path(args.compare), lab, w))
        show(results["runs"][-1])
    results["runs"].append(evaluate(Path(args.run), lab, w))
    show(results["runs"][-1])
    if args.compare:
        results["flips"] = flips(Path(args.compare), Path(args.run), lab)
        print(f"  sampled Articles that changed: {results['flips'] or 'none'}")
    RESULTS_PATH.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
