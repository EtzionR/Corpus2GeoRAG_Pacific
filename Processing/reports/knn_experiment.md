# Report: kNN with seed articles as the AI check

*2026-10-05 · notebook: `Processing/knn_classifier.ipynb` · seeds: `Processing/config/knn_seeds.yaml` ·
runs offline from the cache in about 2 s*

## Summary

- **Question:** can we decide the "unsure" articles by comparing each one to a hand-picked set of known-related
  and known-unrelated articles (k-nearest neighbours, "kNN"), instead of the current method, which compares each
  article to two plain-English descriptions of the scope?
- **Answer:** **not better, but close once tuned.** The best kNN setup reaches **84% accuracy** on articles it never
  saw, vs **88%** for the current method. It catches more of the real articles (recall 93% vs 76%) but lets in more
  wrong ones (precision 75% vs 96%).
- **The most useful finding concerns the seeds:** "close but unrelated" examples, such as Japan fighting the UK or China, are
  what make kNN work. Without them accuracy falls to 38%. Clearly unrelated examples *hurt* (74% vs 85%).
- **Recommendation:** keep the current method as the decision-maker. kNN adds no accuracy on top of it: combining
  the two scores gives the same ranking quality (AUC 0.96). We still recommend keeping the seed lists (cheap to keep current) as a second
  opinion, because it agrees with the current method on 99% of the articles that method accepts.

## Setup

| | |
|---|---|
| **Related seeds (X = 87)** | 55 must-have articles (gold set) + 32 hand-labelled "in" articles |
| **Unrelated seeds (Y = 182)** | **82 look-alikes**: 44 hand-labelled "out" + 38 hand-picked (e.g. *Battle of Kohima*, *Battle of Shanghai*, *Chindits*, *Battle of Beaufort*, *Eisenhower*, *Patton*, *Chiang Kai-shek*) · **100 clearly unrelated**: random articles the rules tied to no battle |
| **What is compared** | e5 vectors of each article's title + war-related sentences, reused from the main pipeline's cache |
| **Decision** | Among the K most similar seeds, the similarity-weighted share that is "related"; related if ≥ 0.5 |
| **Test set** | **68 hand-labelled unsure articles** (29 in, 39 out) that are **never** used as seeds |
| **Fairness** | K is chosen by leave-one-out on the seeds, never by looking at the test set |

## Results

### 1 · First attempt: all seeds, plain vote

| K | Leave-one-out on seeds | Test accuracy | Precision | Recall | AUC |
|---|---|---|---|---|---|
| 1 | 73% | 75% | 77% | 59% | 0.69 |
| 3 | 77% | 78% | 82% | 62% | 0.82 |
| 11 | 77% | 74% | 79% | 52% | 0.88 |
| **31 (chosen)** | **80%** | **74%** | **79%** | **52%** | **0.86** |

Changing K barely matters (72–78%), and weighting votes by similarity vs a plain majority makes no difference.
The real weakness is **recall: about half of the real articles were missed.**

### 2 · Which seeds matter (ablations, K = 31)

| Seeds used | Test accuracy | AUC |
|---|---|---|
| All seeds | 74% | 0.86 |
| **Without the clearly-unrelated seeds** | **85%** | **0.93** |
| Without the look-alike seeds | 38% | 0.41 |
| Without the 38 hand-picked look-alikes | 72% | 0.78 |
| Related seeds = gold set only (55) | 59% | 0.75 |
| 25% / 50% / 75% of all seeds | 70–71% | – |

- **Look-alikes are essential.** They teach the difference between "Japan at war" and "Japan at war *with the US*".
- **Clearly-unrelated seeds are harmful.** They outnumber the related seeds about 2 to 1, so votes lean "unrelated".
- **The hand-picked look-alikes add about 8 AUC points (0.78 → 0.86)**, so curating them is worth the effort.
- **Gold-only related seeds are too narrow.** The gold set is mostly famous articles, while unsure articles are obscure ships and people.

