# Report: fewer wrong articles, measured (round 1)

*2026-10-17 · branch `corpus-pipeline-and-review` · measure with `python Processing/evaluate.py` · evaluation set:
`config/eval_set.yaml` (325 articles) · drafts: `reports/eval_log.tsv`*

## Summary

- **Question:** the corpus returns too many articles that are not really about US–Japan fighting. Where do they come
  from, how many are there, and can e5 and kNN help without hurting recall?
- **Answer:** most of them came from the **rules**, not from the AI check. Two changes cut the estimated share of
  wrong articles from **~9.2% to ~5.6%** (about 500 → 290 articles) while recall stayed the same:
  1. **Battle participants read side by side** (rules): a battle counts only if its fact box puts the US and Japan
     on opposite sides as main combatants.
  2. **A second key** (e5 + kNN): an article that got in only through links is left out when **both** models say
     it is unrelated.
- **⚠️ Provisional.** Every number below uses **Claude's draft labels**. They become real when a person has verified
  the 325 labels in the app's Evaluation tab; `evaluate.py` then reports the agreement between the two.
- **Recall is the bigger open problem:** about 29% of the relevant articles the crawl finds are left out, and about
  24% of relevant articles from independent Wikipedia lists are never found. 663 likely misses now wait for review.

## How it is measured

| Section of `eval_set.yaml` | Drawn from (run of 2026-10-17, before any change) | Measures |
|---|---|---|
| `precision_rules` (120) · `precision_e5` (30) | Corpus articles by how they got in | Share of wrong articles in the Corpus |
| `classification_recall` (75) | Left-out articles tied to a battle or near the e5 cut-off | Relevant articles found but left out |
| `discovery_recall` (100) | Links on 4 Wikipedia lists the crawl never starts from (Medal of Honor WWII, US Navy losses, Japanese Navy ships, US aces) | Relevant articles never found |

- **Labels:** *keep* and *marginal* count as correct (decision of 2026-10-17); only *wrong* is an error. Overrides
  are correct by definition.
- **Weights:** each sampled article stands for its share of the population it was drawn from, so the numbers
  describe the whole Corpus. Intervals are 95% bootstrap intervals.
- **Never tuned on:** no evaluation title is a calibration label, kNN neighbour, seed or Override
  (`tests/test_corpus_output.py`). Each change was tested on the evaluation set **once**, against a bar fixed before
  the test. Thresholds were chosen on development data: the old audit (`corpus_audit.yaml`) and a 60-article
  recovery sample (`recovery_dev.yaml`).

## Results

| Run | Corpus | Wrong in the Corpus | ≈ wrong articles | Classification recall | Discovery recall |
|---|---|---|---|---|---|
| Baseline (`runs/baseline`) | 5,422 | 9.2% (5.0–13.7%) | 500 | 71.2% (64.9–78.4%) | 75.5% (37 / 49) |
| + battle participants (`runs/step4`) | 5,350 | 7.4% (3.7–11.7%) | 398 | 71.2% | 75.5% |
| + second key (`runs/step5`, current) | 5,193 | **5.6% (2.4–9.3%)** | **293** | 70.7% (64.5–77.9%) | 75.5% |

Paired comparison on the sampled articles, baseline → current: **6 wrong removed, 1 correct removed**
(*J. Lawton Collins*, vetoed by the second key), none added. Gold set: 55 / 55 at every step.

### 1 · Battle participants read side by side

The old check called a battle US–Japan if "United States" or "American" appeared **anywhere** in its fact box. It
matched casualty notes (*Battle of Changde*: "American and British claims"), the *American-British-Dutch-Australian
Command* (*Malayan campaign*) and "air support" lines (*Japanese invasion of Burma*). Every person, unit or place
linked to those battles inherited the tie.

