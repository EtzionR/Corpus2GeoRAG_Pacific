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

## Round 1 follow-up (2026-10-17): second labels, recovery decided

*⚠️ Every label and decision in this section is by **Claude sessions, not a person**. A second Claude session
labelled the evaluation set blind to the first session's drafts, then decided the recovery candidates.*

### Second, blind labels for the evaluation set

The 325 titles in `config/eval_set.yaml` now carry labels from a second Claude session that saw neither the drafts in
`reports/eval_log.tsv` nor any pipeline score (reasons: `reports/eval_log_claude2.tsv`). `evaluate.py` reads them as
filled labels; its first line now says "labelled in eval_set.yaml" instead of "verified by a person".

**Agreement with the drafts:** 293 of 325 exact (**90%**); 310 of 325 (**95%**) agree on correct vs wrong (keep and
marginal both count as correct). Of the 32 disagreements, 17 are keep vs marginal and do not move any number. The 15
that do:

| Pattern | Titles (second session / drafts) |
|---|---|
| Second session stricter: no US–Japan combat in the article | *Japanese submarine Yu 17*, *Yu 3003*, *Ha-210*, *Cha-203*, *Hidemi Kon*, *Fusaki Tsutsumi* (Kurils, vs Soviets), *Thirty-Seventh Army (Japan)* (Borneo, vs Australians), *Aleutian Islands WWII National Monument* (memorial) — wrong / marginal |
| Second session stricter: reached the Pacific only at the end | *USS Gantner*, *USS Habersham* — wrong / marginal |
| Factual disagreement | *USS Altair*: the article says she left Pearl Harbor for Bermuda in late 1941 and reached Pearl again only after the surrender (wrong / keep; the draft's "at Pearl Harbor during the attack" looks mistaken). *USS YP-422*: lost on a reef near Nouméa on her way to the Solomons (wrong / keep; the second session's excerpt missed that paragraph — the draft is probably right) |
| Second session more lenient | *Hanaoka mine* (POW camp holding Americans), *Momo-class destroyer* (one sunk by US aircraft), *United States Army Air Forces* — marginal / wrong |

Five of these sit in the precision sections, where each title stands for many Corpus Articles, so the second labels
give a clearly higher wrong rate than the drafts (below). The disagreements are the first place a person should look.

### Results with the second labels (task 2, before the recovery decisions)

| Run | Corpus | Wrong in the Corpus | ≈ wrong | Classification recall | Discovery recall |
|---|---|---|---|---|---|
| Baseline | 5,422 | 12.3% (7.7–17.2%) | 667 | 72.2% (65.8–79.8%) | 70.8% (34 / 48) |
| Step 4 | 5,350 | 10.6% (6.2–15.3%) | 566 | 72.2% | 70.8% |
| Step 5 | 5,193 | 8.9% (4.9–13.4%) | 460 | 71.7% (65.0–79.2%) | 70.8% |

Paired comparison baseline → step 5: 6 wrong removed, 1 correct removed (the same titles as with the drafts);
step 4 → step 5: 3 wrong removed, 1 correct removed. The drafts gave 9.2% → 7.4% → 5.6%: the direction and the paired
evidence hold, the level is about 3 points higher with the second labels.

### Recovery candidates decided

Of the 663 candidates, the 9 that are evaluation titles were left undecided (they may never become overrides). The
other **654** were decided by reading each article, mostly the ships' service histories: **500 included, 154
excluded**, added to `config/overrides.yaml` with one-line reasons in `reports/review_log.tsv` (prefix `recovery:`).

- **Included:** ships and craft that served in the US–Japan fighting (submarines on war patrol, transports and LSTs at
  Iwo Jima and Okinawa, Asiatic Fleet ships of 1941–42, Japanese ships sunk by US forces), plus — as *marginal*, which
  counts as correct — support ships that reached forward Pacific bases (Marshalls, Marianas, Carolines, Philippines,
  Okinawa) before the surrender without combat.
- **Excluded:** reached the Pacific only after 15 August 1945 or served only postwar (about 70), stubs with no
  wartime detail (about 35), and the rest (about 45): Atlantic, Mediterranean or Normandy only, Japanese ships
  completed too late to fight, Japan vs Australia only, and Japanese weapons whose articles show no use against US
  forces.
