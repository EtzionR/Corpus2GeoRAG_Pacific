# Notes for the graph team: contract v1 and the draft graph

**From:** the agent (Michael) · **Date:** 2026-10-13 · **About:** `wwii_agent_graph_draft_v0.1.0.json` and `AGENT_CONTRACT_UPDATE_REQUEST.md`

## Summary
- **Every decision in your request is approved and implemented** in the agent, with the geometry rule you recommended (option 1). See "Decisions".
- **Your acceptance checks pass: 10 of 10** against the draft. The draft loads with no dropped nodes or edges in about 20 s (146,318 nodes, 700,890 edges, about 3 GB of memory). Every tool call takes under 0.6 s.
- **The contract works, but the data still blocks basic questions.** The places people ask about most exist only as unlabeled QIDs without coordinates, so name search finds the wrong thing: "Guadalcanal" finds the ship *USS Guadalcanal*, "Midway Atoll" finds Butaritari Atoll Airport, and "Tokyo" finds "Axis Powers". Fixing names and geometry of key places is the most valuable next step (P1 below).
- **Nothing here needs a format change.** Everything below is export or enrichment work within contract v1.

## Decisions (your section 7)
| # | Your request | Decision | What the agent does |
|---|---|---|---|
| 1 | QIDs as node ids | Approved | Ids are opaque strings; `Q362` and readable slugs both work. |
| 2 | Level 1 + Level 2 relations | Approved | `type` is the exact Level 2 id; `relation_family` is read from the edge, then its attributes, then `relation_catalog`. Filters take either. `list_relations(family)` lists a family's relations. |
| 3 | No forced uppercase | Approved | `wd:P607` matches exactly. Short sample types such as `ATTACKED` still match case-insensitively. |
| 4 | Point dates | Approved | A `point_date` is a one-day interval in every date filter and is kept in outputs. |
| 5 | Generalized provenance | Approved | Sources are `{provider, record_id, url, page, paragraph}`. Answers cite only real Wikipedia pages in `sources`; everything else goes to a separate `source_refs` list. A statement id is never shown as a page. |
| 6 | Geometry on non-location nodes | **Option 1** | Any node may carry geometry. The map marks `geometry_source: direct` (the node's own coordinate) or `related` (drawn at a linked place). |
| 7 | Scope from metadata | Approved | The agent's instructions and its off-topic reply use `metadata.scope`, so it no longer calls the graph "Pacific only". |

Also done: one edge per source assertion (assertions with the same subject, object, relation and dates are combined at query time, so duplicates don't inflate answers); `other` as a node type; coverage counts in `graph_schema()` so data gaps stay visible.

## How the agent reads your relations
"Show me all locations connected to X" follows fixed **roles**, each a list of exact Level 2 relations with a direction. Whole families are never followed, because every family mixes useful relations with hubs: SPATIAL also holds place of birth, sports and fictional characters.

| Role | Relations (INV = edge points the other way) |
|---|---|
| participation (actor → event) | `COMMANDED`, `PARTICIPATED_IN`, `wd:P607`, `wd:P1344`, yago `participatedIn`, `wd:P598`; INV `wd:P710`, dbpedia `commander`, `wd:P4791`, `wd:P8031` |
| occurrence (event → place) | `OCCURRED_AT`, `wd:P276`, `wd:P131`, `wd:P706` |
| action at a place | `ATTACKED` … `BASED_AT`, `wd:P159`, dbpedia `garrison`; INV `wd:P466` |
| part of | `PART_OF`, `wd:P361`, `wd:P749`, dbpedia `commandStructure`, `parentOrganisation`; INV `wd:P527`, `wd:P355`, `childOrganisation` |
| located in | `LOCATED_IN`, `wd:P131`, `wd:P706` (not `wd:P17`, which in the draft mostly links ships and units to a country) |
| a person's places | `wd:P19`, `wd:P20`, `wd:P119`, `wd:P551`, `wd:P937`, `wd:P2632`, dbpedia `birthPlace` / `deathPlace` |

These are never followed: AFFILIATION_STATUS, AWARD_RECOGNITION, EDUCATION_EMPLOYMENT, FAMILY_PERSONAL, CREATION_AUTHORSHIP, OWNERSHIP_CONTROL, CAUSAL, TEMPORAL_SUCCESSION, FORMATION_DISSOLUTION, and UNRESOLVED (except the participation relations above). An event with more than 50 sub-events, reached through participation, adds nothing: World War II has 1,710 parts and the Pacific War 580, and taking part in the war mustn't connect a person to every battle.

**If a relation should play one of these roles, or must never be followed, tell us.** The table is one place in the code.

## What needs to be done, by priority
Counts are from the agent's `graph_schema()` coverage on the draft (2026-10-13).

### P1: names and geometry of key places (blocks basic questions)
- **92,307 nodes use their QID as the name** and **663 of 1,172 locations have no geometry**. Among them are the places the project is about: Guadalcanal, Midway Atoll, Tokyo and Kiska can't be found by name or drawn on a map.
- **Ask:** fetch English labels and aliases (`rdfs:label`, `skos:altLabel`) for every node an edge touches, and coordinates (`wd:P625`) for every `location`. Start with places linked by an occurrence relation (`wd:P276` etc.) to a battle or campaign.

### P2: node types
- **110,744 nodes are `other`,** including the IJN and the US Navy.
- **Ships are mistyped:** USS Yorktown is a `location`, and other ships are `other` with wreck coordinates. A ship drawn as a "place connected to X" is wrong on the map.
- **Ask:** classify via `wd:P31` (instance of) into person / org / event / location; type ships and other vessels as their own type (e.g. `vessel`) or as `other`, never `location`. Military units and navies are `org`.

### P3: relation families vs the catalog
- **143,130 edges carry a family different from the catalog's** (typically the catalog says UNRESOLVED and the edge has a candidate, e.g. `wd:P793` → SPATIAL).
- **61 relation types used on edges are missing from the catalog** (e.g. `wd:P31`, `wd:P21`, `wd:P279`), and 8 types carry more than one family.
- **Two participation relations are tagged SPATIAL on edges:** `wd:P1344` "participant in" and yago `participatedIn`. Our role table works around that, but the label is wrong.
- **102,154 edges are UNRESOLVED.**
- **Ask:** make each relation's family come from one place (the catalog), add the 61 missing types, and fix the mislabeled participation relations.

### P4: locations at the wrong scale
- **Some occurrence locations are whole countries,** e.g. Battle of Midway → Empire of Japan via `wd:P276`. A map then draws a battle over a country.
- **Ask:** prefer the most specific place (island, atoll, city, sea area), and drop country-level `wd:P276` values when a finer one exists, or mark them (e.g. `attributes.scale: "country"`) so the agent can skip them.

### P5: dates
- **Some dates are impossible:** edge dates range from year 0044 to 6194.
- **Coverage is thin:** 618,963 edges have no date; 3,864 events have none even after point-date candidates.
- **Ask:** drop or flag dates outside a plausible range (e.g. 1900–1960 for war events), and add event dates (`wd:P580` / `wd:P582` / `wd:P585`) where sources have them. Questions such as "who attacked X in 1942" depend on dates.

### P6: text for citation
- **94,444 nodes have no text,** and most other text is a short Wikidata description.
- **If answers must cite Wikipedia paragraphs, export them.** Use the node's `text` plus `sources` with `page` / `paragraph`. Today most answers can cite only Wikidata or EventKG records, which go to `source_refs`, not `sources`.

### P7: structure that set questions need (from the business user's question list)
- **`attributes.kind` on events** (battle, raid, landing, campaign, operation, bombing) and **on locations** (island, atoll, city, port, airfield, sea, region). Without kinds the agent guesses from names and says so.
- **`PART_OF` between events** (battle → campaign → war) and between orgs (Combined Fleet → IJN).
- **Campaign and operation nodes** people ask about: the island-hopping campaign, the Manhattan Project, the atomic bomb missions.
- **`attributes.country` on orgs and people** ("Japan", "United States"), needed for "where are the US and Japan both connected".
- **Optional:** `attributes.importance` on events, to rank "major" battles.

### Fine as is
- **74,111 duplicate assertions:** combined at query time, so they don't distort answers.
- **7,096 non-location nodes with geometry** (2,033 events, 897 orgs, 4,160 other, 6 people): allowed under option 1. Please confirm they describe the node itself (e.g. a battle site), not a related place.

## How to check a new export yourselves
From the `Agent/` folder (Python 3.11+, [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
GEO_AGENT_BIG_GRAPH=/path/to/your_graph.json uv run pytest tests/test_big_graph.py   # your acceptance checks 2-10
uv run python -c "
from geo_agent.graph_store import GraphStore
import json, sys
s = GraphStore.from_json(sys.argv[1]); sch = s.schema()
print(json.dumps({'nodes': sch['node_count'], 'edges': sch['edge_count'], 'coverage': sch['coverage']}, indent=1))
for q in ['Guadalcanal', 'Midway Atoll', 'Tokyo', 'Iwo Jima']:
    print(q, '->', [(r['id'], r['name'], r.get('lat')) for r in s.search_entities(q, limit=1)])
" /path/to/your_graph.json
```

A good export makes the coverage counts above drop, and makes each of those four searches return the place itself, with coordinates. To try questions end to end you need an OpenRouter key in `.env`: `GRAPH_PATH=/path/to/your_graph.json uv run geo-agent "Where did the Battle of Midway happen?"`.

The full input schema is in `Agent/README.md`, section "Input schema".