`battle_sides` now reads the combatant fields. Of 320 reference battles, **298** stay in, **13** become Peripheral
battles (US or Japan only in support; in the Corpus, never an article's only anchor), **5** are out and **4** where the
fact box and Wikidata or the categories disagree go to review. **72 articles leave the Corpus.** Almost all are Chinese,
British, Malayan or Burmese subjects; the debatable ones are two Japanese machine guns (Type 96, Type 99). On the dev
audit all 5 articles removed were labelled wrong.

### 2 · The second key: e5 and kNN must agree

kNN (`knn.py`, from the earlier experiment) compares each article with ~1,300 articles people already decided. It is
never a decider on its own:

| Variant (development audit, rules-included articles) | Wrong removed | Correct removed |
|---|---|---|
| e5 alone vetoes (relevance < reject cut-off) | 5 | 6 ❌ |
| e5 **and** kNN (< 0.5) agree | 3 | 1 |
| … but on all ties, run on the full Corpus | – | **MacArthur, Yamashita, Imperial GHQ** (gold set) ❌ |
| **e5 and kNN (< 0.3) agree, link-only ties** (adopted) | 2 | 0 |

Both models misjudge long biographies (MacArthur's article covers two wars), so the veto never touches articles
with a Wikidata or category tie. The 0.3 cut-off was chosen so the gold set stays whole (*Imperial General
Headquarters* scores 0.44). One known loss is *Claire Lee Chennault* (0.26).

**Fixed bar, tested once on the evaluation set:** at least 2 wrong removed per correct article lost, and recall down
at most 2 points. **Result:** 3 wrong for 1 correct, recall −0.5 points. Passed. 157 articles leave the Corpus.

### 3 · Recovery: likely misses, for a person

Of the relevant articles the crawl found but left out, most are US Navy ships the rules could not tie to a battle:
their pages link the island (*Iwo Jima*), not the battle article. Examples are *USS Hinsdale*, *USS LST-884* and
*USS Mendocino*. e5 rates many of them highly, but it never saw them because they were not in the gray zone.

Recovery candidates are articles tied to no battle where e5 ≥ the accept cut-off **and** kNN ≥ 0.8. On the dev sample
they were **83%** right (e5 alone: 67%). That is below the 90% bar the pipeline uses for automatic decisions, and
including them would raise the wrong rate. So the **663** candidates are listed (`recovery.csv`, app → Review),
each with the three decided articles it looks most like. Typical misses among them are Atlantic-only and postwar
ships.

## Caveats

- **Claude's labels throughout.** The draft labels, the dev audit and the recovery dev sample were all labelled by
  Claude. Nothing here is final until a person verifies the evaluation set.
- **Small samples:** 150 precision articles; the wrong-rate intervals overlap between steps. The paired comparison
  (6 wrong vs 1 correct removed) is the stronger evidence.
- **I looked at the evaluation set's recall misses** (section 3) to diagnose them. No threshold was set from them:
  recovery uses the existing e5 cut-off, and its kNN cut-off and its review-not-include decision come from the dev
  sample.
- **Not done this round:** the review log does not yet store the evidence a reviewer relied on.

## Next

1. **Verify the 325 evaluation labels** (app → Evaluation), then run `python Processing/evaluate.py --compare
   Data/interim/runs/baseline` for real numbers.
2. **Decide the recovery candidates.** Sorting by kNN groups similar ships, so precedents speed it up. A rule that
   drops Atlantic-only and postwar ships from the candidates (their service history never names a Pacific place)
   would likely push recovery past the 90% bar, so it could include articles automatically.
3. **Discovery:** 12 of 49 relevant list articles were never found. Most are Pacific Medal of Honor recipients and
   Navy aces (*Robert R. Scott*, *Swede Vejtasa*, *Ray E. Eubanks*). Their categories (Medal of Honor recipients,
   aces) are not seeds. This is where the reviewer's multi-hop idea fits: start from such people categories and
   keep only articles that pass the rules.
4. Then the remaining ideas from the review: per-type e5 descriptions and section-level passages, each tested the
   same way.
