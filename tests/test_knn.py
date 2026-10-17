"""knn.py on toy vectors: balanced voting, leave-one-out K, and no self-vote."""
import numpy as np

import knn


def unit(*v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


VECTORS = {f"in{i}": unit(1, 0.1 * i) for i in range(6)} | {f"out{i}": unit(0.1 * i, 1) for i in range(12)}
LABELS = {t: t.startswith("in") for t in VECTORS}


def test_balanced_vote_is_not_won_by_the_larger_side():
    nb = knn.fit(VECTORS, LABELS)
    scores = knn.score(nb, {"q_in": unit(1, 0.05), "q_out": unit(0.05, 1)})
    assert scores["q_in"][0] > 0.5 > scores["q_out"][0]


def test_a_neighbour_does_not_vote_for_itself():
    nb = knn.fit(VECTORS, LABELS)
    score, nearest = knn.score(nb, {"in0": VECTORS["in0"]})["in0"]
    assert "in0" not in [t for t, _ in nearest]