- The dev sample estimated the candidates at 83% right; here 76% were included. The difference is mostly the stub
  and post-surrender ships, which a rule could drop automatically next time (their articles name no Pacific place
  before August 1945).

**Re-run** (`run_notebook.py`, 25 s): **5,695** Articles in the Corpus (+521, −19), gold recall **55 / 55**,
`pytest tests`: 23 passed.

**Side effect to review.** Overrides are kNN neighbours, so 654 new ones moved kNN scores elsewhere:

- **98 new recovery candidates** appeared (106 in all, 10 of them evaluation titles), mostly Japanese ships.
- **The second-key veto changed for 40 articles.** 19 newly vetoed, including some that look correct (*27th Infantry
  Division (United States)*, *Philippine Division*, *Hideki Tojo*, *Samuel B. Griffith*); 21 back in, mostly ones
  that look wrong (*Korea under Japanese rule*, *National Revolutionary Army*, *Arthur Percival*). The gold set stayed
  whole. A fix to consider: freeze the kNN neighbour set used by the veto, or keep recovery overrides out of it.

### Final numbers (second labels; same weights for both runs)

`evaluate.py --compare Data/interim/runs/step5` and `--compare Data/interim/runs/baseline`, after the recovery
re-run. The weights changed because the 654 new overrides left the sampling pools (`eval_sample.already_used`), so
classification recall is not comparable with the table above; compare within this table.

| Run | Corpus | Wrong in the Corpus | ≈ wrong | Classification recall | Discovery recall |
|---|---|---|---|---|---|
| Baseline | 5,422 | 12.3% (7.7–17.2%) | 667 | 74.6% (68.4–81.7%) | 70.8% (34 / 48) |
| Step 5 | 5,193 | 8.9% (4.9–13.4%) | 460 | 74.0% (67.7–81.1%) | 70.8% |
| **Now** | **5,695** | **8.0% (4.5–12.2%)** | **458** | **76.0% (69.9–82.6%)** | 70.8% |

With the first session's drafts the same runs give 9.2% → 5.6% → **5.1%** wrong and 73.6% → 73.1% → 75.1% recall.
No sampled evaluation title changed between step 5 and now (none can: they are never overrides), so the gain comes
from the 500 recovered Articles, which `evaluate.py` counts as correct because they are overrides. That is only as
good as the Claude decisions behind them; a person's spot check of them would make it real.

## Round 2: frozen kNN neighbours, recovery precision, three labellers

*2026-10-19 · all evaluation labels are by models: two Claude sessions and GPT via opencode. None has been
verified by a person. A person should check the disagreements first.*

### What changed

- **Frozen neighbours.** The Second key now reads `config/knn_neighbours.yaml`: 1,308 Articles, 672 in and 636 out.
  Recovery Overrides are kept out of this list. It changes only through an explicit rebuild. The 654 recovery
  decisions therefore no longer change other Articles' kNN neighbours. The latest cached notebook run has **5,693**
  Articles, **157** vetoes and **9** recovery Candidates. Against step 5, exactly **500** Articles enter through
  Overrides; no other inclusion changes. This removes the side effect described in the follow-up above.
- **Recovery measured on its own.** `precision_recovery` is a random sample of 40 of the 500 recovery
  include-Overrides (seed 3). These Overrides no longer count as correct by definition: the sample estimates their
  wrong rate. The other, original review Overrides still count as correct by definition. Only this evaluation
  section is allowed to overlap Overrides, and it is kept out of calibration and kNN neighbours.
- **A third blind labeller.** GPT read the 365 titles from `Data/interim/eval_titles_blind.tsv`, using cached
  Wikitext and Wikipedia API fetches for missing Articles. It read leads and service or career sections, returning
  to long histories where needed. Other labels, Overrides and pipeline decisions stayed closed until the labels
  in `reports/eval_log_gpt.tsv` were committed on their own (`823e7f4`). Only then were the 40 recovery labels copied
  into `eval_set.yaml`. The original 325 labels were preserved. The main header wording has since been restored,
  with the recovery exception and provenance placed after it; all 365 titles and labels are unchanged.

### Results

Commands run with the repository's Python:

```sh
.venv/bin/python Processing/evaluate.py --compare Data/interim/runs/baseline
.venv/bin/python Processing/evaluate.py --labels majority --compare Data/interim/runs/baseline
.venv/bin/python Processing/evaluate.py --labels gpt --compare Data/interim/runs/baseline
.venv/bin/python Processing/evaluate.py --labels majority --compare Data/interim/runs/step5
.venv/bin/python -m pytest tests -q
```

*Keep* and *marginal* both count as correct. Wrong-rate and classification-recall intervals below are the
evaluator's 95% intervals from 2,000 bootstrap samples within sections. Each comparison uses the same weights
for its earlier and current run. Earlier tables used other labels or treated recovery Overrides as correct;
use this table for the round 2 comparison.

**Correction to the earlier round 2 figures.** The evaluator weighted recovery by how many recovery includes
were in each run, instead of the fixed population of 500. That left almost all missed recovery Articles out of
the baseline and step 5 recall denominators, overstating recall and understating the recovery gain. It also
mistook two baseline e5 inclusions for recovery Overrides and subtracted two original Overrides. The weight is
now 500 / 40 in every run; only recovery includes actually decided by Override are subtracted from the original
Override count (0 in baseline and step 5, 500 now). Majority baseline recall changes from 73.0% to 67.7%, step 5
from 72.5% to 67.3%; current stays 74.5%. Wrong rates are unchanged at the precision shown.

Classification recall excludes the **154 recovery exclude-Overrides**, an unmeasured group removed from the
classification-recall sampling pool. Any relevant Articles among them are missing from this estimate. No sample
was drawn from them. All labels are by models, none verified by a person.

| Label mode | Run | Corpus | Wrong in the Corpus (95%) | ≈ wrong Articles | Classification recall (95%) | Discovery recall |
|---|---|---|---|---|---|---|
| `set` | Baseline | 5,422 | 12.3% (7.6–17.2%) | 667 | 69.0% (63.8–74.8%) | 70.8% (34 / 48) |
| `set` | Current | 5,693 | 8.3% (4.6–12.5%) | 471 | 75.9% (70.2–82.2%) | 70.8% (34 / 48) |
| `majority` | Baseline | 5,422 | 11.9% (7.2–16.9%) | 646 | 67.7% (62.6–73.5%) | 71.4% (35 / 49) |
| `majority` | Step 5, before recovery review | 5,193 | 8.5% (4.5–12.9%) | 439 | 67.3% (62.1–73.0%) | 71.4% (35 / 49) |
| **`majority`** | **Current** | **5,693** | **7.9% (4.3–12.0%)** | **450** | **74.5% (68.8–80.8%)** | **71.4% (35 / 49)** |
| `gpt` | Baseline | 5,422 | 11.5% (6.9–16.3%) | 625 | 65.9% (61.1–71.6%) | 69.2% (36 / 52) |
| `gpt` | Current | 5,693 | 8.1% (4.6–12.3%) | 464 | 71.9% (66.6–78.2%) | 69.2% (36 / 52) |

`set` means the second Claude session's 325 labels plus GPT's 40 recovery labels. `majority` votes on correct
vs wrong, not keep vs marginal. All three modes use GPT alone for recovery: copying those 40 labels into the
set does not supply another independent judgement.

The paired sample comparison, baseline → current, removes **6 wrong and 1 correct** under set and majority
labels, or **5 wrong and 2 correct** under GPT labels. All modes add **39 correct and 1 wrong** sampled recovery
Articles. Step 5 → current adds those same 40 sampled Articles and removes none. The majority wrong rate falls
by about 0.6 points after recovery, while the estimated number of wrong Articles rises from 439 to 450 because
the Corpus is larger. Classification recall rises by about 7.2 points after recovery (6.8 points from baseline).
Discovery does not change: under majority
labels, 35 of 49 relevant list Articles are found and 32 are included. Gold-set recall remains **55 / 55** in the
latest notebook output.

### Pairwise agreement

On the **325 Articles independently labelled by all three models**:

| Pair | Correct vs wrong | Exact keep / marginal / wrong |
|---|---|---|
| Claude drafts / second Claude session | 310 / 325 (95.4%) | 293 / 325 (90.2%) |
| Claude drafts / GPT | 303 / 325 (93.2%) | 278 / 325 (85.5%) |
| Second Claude session / GPT | 310 / 325 (95.4%) | 292 / 325 (89.8%) |

