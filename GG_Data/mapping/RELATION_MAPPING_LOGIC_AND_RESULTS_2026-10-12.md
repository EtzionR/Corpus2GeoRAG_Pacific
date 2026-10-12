# Relation Mapping Logic and Results

**Delivery date:** 2026-10-12

**Sources:** EventKG, Wikidata, and WarSampo

**Scope:** Relations present in the current local source inventories

**Relation map version:** `relation-map-v1.0.0`

## 1. Purpose

This process creates one common relation catalog without changing any source
graph. Each source predicate remains available with its original identifier.
The catalog adds a final Level 2 identifier, direction, Level 1 family,
decision method, status, and evidence.

The map supports three uses:

1. Reuse an existing Wikidata relation when two source predicates represent the
   same relation.
2. Preserve a source-specific relation when no valid common relation is known.
3. Record provisional mappings separately from accepted mappings.

## 2. Relation model

### Level 1: relation family

Level 1 is a broad semantic family, such as spatial, participation, hierarchy,
or affiliation. A source ontology supplies the family where possible. A
data-supported candidate can be recorded when the ontology does not provide a
confirmed family.

### Level 2: specific relation

Level 2 is the relation that is stored on an edge. It can be:

- a Wikidata property, such as `wd:P276`;
- a scoped project identifier, such as `P276+OBJECT_TYPE_CITY`;
- a deterministic combined identifier for a source predicate that normalized
  several properties; or
- the original source predicate URI when no valid cross-source mapping exists.

Different Level 2 relations may remain under the same Level 1 family. They are
not merged only because they share a family.

### Direction and provenance

Direction is part of every mapping. Reverse relations are transformed before
they enter the common direction. Each output row also keeps its source
predicate and decision evidence. Two relations can connect the same entities
when separate source statements support different meanings.

## 3. Mapping process

### Step 1: inventory the source relations

The process reads every relation type found in the three local sources. It
records source labels, occurrence counts, source ontology information, endpoint
types, and existing mappings.

### Step 2: keep native and declared mappings

Wikidata properties keep their Wikidata IDs. Existing exact catalog mappings
from EventKG or WarSampo reuse the same Wikidata ID when the declared meaning
and direction agree.

### Step 3: test endpoint overlap

Relations from two sources are compared only when both endpoints refer to the
same Wikidata items. The process tests the target property and its direction.

An observation-based mapping is provisional only when all these rules pass:

- at least 20 shared endpoint pairs;
- at least five held-out pairs and five distinct targets;
- at least 90% pair coverage;
- at least 80% coverage after the largest target is removed;
- at least 80% held-out coverage;
- a score lead of at least 15 points over the next candidate; and
- all accepted contexts select the same target relation and direction.

Seven EventKG relations passed these tests. They remain provisional because
repeated endpoint agreement does not prove complete semantic equivalence.

### Step 4: audit EventKG relations without endpoint overlap

The process then checks public mapping evidence from EventKG, DBpedia, YAGO,
and Wikidata. This audit covered 217 EventKG predicates that did not have a QID
endpoint-pair overlap result.

The audit produced:

- 43 public declared exact mappings;
- 4 automatic alignment candidates;
- 2 conflicting declared mappings;
- 3 broader or narrower mappings;
- 1 normalized multi-option mapping; and
- 164 predicates with no declared public mapping.

### Step 5: resolve conflicts and broader mappings

Conflicts are resolved with the public property definitions, expected domain
and range, endpoint data types, and observed local endpoints.

Broader mappings keep the missing restriction inside the Level 2 identifier.
This prevents loss of meaning. If EventKG deliberately combines several source
properties and the original property cannot be recovered, the catalog uses one
sorted combined identifier with explicit reverse members.

### Step 6: retain relations with no public map

The 164 EventKG predicates with no declared public mapping remain valid,
independent Level 2 relations. Their final ID is their source predicate URI.
The process does not guess a Wikidata property.

The same rule keeps other source-only EventKG and WarSampo relations as
independent Level 2 relations.

### Step 7: assign a review status

- `ACCEPTED`: native, declared exact, reviewed special case, or deliberately
  retained independent relation.
- `PROVISIONAL`: strong automatic or observation-based candidate that still
  needs semantic confirmation.