### 3 · Improved kNN vs the current method

"Balanced" voting divides each side's votes by its seed count, which removes the bias towards the larger side.

| Method | K | Accuracy | Precision | Recall | AUC |
|---|---|---|---|---|---|
| kNN balanced, all seeds | 1 | 75% | 77% | 59% | 0.69 |
| **kNN balanced, look-alike negatives only** | 7 | **84%** | 75% | **93%** | 0.92 |
| kNN + current method combined (rank average) | 7 | – | – | – | 0.96 |
| **Current method** (two descriptions, one cut-off) | – | **88%** | **96%** | 76% | **0.96** |

*AUC measures how well a score ranks related above unrelated articles (0.5 = random, 1.0 = perfect).*

### 4 · Effect on the 2,975 unsure articles (best kNN vs current method)

| Current method → | Accept (655) | Reject (1,135) | Review band (1,185) |
|---|---|---|---|
| kNN says related | **649** | 296 | 956 (81%) |
| kNN says unrelated | 6 | **839** | 229 |

- **Strong agreement where the current method is confident:** 99% of its accepts and 74% of its rejects.
- **On the review band, kNN calls 81% related**, matching the earlier finding that about 80% of the band belongs.
  On the 15 test articles in the band, kNN was right on 12, exactly what "include the whole band" would score.
  So **kNN does not shrink the human review**.

### 5 · What kNN gets wrong (best setup, test set)

| Type | Articles | Pattern |
|---|---|---|
| **Wrongly included (9)** | *Morningside Air Raid Shelter*, *St Patrick's College Townsville*, *British Commonwealth Air Training Plan*, *Pacific Proving Grounds*, *Command responsibility*, *German submarine U-537*, *Almirante Latorre-class battleship*, *Santa Cruz Church (Manila)*, *Ishirō Honda* | Pacific *setting* without US–Japan *combat*: home front, post-war, legal concepts, other navies |
| **Wrongly left out (2)** | *Jack Sanders (American football)*, *George W. D'Artois* | People whose war service is a small part of their biography (the current method also misses these) |

kNN decides by "which known articles does this look like?". Articles set in the Pacific look like Pacific-war
articles even when nobody fought there. The current method asks a direct question about *combat between the US
and Japan*, which separates these better (precision 96% vs 75%).

## Caveats

- **Small test set:** 68 articles. A 4-point difference is about 3 articles; AUC 0.92 vs 0.96 is within noise.
  The ranking of the methods is consistent across every measure, though.
- **One labeller:** all labels were made by the same person (the assistant) from article text, and have not yet been spot-checked by a domain reviewer.
- **Shared seeds:** the seeds include the calibration labels, which the current method also uses to set its
  cut-offs, so both methods saw the same training information. Neither saw the test set.
- **No new network calls:** the experiment reuses the main pipeline's cached vectors, so it inherits its text choice
  (title + war-related sentences).

## Recommendations

1. **Keep the current method** (two plain-English descriptions) as the AI check. It is the most precise, and kNN
   adds nothing when combined with it.
2. **Do not use kNN to shrink the review band.** It is no better than including the whole band by default
   (decision Q27, option b).
3. **Keep `knn_seeds.yaml` maintained.** The look-alike list is a reusable record of "what out of scope looks like"
   and a cheap second opinion. Worth re-testing if the test set grows.
4. **Grow the labelled test set** (about 150 more articles) before any further method comparison. At 68 articles, only large
   differences are meaningful.

## Reproduce

```bash
# needs Data/interim/ from a run of crawl_corpus.ipynb (cache.sqlite + tagged/excluded/untagged.jsonl)
jupyter nbconvert --to notebook --execute --inplace Processing/knn_classifier.ipynb
```

Outputs: `Data/interim/knn_results.json` (all numbers above) and `Data/interim/knn_gray_zone.csv` (kNN score next to
the current decision for every unsure article).