The command prints `set/gpt` as 350 / 365 (95.9%). That includes the 40 copied recovery labels; the independent
comparison is 310 / 325. The two Claude sessions split on 15 correct-vs-wrong judgements, so neither has a
majority there. GPT breaks those ties. The resulting majority differs from the second Claude session on four:
*Hidemi Kon*, *Japanese submarine chaser Cha-203*, *USS Habersham* and *USS YP-422* become correct.

### Precision of the recovery include-Overrides

GPT's 40 recovery labels are **33 keep, 6 marginal, 1 wrong**. With marginal counted as correct, precision is
**97.5% (39 / 40; 95% Wilson interval 87.1–99.6%)**. Applied to the 500 include-Overrides, that estimates about
488 correct and 13 wrong Articles. The lower bound is below 90%; this small model-labelled sample does not
establish that precision is at least 90%. Strict keep alone is 82.5% (33 / 40).

The one recovery Article labelled **wrong** is **Ha-101-class submarine**. Its Article describes transport
designs, late-war conversions and postwar disposal, but does not document service in US–Japan fighting. This
is an evidence gap, not proof that every boat was inactive.

The six marginal cases are *USCGC McLane*, *USS Bandera*, *USS Beaverhead*, *USS LST-16*, *USS Leyte (ARG-8)*
and *USS Polana*. Most are support or transport ships without a stated combat operation. McLane's claimed
Japanese submarine sinking was later discredited; the target remains unidentified. LST-16's Article gives
its Pacific route and September arrival in Japan, but leaves the intermediate dates unclear. Both deserve
a person's check before treating their inclusion as settled.

### Where GPT disagrees with both Claude sessions

These **11** Articles disagree on correct vs wrong with the two-Claude consensus. Keep vs marginal alone
does not put an Article in this table. The 15 Claude splits above have no two-Claude majority.

| Article | Claude drafts | Second Claude | GPT | GPT's reason |
|---|---|---|---|---|
| List of territories acquired by the Empire of Japan | wrong | wrong | marginal | Broad list, but includes occupied US possessions and their liberation. |
| Marine Air, West Coast | marginal | marginal | wrong | Stateside aviation training and administration, without combat deployment. |
| Osamu Kudō | marginal | marginal | wrong | Stated combat death was at Broome against a Dutch airman. |
| Wei Lihuang | wrong | wrong | keep | Y Force's offensive directly supported Stilwell's US-led Burma operations. |
| Shojiro Hayashi | wrong | wrong | keep | Commanded Japanese troops at Milne Bay, where US forces also served. |
| Masamoto Nasu | wrong | wrong | marginal | Writer who survived the US atomic bombing of Hiroshima as a child. |
| Operation Opossum | wrong | wrong | keep | MacArthur-authorized rescue used US Navy patrol boats with Australian crews. |
| USS Scania (AKA-40) | wrong | wrong | marginal | Carried cargo to Tarawa, Majuro and Kwajalein in July 1945, before surrender. |
| Norfolk Island | wrong | wrong | marginal | US personnel helped build a rear-area Allied airfield; the island was not attacked. |
| United States Navy | wrong | wrong | keep | The service itself fought Japan throughout the naval campaigns. |
| William H. Wilbur | wrong | wrong | marginal | European combat followed by a short, poorly described east Asian posting. |

The main dividing lines are **direct combat vs support**, **a whole organization vs a particular wartime unit**,
and **a passing civilian or biographical tie**. Burma and Australian-led operations need care: a US commander,
US-owned boat or nearby US unit does not always establish the subject's own role in US–Japan engagement.
Late-war ship histories also need dates, not just the word Pacific. GPT's keep for *United States Navy* and
marginal for *Masamoto Nasu* are broader readings than the Claude sessions'. A person should settle these
boundaries before changing any rules or Overrides.

Verification after the correction: **35 tests passed** with `.venv/bin/python -m pytest tests -q`. All four
evaluation comparisons above were re-run with the corrected evaluator. A programmatic check against the committed
evaluation set confirmed that all 365 titles and labels are unchanged. The current flow counts were
checked against the saved outputs of `crawl_corpus.ipynb` and the Article records in `Data/interim/`.