## 4. Observation-level mapping logic

This layer applies the relation catalog to source records. It creates at most
one selected Level 2 relation for one relation observation. It does not change
or delete the source records used to make that selection.

### 4.1 Define one observation

A source assertion is one relation record from one source. A relation
observation can contain several source assertions only when they describe the
same fact. They must have:

- the same canonical subject and object Wikidata IDs;
- the same direction after direction normalization;
- compatible evidence references or compatible relation periods; and
- no evidence that they describe different occurrences.

Every entity endpoint must have a Wikidata ID. An assertion with an unresolved
endpoint stays outside the final graph until identity resolution supplies that
ID. The process does not create a replacement entity ID.

The same entity pair can produce several observations. Keep them separate when
they have different references, relation meanings, or periods. Therefore, one
pair can have different selected relations when different evidence supports
them.

### 4.2 Normalize each candidate

For each source assertion:

1. Map its source predicate through `final_relation_source_map.csv`.
2. Apply the recorded direction transformation.
3. Keep the original predicate, record ID, evidence reference, mapping status,
   and mapping method.
4. Add the source assertion to an observation only when the rules in section
   4.1 pass.

### 4.3 Select one relation

Select the first available candidate in this order:

1. A resolved WarSampo and Wikidata relation: a WarSampo predicate has an
   accepted mapping to a Wikidata property. Use that common Wikidata Level 2
   ID and its normalized direction. A matching Wikidata assertion, when
   present, becomes supporting evidence; it is not required for this priority.
2. An unresolved WarSampo relation: keep the independent WarSampo Level 2 ID.
3. An EventKG relation: use its mapped or independent EventKG Level 2 ID.
4. A Wikidata relation: use its Wikidata property ID.

This priority applies only to source assertions already grouped as the same
observation. It must not remove a second fact that has different evidence,
meaning, direction, or time.

If two candidates remain at the same priority and the evidence does not select
one, set `selection_status=AMBIGUOUS`, keep the candidate list, and do not make
an arbitrary final edge. A later classifier or review can resolve that case.

The selected output keeps all supporting source assertions in provenance. A
lower-priority assertion is evidence for the selected relation; it is not a
second output relation for that observation.

### 4.4 Map relation time

Time is an attribute of the relation observation. It is not a relation type.
Read only time values attached to the selected relation assertion or to an
equivalent supporting assertion in the same observation.

| Source | Start input | End input | Point input |
|---|---|---|---|
| Wikidata | `P580` | `P582` | `P585` |
| EventKG | `sem:hasBeginTimeStamp` | `sem:hasEndTimeStamp` | — |
| WarSampo | `crm:P4_has_time-span` → `crm:P82a_begin_of_the_begin` | `crm:P4_has_time-span` → `crm:P82b_end_of_the_end` | — |

Apply these rules:

1. Store a start value in `time_from` and an end value in `time_to`.
2. Store `P585` in `time_point`. Do not turn a point into an artificial period.
3. If only one boundary exists, leave the other boundary empty.
4. Keep the source precision, such as day, month, or year. Do not invent a day
   for a year-only value.
5. Do not copy an event date, entity lifespan, or article date to the relation
   unless the source explicitly attaches that time to the relation assertion.
6. If equivalent sources disagree, use the time attached to the selected
   assertion, retain every conflicting raw value in provenance, and set
   `time_status=CONFLICT`.
7. If the selected source has several incompatible time values, keep them for
   review and leave the single normalized value empty.
8. Validate that `time_from` is not after `time_to`. Flag an invalid source
   period; do not silently reverse it.

The current local inputs confirm that these values are available, but not for
every relation:

- EventKG has at least one relation boundary on 118,763 of 1,064,397 reified
  relation observations; 95,484 have both boundaries.
- The scoped Wikidata data has a time qualifier on 74,288 of 572,495
  non-deprecated item-valued statements; 18,152 have both `P580` and `P582`.
- WarSampo contains linked CIDOC CRM time spans. Its time coverage must be
  measured after joining each activity or event to its time-span record.

### 4.5 Required output fields

