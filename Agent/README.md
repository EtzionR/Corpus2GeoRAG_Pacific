# GeoRAG Agent: WWII Pacific theatre

This is the **agentic RAG** component of the *From Corpus into GeoRAG* project (see the project slides). Upstream teams turn Wikipedia articles and public knowledge graphs (Wikidata, EventKG, WarSampo) on World War II into a geocoded knowledge graph. This agent answers geographic OSINT/GEOINT questions over that graph, for a text UI and a map UI. The UI, data collection, NER, geocoding and graph building are **out of scope** here.

## Quick start
```bash
uv sync
cp .env.example .env               # set OPENROUTER_API_KEY
uv run pytest                      # tool tests: no LLM, no network
uv run geo-agent "Who attacked Guadalcanal in 1942?"
uv run geo-agent                   # interactive session
GEO_AGENT_JSON=1 uv run geo-agent "Show me where the Battle of Midway happened"   # full UI payload
uv run geo-agent --trace last      # timeline of the last run (see "Debugging a run")
GEO_AGENT_MOCK=1 uv run python -c "from geo_agent.runner import AgentRunner; print(AgentRunner().ask_sync('Who attacked Guadalcanal in 1942?')['answer'])"   # no API key needed
GRAPH_PATH=/path/to/wwii_agent_graph_draft_v0.1.0.json uv run geo-agent "Where did the Battle of Midway happen?"   # the team's full graph
GEO_AGENT_BIG_GRAPH=/path/to/wwii_agent_graph_draft_v0.1.0.json uv run pytest tests/test_big_graph.py   # acceptance checks on it
```

## Layout
| File | Role |
|------|------|
| `geo_agent/graph_store.py` | Loads/validates the graph JSON; name, graph, spatial, date and text queries. No LLM. |
| `geo_agent/tools.py` | LangChain tools wrapping `GraphStore`. The docstrings are the LLM's tool descriptions. |
| `geo_agent/agent.py` | OpenRouter LLM, system prompt, allowlisted MCP loading, `GeoAgent.ask()`, `finalize_answer()`, CLI. |
| `geo_agent/trace.py` | Run tracing (callback handler, JSONL writer), diagnostics, timeline, replay, report. |
| `geo_agent/runner.py` | `AgentRunner` for synchronous callers such as Streamlit: one background event loop, `ask_sync`/`submit`, timeout, cancel, progress, health, stats, entity lookup (see "Using the agent from the UI"). |
| `geo_agent/schema.py`, `geo_agent/schemas/ask_result.schema.json` | `AskResult`, the typed reply model, and its JSON Schema. Regenerate the file with `uv run python -m geo_agent.schema`. |
| `geo_agent/mock.py`, `geo_agent/mock_fixtures/` | Mock mode: recorded replies on the sample graph, generated through the real answer code (`uv run python -m geo_agent.mock`). |
| `data/sample_graph.json` | Hand-written WWII Pacific fixture (sample vocabulary, plus a few draft-style QID nodes and edges) for tests and mock mode. It includes edge cases: meridian (Kiska/Adak, Taveuni), near-tied names (Guam 1941/1944), Chungking alias, Tarawa/Tarakan, and set-question structure (kinds, countries, `PART_OF` campaigns, the Combined Fleet, the Manhattan Project chain), and `loc:nauru`, whose text holds a deliberate prompt-injection test. |
| `mcp_servers.json` | Allowlist of optional external MCP servers/tools (empty by default). |
| `tests/test_tools.py` | Tool/store tests: the three example questions, name matching, meridian and date cases. |
| `tests/test_trace.py`, `tests/test_agent.py` | Tracing, failure stages, viewer/replay/report, error results. Fake model, no network. |
| `tests/test_runner.py`, `tests/test_schema.py`, `tests/test_mock.py` | The UI interface: runner, reply schema, mock fixtures. |
| `docs/GRAPH_TEAM.md`, `docs/UI_TEAM.md`, `docs/EVALUATION.md` | Notes for the graph team (contract decisions, data issues and what to fix), for the UI team (connecting the Streamlit app), and how the agent is evaluated. |
| `tests/test_big_graph.py` | Acceptance checks on the team's full draft graph. Skipped unless `GEO_AGENT_BIG_GRAPH` points at the file. |

