# Request to Update the Agent Graph Contract

**Document status:** Draft for review  
**Graph build:** `wwii-agent-draft-v0.1.0`  
**Relation map:** `relation-map-v1.0.0`  
**Purpose:** Ask the Agent developer to update the graph contract so it can use the mapped WWII data without changing or losing the meanings in the source data.

## 1. Request

Please update the Agent input contract and query logic to support:

1. Wikidata QIDs as node IDs.
2. A two-level relation model: a broad Level 1 family and a precise Level 2 relation.
3. Exact, case-preserving relation IDs such as `wd:P607`.
4. Point dates as well as start and end dates.
5. Provenance from Wikidata, EventKG, WarSampo, and Wikipedia.
6. One separate edge for each source assertion.
7. Graph scope and ontology metadata at the top level of the JSON.

The existing Pacific sample contract must continue to work.

## 2. Why the Contract Must Change

The current README was designed for a small, hand-written Pacific graph. It expects a short relation vocabulary such as `ATTACKED`, `OCCURRED_AT`, and `COMMANDED`.

The new graph is built from public knowledge graphs. These sources contain a larger and more precise relation vocabulary. Replacing every source relation with one short Agent relation would remove information and make the mapping difficult to audit.

The preferred design is to preserve the precise Level 2 relation and connect it to a broad Level 1 family. The Agent can use the family for broad questions and the Level 2 relation for precise questions.

## 3. Verified Differences

The current draft loads successfully into `GraphStore`, but successful loading only proves structural compatibility. It does not prove that the Agent can use the data correctly.

| Area | Current README | Draft graph | Result |
|---|---|---|---|
| Node ID | Convention is `<type>:<slug>` | All node IDs are Wikidata QIDs | Loader accepts them, but the convention is different |
| Relation type | Uppercase Agent vocabulary | 810 case-preserving Level 2 IDs such as `wd:P607` | No draft relation uses the README vocabulary |
| Relation filtering | Query value is converted to uppercase | Stored IDs contain lowercase namespace prefixes | A filter for `wd:P607` returns no results |
| Relation hierarchy | One flat `type` | Level 1 family plus Level 2 relation | The Agent does not expose the family |
| Point date | Not defined | 43,605 edges have only a point date | Date filters ignore these edges |
| Interval date | `start_date` and `end_date` | 38,322 edges have at least one interval date | These edges work with date filters |
| Edge provenance | Wikipedia page, paragraph, and URL | Source system and source record ID | Valid provenance is stored in a field intended for a Wikipedia page |
| Observation model | `count` combines supporting mentions | One edge per source assertion; every `count` is 1 | Support must be calculated across separate assertions |
| Node type | Mainly `event`, `person`, `location`, and `org` | 110,744 nodes are `other` | Open types load, but type-specific questions have limited coverage |
| Node name | Human-readable name and aliases | 92,307 nodes use their QID as the name | Name search is limited for these nodes |
| Source text | Raw Wikipedia paragraphs | Mostly short Wikidata descriptions | 94,444 nodes have no text |
| Location geometry | Geometry expected for every location | 509 of 1,172 locations have geometry | 663 locations cannot be mapped |
| Non-location geometry | Expected to be `null` | 7,096 non-location nodes have geometry | Current map behavior differs from the README |
| Event dates | Start and end dates in attributes | 1,245 of 6,224 events have selected start or end dates | Event date coverage is partial |
| Scope | WWII Pacific | Broader WWII population | This difference is intentional for the current proof of concept |

## 4. Requested Contract Changes

### 4.1 Node IDs

Accept a Wikidata QID as a standard node ID:

```text
Q362
Q2383184
```

Do not require an ID prefix that repeats the node type. The `type` field already stores the node type.

**Rationale:** A QID is shared by Wikidata, EventKG, WarSampo links, and Wikipedia. It prevents duplicate identities and makes source alignment reproducible.

### 4.2 Two-Level Relations

Use these relation fields:

```json
{
  "type": "wd:P607",
  "relation_family": "CONFLICT_AFFILIATION"
}
```

- `type` is the precise Level 2 relation ID.
- `relation_family` is the broad Level 1 class.
- `type` is case-preserving and must not be converted to uppercase.
- The top-level `relation_catalog` defines labels, families, source predicates, directions, and mapping status.
- Relation filters must support an exact Level 2 ID and a Level 1 family.

**Rationale:** The precise source meaning remains available, while the family supports broad Agent queries. This also supports unresolved source relations without inventing a false match.

### 4.3 Relation Catalog Access