| Field | Purpose |
|---|---|
| `observation_id` | Stable ID for the grouped fact |
| `subject_qid` | Canonical subject Wikidata ID |
| `object_qid` | Canonical object Wikidata ID |
| `level1_relation` | Broad source-supported relation family |
| `level2_relation` | One selected final relation |
| `selection_priority` | Applied priority from 1 to 4 |
| `selection_status` | `SELECTED` or `AMBIGUOUS` |
| `direction` | Normalized edge direction |
| `time_from` | Relation start, when available |
| `time_to` | Relation end, when available |
| `time_point` | Relation point in time, when available |
| `time_precision` | Precision of each normalized time value |
| `time_status` | `DIRECT`, `PARTIAL`, `CONFLICT`, `MULTIPLE`, or `NONE` |
| `source_support` | All source assertions and original predicates |
| `evidence_references` | Source record and text or graph references |
| `relation_map_version` | Version used to select the relation |

### 4.6 Version rule

Every mapped observation must store `relation_map_version`. The current value
is `relation-map-v1.0.0`.

- Increase the major version when an existing relation changes meaning or the
  output contract becomes incompatible.
- Increase the minor version when a relation mapping or selection rule is
  added or changed without breaking the output contract.
- Increase the patch version for corrections that do not change a selected
  relation or its meaning.

Keep the map version with the output data so a later run can reproduce each
selection.

## 5. Final results

| Measure | Result |
|---|---:|
| Source relation rows | 1,520 |
| Final Level 2 identifiers | 1,051 |
| Final identifiers shared by more than one source | 405 |
| Accepted or retained source rows | 1,509 |
| Provisional source rows | 11 |
| Source data rows changed | 0 |

### Source inventory

| Source | Relation types | Final Level 2 IDs used |
|---|---:|---:|
| EventKG | 808 | 748 |
| Wikidata | 601 | 601 |
| WarSampo | 111 | 111 |
| **Total** | **1,520** | — |

## 6. WarSampo results

WarSampo contributes 111 relation types. Four are mapped to common Wikidata
relations. The other 107 remain independent WarSampo Level 2 relations.

| WarSampo relation | Final Level 2 | Direction | Status |
|---|---|---|---|
| `warsa:actors/hasConflict` | `wd:P607` | same | accepted |
| `crm:P10_falls_within` | `wd:P361` | same | accepted |
| `crm:P11_had_participant` | `wd:P710` | same | accepted |
| `crm:P7_took_place_at` | `wd:P276` | same | accepted |

## 7. Reviewed EventKG special cases

| EventKG relation | Final Level 2 | Reason |
|---|---|---|
| `dbo:locationCity` | `P276+OBJECT_TYPE_CITY` | Retains the city restriction missing from the broader `P276` relation. |
| `dbo:locationCountry` | `P276+OBJECT_TYPE_COUNTRY` | Retains the country restriction missing from the broader `P276` relation. |
| `dbo:nextEvent` | `P156+EVENT_TO_EVENT` | Retains the event-to-event restriction. |
| `dbo:position` | `wd:P413` | Observed objects are sports-position items; `P1545` requires a string ordinal. |
| `dbo:voice` | `wd:P725` | Observed objects are people; `P990` requires an audio file. |
| `schema:containedInPlace` | `COMBINED[P17\|P131\|P706\|P1376\|P3018\|REV(P36)\|REV(P150)\|REV(P1383)]` | EventKG normalized eight Wikidata properties and did not retain the original property. |

The combined `containedInPlace` direction is **contained object to containing
place**. `P36`, `P150`, and `P1383` are reversed before the common edge is
created.

## 8. Public declared exact EventKG mappings

