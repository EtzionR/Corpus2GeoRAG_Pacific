# Notes for the graph team: contract v1 and the draft graph

**From:** the agent (Michael) · **Date:** 2026-10-13 · **About:** `wwii_agent_graph_draft_v0.1.0.json` and `AGENT_CONTRACT_UPDATE_REQUEST.md`

## Summary
- **Every decision in your request is approved and implemented** in the agent, with the geometry rule you recommended (option 1). See "Decisions".
- **Your acceptance checks pass: 10 of 10** against the draft. The draft loads with no dropped nodes or edges in about 20 s (146,318 nodes, 700,890 edges, about 3 GB of memory). Every tool call takes under 0.6 s.
- **The contract works, but the data still blocks basic questions.** The places people ask about most exist only as unlabeled QIDs without coordinates, so name search finds the wrong thing: "Guadalcanal" finds the ship *USS Guadalcanal*, "Midway Atoll" finds Butaritari Atoll Airport, and "Tokyo" finds "Axis Powers". Fixing names and geometry of key places is the most valuable next step (P1 below).
- **We ran the agent's full evaluation on the draft: 9 of 56 questions pass,** against 56 of 56 on our sample graph. Most failures come from the data, not the agent. The answer text is usually right, but the place it names has no name or coordinates in the draft, or the "who" is only recorded at unit level. The **acceptance list** below names the entities to fix first.
- **Nothing here needs a format change.** Everything below is export or enrichment work within contract v1.

## Review of the fixed-10K delivery (2026-10-25)
**About:** `GG_Data/agent_export/final_fixed_10k_20261024/` (7,710 nodes, 32,899 edge views, 16,466 assertions). Both checksums match your README. We loaded the Agent JSON as delivered, then a test conversion of it, to judge the format and the content separately. **Please fix the blockers below before the full 309,864-mention run.** They're mechanical, but at full scale they would hide everything else.

### Agent changes after your alignment message (2026-10-26)
**Done in the Agent:**
- **Typed records stay separate** (`Q100::LOC`, `Q100::ORG`). A bare QID in a relation resolves through `wikidata_id`: to the one typed record, or, when there are several, to the one whose type the relation's role expects. On your 10K file, relations went from 0 loaded to 14,481 of 16,466.
  *Update 2026-11-03:* the Agent now reads the records of one `wikidata_id` as **one entity** (one node, the record with geometry, else an event, else the most mentions; old record ids still resolve). Your export can keep the typed records as they are; see the README input schema.
- **Reverse views count once.** A view with `source_edge_id` folds into its base: both directions stay queryable, the assertion counts once. All 16,433 views in the 10K fold.
- **DD/MM/YYYY is read,** and so is a precision next to each date (below). Wikidata dates are read too.
- **Geometry as WKT inside each node** (`geometry_wkt`) is read and restored as a shapely object.
- **Every evaluation records** our Agent commit, test-set hash and your graph's SHA-256.
- **The agreed rules are acceptance checks:** location lookup doesn't depend on dates, and events outside WWII load.
  *Update 2026-11-05:* the Agent now covers 1938 to 1948, and removes events outside those years when it loads the graph. What we need from you now is in `TEAM_REQUESTS.md`.

**Still open, please answer:**
1. **QIDs with several typed records:** 1,985 relations in the 10K (12%) name a QID that has several typed records, and no role says which one. Either put the typed id in the relation (simplest, exact), or tell us which rule your direction catalog gives. *Resolved on our side on 2026-11-03 by the merge above; no change needed from you.*
2. **Date precision:** if "1942" becomes 01/01/1942 with nothing else, a "June 1942" filter misses it and a "January 1942" filter wrongly finds it (we tested both). Please add the precision next to each date: `"start_date": "01/06/1942", "start_date_precision": "month"`. The Agent already reads it, and counts day-01 dates without a precision (`dates_day_one_without_precision`), so the gap stays visible.
3. **Geometry field and coordinate system:** proposal `geometry_wkt`, longitude/latitude, with `metadata.crs = "EPSG:4326"`. OK?
4. **Citations:** not agreed on our side yet; Michael has asked you about it. Until then, the Agent keeps citing sources wherever the graph provides them.