## Decisions
1. **LangChain `create_agent` with tool calling.** The LLM plans which tools to chain, so questions aren't limited to a fixed list of templates. The three example questions are covered by tests, but the prompt tells the agent to compose tools freely.
2. **The graph is the only source of truth.** There are no calls to Wikipedia or geocoding services at query time. If the graph doesn't have the answer, the agent says so.
3. **LLM via OpenRouter**, through `ChatOpenAI` on the OpenAI-compatible endpoint. The default model is `z-ai/glm-5.2`, chosen for cost. Change it with `GEO_AGENT_MODEL`; it must support tool calling.
4. **MCP is opt-in and allowlisted.** Built-in graph tools are native LangChain tools. External MCP tools load only if (a) the server is in `mcp_servers.json`, (b) the tool name is in that server's `allowed_tools`, and (c) the name does not collide with an existing tool. A server that fails to load is skipped, and the agent keeps working.
5. **The LLM returns ids and the code builds the map data.** The model returns `{answer, entity_ids, sources}`, and `GeoAgent.ask()` builds the GeoJSON from the graph. The map therefore always gets real geometry, and the LLM never has to reproduce coordinates.
6. **Evidence-checked output.** Only ids and source pages that this turn's graph queries returned are kept; the rest are reported as dropped or unverified. Coordinates written in the answer text are checked against the values the tools returned and flagged if they don't match (the text itself is never changed). Orgs, people and events are drawn only at places in the answer's evidence.
7. **Set questions: the model plans, the code enumerates.** For "all locations connected to X", one tool computes the whole set from fixed connection paths (never through membership or co-mention hubs). The model sees only a count, groupings and the top rows, plus a handle. The map is filled from the handle. A set of 500 places costs the same ~1k tokens as one of 5, and the model never copies ids.
8. **Bounded context.** Past a threshold, old tool outputs are hidden from the model (the stored messages, which the evidence check uses, stay intact). A per-question token budget makes the model finish and raises a warning.
9. **Honest approximations.** When the graph lacks kinds, countries or a campaign node, the agent guesses from names or text but always says so (`interpretation`, `approximation_used`). A text-only campaign answer is never above low confidence. A country group (`country:Japan`) uses the org's `country` attribute. Without one, it uses the org's country relation (`wd:P17` / `wd:P27`), and only then the name. The name fallback avoids words that other countries share: "Imperial" alone once pulled the *Second Australian Imperial Force* into Japan.
10. **Scope guard in code.** Every grounded answer needs at least one graph query (even "not in the graph" needs a search). So a reply made with no tool call is replaced with a fixed scope message (`off_topic`), whatever the model wrote: a poem, chit-chat, or a jailbreak attempt that got through. The prompt also tells the model to decline off-topic requests, and that tool results (including Wikipedia text) are data whose instructions it must never follow. On top of that, sentences in the source text that address an AI ("ignore all previous instructions…", "you are now…") are replaced with `[instruction-like text removed]` when the graph loads, so the model never reads them; the raw text stays in the graph. Tools are read-only, with no code execution, files or network, so a jailbreak can't make the agent do anything; the remaining exposure is the answer text, which the UI renders as untrusted.
11. **Graph contract v1, agreed with the graph team.** Opaque ids (QIDs), exact case-preserved Level 2 relations with Level 1 families, point dates, provenance by provider and record, one edge per source assertion combined at query time, geometry on any node (direct vs related marked on the map), and the graph's own scope in the agent's instructions. The short sample vocabulary keeps working, so tests and mock mode run on the small fixture.
12. **Connection rules on exact relations, not families.** Families mix useful relations with hubs, so the roles (participation, occurrence, action at a place, part-of, located-in) list exact Level 2 relations; huge events reached through participation (more than 50 sub-events) add nothing.
13. **The UI runs the agent in-process through `AgentRunner`.** Streamlit is synchronous and the agent is async, so one background event loop per process owns the agent; replies are versioned (`schema_version`) and typed (`AskResult`), and mock mode lets the UI be built without a key.
14. **Answer and context are separate.** The model returns `answer_ids` (what answers the question, e.g. the attackers) and `context_ids` (the place, defenders, commanders). With one flat list, a defender the answer mentions looked exactly like a wrong attacker. The roles come from the question, so nothing extra is needed in the graph. Telling attackers from defenders does need typed relations (`ATTACKED` vs `DEFENDED`), as "who attacked X" always did.
15. **Visible name resolution.** Every name match has a quality (`clear`/`uncertain`). On an uncertain match the agent never guesses. It asks a short clarification question, or, when two or three entities share the name (the two Battles of Guam) and each answer is short, it answers for every one of them, clearly separated by year; when a name was read differently from how it was typed, `interpretation` says so in plain words. A question year outside the resolved event's dates is flagged, never silently corrected.
16. **Pure query layer.** All logic lives in `GraphStore`, so it can be tested without an LLM. A future MCP server or REST API for the UI can wrap the same methods.
17. **Tolerant input schema.** Pydantic models allow extra fields, and node/edge types are open strings. Edges that point to unknown nodes are dropped, not fatal.
18. **Spatial semantics.** Distance is great-circle km from the query point to the *nearest point* of a geometry (0 if inside). A point on Guadalcanal therefore matches the island polygon and the Solomon Islands. Distances wrap correctly across the 180° meridian (Kiska to Adak is ~396 km, not ~40,000). A bounding box with `min_lon > max_lon` crosses the meridian: `170 … -170` is the strip around the date line.
19. **Dates.** Partial ISO dates are allowed (`1942`, `1942-06`). Date filters use interval overlap, and undated edges are excluded when a filter is set.
20. **Sub-locations.** `find_relations` expands a location through `LOCATED_IN` by default. "Who attacked Guadalcanal" therefore includes attacks on Henderson Field.
21. **Text retrieval is BM25** over node text paragraphs. It is simple, needs no API, and returns citable paragraphs.
22. **Local tracing, no hosted service.** Every run writes JSONL events through a LangChain callback handler, so tools and `GraphStore` stay untouched. Trace writes can never fail a run. Questions and 1000-character result previews are stored locally; keys and env values never are.
23. **Errors are returned as data.** `ask()` never raises for model, tool, step-limit or post-processing failures; it returns `error: {type, message, stage}` so the UI always gets a reply and the trace says where the run stopped.
24. **Nicknames come from the graph; the model's own reading is allowed but shown.** A nickname is matched against what the graph holds: aliases, typed aliases (`alias_details`), the quoted part of an alias, and phrases such as *codename was "Cactus"* in the text. Matches are exact (a leading "the" ignored) or a person's nickname and surname in order, and never partial, so "Bull Halsey" doesn't find the Tu-4's alias "Bull". When the graph doesn't hold the nickname, the model may search the name it stands for, if that entity is in the graph (decided with product). `interpretation` then says it was the model's reading, and `resolved_entities[].model_reading` is true.
25. **Location first.** This is a geo agent. When a question could be about an event or its place ("Where was the Long Lance used?", "What happened on Guam in 1944?"), the place goes on `answer_ids` and is highlighted on the map; the events are still named. Questions given as coordinates answer with the events at that point.

## Input schema (for the knowledge-graph builder)
The agent reads **one JSON file**, set by `GRAPH_PATH` (default `data/sample_graph.json`; a relative path that doesn't exist from the working directory is read from the agent's project root, so a UI started in another folder finds the `.env` value):

```text
{"metadata": { ... }, "relation_catalog": { ... }, "nodes": [ ... ], "edges": [ ... ]}
```

`nodes` and `edges` are required; `metadata` and `relation_catalog` are optional (contract v1, agreed with the graph team on 2026-10-12). Two kinds of file are supported and tested:
- **The sample graph**, `data/sample_graph.json` (hand-written, short relation vocabulary, no metadata). It's the fixture for tests and the UI's mock mode.
- **The team's draft graph**, `wwii_agent_graph_draft_v0.1.0.json` (146k nodes, 701k edges, 465 MB, Wikidata ids and Level 2 relations). It loads in about 20 s and uses about 3 GB of memory; tool calls take under 0.6 s.

### Top level
| Field | Meaning |
|---|---|
| `metadata` | `{schema_version, relation_map_version, release_status, scope}`. The agent states the graph's scope from here (in its instructions and in the off-topic reply), so a broader WWII graph isn't described as "Pacific only". Without metadata the agent assumes the WWII Pacific theatre. |
| `relation_catalog` | Definitions of the Level 2 relations: label, Level 1 family, source predicate, direction, mapping status. `graph_schema` shows a family summary; the tool `list_relations` lists the relations of one family. |

