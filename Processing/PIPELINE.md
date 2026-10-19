# Pacific War corpus — how it is built

**Goal:** collect every English Wikipedia article about the war between the **United States and Japan in WWII**
(battles, people, ships, units, aircraft, places, events…) and hand it over as one JSON file:
`{ article title: article text }`.

*"The app" in these docs is the corpus review app, `App/corpus_review_app.py`
(`.venv/bin/streamlit run App/corpus_review_app.py`), not the project UI in `App/app.py`.*

## At a glance

| | |
|---|---|
| **Articles delivered** | **5,693** (104.3 MB), out of 14,665 examined |
| **Wrong articles in the Corpus** | **~7.9%** (95% 4.3–12.0%), down from ~11.9% (7.2–16.9%) before the 2026-10-17 changes · *provisional: majority of two Claude sessions and GPT; all labels are by models, none verified by a person; the 40 recovery labels are by GPT alone* |
| **Recall** | **74.5%** (95% 68.8–80.8%) of relevant Articles that were found are included, up from **67.7%** (62.6–73.5%) in baseline and **67.3%** (62.1–73.0%) before recovery review; excludes 154 unmeasured recovery exclude-Overrides; **71.4%** (35 / 49) of relevant Articles from independent Wikipedia lists are found at all · *provisional, same caveat* |
| **Quality check** | 55 / 55 hand-picked must-have articles are included |
| **Accuracy on unclear cases** | ~93% when the AI check is confident (50 of 54 hand-labelled articles it never saw) |
| **Hand decisions** | 1,832 articles decided one by one (1,103 kept, 729 dropped): 1,178 in the 2026-10-12 review and 654 recovery candidates decided by a Claude session on 2026-10-17 (500 kept, 154 dropped; *not yet checked by a person*); every reason is in `reports/review_log.tsv` |
| **Run time** | ~5 min from scratch · ~20 s when re-run (everything is cached) |
| **Waiting on people** | **365** evaluation labels to verify; start with the **11** correct-vs-wrong disagreements between GPT and both Claude sessions, then the **15** Claude splits (app → Evaluation) · **9** recovery Candidates, all evaluation titles which may never become Overrides, and **7** Gray-zone cases (4 disputed battles; app → Review) · check the 654 Claude recovery decisions; the 40-Article include sample is 97.5% correct (95% 87.1–99.6%), by GPT alone |

How these numbers are measured: [`reports/precision_round1.md`](reports/precision_round1.md) and
`python Processing/evaluate.py`.

The earlier round 2 figures gave recovery Articles a smaller weight before they were included, leaving almost
all missed recovery Articles out of the recall denominator. Two baseline e5 inclusions were also mistaken for
recovery Overrides, removing two original Overrides from the count. Using the same 500-Article recovery
population in every run corrects majority baseline recall from 73.0% to 67.7% and pre-recovery recall from 72.5%
to 67.3%; current recall stays 74.5%, and wrong rates are unchanged at the precision shown. Recall excludes the
154 unmeasured recovery exclude-Overrides. All labels are by models, none verified by a person.

## The flow

```mermaid
flowchart LR
    FIND["<b>1 · Find</b><br/>Search Wikipedia's topic<br/>categories for the war"]
    COLLECT["<b>2 · Collect</b><br/>Download each article<br/>+ structured facts (Wikidata)<br/><i>14,665 articles</i>"]
    RULES{"<b>3 · Rules</b><br/>Clearly about<br/>US–Japan fighting?"}
    KEY{"<b>3b · Second key</b><br/>Do e5 and kNN<br/>both object?"}
    AI{"<b>4 · AI check</b><br/>Reads the article's<br/>war-related sentences"}
    HUMAN{{"<b>5 · Human review</b><br/><i>1,832 decided · 16 waiting</i>"}}
    IN["✅ <b>Included</b>"]
    OUT["❌ <b>Left out</b>"]
    DELIVER[("<b>Deliverable</b><br/>corpus.json<br/><i>5,693 articles</i>")]

    FIND --> COLLECT --> RULES
    RULES -- "Yes · 4,088" --> KEY
    RULES -- "No · 7,812" --> KEY
    KEY -- "No objection · 3,931" --> IN
    KEY -- "Both say unrelated · 157" --> OUT
    KEY -- "No objection · 7,060" --> OUT
    KEY -- "Both say related · 663" --> HUMAN
    KEY -- "Previously reviewed · 89" --> HUMAN
    RULES -- "Unsure · 2,765" --> AI
    AI -- "Confident yes · 659" --> IN
    AI -- "Confident no · 961" --> OUT
    AI -- "Too close to call · 1,145" --> HUMAN
    HUMAN -- "Keep · 1,103" --> IN
    HUMAN -- "Drop · 729" --> OUT
    HUMAN -- "Umbrella only · 49" --> OUT
    IN --> DELIVER

    classDef step fill:#e7f1ff,stroke:#0b5ed7,color:#052c65
    classDef human fill:#ffe5b4,stroke:#d9480f,color:#5c2200,stroke-width:3px
    classDef good fill:#d1e7dd,stroke:#146c43,color:#0a3622
    classDef bad fill:#f8d7da,stroke:#b02a37,color:#58151c
    class FIND,COLLECT,RULES,KEY,AI step
    class HUMAN human
    class IN,DELIVER good
    class OUT bad
```