### Findings from the test runs on the 10K (2026-10-26)
We ran all 63 questions of our evaluation against the 10K, in three batches (14 of the last 43 passed): geometry as WKT in the nodes, as agreed for B2; bare QIDs resolved as above. Two problems were ours and are fixed:
- **Tool output carried full shapes:** your OpenStreetMap polygons made one call return 22 MB.
- **"Part of" relations to QIDs with several records were dropped:** the Pacific War had 0 battles, now 98.

**Data findings for the full run:**

| # | Finding | Measured on the 10K | Effect on answers |
|---|---|---|---|
| F1 | **Battles without a place** | 206 of 457 battle, campaign, raid and operation events have a location relation | Most sub-battles of the Guadalcanal campaign can't be drawn, so "show the campaign" gives Guadalcanal and Tulagi only |
| F2 | **Only one side recorded** | Only 39 battles record both the US and Japan as participants; 87 record only one (Battle of Guam 1941: only the US; Coral Sea: the US and Australia) | "Who attacked / who won X" can't be answered from the graph (see also P8) |
| F3 | **Key battles not part of their war** | 375 of 457 have a "part of" parent, but the Battle of Midway and the Battle of Iwo Jima have none | "Major battles of the Pacific War" omits Midway and Iwo Jima |
| F4 | **Entities only as unlinked synthetic nodes** | "Nagumo" is five `SYN_` nodes ("Nagumo" ×4, "Admiral Nagumo"), none with a relation. The same for "Smith" ×5, "Enterprise" ×3 (as orgs) and "1st Marines" | "Which battles did Nagumo command?" has no answer; nicknames such as "Howlin' Mad Smith" or "the Big E" find only empty nodes |
| F5 | **Places not linked to their event** | Los Alamos exists, with 2 relations, but isn't linked to the Manhattan Project, which links only to Oak Ridge | The Manhattan Project map misses Los Alamos |
| F6 | **`metadata.scope` is shown to users** | The off-topic reply now reads "…this knowledge graph's scope (Fixed 10,000 Daniel mention POC; retained Daniel identities and approved Wikidata Events; not production.)" | Internal wording reaches users. Please make `scope` a short user-facing description (e.g. "World War II"), and keep internal notes in another field |
| F7 | **Wrong geocoding of a synthetic node** | `SYN_6b468a7bbb5acbcf0470ef03` "Truk Lagoon" lies at (51.97, 177.52), which is Kiska in the Aleutians. Truk exists correctly as `SYN_2d230ede17a5ccde6b439a69` (7.42, 151.78) and as Chuuk (`Q221684`) | "What is near Adak?" lists Truk Lagoon in the Aleutians |
| F8 | **Countries as participants are location records** | 746 of 1,604 participant links (`wd:P710`) point to a location record: the United States 144, Nazi Germany 74, the United Kingdom 71. The United States has only a location record (`Q30::LOC`), while Japan's participant is an org (`Q188712::ORG`) | The US is drawn on the map as a "place" of Pearl Harbor and Midway, and "who attacked" answers mix a country shape with units. Please give countries that act an org (or country) record, or point participant links to it |
| F9 | **The forces are in the graph but not linked to their battles** | The Imperial Japanese Navy (28 relations), Imperial Japanese Army (48), US Marine Corps (29) and US Army Air Forces (26) are not linked to Midway, Corregidor, Iwo Jima, Henderson Field or the Doolittle Raid. The Combined Fleet links only to three wars ("conflict") | "Who attacked X" names only countries or nothing; the Combined Fleet's map shows Korea and Manchuria instead of Pearl Harbor and Midway (see also F2, P8) |
| F10 | **Wars that don't look like hubs** | In the 10K, World War I has 119 participants but only 7 sub-events, so the Agent's hub rule (more than 50 sub-events) treats it like a campaign. Only 365 of 2,717 events have an instance-of kind (`wd:P31`: battle 290, war 36), and World War I has none | MacArthur's and the Marine Corps' maps include Africa, Europe, Asia and the oceans through World War I. Please mark wars and theatres (P10), e.g. an event kind on every event |

