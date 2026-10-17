"""kNN over decided Articles: "which past decisions does this Article look like?" Used by crawl_corpus.ipynb next to
e5 Relevance, never instead of it: it never decides alone (see the second key and recovery in the Decide cell).
Balanced voting with look-alike negatives, from the experiment in knn_classifier.ipynb (reports/knn_experiment.md):
each side's similarity-weighted votes are divided by its number of neighbours, so the larger side does not win
by size. K is chosen by leave-one-out on the neighbours themselves."""
from dataclasses import dataclass

import numpy as np

K_GRID = [3, 5, 7, 9, 11, 15, 21]


@dataclass
class Neighbours:
    titles: list
    S: np.ndarray  # one e5 vector per row
    y: np.ndarray  # 1 = in scope, 0 = out
    k: int


def balanced(sims, y, k):
    """Similarity rows (queries × neighbours) -> share of balanced votes for "in scope", and the top-k indices."""
    k = min(k, sims.shape[1])
    top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
    w = np.clip(np.take_along_axis(sims, top, axis=1), 0, None)
    pos = (w * (y[top] == 1)).sum(1) / max((y == 1).sum(), 1)
    neg = (w * (y[top] == 0)).sum(1) / max((y == 0).sum(), 1)
    return pos / np.maximum(pos + neg, 1e-9), top


def fit(vectors, labels):
    """vectors {title: vector}, labels {title: bool} -> Neighbours with K chosen by leave-one-out accuracy."""
    titles = sorted(t for t in labels if t in vectors)
    S = np.stack([vectors[t] for t in titles])
    y = np.array([int(labels[t]) for t in titles])
    sims = S @ S.T
    np.fill_diagonal(sims, -np.inf)
    accuracy = {k: ((balanced(sims, y, k)[0] >= 0.5) == y).mean() for k in K_GRID}
    k = max(accuracy, key=lambda k: (round(accuracy[k], 3), -k))
    return Neighbours(titles, S, y, k)


def score(nb, queries, n_shown=3):
    """queries {title: vector} -> {title: (score, [(neighbour, in scope?), …])}. An Article that is itself a neighbour
    is scored without its own vote."""
    names = list(queries)
    if not names:
        return {}
    sims = np.stack([queries[t] for t in names]) @ nb.S.T
    index = {t: i for i, t in enumerate(nb.titles)}
    for row, t in enumerate(names):
        if t in index:
            sims[row, index[t]] = -np.inf
    scores, top = balanced(sims, nb.y, nb.k)
    out = {}
    for row, t in enumerate(names):
        nearest = sorted(top[row], key=lambda j: -sims[row, j])[:n_shown]
        out[t] = (float(scores[row]), [(nb.titles[j], bool(nb.y[j])) for j in nearest])
    return out
