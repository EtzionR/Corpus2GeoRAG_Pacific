# What the Agent needs from the entity and graph teams

November 2026. Measured on checkpoint `cpu5747-bbebe4035e1e` (3,529 records, 1,985 Wikidata entities, 30,809 relations).

The Agent's test questions are ordinary user questions about the Pacific War, with answers checked against Wikipedia and Wikidata. When an answer is wrong, the cause is now usually in the data, not in the Agent. This file lists what we need, why, how we think it can be done, and how we'll know it worked. You know your pipelines better than we do, so treat the "how" parts as suggestions.

## Summary, most important first

| # | Team | Request | Unlocks |
|---|---|---|---|
| E1 | Entity | Fix four wrong links | Battle of Midway, Hiroshima, Doolittle, Type 93 |
| E2 | Entity | Link mentions that were left unlinked | Truk, Tarawa, Halsey and others |
| G1 | Graph | Export dates, participants, places and part-of for battles | When, who and where for almost every battle |
| G2 | Graph | Link units and people to battles | Questions about a unit or commander |
| G3 | Graph | Correct types: battles, ships, countries, cities | The map, and "what happened at" |
| G4 | Graph | Make source text smaller | Loading the full graph at all |
| G5 | Graph | One record per entity, or agree our reading | Fewer conversions on our side |
| G6 | Graph | Small export fixes | Accuracy and citations |

---

## Entity team

### E1. Fix four wrong links

**What we found.** These mentions are linked to the wrong Wikidata entity, so the real entity is missing from the graph or has almost nothing attached.

| Correct entity | QID | Paragraphs mentioning it | Linked to instead |
|---|---|---|---|
| Battle of Midway | Q173034 | 780 | Battle of Attu (Q700572), Battle of Vella Gulf (Q702142) |
| Hiroshima, the city | Q34664 | 866 | Kure (Q223439), and *Hiroshima*, John Hersey's book (Q3235195) |
| Jimmy Doolittle | Q366547 | 317 | Little Boy, the atomic bomb (Q181013) |
| Type 93 torpedo | Q1970825 | 121 | Mitsubishi A6M Zero (Q40156) |

**Why it matters.** The Battle of Midway alone is needed by many of our questions. Today the Agent can only answer about it from raw text.

**How we think you can do it.**
- Look at why these candidates won. A shared context looks likely: Midway and Attu were one Japanese operation, Hiroshima and Kure are neighbours, Doolittle and Little Boy both appear on bombing pages.
- An exact-title check may catch most cases: when a mention's text equals an entity's Wikipedia title ("Battle of Midway"), that entity should win over any context score.
- A Wikipedia link in the paragraph is stronger evidence than the mention's context. `[[Battle of Midway]]` already names the target.

**How we'll check.** Each QID in the table exists in the graph, with most of its mentions, and the wrong targets no longer hold them.

### E2. Link mentions that were left unlinked

**What we found.** These entities are mentioned in the corpus but have no record at all.

| Entity | QID | Paragraphs |
|---|---|---|
| Truk / Chuuk Lagoon | Q3593555 | 1,280 for "Truk", 553 for "Chuuk" |
| William Halsey Jr. | Q439984 | 678 |
| Battle of Tarawa | Q250309 | 178 |
| Alexander Vandegrift | Q361467 | 171 |
| Bombing of Darwin | Q696557 | 107 |
| Masaharu Homma | Q517297 | 87 |
| Atomic bombing of Hiroshima | Q703203 | 66 |
| Los Alamos, the town | Q318637 | 64 (only the laboratory, Q379848, is linked) |
| Tadamichi Kuribayashi | Q297013 | 58 |
| Sand Island, Midway Atoll | Q32015300 | 35 |
| Eastern Island, Midway Atoll | Q31913055 | 20 |

**How we think you can do it.**
- Short forms are the likely gap: "Halsey", "Homma" and "Truk" don't match a full title. A surname or short-name list built from the linked full mentions on the same page should recover most of them. Once a page says "Admiral William Halsey", a later "Halsey" on that page is the same person.
- Check whether a confidence threshold dropped them. If so, a list of must-have entities could bypass it. We can supply one.
- Hyphenated or old spellings need aliases: Truk and Chuuk are one place.

**How we'll check.** Each QID is in the graph, with mentions in the same range as the paragraph counts.

### E3. For information: not in the corpus

Taveuni, Fiji (Q1138545) has no mention at all. That's fine unless you think it should be there.

---

## Graph team

### G1. Export dates, participants, places and part-of for battles

**What we found.** Of 244 battle, campaign, operation and raid entities, your export carries far less than Wikidata holds:

| Fact | In the export | In Wikidata |
|---|---|---|
| Dates (`P580`, `P582`, `P585`) | 3 | 232 |
| Location (`P276`) | 14 | 220 |
| Part of (`P361`) | 31 | 223 |
| Participants (`P710`) | 9, Japan only | 103 |

Example: Wikidata has the Battle of Iwo Jima (Q180182) from 8 February to 26 March 1945, with the United States and the Empire of Japan, at Iwo Jima. The export keeps only `P17` and `P710 → Empire of Japan`.

**Why it matters.** Without these the Agent can't answer "when" or "who fought" from the graph.