### Nodes
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | **yes** | Unique, opaque id: a Wikidata QID (`Q362`) or a readable slug (`loc:midway_atoll`). Edges refer to it. |
| `name` | string | **yes** | Display name, the main name used for matching. A name that is just the QID can't be found by name search; the agent reports such gaps. |
| `type` | string | **yes** | `location`, `person`, `org`, `event`, or `other` for unresolved items. Open list. |
| `aliases` | list of strings | no, but important | Other names and spellings: `["Midway", "Midway Island"]`, romanizations (`"Chungking"`), abbreviations (`"IJN"`). The agent finds an entity only by its name and aliases. It makes no online lookups, so a spelling missing here isn't found. |
| `alias_details` | list of objects | no | Typed aliases: `{label, kind, provider, record_id}`, `kind` one of `alias`, `nickname`, `codename`, `hull_number`. Nicknames and codenames are matched exactly (a leading "the" ignored), never partially. Without this field the agent still finds nicknames the graph holds elsewhere: the quoted part of an alias (`Holland McTyeire "Howlin' Mad" Smith`) and phrases in `text` (*nicknamed …*, *known as …*, *codename was "…"*). |
| `geometry` | GeoJSON geometry or `null` | yes for places | `Point`, `Polygon` or `MultiPolygon`, WGS84, **[longitude, latitude]** order, longitude in -180…180; split a shape that crosses the 180° meridian into a `MultiPolygon`. **Any node may carry geometry** when the source gives a direct coordinate (e.g. a battle). The map marks it `geometry_source: "direct"`; entities without their own geometry are drawn at linked places (`"related"`). |
| `sources` | list of source objects | no, but important | See "Provenance". |
| `text` | string | no, but important | Raw source text (Wikipedia paragraphs separated by blank lines, or a short description). Used for text search and quotes. Sentences that address an AI ("ignore all previous instructions…") are removed before the model sees them. |
| `attributes` | object | no | Free-form extras: `start_date` / `end_date` / `point_date` on events, `kind`, `country`, `importance` (see below). |
| `embedding` | list of numbers or `null` | no | Accepted but not used yet. |

### Edges
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | recommended | Unique edge id, one per source assertion. Generated (`e<n>`) when missing. |
| `source`, `target` | string | **yes** | Node ids of the subject and the object. An edge whose endpoint doesn't exist is dropped at load time. |
| `type` | string | **yes** | The exact relation (Level 2): `wd:P607`, or a short sample type such as `ATTACKED`. **Case is kept**; filters match it exactly (short sample types also case-insensitively). |
| `relation_family` | string | no | The broad Level 1 family, e.g. `CONFLICT_AFFILIATION`. Read from the edge, then its attributes, then the catalog. Filters accept a family or an exact type. |
| `count` | integer ≥ 1 | no (default 1) | Mentions behind this assertion. Assertions with the same subject, object, relation and dates are combined at query time; their total is the relation's support. |
| `start_date`, `end_date`, `point_date` | string or `null` | no, but important | ISO dates, partial allowed (`"1942"`, `"1942-06"`). A `point_date` counts as a one-day interval in every date filter and is kept in outputs. |
| `sources` | list of source objects | no | See "Provenance". |
| `attributes`, `embedding` | | no | As for nodes. |

Unknown extra fields are kept and ignored, so the format can grow.

### Provenance
A source object is `{provider, record_id, url, page, paragraph}`; any subset, as long as something identifies the source. Examples: `{"provider": "Wikidata", "record_id": "Q362$...", "url": "https://www.wikidata.org/wiki/Q362"}`, or `{"page": "Battle of Midway", "paragraph": 3}`. The older `{page, paragraph, url}` shape still works, and a record id written into `page` ("Wikidata:…", "EventKG:…", a bare QID) is moved to `provider` + `record_id`. **Answers only ever cite real Wikipedia pages in `sources`**; everything else goes to `source_refs`.

### Relation vocabulary
- **Draft graph:** Level 2 relations from the catalog (`wd:P607` "conflict", `wd:P276` "location", ...) grouped into Level 1 families. Ask the agent's `list_relations` tool, or see the team's relation map.
- **Sample graph (short vocabulary, still accepted).** Direction matters: `source` does `type` to `target`.

| Type | Direction | Meaning |
|------|-----------|---------|
| `OCCURRED_AT` | event → location | where an event happened |
| `PARTICIPATED_IN` | person/org → event | took part in |
| `COMMANDED` | person → org/event | command role |
| `ATTACKED`, `DEFENDED` | org → location | offensive / defensive action |
| `CAPTURED`, `OCCUPIED`, `LANDED_AT`, `BASED_AT` | org → location | territorial change, base |
| `BOMBED` | org → location | air/naval bombardment |
| `SANK` | org → org/vessel | naval loss |
| `MEMBER_OF` | person → org | affiliation |
| `ALLIED_WITH` | org → org | alliance/cooperation |
| `PART_OF` | event → event, org → org | battle in a campaign, unit in a parent |
| `LOCATED_IN` | location → location | containment: "who attacked Guadalcanal" also finds attacks on Henderson Field |
| `CO_MENTIONED` | any → any | fallback when only co-occurrence is known |

### Dates
- Edges: `start_date` / `end_date`, or a single `point_date`. A date filter matches by interval overlap; a point date is a one-day interval. Edges without any date are left out when a filter is used, except structural ones (`PART_OF`, `LOCATED_IN`), and participation / occurrence edges, which take their event's dates.
- Events: put `start_date` / `end_date` (or `point_date`) in `attributes`. "What happened at (x, y) in 1942" uses them.

### Data quality is reported, not hidden
`graph_schema()` returns `coverage` counts (QID-only names, nodes without text, `other` types, events without dates, locations with and without geometry, non-locations with geometry, edges by date kind, unresolved families), and `get_entity` names the gaps of one entity (`data_limits`). The agent is told to mention them when they affect an answer.

### What the agent needs from the graph, in priority order
1. **Battles, raids and landings as `event` nodes**, with `OCCURRED_AT` edges to their locations. Without them "where did the Battle of X happen" can't be answered.
2. **Dates** on edges, and in the attributes of event nodes.
3. **Geometry** on every location (lon/lat order, meridian-crossing shapes split).
4. **Aliases**: alternative names, transliterations, abbreviations.
5. **`sources` and `text`**, so answers can cite and quote Wikipedia.
6. **`LOCATED_IN` edges** for places inside other places.
7. **Clean ids**: unique, and every edge endpoint present.

For "show me all locations connected to X" questions (see "Set questions" below), also:

