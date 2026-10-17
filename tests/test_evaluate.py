"""evaluate.measure on a hand-made run: weights, Overrides and the keep/marginal/wrong rule."""
from evaluate import measure
import evaluate
import pytest

W = {"precision_rules": 10.0, "classification_recall": 100.0}


def test_wrong_rate_and_recall():
    run = {"A": {"in_scope": True}, "B": {"in_scope": True}, "C": {"in_scope": False}}
    sample = [("A", "precision_rules", "keep"), ("B", "precision_rules", "wrong"),
              ("C", "classification_recall", "marginal")]
    m = measure(run, sample, W, n_override_in=0)
    assert m["wrong_rate"] == 0.5               # B is wrong, A is not; both weigh 10
    assert m["classification_recall"] == 10 / 110  # A (10) is in; C (100, marginal counts as correct) is out


def test_overrides_count_as_correct_and_included():
    run = {"B": {"in_scope": True}}
    m = measure(run, [("B", "precision_rules", "wrong")], W, n_override_in=30)
    assert m["wrong_rate"] == 10 / 40


def test_recovery_overrides_have_their_own_weight(monkeypatch):
    run = {t: {"in_scope": True, "decided_by": "override"} for t in ("A", "B", "C", "D", "person")}
    run["rule"] = {"in_scope": True, "decided_by": "rules"}
    monkeypatch.setattr(evaluate.eval_sample, "rows", lambda _: run)
    monkeypatch.setattr(evaluate.eval_sample, "recovery_includes", lambda: {"A", "B", "C", "D"})
    monkeypatch.setattr(evaluate, "BOOTSTRAP", 20)
    lab = {"A": ("precision_recovery", "wrong"), "B": ("precision_recovery", "marginal"),
           "rule": ("precision_rules", "keep")}
    res = evaluate.evaluate("run", lab, {"precision_rules": 10})
    assert res["wrong_rate"] == pytest.approx(2 / 15)  # recovery weight 4/2, original Override weight 1
    assert res["classification_recall"] == 1
    unlabelled = evaluate.evaluate("run", {"rule": lab["rule"]}, {"precision_rules": 10})
    assert unlabelled["wrong_rate"] == 0
    assert unlabelled["recovery_labelled"] == 0
