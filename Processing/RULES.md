# How the rules are implemented

The code is in `Processing/crawl_corpus.ipynb`. The rule functions are in the "Tagging and scope rules" cell, and
`classify(p)` applies them. Terms are defined in [`../CONTEXT.md`](../CONTEXT.md); the overview is in
[`PIPELINE.md`](PIPELINE.md).

1. **Tag:** `tag_page` takes the first non-Media Tag from four sources, in order: the infobox (`INFOBOX_TAGS`),
   intelligence categories, the Wikidata P31 class (`TAG_ROOTS`), then category keywords (`CATEGORY_TAGS`).
   `refine` then corrects the Tag from the title.
2. **Route** (`classify`): Media goes **out** (Rule 1). Battle, Campaign and Operation go to `battle_scope`
   (Rule 2). Countries and metropolises go **out** (`too_broad`). Everything else goes to `anchor_scope` (Rule 3).
3. **Rule 2, `battle_scope`:** a battle dated outside 1941–45 is **out**. A battle is **in** when any one source
   shows both the US and Japan took part: the infobox text, Wikidata P710 (`US_QIDS` / `JAPAN_QIDS`), or the
   categories. Battles that pass form the reference list (`pipe.battles`). A battle with no participants falls
   through to Rule 3.
4. **Rule 3, `anchor_scope`:** the Article's ties to reference battles come from Wikidata (`ANCHOR_PROPS`; for a
   Person, P793 is only a weak tie), from battle-named categories, and from wikilinks.
   - **In** if a Wikidata or category tie is to a battle that is not an Umbrella conflict.
   - **In** if there are 2 or more ties (`MIN_BATTLE_LINKS`) and at least one is not an Umbrella conflict.
   - **Gray** if there are other ties, or if the Article is anchored but has no Tag.
   - **Out** if there are no ties.
5. **Decide** (the "Decide" cell):
   1. Overrides win.
   2. Gray Articles are scored by e5 against `ACCEPT_AT` / `REJECT_BELOW`.
   3. A gray Article that is too close to call and tied only through Umbrella conflicts is **out**.
   4. Everything else keeps its rule verdict.

**Config:** `config/scope.yaml` (Umbrella conflicts, e5 queries) · `config/categories.yaml` (seeds, Blocked
categories) · `config/overrides.yaml` · `config/gray_labels.yaml` (threshold labels; holdout titles are never used
to set thresholds) · constants in the notebook (`TAG_ROOTS`, `US_QIDS`, `TOO_BROAD_ROOTS`, `ANCHOR_PROPS`,
`MIN_BATTLE_LINKS`).