8. **Typed relations** (`ATTACKED`, `COMMANDED`, `PARTICIPATED_IN`, ...) rather than `CO_MENTIONED`. The agent ignores plain co-mentions by default, because hubs like Pearl Harbor co-occur with almost everything.
9. **`attributes.kind`** on events (`battle`, `raid`, `landing`, `campaign`, `operation`, `bombing`) and on locations (`island`, `atoll`, `city`, `port`, `airfield`, `sea`, `region`). Without kinds, "islands" and "battles" are guessed from names, and the answer says so.
10. **`PART_OF` edges**: battles to their campaign, campaigns to the war (`event -PART_OF-> event`), and units to their parent (`org:combined_fleet -PART_OF-> org:ijn`).
11. **Campaign and operation nodes** for concepts people ask about: the island-hopping campaign, the Manhattan Project, the atomic bomb missions. Without them, the agent falls back to events whose text mentions the concept, which it marks as approximate.
12. **`attributes.country`** on orgs and people (`"Japan"`, `"United States"`). Without it, countries are guessed from org names.
13. Optionally, **`attributes.importance`** (a number) on events, used to rank "major" battles. Without it, importance is how much the graph says about the event.

### How "connected" is defined
"Locations connected to X" follows fixed paths that depend on what X is. One table in `graph_store.py` holds them:

| X is a | Its locations |
|---|---|
| person | places linked to the person directly, and the places of events they `COMMANDED` or `PARTICIPATED_IN` |
| org | places it `ATTACKED`, `DEFENDED`, `CAPTURED`, `OCCUPIED`, `LANDED_AT`, `BOMBED` or was `BASED_AT`; the places of its events; the same for its sub-units (`PART_OF`) |
| event / campaign | where it `OCCURRED_AT`, including all its sub-events (`PART_OF`), recursively |
| location | itself and the places `LOCATED_IN` it |

`MEMBER_OF`, `ALLIED_WITH` and `CO_MENTIONED` are never followed. Otherwise Yamamoto would get every place the IJN ever touched.

On the draft graph, the same roles are defined on exact Level 2 relations (table `RELATION_ROLES` in `graph_store.py`), not on whole families: every family mixes relations worth following with hubs (the spatial family also holds place of birth). Examples: participation is `wd:P607` "conflict", `wd:P1344` "participant in" and the inverse of `wd:P710` "participant"; occurrence is `wd:P276` "location", `wd:P131`, `wd:P706`; part-of is `wd:P361` and the inverse of `wd:P527`. `wd:P17` "country" is not treated as "located in", because in the draft it mostly links ships and units to a country. Families such as affiliation, awards, education, family, authorship and ownership are never followed. An event with more than 50 sub-events (World War II has 1,710, the Pacific War 580) reached through participation adds nothing, so taking part in the war doesn't connect a person to every battle.

### Known problems in the draft graph (v0.1.0), for the graph team
- 143,130 edges carry a family different from the catalog's; 61 relation types are missing from the catalog (e.g. `wd:P31`, `wd:P21`, `wd:P279`); 8 types carry more than one family. `wd:P1344` "participant in" and yago `participatedIn` are tagged as spatial on edges.
- Key places exist only as unlabeled QIDs with no geometry (Guadalcanal, Midway Atoll, Tokyo, Kiska), so they can't be found by name or mapped.
- Ships are typed `location` (USS Yorktown) or `other` with wreck coordinates; the IJN and the US Navy are `other`.
- Some `wd:P276` locations are country-scale (Battle of Midway → Empire of Japan).
- Edge dates range from 0044 to 6194; 3,864 events have no date at all.
- 74,111 duplicate assertions (combined at query time, so harmless to answers).


### A minimal valid file
```json
{
  "nodes": [
    {"id": "loc:midway_atoll", "name": "Midway Atoll", "type": "location", "aliases": ["Midway"],
     "geometry": {"type": "Point", "coordinates": [-177.37, 28.21]},
     "sources": [{"page": "Midway Atoll", "paragraph": 0, "url": "https://en.wikipedia.org/wiki/Midway_Atoll"}],
     "text": "Midway Atoll is a small atoll in the North Pacific Ocean..."},
    {"id": "event:battle_of_midway", "name": "Battle of Midway", "type": "event", "geometry": null,
     "attributes": {"start_date": "1942-06-04", "end_date": "1942-06-07"},
     "sources": [{"page": "Battle of Midway", "paragraph": 0}], "text": "The Battle of Midway was..."},
    {"id": "org:ijn", "name": "Imperial Japanese Navy", "type": "org", "aliases": ["IJN"], "geometry": null}
  ],
  "edges": [
    {"source": "event:battle_of_midway", "target": "loc:midway_atoll", "type": "OCCURRED_AT", "count": 8,
     "start_date": "1942-06-04", "end_date": "1942-06-07", "sources": [{"page": "Battle of Midway", "paragraph": 3}]},
    {"source": "org:ijn", "target": "loc:midway_atoll", "type": "ATTACKED", "count": 6,
     "start_date": "1942-06-04", "end_date": "1942-06-04"}
  ]
}
```

### Checking a file before handing it over
```bash
uv run python -c "
from geo_agent.graph_store import GraphStore
import json, sys; path = sys.argv[1]
s = GraphStore.from_json(path); raw = json.load(open(path))
print(s.schema())
print('edges dropped (unknown node id):', len(raw['edges']) - len(s.edges))
print('locations without geometry:', [n.id for n in s.nodes.values() if n.type == 'location' and n.id not in s.geoms])
" path/to/graph.json
```
Then point the agent at it with `GRAPH_PATH=path/to/graph.json uv run geo-agent "..."`.