**How we think you can do it.**
- **Dates:** your exporter writes dates only on records typed `event`, and only 4 records have that type. Applying `event_date_attributes` to every entity whose Wikidata `P31` is a battle, campaign, operation or similar should fix most of this. Your `events.json` already holds the claims.
- **Relations:** widen the relation selection for these entities to `P710`, `P276`, `P361`, `P607`, `P1344`, and keep every claim whose two ends are in the graph.
- We tested this by reading Wikidata directly for these entities. It gave dates on 232, and 2,973 extra relations across the graph.

**How we'll check.** The table's export column approaches the Wikidata column.

### G2. Link units and people to battles

**What we found.**
- The United States Marine Corps links to four events, and all four are wars. It has no link to any Pacific battle, so "locations connected to the Marine Corps" has no answer.
- Commanders: 0 battles have one in the export, and Wikidata has them for only 5 of 244.
- 250 of 475 people have conflicts in Wikidata (`P607`). Nimitz lists the Coral Sea, Leyte Gulf and Okinawa.

**How we think you can do it.**
- Export `P607` and `P1344` from people and units to battles, not only to wars.
- Commanders and sides aren't in Wikidata, but the English Wikipedia battle infoboxes have them: both sides for 230 of the 244 battles, commanders for 227. `Infobox military conflict` fields `combatant1`, `combatant2`, `commander1`, `commander2` and `units1`, `units2`, with their links resolved to QIDs, would give this. Infobox facts are CC BY-SA, so each needs its page and revision.
- Keep sides apart (who was on which side). No source states attacker or defender directly.

**How we'll check.** The Marine Corps and its divisions link to their battles, and most battles have commanders on both sides.

### G3. Correct types

**What we found.** The type comes from the mention's NER label, not from what the entity is.
- **Battles:** 145 of 244 battle-like entities have location as their main record. Only 4 records are typed `event`.
- **Ships:** 229 entities are ships, and many are typed location, like the carrier Akagi.
- **Countries:** 108 are countries, and many are drawn as places, like the Empire of Japan.
- **Cities:** Hiroshima (Q34664) has no location record and no geometry, so it can't be mapped.

**How we think you can do it.** Take the type from Wikidata `P31` rather than the NER label, using a table of classes:
- battle `Q178561`, military campaign `Q831663`, military operation `Q645883` → event
- ship `Q11446`, aircraft carrier `Q17205`, battleship `Q182531`, cruiser `Q104843`, destroyer `Q174736`, submarine `Q2811`, shipwreck `Q852190` → ship
- country `Q6256`, sovereign state `Q3624078`, historical country `Q3024240`, empire `Q48349`, colony `Q133156` → country

Keep the event's coordinates, so "where" still works. Add `P625` coordinates for places missing a record with geometry.

**How we'll check.** All 244 battle-like entities are events, ships are no longer locations, and Hiroshima has a point.

### G4. Make source text smaller

**What we found.** `graph.json` is 463 MB for 3,529 records, about 130 KB per record. At the full graph's 268,000 nodes that's about 35 GB, which can't be loaded in memory. The same paragraph is repeated on every entity it mentions: 258,000 source records hold only 76,000 distinct paragraphs. The text is raw wiki markup with full citation templates.

**How we think you can do it.** Any of these would solve it; the first two together are best.
1. Store each paragraph once in a side file, keyed by id, and have `sources[]` point to it. This alone cuts the text by about two thirds.
2. Clean the text at export: remove `<ref>` tags, citation templates and link markup. Our paragraphs average 500 characters after cleaning, against 850 before.
3. Keep only the sentence around the mention, not the paragraph.

**How we'll check.** `graph.json` without text is small, and the full text fits in a file the Agent can index separately.

### G5. One record per entity, or agree our reading

**What we found.** 1,053 of 1,985 entities have 2 to 4 records, one per NER label, while relations name the bare QID. Douglas MacArthur has 718 person mentions plus small org and misc records from mislabels such as "Nimitz-McArthur Building".

**What we do today.** We know you keep these apart on purpose, so the Agent merges the records of one `wikidata_id` when it loads: the record with geometry wins, otherwise an event, otherwise the most mentions. Nothing is lost, and all relations load.

**What we ask.** Either export one node per entity with the label counts in an attribute (`mention_types: {"PER": 718, "ORG": 5}`), or confirm that our reading is fine. Both work for us. Please just don't change the id scheme without telling us.

### G6. Small export fixes

- **Co-mention mirrors:** 14,772 reverse co-mentions have `source_edge_id` but no `derived: true`, unlike typed reverse records. Please mark them, or confirm `source_edge_id` alone means "mirror".
- **Page titles:** `sources[].page` holds `enwiki:450101`. A title field would let us cite a readable name. The URL works.
- **Relations without a real inverse:** for example, "capital" has no "capital of" (`P1376`), and the reverse record is a different fact. There are also a few exact duplicates, such as "Queensland → capital → Brisbane" twice. In cpu269 we counted 22.
- **Metadata:** `metadata.scope` still reads "ten accepted identities" on every checkpoint.

---

## A change on our side you should know about

**The Agent now covers 1938 to 1948.** This replaces the earlier agreement that events outside WWII load and stay searchable. Events entirely outside those years are removed when the Agent loads the graph, and so are dated relations entirely outside them. On cpu5747 that's 22 events and 1,693 relations, among them the Korean War, World War I and the Cuban Missile Crisis, and also 1937 events such as the Battle of Shanghai. You don't need to change the export, but you could leave these out and save space.

**Wars and theatres state no places.** The Agent reaches places from the campaign level down, so you don't need to worry about wars linked to whole continents.