*Counts are from the latest cached notebook run, checked on 2026-10-19. "Both say related · 663" is the 654 recovery
Candidates decided by a Claude session (500 kept, 154 dropped) plus 9 that still wait. "Previously reviewed · 89"
are rule-excluded Articles a reviewer confirmed; they are included in "Drop · 729". Of the 1,145 too close to call,
49 tied only to Umbrella conflicts are dropped without review and 7 Gray-zone cases still wait. All 9 remaining
recovery Candidates are evaluation titles, which may never become Overrides. The Second key now uses 1,308 frozen
kNN neighbours from `knn_neighbours.yaml`, with recovery Overrides kept out. Against step 5, only the 500 recovery
include-Overrides change inclusion (see round 2 in `reports/precision_round1.md`).*

**In plain words**

1. **Find:** start from Wikipedia topic categories such as *Pacific War* or *Battles of WWII involving Japan*,
   skipping noise like films, novels and memorials.
2. **Collect:** download every article found, plus facts from Wikidata (who fought in which battle, and which
   people, units, ships and places Wikidata records as taking part in each battle, which also finds articles
   the categories miss).
3. **Rules:** an article is in if it is a battle where the US and Japan fought **each other**, or if it is
   clearly tied to such a battle (a ship that fought at Midway, a general who commanded at Okinawa).
   Films, books and anything with no tie to a US–Japan battle are left out.
   **3b · Second key:** two AI checks may overturn the rules, but only when they agree. If an article got in only
   through links, and both e5 and kNN (a comparison with 1,308 frozen, previously labelled Articles) say it is
   unrelated, it is left out. If the rules found no tie but both say it is related, it waits for a person.
4. **AI check:** for articles the rules cannot settle, an AI language model (e5) reads the war-related sentences and
   scores how strongly the article is about US–Japan fighting rather than China, Burma, Europe or civilian life.
5. **Human review:** articles the AI cannot call either way are decided by a person. The first 1,216 were
   decided on 2026-10-12: 603 kept (mostly US Navy ships, Marines and Pacific airfields), the rest dropped. The
   654 recovery candidates of 2026-10-17 were decided by a Claude session (500 kept, 154 dropped), not yet by a person.
   Articles tied only to a whole war (an "umbrella", see step 3) are dropped automatically instead of waiting
   for review.

## How each step decides

### Step 3 · Rules: "Clearly about US–Japan fighting?"

Three rules, applied in order. Each article ends up **Yes**, **No** or **Unsure**.

**Rule 1: Culture and media are always out.**
Films, books, games, songs, museums and memorials are *about* the war, not *part of* it.
*Examples:* the film *Letters from Iwo Jima* and the *USS Arizona Memorial* are left out.

**Rule 2: A battle is in when both the US and Japan fought in it, during 1941–1945.**
This covers battles, campaigns and operations. Participants are checked in three independent places,
and any one of them is enough:

- the battle's fact box on Wikipedia (its list of combatants),
- Wikidata's structured list of participants,
- Wikipedia's own categories (*Battles involving the United States* **and** *Battles involving Japan*).

| Example | Result | Why |
|---|---|---|
| Battle of Midway | ✅ Yes | US vs Japan, 1942 |
| Battle of Kohima | ❌ Not by this rule | UK vs Japan, no US forces |
| USS Panay incident | ❌ No | US vs Japan, but 1937, before the war |
| Operation Downfall | ➡ Rule 3 | Invasion of Japan that was cancelled, so it has no combatants; judged by its ties instead |

The battles that pass, **320 of them**, are then checked once more: the fact box's combatant lists must put the US
and Japan **on opposite sides as main combatants**. Since 2026-10-17 a mere mention no longer counts: "American and
British claims" in a casualty note, the *American-British-Dutch-Australian Command*, or the US listed only for "air
support". **298** pass and form the **reference list** every other article is measured against; **13** in which the
US or Japan only supported (e.g. *Battle of West Hunan*) stay in but cannot tie another article on their own; **5** are
out (*Malayan campaign*, *Battle of Changde*…); **4** where the fact box and Wikidata disagree go to a person.

**Rule 3: Everything else is judged by its tie to the reference battles.**
This covers people, ships, units, aircraft, weapons, places and events.