## Tools
| Tool | Purpose |
|------|---------|
| `graph_schema()` | Node types, a relation-family summary, the top relation types, date range, bbox, graph `metadata` (scope, versions) and `coverage` (data limits). |
| `search_entities(query, type?, limit)` | Fuzzy name/alias → entity ids. |
| `get_entity(entity_id)` | Full node: geometry, attributes, sources, text excerpt. |
| `get_neighbors(entity_id, relation_types?, direction?, neighbor_type?, year?/date_from?/date_to?)` | One-hop neighbors with filters. |
| `find_relations(source_id?, target_id?, relation_type?, year?/dates?, include_sublocations)` | Edge query; empty endpoint = wildcard. |
| `get_locations(entity_id)` | Geolocated places linked to an event/org/person. |
| `entities_near(lat, lon, radius_km, type?)` | Radius search, nearest first. |
| `entities_in_bbox(min_lon, min_lat, max_lon, max_lat, type?)` | Map-region search. |
| `what_happened_at(lat, lon, radius_km, year?/date_from?/date_to?)` | Nearby places → their events and relations, in time order; optionally only those in a period (events by their own dates, relations by edge dates). |
| `list_relations(family?, query?)` | The relations of one Level 1 family (id, label, direction, mapping status, number of edges), or a search by label. |
| `search_source_text(query, entity_ids?, k)` | BM25 over source text, with Wikipedia citations. |
| `to_geojson(entity_ids)` | FeatureCollection. Non-spatial entities are drawn at their linked places. |
| `connected_locations(entity_id, location_kind?, year?/dates?, include_weak?)` | **Set tool.** All locations connected to a person, org, event or campaign (see "How connected is defined"). |
| `find_entities(type, kind?, country?, side?, year?/dates?, limit)` | **Set tool.** Ranked listing, e.g. the top battles by importance; their places go on the map. |
| `shared_connections(group_a, group_b)` | **Set tool.** Places both groups connect to, e.g. `"country:United States"` and `"country:Japan"`. |
| `entities_mentioning(phrase, type?)` | **Set tool, fallback.** Entities whose text mentions a concept that isn't a graph entity. Always marked approximate. |
| `overview(scope?, type?, kind?, year?/dates?, limit)` | **Set tool for broad questions.** A war, campaign, period, org or person as a whole, in one call: total, counts by kind, year and part, the top items with their places, and `narrow_by` suggestions. The answer says it's an overview and offers ways to narrow (`interpretation`: "broad question: overview of N items"). |

List tools (`find_relations`, `get_neighbors`, `get_locations`, `entities_near`, `entities_in_bbox`) return `{total, shown, truncated, results}`, so a cut list is never mistaken for a complete one.

**Set tools** don't list their results. They store the full set under a short handle (e.g. `rs-3f9a1c`) and return only the count, groupings (`by_via`: per battle or org; `by_region`) and the top 15 rows with their path, about 1k tokens whatever the size. The model puts the handle in its final answer, and the code draws every member on the map. The model never sees or copies a list of hundreds of places.

How the example questions map to tools (examples, not a closed list):
- *"Show me places where the Battle of X happened"*: `search_entities` → `get_locations`
- *"Who attacked X in 1942?"*: `search_entities` → `find_relations(target_id, "ATTACKED", year=1942)`
- *"What happened at (x, y)?"*: `what_happened_at` (then `search_source_text` for detail)
- *"Show me all locations connected to Admiral Yamamoto"*: `search_entities` → `connected_locations`
- *"Show me major battles in the Pacific War on the map"*: `search_entities("Pacific War")` → `overview(scope, kind="battle")` (without a war node, `find_entities(type="event", kind="battle")`)
- *"What happened in 1943?"*, *"Everything about the Imperial Japanese Navy"*: `overview` (broad questions get an overview, never a refusal)
- *"Where are the United States and Japan both connected?"*: `shared_connections("country:United States", "country:Japan")`

## Output schema (for the UI)
### How to call the agent
```python
from geo_agent.agent import GeoAgent

agent = await GeoAgent.create()            # once at startup: loads the graph and the model
result = await agent.ask("Who attacked Guadalcanal in 1942?", thread_id="session-123")
```
From the command line, `GEO_AGENT_JSON=1 uv run geo-agent "question"` prints the same dict as JSON.

From synchronous code such as Streamlit, use `AgentRunner` instead (see "Using the agent from the UI"); it returns the same dict.

`ask()` always returns a dict and never raises for model or tool problems: errors come back in the `error` field. Use the same `thread_id` for one user's conversation. Follow-up questions, and answers to a clarification question, need it to keep context. Memory is per process and lost on restart.

