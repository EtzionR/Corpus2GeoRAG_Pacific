# Wikidata enrichment of the agent graph (build time)

A standalone step for the graph pipeline. It reads an agent graph JSON (the `agent_export` format), looks up every entity's Wikidata claims once, and writes an **enriched copy**. The GeoRAG agent then loads the copy like any other graph. The agent itself never calls Wikidata; this tool is the only part that uses the network, and only when it builds.

## What it adds
On cpu5747 (3,529 nodes, 1,985 Wikidata entities), one run adds:

| What | Where | cpu5747 |
|---|---|---|
| **Dates**: P580 start, P582 end, P585 point in time | node `attributes.start_date` / `end_date` / `point_date`, each with `<field>_precision` (Wikidata's integer: 9 year, 10 month, 11 day) | 1,020 dates |
| **Event kind**: P31 (instance of) in a hand-checked table of military-event classes | node `attributes.kind` (battle, campaign, operation, raid, siege, war, attack, landing) and `attributes.wikidata_instance_of` | 280 entities |
| **Typed relations**: P710 participant, P276 location, P361 part of, P607 conflict, P1344 participant in, P4791 commanded by, P17 country | new edges at the end of `edges` | 2,973 edges |
| **Commanders from qualifiers**: P4791 / P1037 on a P710 participant claim | `wd:P4791` edge from the battle to the commander | 1 edge |
| **Provenance of the run** | `metadata.enrichment` | |

Rules:
- **A date is added only where the record has none.** The graph's own dates always win. When Wikidata has several values, the earliest start, the latest end and the earliest point are kept, and `attributes.enrichment_notes` says so. A date is written in the graph's Wikidata form, cut to its precision: a year is `+1944-00-00T00:00:00Z`.
- **A relation is added only when both ends are entities of the graph** (by `wikidata_id`) and no edge with the same subject, object and type exists yet. A reverse view (`inverse:wd:P607`) counts as its base relation. Deprecated claims are skipped.
- **New edges** use bare QIDs as endpoints, like your export. They look like this:
  ```json
  {"id": "wdenrich:Q180182$4E2A…", "source": "Q180182", "target": "Q30", "type": "wd:P710",
   "relation_family": "PARTICIPATION_ROLE", "start_date": null, "end_date": null, "point_date": null,
   "count": 1, "provenance": "wikidata", "statement_id": "Q180182$4E2A…"}
  ```
  `relation_family` comes from your `relation_catalog` when it has the type, and is left out otherwise. Dates come from the statement's P580 / P582 / P585 qualifiers.
- **A record named only by its QID gets a name**: the English Wikidata label, or, without one, the anchor text its mentions use most. Other anchor texts used at least twice become `aliases` (up to 5). `attributes.name_source` says `wikidata_label` or `anchor_text`. Labels are fetched only for these entities and cached as `labels-*.json`.
- **A place or event with no geometry gets its P625 point** (preferred statement first, Earth only), with `geolocation_evidence.source_kind: "WIKIDATA_P625_ENRICHMENT"`. Ships, countries and broad regions get none. The graph's own geometry always wins.
- **People get their birth and death dates**: P569 / P570 (preferred statement first, else the earliest) as `attributes.birth_date` / `death_date` with their precision, for humans (P31 Q5) only. The agent uses the death date: a person's map leaves out events that began after it.
- **A relation the graph already has gets its qualifier dates** (P580 / P582 / P585 on the matching statement) when it has none, reverse views included. Co-mentions are never touched.
- **Node types are not changed.** The kind is a hint for the reader; the agent types an entity with a military-event kind as an `event` (keeping its geometry).
- **Nothing else changes:** same nodes in the same order, same edges in the same order, new edges appended, the rest of `metadata` kept.

## How to run it
```bash
uv run python -m graph_enrichment.enrich --graph graph.json --out graph.enriched.json --cache wikidata_cache/
```
- About 40 calls for 2,000 entities (50 ids per `wbgetentities` call, 1 s apart, retried with backoff on 429 and 5xx, a generic User-Agent). The first run takes about a minute; the enrichment itself about 20 s on the 463 MB file.
- The cache holds one JSON file per batch of 50 ids, and `snapshot.json`: the ids, the fetch time and the SHA-256 of all claims combined. That hash is copied to `metadata.enrichment.snapshot_sha256`.
- Keep the graph files and the cache out of git.

## Reproducing a build
```bash
uv run python -m graph_enrichment.enrich --graph graph.json --out graph.enriched.json --cache wikidata_cache/ --offline
```
With `--offline` the tool reads only the cache and stops with a clear message if a batch is missing. The same graph and the same cache give the same output and the same `snapshot_sha256`. Wikidata changes every day, so a fresh fetch gives a different snapshot: keep the cache folder next to the graph you deliver if the build must be reproduced later.

## Limits
- **Commanders are almost absent.** Wikidata records a commander (P4791, or a qualifier on a participant) for about 5 of the 244 battles in cpu5747. "Who commanded at X" still needs your extraction, or another source such as the infobox `commander` fields.
- **Participants don't say the side.** P710 lists the United States and the Empire of Japan for the Battle of Iwo Jima, but not which one attacked. Telling attacker from defender needs another source.
- **Countries are often location records only.** A participant edge to `Q30` reaches the United States record you have, which may be a `location`.
- **The military-event table is hand-picked** from the P31 classes seen on cpu5747's battles. A new class in a later export gives no kind until it is added to `MILITARY_EVENT_CLASSES` in `enrich.py`.
- **Only entities already in the graph are linked.** No node is added.

# Self-fix layer (build time)

`graph_enrichment.selffix` runs after the Wikidata enrichment above and adds what the corpus's own sources state: entities its paragraphs link to but the graph lacks, the sides, commanders, units and places of battle infoboxes, and types from Wikidata classes. It is one command from the team's raw graph:

```bash
uv run python -m graph_enrichment.selffix --graph graph.json --out selffix/graph.selffix.json --cache selffix/cache \
    --wikidata-cache wikidata_cache/ --wikipedia-cache wikipedia_cache/ [--offline] [--steps 1,3,5,6] [--keep-stages]
```

- **Stage `enrich`** always runs first: the enrichment above, from `--wikidata-cache`. On partof521 it reproduces `graph.enriched.json`.
- **Step 1, links and titles.** The graph's `sources[].text` has its wiki markup stripped, but each source's `position.raw_anchor_span` indexes the wikitext of its `processing_revision_id`. Step 1 fetches those revisions (by revision id, 10 per call), takes the paragraph (block between blank lines) around every aligned anchor, reads the `[[Title|text]]` links of its prose (links inside `<ref>` citations and `{{templates}}` name sources, not mentions), and resolves every distinct title to a QID. It changes no record; `step1_report.json` has the counts.
- **Step 3, missing entities.** A QID the prose links at least 3 times (`MIN_LINKS`) that the graph lacks is added when its class and claims put it in the theatre and the war:
  - **Class** (`graph_enrichment/classes.py`): P31, followed up P279 for at most 3 levels, against root classes for person, ship, event, unit and place; an item with P279 itself is a class (a ship type, a rank), an institution, company or work (business, publisher, museum, memorial, academy, government agency…) is none of these, and an administrative territory is a place even when it is also an occupation.
  - **Scope:** an event dated within 1938–1948 (or undated and part of a war) and in the theatre: part of the Pacific War or the Second Sino-Japanese War through P361, or located (own P625, else a P276 location's, else its P17 country's) in the Asia-Pacific box, lon 60°E across the antimeridian to 130°W, lat 50°S–60°N. A person or unit with a theatre conflict in P607, or in the `battles` field of its own enwiki infobox ("World War II" alone is not enough). A ship with a conflict in scope (or a service period from 1945 at the latest) and a dated life. A place with a P625 point in the box.
  - **Records:** name = English Wikidata label; aliases = proper-name link texts used at least twice that are not a fragment of the label, not another entity's name, and, when they only come through redirects, an English Wikidata alias; `type` from the class (`other` with `entity_class: ship` for ships, which the agent never draws as a place); a P625 point for places; one source per link occurrence (page, revision, span, anchor, the team's cleaned paragraph that holds the anchor), `provenance: "wikipedia_link"`, CC BY-SA.
  - **Duplicates:** a candidate whose label or any link text used twice names a graph record (or an earlier addition) of the same class is held back and listed, not added.
  - **Short forms:** for an added person with a P734 family name, later plain mentions of the surname on a page that links the person count as mentions, unless any other entity linked or mentioned on that page has the word in its name, the word names a graph entity, or a capitalized word other than a title stands before it ("John Halsey", "USS Halsey") or after it ("Halsey Field").
  - **Facts:** the Wikidata enrichment above runs again over the new QIDs (edges tagged `selffix_step: 3`); an event point more than 500 km from every point-like location it states is dropped.
