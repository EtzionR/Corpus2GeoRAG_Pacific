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


def test_recovery_population_is_fixed_before_recovery(monkeypatch):
    run = {t: {"in_scope": False, "decided_by": "rules"} for t in ("A", "B", "C", "D")}
    run["C"] = {"in_scope": True, "decided_by": "e5"}
    run["original"] = {"in_scope": True, "decided_by": "override"}
    run["rule"] = {"in_scope": True, "decided_by": "rules"}
    monkeypatch.setattr(evaluate.eval_sample, "rows", lambda _: run)
    monkeypatch.setattr(evaluate.eval_sample, "recovery_includes", lambda: {"A", "B", "C", "D"})
    monkeypatch.setattr(evaluate, "BOOTSTRAP", 20)
    lab = {"A": ("precision_recovery", "keep"), "B": ("precision_recovery", "keep"),
           "rule": ("precision_rules", "wrong")}
    res = evaluate.evaluate("baseline", lab, {"precision_rules": 10})
    assert res["classification_recall"] == pytest.approx(1 / 5)  # original 1, missed recovery 4
    assert res["wrong_rate"] == pytest.approx(10 / 11)  # original Override was not subtracted
    assert res["recovery_includes"] == 0  # C got in by e5, not Override


def test_append_recovery_preserves_header_and_labels(tmp_path, monkeypatch):
    import yaml

    path = tmp_path / "eval_set.yaml"
    header = evaluate.eval_sample.HEADER.format(seed=1, stamp="2026-10-17 22:07")
    original = {"precision_rules": {"rule": "keep"}}
    path.write_text(header + yaml.safe_dump(original))
    monkeypatch.setattr(evaluate.eval_sample, "EVAL_PATH", path)
    monkeypatch.setattr(evaluate.eval_sample, "recovery_includes", lambda: {f"R{i}" for i in range(50)})
    evaluate.eval_sample.append_recovery()
    text = path.read_text()
    sections = yaml.safe_load(text)
    assert text.startswith(header + evaluate.eval_sample.RECOVERY_EXCEPTION + "# precision_recovery:")
    assert sections["precision_rules"] == original["precision_rules"]
    assert len(sections["precision_recovery"]) == 40
    assert all(label is None for label in sections["precision_recovery"].values())
    with pytest.raises(SystemExit, match="already exists"):
        evaluate.eval_sample.append_recovery()
    assert path.read_text() == text


@pytest.mark.parametrize("votes,set_label,expected", [
    (["keep", "marginal", "wrong"], "wrong", "keep"),
    (["wrong", "wrong", "keep"], "keep", "wrong"),
    (["wrong", "marginal"], "marginal", "marginal"),
    (["keep", "wrong"], "wrong", "wrong"),
    ([None, "marginal", None], None, "marginal"),
    ([None, None, None], None, None),
    (["wrong", "keep"], None, None),
])
def test_majority(votes, set_label, expected):
    assert evaluate.majority(votes, set_label) == expected


def test_label_sources_and_missing_gpt(tmp_path, monkeypatch, capsys):
    path = tmp_path / "set.yaml"
    path.write_text("precision_rules:\n  A: wrong\n  B: null\nprecision_recovery:\n  C: null\n")
    drafts = tmp_path / "drafts.tsv"
    drafts.write_text("title\tdraft\nA\tkeep\nB\tmarginal\n")
    gpt = tmp_path / "gpt.tsv"
    monkeypatch.setattr(evaluate.eval_sample, "EVAL_PATH", path)
    monkeypatch.setattr(evaluate, "DRAFTS_PATH", drafts)
    monkeypatch.setattr(evaluate, "GPT_PATH", gpt)
    lab, agreement = evaluate.labels(source="majority")
    assert lab == {"A": ("precision_rules", "wrong"), "B": ("precision_rules", "marginal")}
    assert agreement["pairwise"]["set/drafts"] == {"total": 1, "agree": 0}
    assert "gpt.tsv missing" in capsys.readouterr().out
    assert evaluate.labels(True)[0] == lab  # legacy --drafts fills only gaps
    assert evaluate.labels(source="drafts")[0]["A"][1] == "keep"
    assert evaluate.labels(source="gpt")[0] == {}
    gpt.write_text("title\tsection\tlabel\twhy\nA\tprecision_rules\tmarginal\ttest\n"
                   "C\tprecision_recovery\twrong\ttest\n")
    lab, agreement = evaluate.labels(source="majority")
    assert lab["A"] == ("precision_rules", "keep")
    assert lab["C"] == ("precision_recovery", "wrong")
    assert agreement["majority_differs"] == 1
    assert agreement["pairwise"]["drafts/gpt"] == {"total": 1, "agree": 1}