### Fields
| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Version of this reply contract, now `"1.4"` (1.1 added `bbox`/`center`; 1.2 added `source_refs`, feature `geometry_source`, confidence `n_providers`; 1.3 added `ungrounded_names`; 1.4 added `match` values `nickname`, `hull_number`, `alias_part`). The JSON Schema is `geo_agent/schemas/ask_result.schema.json`. |
| `answer` | string | Text for the chat panel. May contain Markdown (bold, lists). Empty when `error` is set. **Treat it as untrusted text:** render it with a sanitizing Markdown renderer (e.g. markdown-it with HTML disabled, or DOMPurify on the output) and never insert it as raw HTML. It's written by a language model that also reads Wikipedia text, so it could contain HTML or links that someone planted in the data. |
| `answer_ids` | list of strings | The entities that **answer the question**. "Who attacked X": only the attackers. "Where did X happen": the places. "What happened at (x, y)": the events. Highlight these. Empty for a clarification question, a "not in the graph" answer, or an error. |
| `context_ids` | list of strings | Other entities the answer mentions that help on the map: the place asked about, defenders, commanders, the related battle. Show these more quietly. |
| `entity_ids` | list of strings | `answer_ids` followed by `context_ids`, e.g. `["org:usmc", "loc:guadalcanal"]`. Kept so code written for the earlier output keeps working. All three lists hold only ids that the agent's graph queries actually returned; anything else the model named goes to `dropped_ids`. |
| `sources` | list of strings | Wikipedia page titles the answer relies on, e.g. `["Guadalcanal campaign"]`. Only real pages that appeared in the agent's evidence, never record ids; others go to `unverified_sources`. |
| `source_refs` | list of `{provider, record_id, url, page}` | Structured provenance of the evidence (Wikidata statements, EventKG records, Wikipedia pages), at most 25. Good for a "sources" panel with links. |
| `geojson` | GeoJSON FeatureCollection | Ready-to-draw map data built from the graph, never from the model (see below). Empty `features` when there's nothing to show. |
| `tool_calls` | list of `{name, args}` | The graph queries the agent ran. Useful for a "how I found this" panel or for debugging. |
| `run_id` | string | Id of this run's trace file entry. Show it in bug reports: `uv run geo-agent --trace <run_id>` replays what happened. |
| `confidence` | `{level, basis}` | `level` is `high`, `medium` or `low`: how well the graph supports the answer. Show it as a label, not a percentage; the thresholds aren't calibrated yet. `basis` has `max_edge_count` (combined support), `n_source_pages`, `n_providers`, `uncertain_match_used`. |
| `interpretation` | list of strings | How names in the question were read when it wasn't literal, e.g. `"'Chungking' read as Chongqing (alias)"`. **Show these to the user**, so a wrong reading is visible. Empty when every name matched as typed. |
| `resolved_entities` | list of objects | One per final entity found by name: `id`, `name`, `typed` (the text searched), `matched_label`, `match` (`name`, `alias`, `nickname`, `hull_number`, `alias_part` (a person's nickname and surname found in order inside one alias) or `fuzzy`), `score`, `quality` (`clear`/`uncertain`), `model_rewritten_query`, and `context_mismatch` when the question's year doesn't fit the event's dates. For debugging or a details panel. | `evidence` says where the graph holds a matched nickname (the alias or the text). `model_reading` is true when the model searched a name the user didn't use and the graph doesn't tie the user's words to the entity.
| `dropped_ids` | list of `{id, reason}` | Ids the model named but the evidence didn't support (`reason`: `unknown` or `not_in_tool_output`). Not drawn. |
| `unverified_sources` | list of strings | Pages the model cited that never appeared in its evidence. |
| `unverified_numbers` | list of numbers | Coordinates written in `answer` that match no value the graph returned. The text isn't changed; consider a small "unverified" marker. |
| `ungrounded_names` | list of strings | Graph entities the answer names although no tool returned them this turn: the model added them from its own knowledge. In a reply with no answer ("not in the graph", or a clarification question), also any capitalized name that neither the question, nor a tool result, nor the graph's scope contains (e.g. "Normandy" for a division the graph links to no battle). The model is sent back once to look them up or remove them; what remains is listed here. Show a "not from the graph" note. |
| `warnings` | list of strings | Codes for things worth flagging, empty when all went well. Answer quality: `low_confidence`, `uncertain_match_used` (a weak or ambiguous name match was used), `context_mismatch` (e.g. "Battle of Midway in 1944"; the battle was 1942), `model_rewritten_query` (the model searched a spelling the user didn't type), `ids_dropped`, `sources_unverified`, `text_coordinates_unverified`, `approximation_used` (a kind, country or concept was guessed; see `interpretation`), `result_set_unknown`, `ungrounded_names` (see that field), `nothing_on_map` (the answer has entities, but none has a place in the graph to draw: say so instead of showing an empty map). Scope: `off_topic` (the question wasn't about the graph; `answer` is the fixed scope message), `instruction_text_removed` (source text used for this answer contained instruction-like sentences, which were removed before the model saw them). Run health: `structured_output_failed` (plain text without ids), `repeated_tool_call`, `empty_resolution` (the first name lookup found nothing), `many_steps`, `token_budget` (the question used more tokens than the budget). |
| `bbox` | `[min_lon, min_lat, max_lon, max_lat]` or `null` | Bounding box of the map features, for fitting the map. A box crossing the 180° meridian has `min_lon > max_lon` (Midway + Guadalcanal: `[159.6, -10.0, -177.37, 28.21]`, about 23° wide, not the whole world); for Leaflet `fit_bounds`, add 360 to `max_lon` in that case. |
| `center` | `{lat, lon}` or `null` | A suggested map center. |
| `result_sets` | list of `{handle, label, total, on_map, map_truncated}` | For set questions: the sets the answer is about, e.g. `{"label": "locations connected to Isoroku Yamamoto", "total": 5, "on_map": 5}`. Their members are in `answer_ids` and on the map. Show `total` ("5 places"), since the text names only the top ones. |
| `dropped_sets` | list of `{handle, reason}` | Set handles the model named that weren't valid. For debugging. |
| `error` | `null` or `{type, message, stage}` | `null` on success. On failure: `stage` is `model_call` (the model provider failed or timed out), `tool_call`, `step_limit` (the agent looped too long), `post_processing`, `timeout` (the runner's time limit, see below), `cancelled`, `busy` (another question is still running on this conversation; this one didn't run) or `unknown`. `answer`, `entity_ids` and `sources` are then empty. |

### The `geojson` field
- A standard FeatureCollection: `{"type": "FeatureCollection", "features": [...]}`.
- Each feature's geometry is `Point`, `Polygon` or `MultiPolygon`, with coordinates in **[longitude, latitude]** order as in all GeoJSON. Leaflet's `L.geoJSON`, Mapbox and OpenLayers read it directly.
- A `MultiPolygon` can straddle the 180° meridian (e.g. Taveuni in Fiji), split into one part per side.
- Feature `properties`:
  - `id`: graph id, matches `entity_ids`
  - `name`: display name, for the label
  - `type`: `location` today
  - `related_to`: present when the place is drawn on behalf of a non-spatial entity. For example, `event:battle_of_midway` is drawn at Midway Atoll with `related_to: "event:battle_of_midway"`. Use it to style or group markers.
  - `role`: `answer` when the feature (or the entity it's drawn for) is in `answer_ids`, otherwise `context`. Use it to highlight the answer, e.g. a stronger color for `answer`.
  - `geometry_source`: `direct` when the entity's own geometry is drawn (e.g. a battle with coordinates), `related` when it's drawn at a linked place (then `related_to` names the entity).
  - `via` and `strength`: on places from a set question. `via` is the path that connects the place, e.g. `"Isoroku Yamamoto -COMMANDED-> Battle of Midway -OCCURRED_AT-> Midway Atoll"`, good for a tooltip. `strength` is how well supported that path is (higher is stronger), good for marker size.
- Set questions can put many places on the map, up to 1,000 features (`result_sets[].map_truncated` says if more were cut). Use marker clustering, and consider coloring by the battle or org in `via`. The answer text is a summary (count, groups, top places), because the map holds the full set.
- An org, person or event in `entity_ids` is drawn only at its linked places that are part of this answer's evidence. For "Who attacked Corregidor in 1942?", the Imperial Japanese Army is drawn at Corregidor, not at every place it fought.

### The four kinds of reply to handle
The examples below leave out `schema_version`, `source_refs`, `bbox` and `center` for brevity; every real reply has them.

**1. Answer.** Text, ids and map features:
```json
{"answer": "The US Marine Corps attacked Guadalcanal from 7 August 1942; the Imperial Japanese Army attacked Henderson Field in September-October 1942.",
 "answer_ids": ["org:usmc", "org:ija"],
 "context_ids": ["loc:guadalcanal", "loc:henderson_field"],
 "entity_ids": ["org:usmc", "org:ija", "loc:guadalcanal", "loc:henderson_field"],
 "sources": ["Guadalcanal campaign"],
 "geojson": {"type": "FeatureCollection", "features": [
   {"type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [[[159.6, -9.2], [160.9, -9.2], [160.9, -10.0], [159.6, -10.0], [159.6, -9.2]]]},
    "properties": {"id": "loc:guadalcanal", "name": "Guadalcanal", "type": "location", "related_to": "org:usmc", "role": "answer"}},
   {"type": "Feature", "geometry": {"type": "Point", "coordinates": [160.05, -9.43]},
    "properties": {"id": "loc:henderson_field", "name": "Henderson Field", "type": "location", "related_to": "org:ija", "role": "answer"}}]},
 "tool_calls": [{"name": "search_entities", "args": {"query": "Guadalcanal"}},
                {"name": "find_relations", "args": {"target_id": "loc:guadalcanal", "relation_type": "ATTACKED", "year": 1942}}],
 "run_id": "605f2d20c1e44f0e9a3b7d2a4c1f8e01",
 "confidence": {"level": "high", "basis": {"max_edge_count": 5, "n_source_pages": 2, "uncertain_match_used": false}},
 "interpretation": [],
 "resolved_entities": [{"id": "loc:guadalcanal", "name": "Guadalcanal", "typed": "Guadalcanal", "matched_label": "Guadalcanal",
                        "match": "name", "score": 100, "quality": "clear", "model_rewritten_query": false}],
 "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [], "ungrounded_names": [],
 "warnings": [], "error": null}
```

**2. Clarification question.** The name was ambiguous, so the agent asks instead of guessing. `entity_ids` and `features` are empty. Show the question, and send the user's reply with the **same `thread_id`**:
```json
{"answer": "Did you mean the Battle of Guam (1941) or the Battle of Guam (1944)?",
 "answer_ids": [], "context_ids": [], "entity_ids": [], "sources": [], "geojson": {"type": "FeatureCollection", "features": []},
 "tool_calls": [{"name": "search_entities", "args": {"query": "Battle of Guam"}}],
 "run_id": "…", "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": false}},
 "interpretation": [], "resolved_entities": [], "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [], "ungrounded_names": [],
 "warnings": [], "error": null}
```
"Not in the graph" replies look the same: an explanation in `answer` and no `answer_ids`. They may still have `context_ids`, e.g. the place that was asked about.

**3. Off-topic.** Poems, code, other subjects, requests to change role or reveal instructions. `answer` is a fixed message written by the code (not the model), ids and map are empty, and `warnings` is `["off_topic"]`:
```json
{"answer": "I can only answer questions about the Pacific theatre of World War II, using the knowledge graph built from Wikipedia: places, battles, people and organizations, and how they are connected. For example: \"Where did the Battle of Midway happen?\" or \"Show me all locations connected to Admiral Yamamoto.\"",
 "answer_ids": [], "context_ids": [], "entity_ids": [], "sources": [], "geojson": {"type": "FeatureCollection", "features": []},
 "tool_calls": [], "run_id": "…", "warnings": ["off_topic"], "error": null}
```

**4. Error.** Show a friendly message and keep `run_id` for the bug report:
```json
{"answer": "", "answer_ids": [], "context_ids": [], "entity_ids": [], "sources": [],
 "geojson": {"type": "FeatureCollection", "features": []},
 "tool_calls": [], "run_id": "…", "warnings": [],
 "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": false}},
 "interpretation": [], "resolved_entities": [], "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [], "ungrounded_names": [],
 "error": {"type": "ModelDeadlineExceeded", "message": "model call exceeded 90s deadline 2 times", "stage": "model_call"}}
```

Every reply has all of these keys, including errors; list fields are empty and `confidence` is `low` when there is nothing to report. New keys may be added later; existing keys won't change name or type.

For a clarification reply, `confidence` is `low` but no `low_confidence` warning is raised: that warning is only for answers that have entities.

## Using the agent from the UI
The team's UI is a Streamlit app that imports the agent in-process. The adapter inside `App/` belongs to the UI developer; the agent provides:

```python
from geo_agent.runner import AgentRunner, get_runner, PendingAnswer, ThreadBusy
from geo_agent.schema import AskResult, SCHEMA_VERSION
from geo_agent.agent import AgentConfigError      # raised when OPENROUTER_API_KEY is missing (not in mock mode)
```

- `AgentRunner(graph_path=None, mcp_config=None, llm=None, *, timeout=None, start_timeout=120.0, mock=None)` owns **one background event loop** with one agent on it. The agent is async and bound to that loop, so **never** call it with `asyncio.run()` per question; hold one runner per process (`st.cache_resource`, or `get_runner()`).
- `.ask_sync(question, thread_id="default", *, timeout=None, if_busy="reject", on_progress=None) -> dict` returns the normal reply dict. A timeout, cancel or busy conversation comes back as an error reply (stage `timeout`, `cancelled`, `busy`), never as an exception.
- `.submit(...)` (same arguments) returns a `PendingAnswer`: `.done()`, `.result(timeout=None)`, `.cancel()`, `.poll_progress()` (step events such as "Searching the graph…"), `.run_id`.
- `.new_thread_id()`, `.is_busy(thread_id)`, `.cancel(thread_id)`, `.health()`, `.stats()` (graph size, geocoded count, version), `.get_entity_feature(entity_id)` (name, type, excerpt, sources and a GeoJSON feature, for map clicks), `.close()`.

```python
import time, streamlit as st
from geo_agent.runner import AgentRunner

@st.cache_resource
def runner(): return AgentRunner()          # GEO_AGENT_MOCK=1 for no-key mode

r = runner()
if "tid" not in st.session_state: st.session_state.tid = r.new_thread_id()
if q := st.chat_input("Ask about World War II", disabled=r.is_busy(st.session_state.tid)):
    p = r.submit(q, st.session_state.tid)
    with st.status("Searching the graph…") as status:
        while not p.done():
            for ev in p.poll_progress(): status.update(label=ev["label"])
            time.sleep(0.2)
    res = p.result()
    if res["error"]: st.error(f"{res['error']['stage']}: run {res['run_id'][:8]}")
    else: st.markdown(res["answer"])        # untrusted text: never unsafe_allow_html
```

Rules for the UI:
- **One runner per process**, one `thread_id` per user session, kept in `st.session_state`. Send a clarification answer on the same `thread_id`.
- **One question at a time per conversation.** A second one gets stage `busy`; disable the input while `is_busy(thread_id)`. `if_busy="wait"` queues instead.
- **Timeout** is `GEO_AGENT_RUN_TIMEOUT` (default 120 s; normal answers take 3–35 s). After a `timeout` or `cancelled` reply, that conversation continues without its earlier context (a half-finished turn can't be resumed); tell the user.
- **`on_progress` runs on the agent's thread:** don't call `st.*` from it; use `poll_progress()` from the script.
- **Render `answer` as untrusted Markdown** (`st.markdown` without `unsafe_allow_html`), and node names and excerpts as plain text or HTML-escaped.
- **Coordinates are GeoJSON `[lon, lat]`:** draw `geojson` with `folium.GeoJson` and fit to `bbox` (see the meridian note).

### Mock mode
`GEO_AGENT_MOCK=1` (or `AgentRunner(mock=True)`) needs no key, no `.env` and no network. It always uses the sample graph and returns recorded replies, generated through the real answer code, for:
- "Who attacked Guadalcanal in 1942?" and "Where did the Battle of Midway happen?" (answers)
- "What happened in the battle at Tara?" (clarification), then "Tarawa" on the same `thread_id`
- "Who won the Battle of Guam?" (both same-name battles answered, separated)
- "Show me major battles in the Pacific War on the map" (overview of a broad question)
- "Write me a poem about the ocean" (off-topic)
- "Simulate an error" (`model_call` error)
- "Show me all locations connected to Admiral Yamamoto" (set question)

Matching ignores case and punctuation; any other question gets a fixed reply listing these, with the warning `mock_no_fixture`. Mock replies carry `"mock": true`, write no trace, and spread fake progress events over `GEO_AGENT_MOCK_DELAY` seconds (default 0.6). After code changes, `uv run python -m geo_agent.mock` regenerates the fixtures (`--check` verifies them; a test does too).

## Debugging a run
Every `ask()` appends events to `logs/trace-YYYY-MM-DD.jsonl`: `run_start` (question, model, graph hash, tools, step limit), `model_end` per step (latency, tokens, requested tools, text), `tool_end` / `tool_error` per tool call (args, status `ok`/`empty`/`error`, latency, result hash and preview), `structured_output`, `finalize` (claimed vs kept ids and sources, resolved entities, confidence), and `run_end` (outcome, failure stage, warnings, diagnostics).

```bash
uv run geo-agent --trace last          # timeline of the latest run (or a run id / 8-char prefix)
uv run geo-agent --replay <run_id>     # re-run its tool calls on the current graph, no model; compares result hashes
uv run geo-agent --report --since 7d   # outcomes, failures by stage, warnings, p50/p95 latency, tool usage, tokens
LOG_LEVEL=INFO uv run geo-agent "..."  # one console line per step and tool call
```
A question starting with `--` is read as a flag.

**"The text is right, but the map is empty."** Open the run (`--trace <run_id>`; the UI shows the run id) and check two things:
1. **Did any tool return the places the answer names?** If not, the model wrote them from its own knowledge. The reply then carries `ungrounded_names` (the agent already sent the answer back once), and the fix is in the data or the question, not the map.
2. **Can the answer's entities be drawn?** Only places and entities with their own coordinates appear. A person or org is drawn at linked places that are part of the evidence, and an event at the places it occurred at. When nothing can be drawn, the reply has the warning `nothing_on_map`.

Example: "Where was the first kamikaze attack?" on the draft graph (run `c2cc1e3a`). The answer named the Battle of Leyte Gulf and USS St. Lo, but the tools had returned only the Kamikaze unit and Yukio Seki. Neither has a place, so the map stayed empty.

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `WARNING` | Console level |
| `GEO_AGENT_TRACE` | `1` | `0` turns trace files off |
| `GEO_AGENT_LOG_DIR` | the project's `logs/` | Where trace files go. The default is an absolute path (the project root's `logs/`, or `~/.geo_agent/logs` for an installed package), so it doesn't depend on the working directory |
| `GEO_AGENT_TRACE_FULL` | `0` | `1` stores full tool results instead of 1000-character previews |
| `GEO_AGENT_TRACE_TEXT_CHARS` | `2000` | Cut for model text and reasoning |
| `GEO_AGENT_LOG_QUESTIONS` | `1` | `0` omits the question text |
| `GEO_AGENT_WARN_STEPS` | `10` | Threshold for the `many_steps` warning |
| `GEO_AGENT_MAX_STEPS` | `25` | Step limit per run (LangGraph recursion limit). A few model calls before it, the model is told to answer with what it has, and the last call that fits (12 of 25 steps) is offered only the final-answer tool, so a question whose relation the graph lacks, or a model that keeps searching, ends with an answer, not a `step_limit` error |
| `GEO_AGENT_TIMEOUT` | `60` | Seconds per model request; a stalled request then fails as a `model_call` error instead of hanging |
| `GEO_AGENT_MAX_RETRIES` | `2` | Retries of a failed or timed-out model request (worst case ~3 × timeout per step) |
| `GEO_AGENT_RUN_TIMEOUT` | `120` | Seconds the runner waits for one answer before returning a `timeout` error reply |
| `GEO_AGENT_MOCK` | `0` | `1`: mock mode (recorded replies on the sample graph, no API key) |
| `GEO_AGENT_MOCK_DELAY` | `0.6` | Seconds a mock reply takes, spread over fake progress events |
| `GEO_AGENT_BIG_GRAPH` | unset | Path to the team's full graph; turns on `tests/test_big_graph.py` |
| `GEO_AGENT_TOKEN_BUDGET` | `80000` | Input tokens per question. Past it the model is told to answer with what it has, and `token_budget` is raised |
| `GEO_AGENT_CONTEXT_TRIGGER` | `30000` | Context size (tokens) above which old tool outputs are hidden from the model (set-tool outputs are kept) |
| `GEO_AGENT_MODEL_DEADLINE` | `90` | Hard total seconds per model call, retried once. Needed because OpenRouter keeps a hung request alive with keep-alive bytes, so the read timeout alone never fires |

Trace files are never deleted automatically. Delete `logs/` by hand when it gets big (about 4 KB per run).

## Optional MCP tools
Add a server to `mcp_servers.json`. Every key except `allowed_tools`/`enabled` is passed to `langchain-mcp-adapters` as the connection config:
```json
{"servers": {
  "my_server": {"transport": "stdio", "command": "uv", "args": ["run", "my-mcp-server"],
                "allowed_tools": ["tool_name"], "enabled": true}
}}
```

## Next steps
- Point `GRAPH_PATH` at the real graph when the pipeline produces it, and add tests for its quirks.
- Use node/edge `embedding`s for semantic search once the graph team fixes the embedding model; the query must use the same model.
- Expose `GraphStore` to the UI, via an MCP server or a small API, if the map needs direct queries without the LLM.