- **Step 5, infobox facts** (`graph_enrichment/infobox.py` parses, `selffix.step5_infoboxes` writes). Each event's and each person's or unit's enwiki page is read at its current revision (cached by revision id):
  - `combatant1/2/3` → `wd:P710` with `side`: countries, units and alliances, linked or named by a flag template (`{{flag|X}}`, `{{flagcountry|X}}`, `{{flagicon|X}}`: the argument is the country); a bare flag image and anything after "Supported by" are left out.
  - `commander1/2/3` → `wd:P4791` with `side`, people only.
  - `units1/2/3` → `wd:P710` with `participant_kind: "unit"`.
  - `place` → `wd:P276` from the first link of each line, with `place_qualifier: "off"|"near"`, never to a country or region.
  - A target named on both sides is dropped. A relation the graph has is counted as confirmed (team edges unchanged; the enrichment's own edges take the side).
  - People and units: `battles` → `wd:P607` to graph battles of 1938–1948, never to a war, from links and from plain-text items that name a graph event exactly (name or alias, years compatible).
  - An entity an infobox names that the graph lacks is added (`provenance: "wikipedia_infobox"`) only for a battle of the theatre.
  - Every edge records `provenance: "wikipedia_infobox"`, `page_title`, `page_id`, `revision_id`, `field` and the licence, and is tagged `selffix_step: 5`. No attacker or defender is derived.
  - Only battles in the agent's window (1938–1948) and the theatre are read; a place link that ends a longer name or a direction ("Territory of [[New Guinea]]", "East of [[Samar]]") is no place.
- **Step 6, types and names** (`selffix.step6_types`). Each QID's class from the class table decides its records' `type` where they disagree: person, unit → `org`, event, place → `location`, ship → `other` with `entity_class: ship`, aircraft and weapons → `other`.
  - The old type stays in `attributes.ner_type`, the class in `attributes.wikidata_type`.
  - Countries and regions keep their type and `entity_class`.
  - A record is retyped to `event` or `location` only inside the agent's window and the theatre: the agent drops events outside 1938–1948 at load, and European places would reach Pacific maps.
  - A record newly typed `location` or `event` gets its P625 point.
  - Names change only on records this build named: a unit takes the corpus's own name when it passes step 5's target test; a ship whose bare label names another entity takes its enwiki title.
  - The declared changes are written to `step6_declared.json` for the impact report (`impact report --declared-type-changes`).
- **Last pass, life dates:** the people steps 3 and 5 add get their birth and death dates too, from the same cache.
- **Page titles:** the graph names its corpus pages by id (`enwiki:189095`). For the ids no source gives a `page_title` for, the build looks the title up once in the Wikipedia API by page id (cached in the Wikipedia cache as `page_titles.json`) and writes `metadata.page_titles`, so the agent shows and checks titles. Deleted pages are left out.
- **Title → QID** (`graph_enrichment/wikipedia.py`): enwiki `action=query&titles=…&redirects=1&prop=pageprops|info`, 50 titles per call, which gives the normalization and redirect mapping explicitly, then `wbgetentities?ids=…` (50 per call) for P31. A title resolves to nothing when its page is a disambiguation page (the `disambiguation` page property, or P31 Q4167410 / Q22808320), a redirect into a section of another page (`X` → `Y#Section` names a part of Y, not Y), missing, outside the article namespace, or without a Wikidata item. `wbgetentities?sites=enwiki&titles=…` is not used for this: with more than one title it follows no redirects, so it can't say which asked title became which item.
- **Caches** (all reruns can be `--offline`): `--wikipedia-cache` holds `revisions/<revid>.json` (wikitext, SHA-256, page id, title), `titles.jsonl` (one line per asked title, with the page's `lastrevid`) and `titles-raw/` (each raw API answer); `--cache` holds `wikidata-lite.jsonl` (per QID: Wikidata revision id, English label and aliases, enwiki title, and the claims of a fixed set of properties). The files are append-only and keyed per title, revision or QID, so a changed graph only fetches what is new.
- **Network etiquette:** a generic User-Agent (`georag-enrichment/0.2`), at least 1 s between calls, retry with backoff on 429 and 5xx. Wikitext is untrusted data: it is read with regular expressions, never executed.

## Impact report

`graph_enrichment.impact` measures each step through `GraphStore` only (no LLM): counts, nothing lost (every node and edge key of the graph before the step, with its name, type and geometry), the `coverage` counts, drift on a fixed probe list (`probes.json`: searches, `connected_locations`, `what_happened_at` with `place_id`, `locations_of`), new noise (ships or countries drawn as places, duplicate names, places outside the Asia-Pacific box or 1938–1948), and the held-out gold rows' `missing_in_graph`.

```bash
uv run python -m graph_enrichment.impact snapshot --graph graph.stage-enrich.json --probes graph_enrichment/probes.json --out before.snap.json
uv run python -m graph_enrichment.impact snapshot --graph graph.stage-1.json --probes graph_enrichment/probes.json --out after.snap.json
uv run python -m graph_enrichment.impact report --before before.snap.json --after after.snap.json --step 1 \
    --probes graph_enrichment/probes.json [--heldout HELDOUT.jsonl] --out impact_step1.md
```

The probes were chosen once, by rule, from the enriched graph before any step: per class (event, person, unit, place, ship) the most-mentioned entities plus a seeded random draw from the rest with at least 3 mentions, leaving out every QID of the evaluation question sets (`impact probes --exclude GOLD.jsonl ...`). The question sets and the evaluation live in the agent developer's repo.
