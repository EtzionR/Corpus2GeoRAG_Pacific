# How the rules are implemented

The rule functions are in [`scope_rules.py`](scope_rules.py) (tests: `tests/test_scope_rules.py`), and
`classify(p)` in `Processing/crawl_corpus.ipynb` applies them. Terms are defined in [`../CONTEXT.md`](../CONTEXT.md); the overview is in
[`PIPELINE.md`](PIPELINE.md).

1. **Tag:** `tag_page` takes the first non-Media Tag from four sources, in order: the infobox (`INFOBOX_TAGS`),
   intelligence categories, the Wikidata P31 class (`TAG_ROOTS`), then category keywords (`CATEGORY_TAGS`).
   `refine` then corrects the Tag from the title.
2. **Route** (`classify`): Media goes **out** (Rule 1). Battle, Campaign and Operation go to `battle_scope`
   (Rule 2). Countries and metropolises go **out** (`too_broad`). Everything else goes to `anchor_scope` (Rule 3).
3. **Rule 2, `battle_scope`:** a battle dated outside 1941–45 is **out**. A battle is **in** when any one source
   shows both the US and Japan took part: the infobox text, Wikidata P710 (`US_QIDS` / `JAPAN_QIDS`), or the
   categories. Battles that pass this broad check drive discovery (`pipe.battles`). Classification then reads the
   infobox combatant fields side by side (`battle_sides`, `battle_grade`): US against Japan as main combatants is
   **in**; one of them only in a supporting role ("air support") is a **Peripheral battle**, in the Corpus but
   never an Article's only anchor; no US against Japan is **out**, or **gray** when Wikidata P710 or the categories
   say otherwise (`sources disagree`). Battles that are not out form the reference list. A battle whose
   participants are listed (a flag template in the infobox, or Wikidata P710) but are not both the US and Japan is
   **out** (`participants_known`). A battle with no listed participants, or a cancelled or proposed operation, falls
   through to Rule 3.
4. **Rule 3, `anchor_scope`:** the Article's ties to reference battles come from Wikidata (`ANCHOR_PROPS`; for a
   Person, P793 is only a weak tie), from battle-named categories, and from wikilinks.
   - **In** if a Wikidata or category tie is to a battle that is not an Umbrella conflict or Peripheral battle.
   - **In** if there are 2 or more ties (`MIN_BATTLE_LINKS`) and at least one is not an Umbrella conflict.
   - **Gray** if there are other ties, or if the Article is anchored but has no Tag.
   - **Out** if there are no ties.
   - **Out**, whatever the ties, if the Article belongs to a non-US Allied force by its categories (Australian,
     British, Indian, Chinese, Dutch…), is not US or Japanese itself, and never mentions the US (`allied_only`).
   - **Out** if it is a Ship whose ties come only from links in its Namesake section and whose own history never
     mentions the Pacific war (`without_namesake`, `PACIFIC`).
5. **Decide** (the "Decide" cell):
   1. Overrides win.
   2. Gray Articles are scored by e5 against `ACCEPT_AT` / `REJECT_BELOW`.
   3. A gray Article that is too close to call and tied only through Umbrella conflicts is **out**.
   4. Everything else keeps its rule verdict, unless the **second key** objects (`second_key` in `scope.yaml`;
      kNN from `knn.py`, neighbours = Overrides, calibration labels, Gold set, look-alikes):
      - a non-battle Article in only through links (`anchor_grade == "links"`) goes **out** when e5 Relevance is
        below `REJECT_BELOW` and kNN is below `veto_knn` (`decided_by = "e5+knn"`);
      - an Article tied to no battle with e5 Relevance ≥ `ACCEPT_AT` and kNN ≥ `recovery_knn` is a **recovery
        candidate** (`decided_by = "recovery"`, left out, listed in `recovery.csv` and the app's Review tab).

**Config:** `config/scope.yaml` (Umbrella conflicts, e5 queries) · `config/categories.yaml` (seeds, Blocked
categories) · `config/overrides.yaml` · `config/gray_labels.yaml` (threshold labels; holdout titles are never used
to set thresholds) · constants in `scope_rules.py` (`TAG_ROOTS`, `US_QIDS`, `TOO_BROAD_ROOTS`, `ANCHOR_PROPS`) · `MIN_BATTLE_LINKS`
in the notebook.