| EventKG relation | Wikidata relation |
|---|---|
| `dbo:affiliation` | `wd:P1416` |
| `dbo:anthem` | `wd:P85` |
| `dbo:architecturalStyle` | `wd:P149` |
| `dbo:artist` | `wd:P175` |
| `dbo:basedOn` | `wd:P144` |
| `dbo:capital` | `wd:P36` |
| `dbo:colour` | `wd:P462` |
| `dbo:composer` | `wd:P86` |
| `dbo:computingPlatform` | `wd:P400` |
| `dbo:creator` | `wd:P170` |
| `dbo:crewMember` | `wd:P1029` |
| `dbo:crosses` | `wd:P177` |
| `dbo:curator` | `wd:P1640` |
| `dbo:currency` | `wd:P38` |
| `dbo:developer` | `wd:P178` |
| `dbo:diocese` | `wd:P708` |
| `dbo:discipline` | `wd:P101` |
| `dbo:district` | `wd:P131` |
| `dbo:editor` | `wd:P98` |
| `dbo:era` | `wd:P2348` |
| `dbo:homeport` | `wd:P504` |
| `dbo:ideology` | `wd:P1142` |
| `dbo:industry` | `wd:P452` |
| `dbo:inflow` | `wd:P200` |
| `dbo:instrument` | `wd:P1303` |
| `dbo:language` | `wd:P407` |
| `dbo:locatedInArea` | `wd:P131` |
| `dbo:manager` | `wd:P286` |
| `dbo:material` | `wd:P186` |
| `dbo:mother` | `wd:P25` |
| `dbo:movement` | `wd:P135` |
| `dbo:outflow` | `wd:P201` |
| `dbo:owningOrganisation` | `wd:P1830` |
| `dbo:picture` | `wd:P18` |
| `dbo:placeOfBurial` | `wd:P119` |
| `dbo:primeMinister` | `wd:P6` |
| `dbo:profession` | `wd:P512` |
| `dbo:province` | `wd:P131` |
| `dbo:recordLabel` | `wd:P264` |
| `dbo:religion` | `wd:P140` |
| `dbo:subsidiary` | `wd:P355` |
| `dbo:team` | `wd:P54` |
| `dbo:translator` | `wd:P655` |

These mappings are accepted from published exact ontology paths in the current
catalog. Their evidence paths are retained in the machine-readable decision
file.

## 9. Provisional mappings

### Automatic EventKG alignment candidates

| EventKG relation | Candidate Wikidata relation |
|---|---|
| `yago:graduatedFrom` | `wd:P69` |
| `yago:hasCapital` | `wd:P36` |
| `yago:hasCurrency` | `wd:P38` |
| `yago:hasOfficialLanguage` | `wd:P407` |

### Observation-overlap candidates

| EventKG relation | Candidate Wikidata relation | Maximum shared pairs |
|---|---|---:|
| `dbo:award` | `wd:P166` | 719 |
| `dbo:battle` | `wd:P607` | 7,178 |
| `dbo:nonFictionSubject` | `wd:P921` | 22 |
| `dbo:origin` | `wd:P495` | 209 |
| `dbo:restingPlace` | `wd:P119` | 76 |
| `dbo:usedInWar` | `wd:P607` | 507 |
| `yago:hasWonPrize` | `wd:P166` | 409 |

## 10. Interpretation and limits

- This is the complete mapping for the **current local relation inventory**. It
  is not a universal list of every relation in EventKG, Wikidata, or WarSampo.
- The 164 EventKG relations with no public map are retained, not discarded.
- A shared endpoint pair is mapping evidence. It is not independent proof of
  equal meaning.
- A missing cross-source match means that the relation stays independent. It
  does not mean that the relation is invalid.
- Source occurrence totals can include the same real-world fact in more than
  one source.
- Level 1 can remain unresolved when only a ranked candidate exists. The Level
  2 relation remains usable.
- The 11 provisional mappings require later semantic review. They must remain
  marked as provisional until that review passes.

## 11. Delivery files and reproducibility

The machine-readable outputs are:

- [`RELATION_MAPPING_TABLE_v1.0.0.csv`](RELATION_MAPPING_TABLE_v1.0.0.csv): the delivered runtime lookup table, with one row for every source predicate, selection priority, source-specific time inputs, evidence and conflict policies, and `relation-map-v1.0.0`;
- [`final_relation_groups.csv`](../poc/relation_alignment/final_relation_map/final_relation_groups.csv): one row for every final Level 2 identifier;
- [`audited_eventkg_decisions.csv`](../poc/relation_alignment/final_relation_map/audited_eventkg_decisions.csv): the 217 EventKG audit decisions; and
- [`summary.json`](../poc/relation_alignment/final_relation_map/summary.json): validation totals.

The catalog can be rebuilt with:

```powershell
python poc/relation_alignment/build_final_relation_map.py
```

The build includes checks for source-row preservation, complete audit coverage,
decision counts, non-empty final identifiers, and unchanged source data.