**Not graph issues** (our test set was written for a small Pacific-only sample):
- The 10K correctly holds more than the test set expects: the combined "atomic bombings" event, Attu near Kiska, Stalingrad.
- Also correct: MacArthur's Korean War places (Inchon), the many Guadalcanal events at Henderson Field, events outside WWII (Paris, Nauru) as agreed.
- Nimitz, the Type 93 torpedo and USS Enterprise (CV-6) aren't in the 10K (already listed under "Missing from this sample" below).
- Slow answers in the second run (up to 3 minutes) were the model provider, not the graph: tools took at most 6 s per question.

### The good news
- **The key places are there now.** In the draft most of these were unlabeled QIDs. Now they're found by name and have coordinates: Guadalcanal, Guam, Midway Atoll, Tokyo, Iwo Jima, Tarawa, Tinian, Kiska, Darwin, Rabaul, Corregidor, Bataan and Saipan; Truk is found as Chuuk.
- **Geometry is good:**
  - 1,246 shapes, all valid
  - each with source, URL, match method and review status
  - half of them (626) are in the Pacific and East Asia
- **Relations match the catalog:**
  - every relation type is in it
  - only 37 relations carry a family that differs from the catalog's (143,130 in the draft)
- **Shapely is fine on our side.** The agent works in shapely internally, so WKB or WKT geometry needs no conversion in principle; see B2.

### Blockers: the agent can't use the export as delivered
| # | Issue | Measured | Ask |
|---|---|---|---|
| B1 | **Edges and nodes use different ids.** Nodes are `Q100::LOC`; edge endpoints are the bare `Q100`. | All 32,899 edges point at no node id, so the agent sees a graph with no relations. 100 QIDs have two or three nodes (`Q1149081::LOC` and `::ORG`), so 7,508 endpoints can't be resolved even in principle. | Use the node id in `source` and `target`. |
| B2 | **No geometry in the Agent JSON.** The shapes are only in `geometries.parquet`. | 0 of 1,272 locations have geometry in the JSON. | **Decided 2026-10-25: one file.** Put each shape in its node as WKT (`"geometry_wkt": "POLYGON((…))"`, shapely-native, lon/lat), with its source fields next to it. GeoJSON in `geometry` stays accepted. |
| B3 | **One entity, one node.** The NER mention type creates a node per type. | The Battle of Midway is `Q173034::MISC` (0 relations) and `Q173034::event` (1 relation); the Guadalcanal campaign is also an `org`; the Battle of Okinawa has three nodes. 348 names are used by several nodes. Every such search is a tie, and the relations are split. | One node per QID, with the type from Wikidata (`wd:P31`), not from the NER label. |
| B4 | **Half of the events have no dates.** *(Corrected 2026-10-26: our first count said none; it read a conversion that had dropped node attributes.)* | 1,425 of 2,717 events have dates (as Wikidata strings, e.g. the Battle of Midway 4–7 June 1942); 1,292 have none. 12,946 of 14,481 relations are undated. | Event `start_date` / `end_date` / `point_date` (`wd:P580`, `wd:P582`, `wd:P585`) wherever the source has them. "Who attacked X in 1942" depends on them. |
| B5 | **Dates are raw Wikidata strings.** | `+1942-06-04T00:00:00Z`, and `+1942-00-00…` for a year only. With these, a 1942 filter finds nothing. This is the "composite spatial date filter" issue in your manifest: it isn't an agent defect. | ISO partial dates as agreed (`1942`, `1942-06`, `1942-06-04`). We'll also make the agent accept your format, so old exports keep working. |