| Tie to the reference battles | Result | Example |
|---|---|---|
| **Strong.** Wikidata records that it took part in one (as its conflict, as a battle it participated in, or as a significant event in its history), *or* it is filed under a category named after one, *or* it links to **two or more** of them | ✅ **Yes** | *Chester Nimitz* (took part in Midway) · *Richard E. Cole* (Doolittle Raid) · *Banzai Cliff* (Saipan) · *USS Arizona* (filed under *Attack on Pearl Harbor*) · *USS Enterprise* (dozens of battles) |
| **Weak.** Tied only to a whole war, *or* to exactly **one** battle, *or* its kind can't be determined | ❓ **Unsure** → step 4 | *USS Bache* (only Leyte Gulf) · a Japanese general tied only to the *Second Sino-Japanese War* |
| **None** | ❌ **No** | *Dwight Eisenhower* (Europe) · Atlantic convoy ships |

Why weak ties are not enough on their own:

- **A whole war is too broad.** The *Pacific War*, the *Second Sino-Japanese War* and the *Burma campaign* include a
  great deal of fighting without the US, for example Chinese generals and British regiments. Counting a tie to a whole war as proof
  let in about 800 off-topic articles, so these "umbrella" wars can only *support* a tie, never be the only one.
- **One link is often a passing mention.** Eisenhower's article mentions Pearl Harbor once.
- **For people, a "significant event" is often one they only witnessed.** Wikidata lists the atomic bombings as a
  significant event of dozens of photographers and artists, so for people that claim is only a weak tie.
- **Countries and metropolises are tied to everything.** *United States*, *Australia*, *Tokyo* and *Bangkok* link
  to many battles without being about the fighting, so they are always left out unless a reviewer includes them.
  Ordinary cities such as *Hiroshima* or *Rabaul* are battle sites and are judged normally.

**Outcome** (run of 2026-10-17): 4,088 Yes · 7,812 No · 2,765 Unsure.

### Step 4 · AI check: "Is this article really about US–Japan fighting?"

For the 2,765 unsure articles, an AI language model (**e5**) reads the article's war-related sentences, meaning every
sentence that names a theatre or a side (Japan, Pacific, Okinawa, China, Burma, Atlantic…). It compares them with two
descriptions written in plain English:

- ✅ *"An American or Japanese ship, unit, person, aircraft or place that took part in the fighting between the
  United States and Japan in the Pacific."*
- ❌ *"The war in China, Burma, Europe or the Atlantic, or civilian life, not involving combat between the
  United States and Japan."*

The article gets a score for how much closer it is to the first description than to the second. Two cut-offs turn
the score into a decision. They were first set from about 80 articles labelled by hand; the review decisions from
step 5 now count as labels too (about 1,200 in total), and the cut-offs barely moved:

| Score | Decision | Count |
|---|---|---|
| High: at least 90% of labelled articles at this level belonged | ✅ Include | 659 |
| Low: at least 90% of labelled articles at this level did not belong | ❌ Leave out | 961 |
| In between | ➡ Human review (step 5) | 1,145 |

The cut-offs were then tested on a **second, separate set of 68 hand-labelled articles** that played no part in
setting them, and are never used to set them. **93%** (50 of 54) of the AI's confident decisions were correct.
Typical AI misses are people whose war service is a small part of their biography, such as a baseball player who fought on Iwo Jima.

### Step 5 · Human review: "Keep or drop?"

A reviewer opens the review list (`gray_zone.csv` or the app's Review tab, most likely articles first) and marks each
article *keep* or *drop*. The decisions are saved in one small file (`overrides.yaml`) and:

- beat every rule and the AI on all future runs,
- are reused to tune the AI's cut-offs, so the review list gets shorter over time (except for the 68 test articles,
  which are kept out of tuning so the accuracy check stays honest).

**How the 2026-10-12 review decided.** *Keep* means the article's subject served in, or was directly part of, the
fighting between the US and Japan in 1941–45. *Drop* covered Atlantic-only US ships, ships that reached the Pacific
only after the surrender, fighting between Japan and Britain, Australia, the Netherlands or China with no US forces,
Japanese officers who served only in China, photographers, artists and civilian survivors, memorials and postwar
bases. Each decision and its one-line reason is in `Processing/reports/review_log.tsv`.

**Umbrella-only articles are dropped without review.** If the AI cannot call an article and its only tie is to a
whole war (the umbrella list in `scope.yaml`), it is left out automatically; 38 articles were dropped this way.
A reviewer can still bring one back with an override.

## Where people are needed