The Agent must read the top-level `relation_catalog`. `graph_schema()` should return a short summary of relation families. A separate catalog lookup can return the Level 2 relations in a selected family.

Do not send all 810 relation definitions in every schema response.

**Rationale:** The language model needs the meaning of a relation before it can select that relation. A short family summary also limits prompt size.

### 4.4 Time Fields

Support three time fields:

```json
{
  "start_date": "1942-06-04",
  "end_date": "1942-06-07",
  "point_date": null
}
```

For date filtering, a point date must behave as an interval in which the start and end are the same date. Keep the original point-date field so its meaning is not lost.

**Rationale:** Wikidata and EventKG frequently describe a relation with one date instead of a period. The current Agent ignores 43,605 point-dated edges.

### 4.5 Provenance

Generalize each source object:

```json
{
  "provider": "Wikidata",
  "record_id": "Q362$statement-id",
  "url": "https://www.wikidata.org/wiki/Q362",
  "page": null,
  "paragraph": null
}
```

- `provider` and `record_id` identify the source assertion.
- `url` can point to the source record or page.
- `page` and `paragraph` are optional Wikipedia evidence fields.
- The Agent must not present an internal record ID as a Wikipedia page title.
- If Wikipedia-only citations remain mandatory, the exporter must add the relevant Wikipedia text and paragraph before release.

**Rationale:** The graph uses more than one public source. Provenance must identify the real source and must not imply that every record came from a Wikipedia paragraph.

### 4.6 Separate Source Assertions

Keep one edge for every source assertion. Require a unique edge `id`.

Two sources can state different relations between the same entities. These statements must remain separate. The Agent can calculate combined support at query time when the subject, object, Level 2 relation, direction, and time are the same.

**Rationale:** Separate assertions preserve source evidence and prevent one source from replacing another. They also follow the project decision to keep different page references as separate observations.

### 4.7 Node Types and Geometry

Formally allow `other` as a node type. Define one of these geometry rules in the contract:

1. Geometry is allowed on any node when the source gives a direct coordinate; or
2. Geometry is only allowed on locations, and the exporter must move other coordinates to linked location nodes.

The recommended rule is option 1. The map code must clearly mark direct node geometry and geometry reached through a relation.

**Rationale:** Wikidata can provide coordinates for events, organizations, and other objects. Deleting those coordinates can remove useful evidence.

### 4.8 Graph Metadata and Scope

Accept and expose the top-level fields:

```json
{
  "metadata": {
    "schema_version": "agent-graph-draft-v0.1.0",
    "relation_map_version": "relation-map-v1.0.0",
    "release_status": "DRAFT_AGENT_MECHANISM_QA",
    "scope": {}
  }
}
```

The Agent system prompt must read the graph scope instead of always stating that the graph covers only the Pacific theatre.

**Rationale:** The current proof of concept covers a broader WWII population. Scope metadata prevents the Agent from describing the data incorrectly.

## 5. Data Problems That a Contract Change Does Not Solve

The following items remain export or enrichment work:

- Resolve more `other` node classifications.
- Fetch names and aliases for QID-only nodes.
- Add geometry for missing locations.
- Add event dates where the sources support them.
- Add Wikipedia paragraphs when text retrieval and Wikipedia citation are required.
- Validate whether coordinates on non-location nodes describe the node itself or a related place.

These gaps must stay visible in data-quality results. The Agent contract must not hide them.

## 6. Acceptance Checks for the Updated Agent

The update is complete when all these checks pass:

1. The existing `sample_graph.json` still loads and its tests pass.
2. The WWII draft loads without dropped nodes or edges.
3. `find_relations(relation_type="wd:P607")` returns matching relations.
4. A Level 1 family query returns all valid Level 2 relations in that family.
5. An edge with only `point_date="1942-06-04"` matches a 1942 date query.
6. Source output separates source provider, record ID, URL, Wikipedia page, and paragraph.
7. The Agent does not call a Wikidata statement ID a Wikipedia page.
8. `graph_schema()` reports the graph scope, schema version, relation-map version, and Level 1 families.
9. Direct geometry and relation-derived geometry follow the selected geometry rule.
10. The Agent reports missing names, text, types, dates, and geometry as data limitations.

## 7. Requested Decision from the Agent Developer

Please confirm:

1. Approval of QIDs as standard node IDs.
2. Approval of Level 1 plus Level 2 relations.
3. Removal of forced uppercase conversion for relation IDs.
4. Support for point dates in date filters.
5. The generalized provenance structure.
6. The geometry rule for non-location nodes.
7. Use of graph metadata to set the Agent scope.

After these decisions, the exporter and Agent can be updated against one versioned contract.