### High priority
- **No text and no sources.** Every node lacks `text` and `sources`, and relations lack provenance, so answers can't cite anything. Your data model has `wiki_pages` and `mentions`: please put the source page and paragraph on each node and relation (contract v1 provenance).
- **Synthetic `SYN_` nodes have no relations.** There are 2,444 of them (no QID), none linked to anything, e.g. four separate "Nagumo" nodes and nodes named "U" and "Provisional". In all, 3,092 of 7,710 nodes have no relations. Either link them or leave them out of the Agent JSON: they only cause ties.
- **Out-of-scope content.** Among the best-connected events are the Kyoto Protocol, WIPO treaties, "Liberation Day tariffs" and "keel laying". Please filter to the war's scope (`metadata.scope`).
- **Generic concepts as nodes.** "battle" (303 relations), "war", "nurse", "corps" are nodes, reached mostly through `wd:P31`. Drop `wd:P31` targets that are classes, or type them so the agent can skip them.
- **Alias flood.** 230,178 labels for 7,710 nodes: up to 857 aliases per node, 103,114 in non-Latin scripts ("Bostona", Cyrillic). A name search takes 0.8 s here, against 0.35 s on the 146k-node draft; at 309k mentions this grows. Please export English labels, Latin transliterations and real nicknames only.
- **`MISC` is not a contract type.** Use `other`, or the Wikidata type.
- **Reverse views double the edges.** Each relation also comes as `inverse:<type>` (`derived: true`). The agent reads relations in both directions itself; please drop the derived views, or keep the `derived` flag so we skip them.

### Missing from this sample (fine for a 10K POC; check in the full run)
- **People:** Nimitz and Kuribayashi aren't in it.
- **Ships and weapons:** USS Enterprise, Holland Smith's quoted alias and the Type 93 are absent, so our nickname checks can't run.
- **Battles have few relations:** the Battle of Midway has 4 (Midway Atoll, the US, the Empire of Japan, Yamamoto); Yamamoto has 22 connected places (36 in the draft). *(Corrected 2026-10-26: the first review said 1 and 3, from a conversion that dropped relations.)*

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

## What the agent's evaluation found on the draft
We ran all 56 questions of the agent's evaluation (see `EVALUATION.md`) against the draft. Each expected entity was matched to its draft QID by name, by year, or through the battle's location relation; a few were checked by hand.