| Task | How often | Effort | Why it matters |
|---|---|---|---|
| Review new "too close to call" articles | After a re-run finds new articles | Usually a handful | The first full pass (1,216 articles) is done |
| Verify the evaluation labels | Once, then when the set grows | 365 labels; start with the 11 GPT / two-Claude disagreements and 15 Claude splits | All three labellers are models; none of these labels has been verified by a person |
| Decide recovery candidates | After a re-run | 9 now, all evaluation titles kept out of Overrides; spot-check the 654 decided by Claude | Frozen kNN neighbours prevent recovery decisions from changing other Candidates |
| Check noisy topic categories | After big changes | Minutes | Keeps the search focused |
| Label a sample of unclear articles | Rarely | ~80 articles | Keeps the AI check honest and measurable |
| Confirm the must-have list | Rarely | Minutes | Guarantees key articles are never lost |

Every human decision is saved in a small config file and applied automatically on the next run.

## Open decisions

- **British ships in US-led operations.** British Pacific Fleet ships that fought alongside US forces are currently
  *in* (*HMS Howe*, *HMS Indomitable* and the *King George V* class, which joined the Okinawa operation). *HMS Duke of
  York*, which arrived too late to fight, is out. If British-only crews should be out entirely, flip these in
  `overrides.yaml`.

---

<details>
<summary><b>Technical detail (for engineers)</b></summary>

Terms are defined in `CONTEXT.md`; all config lives in `Processing/config/`. Where each rule lives in the code:
[`RULES.md`](RULES.md).

```mermaid
flowchart TD
    subgraph STREAM["Streaming pipeline — parallel workers, every result cached in cache.sqlite"]
        CRAWL("Category crawl<br/>categories.yaml: seeds, depth, Blocked")
        CAND("Candidates")
        FETCH("Fetch pages — 4 workers<br/>wikitext · categories · Wikidata id")
        CLAIMS("Wikidata — 3 workers<br/>P31 instance of · P607 conflict · P710 participants<br/>P1344 participant in · P793 significant event")
        TAG("Tag: infobox → Intelligence cats → P31 class → category keywords")
        PARTIC{"Battle-type with US + Japan<br/>participants, dated 1941–45?"}
        BATTLE("In-scope Battle")
        EXPAND("Expansion: articles whose P607 / P1344 / P793 = this battle")
        EMBED("e5 embedding — GPU thread<br/>title + war-related sentences")
        CRAWL --> CAND --> FETCH --> CLAIMS --> TAG --> PARTIC
        PARTIC -- yes --> BATTLE --> EXPAND -- new titles --> CAND
        FETCH --> EMBED
    end

    PARTIC -- no --> BROAD{"Country or metropolis?<br/>(P31 class)"}
    BROAD -- no --> ANCHOR{"Tie to In-scope Battles<br/>(Umbrella conflicts never count alone)"}
    BATTLE --> IN["In"]
    ANCHOR -- "strong: P607 / P1344 / P793 (not P793 for a Person) · battle-named Category · ≥2 battles" --> IN
    ANCHOR -- "none / Media" --> OUT["Out"]
    BROAD -- yes --> OUT
    ANCHOR -- "weak: umbrella only · one battle · untagged" --> REL

    EMBED --> REL("Relevance = sim(in_scope query) − sim(out_of_scope query)<br/>queries in scope.yaml")
    REL --> ZONE{"vs thresholds from gray_labels.yaml<br/>(90% precision)"}
    ZONE -- "≥ accept_at" --> IN
    ZONE -- "< reject_below" --> OUT
    ZONE -- between --> UMB{"Umbrella only?"}
    UMB -- yes --> OUT
    UMB -- no --> WAIT{{"Review band — gray_zone.csv<br/>(empty after the 2026-10-12 review)"}}
    WAIT -- "overrides.yaml include / exclude" --> FINAL
    IN --> FINAL("Apply overrides")
    OUT --> FINAL
    FINAL --> CORPUS[("Data/corpus.json")]
    FINAL --> REPORTS("Reports: gold recall · category_review.csv · holdout accuracy")

    classDef wait fill:#ffe5b4,stroke:#d9480f,color:#5c2200,stroke-width:2px
    class WAIT wait
```

| Waiting on | Look at | Edit |
|---|---|---|
| Review band | `Data/interim/gray_zone.csv` (`decision = review`) | `config/overrides.yaml` (reasons in `reports/review_log.tsv`) |
| Noisy discovery | `Data/interim/category_review.csv` (`suggest_block`) | `config/categories.yaml` → `blocked` |
| Missing gold article | Gold-recall report in the notebook | `config/categories.yaml` seeds, or a rule |
| Threshold quality | Holdout line in the notebook | `config/gray_labels.yaml` |
| What a change did | `Data/interim/changes.csv` (articles added / removed vs the previous run) | the rule or config you changed |

Gray-zone overrides are added to the calibration labels automatically, except for titles in the `holdout` section of
`gray_labels.yaml`, so the holdout accuracy is never measured on articles that tuned the cut-offs.

</details>