- **9 of 56 pass** (8 when a combined or mistyped node doesn't count as a match).
- **In most failed answers the text is right** (e.g. "the Battle of Iwo Jima took place on Iwo Jima, at 24.78, 141.32"), but the place itself can't be returned: it has no name or coordinates. So the agent falls back on the battle's own coordinate.

Why the 47 others fail:

| Cause | Questions | What it means for the graph |
|---|---|---|
| The place exists only as an unlabeled QID without coordinates, or not at all | 14 | P1: the acceptance list below |
| The "who" is only recorded at unit level: the 1st Marine Division, not the US Marine Corps; no attacker at all for Midway | 8 | P8 |
| Set questions: expected places are missing, or drawn at the battle's coordinate instead of the island | 12 | P1, P2, P4 |
| The answer is right, but the draft holds more correct entities than the sample graph did | 5 | none (our scoring) |
| The question was written as "not in the graph" (Stalingrad, Leyte Gulf, Paris), but your broader graph has it | 3 | none: good coverage |
| Tied to a test node that exists only in our sample graph | 2 | none |
| Agent behaviour (one run hit the step limit; two "which Battle of Guam?" questions were answered for both battles instead of asking) | 3 | none: on our side |

### Acceptance list: entities the questions need
A good export makes each of these findable by its common name, with the right type and coordinates. Today's state in the draft:

| Entity | Draft today | Needed |
|---|---|---|
| Guadalcanal (Q192767), Guam (Q16635), Midway Atoll (Q47863), Tokyo (Q1490), Tarawa (Q831455), Coral Sea (Q82931), Chongqing (Q11725), Bataan (Q13739), Savo Island (Q1059040) | present, but named by QID, typed `other`, no coordinates | label, aliases, type `location`, `wd:P625` coordinates |
| Iwo Jima (Q21813724) | named, typed `other`, no coordinates | type `location`, coordinates |
| Kiska, Tinian, Nagasaki (city), Darwin, Port Moresby, Tulagi, Truk Lagoon, Taveuni, Buna, Solomon Islands, Nauru, Los Alamos | not in the draft as places (only as events, e.g. "Battle of Tinian") | add the place nodes, linked from their battles by `wd:P276` |
| Sand Island, Eastern Island (Midway) | absent | optional: add them, or keep Midway Atoll only |
| Q48809 (Oak Ridge) | the Manhattan Project's only location | add Los Alamos and Hanford (`wd:P276` / `wd:P159`) |
| Q328121 | the Doolittle Raid's place, a district, not Tokyo | link the raid to Tokyo (Q1490) and the other bombed cities |
| United States Army Air Forces (Q741723) | typed `event` | type `org` |
| Imperial Japanese Air Force (Q702107), Naval Air Service (Q1568476) | typed `event` | type `org` |
| Hickam Field (Q1617253) | typed `org`, named only "Hickam Air Force Base" | type `location`, alias "Hickam Field" |
| Island hopping (Q646500) | named "leapfrogging", typed `location` | alias "island hopping"; a strategy, not a place (type `other` or `event`) |
| Atomic bombings (Q488) | one event for both cities | fine as is, but link it to both Hiroshima and Nagasaki as places |
| Battle of Guam ×2 (Q699897, Q696900) | same name; Q699897 has dates but no coordinates; Q696900 has coordinates but only a point date; neither has a commander | distinguishing labels or aliases ("First/Second Battle of Guam"), full dates and coordinates on both, commanders (`wd:P4791` / dbpedia `commander`) |
| US Marine Corps, Imperial Japanese Army, Imperial Japanese Navy, US Navy | typed `other`; not linked to the battles they fought | type `org`; see P8 |

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

### P8: who fought whom (blocks "who attacked X" questions)
- **The draft records battles at unit level:** the 1st Marine Division at Guadalcanal, bomb squadrons in the Doolittle Raid. A question like "who attacked Guadalcanal" expects the service or country (the US Marine Corps, the Imperial Japanese Army).
- **The link from a unit up to its service is often missing,** so the agent can't climb from the division to the Marine Corps.
- **Some battles have no attacker at all.** The Battle of Midway has no Imperial Japanese Navy link, and the agent ran out of steps searching for one.
- **Ask:**
  - add `wd:P361` / `wd:P749` "part of" from units up to their service and country
  - add participants (`wd:P710`) and commanders (`wd:P4791`, dbpedia `commander`) on major battles
  - mark the side where the source has it (e.g. an edge attribute `side: attacker | defender`)
- **Also check:** the draft links some US or Japanese org to the North African campaign, so "where are the US and Japan both connected" returns 50 North African places. Please check which `wd:P607` / `wd:P710` edges cause this.

### P9: nicknames and same-name ships
Users name things by nickname: "Big E", "Mighty Mo", "Bull Halsey", "the Slot", "Cactus Air Force".

**In the draft today:**
- Only 12.6% of nodes have aliases.
- Some nicknames work, because they are plain aliases ("Mighty Mo" finds USS Missouri).
- Others exist only inside quoted full names, e.g. `Holland McTyeire "Howlin' Mad" Smith` (219 such aliases).
- Halsey has no "Bull" alias, so "Bull Halsey" finds the Tu-4 bomber, whose alias is "Bull".
- 271 groups of ships share a name (551 nodes), and few carry a hull number.

**Ask:**
- **What the agent already uses:** your aliases, the quoted part of an alias (`Holland McTyeire "Howlin' Mad" Smith`), and phrases in the text such as *nicknamed …*, *known as …*, *codename was "…"*. It keeps no nickname list of its own.
- **The text helps only if it holds such phrases.** Today's text is a short description (median 30 characters), and only 15 texts say "nicknamed" or "known as". Exporting Wikipedia paragraphs (P6) would let the agent find many more nicknames on its own.
- **Nicknames:** export Wikidata `skos:altLabel` and `wd:P1449` (nickname) and DBpedia `nickname`. If you can, give each alias a kind (`alias`, `nickname`, `hull_number`, `codename`) and its provider, as an optional `alias_details: [{label, kind, provider, record_id}]` next to `aliases`. The agent will read both.
- **Ships:** add hull numbers (`wd:P879`) and commissioning and decommissioning dates (`wd:P729` / `wd:P730`), so "Enterprise in 1942" can be told apart from a later Enterprise. Never merge same-name ships into one node.
- **Places known by a nickname:** add New Georgia Sound ("the Slot") and Ironbottom Sound with coordinates, and the codename "Cactus" on Guadalcanal.

### P10: mark wars, theatres and countries (hubs)
Some nodes link to almost everything: World War II has 1,256 parts and 27,092 edges, the Pacific War 462 parts, Empire of Japan 2,455 edges.

The agent must not treat them like ordinary entities:
- Taking part in World War II mustn't connect a person to every battle.
- Mentioning the Pacific War in an answer is no specific claim.

Today it guesses which nodes are hubs from the graph's shape: an event with more than 50 parts, or a node with more than 2,000 edges. Those thresholds are ours, and the data should state this directly.

**Ask:**
- **The level of every event,** as `attributes.level`: `war`, `theatre`, `campaign`, `operation`, `battle` or `incident`. It can come from Wikidata `wd:P31` (e.g. "theatre of war", "military campaign", "battle").
- **Mark countries and states** (`kind: "country"`), and **mark top-level armed forces** (`kind: "service"` for the US Navy, the Imperial Japanese Army…).

When these attributes are present, the agent uses them and the shape-based guess switches off. This is the same rule as for event kinds (P7) and for country-scale locations (P4).

### P11: data for operational questions (built 2026-11-10, except control)
The agent answers operational questions (control snapshots wait for dated control relations):
- **a unit's path through the war:** "E Company from England to Normandy and Carentan", shown as stops A, B, C on the map with a timeline
- **who controlled which places on a given date,** and how that changed
- **what else happened at the same time**
- **who fought in which formation**
- **what led to an event**

All of it is computed from dated graph relations; nothing comes from the model's memory. The agent answers at the level the graph holds, and offers a higher-level unit or other dates when the asked one is missing. So the more of this the full run has, the more detailed the answers.

**Ask, for the full run:**
- **Dates on events and on participation relations:** which unit was at which event, and when (`wd:P580`, `wd:P582`, `wd:P585` on the event or the relation).
- **The unit hierarchy** (`wd:P361` / `wd:P749` "part of"), down to the levels people ask about: division, regiment, battalion and, where sources have them, companies.
- **Dated control relations:** occupation, capture and liberation with start and end dates (e.g. `wd:P466` occupant, with qualifiers), so a place's controlling side can be read for any date.
- **Precise event places:** a point or area for battles, not only the country.
- **Cause and succession** between events, where the source has them (`wd:P828`, `wd:P1542`, `wd:P155` / `wd:P156`).

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

A good export makes the coverage counts above drop, and makes each of those four searches return the place itself, with coordinates. The acceptance list above is the full set of entities to check. To try questions end to end you need an OpenRouter key in `.env`: `GRAPH_PATH=/path/to/your_graph.json uv run geo-agent "Where did the Battle of Midway happen?"`.

The full input schema is in `Agent/README.md`, section "Input schema".
