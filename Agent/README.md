# GeoRAG Agent: WWII Pacific theatre

This is the **agentic RAG** component of the *From Corpus into GeoRAG* project (see the project slides). Upstream teams turn Wikipedia articles on the WWII Pacific theatre into a geocoded knowledge graph. This agent answers geographic OSINT/GEOINT questions over that graph, for a text UI and a map UI. The UI, data collection, NER, geocoding and graph building are **out of scope** here.

## Quick start
```bash
uv sync
cp .env.example .env               # set OPENROUTER_API_KEY
uv run pytest                      # tool tests: no LLM, no network
uv run geo-agent "Who attacked Guadalcanal in 1942?"
uv run geo-agent                   # interactive session
GEO_AGENT_JSON=1 uv run geo-agent "Show me where the Battle of Midway happened"   # full UI payload
uv run geo-agent --trace last      # timeline of the last run (see "Debugging a run")
```

## Layout
| File | Role |
|------|------|
| `geo_agent/graph_store.py` | Loads/validates the graph JSON; name, graph, spatial, date and text queries. No LLM. |
| `geo_agent/tools.py` | LangChain tools wrapping `GraphStore`. The docstrings are the LLM's tool descriptions. |
| `geo_agent/agent.py` | OpenRouter LLM, system prompt, allowlisted MCP loading, `GeoAgent.ask()`, `finalize_answer()`, CLI. |
| `geo_agent/trace.py` | Run tracing (callback handler, JSONL writer), diagnostics, timeline, replay, report. |
| `data/sample_graph.json` | Hand-written WWII Pacific fixture (50 nodes, 84 edges) used until the real graph exists. It includes edge cases: meridian (Kiska/Adak, Taveuni), near-tied names (Guam 1941/1944), Chungking alias, Tarawa/Tarakan. |
| `mcp_servers.json` | Allowlist of optional external MCP servers/tools (empty by default). |
| `tests/test_tools.py` | Tool/store tests: the three example questions, name matching, meridian and date cases. |
| `tests/test_trace.py`, `tests/test_agent.py` | Tracing, failure stages, viewer/replay/report, error results. Fake model, no network. |

## Decisions
1. **LangChain `create_agent` with tool calling.** The LLM plans which tools to chain, so questions aren't limited to a fixed list of templates. The three example questions are covered by tests, but the prompt tells the agent to compose tools freely.
2. **The graph is the only source of truth.** There are no calls to Wikipedia or geocoding services at query time. If the graph doesn't have the answer, the agent says so.
3. **LLM via OpenRouter**, through `ChatOpenAI` on the OpenAI-compatible endpoint. The default model is `z-ai/glm-5.2`, chosen for cost. Change it with `GEO_AGENT_MODEL`; it must support tool calling.
4. **MCP is opt-in and allowlisted.** Built-in graph tools are native LangChain tools. External MCP tools load only if (a) the server is in `mcp_servers.json`, (b) the tool name is in that server's `allowed_tools`, and (c) the name does not collide with an existing tool. A server that fails to load is skipped, and the agent keeps working.
5. **The LLM returns ids and the code builds the map data.** The model returns `{answer, entity_ids, sources}`, and `GeoAgent.ask()` builds the GeoJSON from the graph. The map therefore always gets real geometry, and the LLM never has to reproduce coordinates.
6. **Evidence-checked output.** Only ids and source pages that this turn's graph queries returned are kept; the rest are reported as dropped or unverified. Coordinates written in the answer text are checked against the values the tools returned and flagged if they don't match (the text itself is never changed). Orgs, people and events are drawn only at places in the answer's evidence.
7. **Answer and context are separate.** The model returns `answer_ids` (what answers the question, e.g. the attackers) and `context_ids` (the place, defenders, commanders). With one flat list, a defender the answer mentions looked exactly like a wrong attacker. The roles come from the question, so nothing extra is needed in the graph. Telling attackers from defenders does need typed relations (`ATTACKED` vs `DEFENDED`), as "who attacked X" always did.
8. **Visible name resolution.** Every name match has a quality (`clear`/`uncertain`). On an uncertain match the agent asks a short clarification question instead of guessing; when a name was read differently from how it was typed, `interpretation` says so in plain words. A question year outside the resolved event's dates is flagged, never silently corrected.
9. **Pure query layer.** All logic lives in `GraphStore`, so it can be tested without an LLM. A future MCP server or REST API for the UI can wrap the same methods.
10. **Tolerant input schema.** Pydantic models allow extra fields, and node/edge types are open strings. Edges that point to unknown nodes are dropped, not fatal.
11. **Spatial semantics.** Distance is great-circle km from the query point to the *nearest point* of a geometry (0 if inside). A point on Guadalcanal therefore matches the island polygon and the Solomon Islands. Distances wrap correctly across the 180° meridian (Kiska to Adak is ~396 km, not ~40,000). A bounding box with `min_lon > max_lon` crosses the meridian: `170 … -170` is the strip around the date line.
12. **Dates.** Partial ISO dates are allowed (`1942`, `1942-06`). Date filters use interval overlap, and undated edges are excluded when a filter is set.
13. **Sub-locations.** `find_relations` expands a location through `LOCATED_IN` by default. "Who attacked Guadalcanal" therefore includes attacks on Henderson Field.
14. **Text retrieval is BM25** over node text paragraphs. It is simple, needs no API, and returns citable paragraphs.
15. **Local tracing, no hosted service.** Every run writes JSONL events through a LangChain callback handler, so tools and `GraphStore` stay untouched. Trace writes can never fail a run. Questions and 1000-character result previews are stored locally; keys and env values never are.
16. **Errors are returned as data.** `ask()` never raises for model, tool, step-limit or post-processing failures; it returns `error: {type, message, stage}` so the UI always gets a reply and the trace says where the run stopped.

## Input schema (for the knowledge-graph builder)
The agent reads **one JSON file**, set by `GRAPH_PATH` (default `data/sample_graph.json`). It holds two arrays:

```text
{"nodes": [ ... ], "edges": [ ... ]}
```

`data/sample_graph.json` is a complete working example (50 nodes, 84 edges) to copy from.

### Nodes
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | **yes** | Unique id. Convention: `<type>:<slug>`, e.g. `loc:midway_atoll`, `event:battle_of_midway`, `org:ijn`, `per:yamamoto`. Edges refer to it. |
| `name` | string | **yes** | Display name, the main name used for matching, e.g. `"Midway Atoll"`. |
| `type` | string | **yes** | `location`, `person`, `org` or `event`. Open list: a new type such as `time` loads without code changes. |
| `aliases` | list of strings | no, but important | Other names and spellings: `["Midway", "Midway Island"]`, romanizations (`"Chungking"` for Chongqing), abbreviations (`"IJN"`). The agent finds an entity only by its name and aliases. It makes no online lookups, so a spelling missing here isn't found. |
| `geometry` | GeoJSON geometry or `null` | yes for `location` | `Point`, `Polygon` or `MultiPolygon`, WGS84, coordinates in **[longitude, latitude]** order, longitude in -180…180. Split a shape that crosses the 180° meridian into a `MultiPolygon`. Use `null` for people, orgs and events: they're placed on the map through their edges. |
| `sources` | list of `{page, paragraph, url}` | no, but important | Where the entity was extracted from: Wikipedia page title, paragraph index, URL. The answers cite the `page` values. |
| `text` | string | no, but important | Raw source paragraph(s) the entity came from, separated by blank lines. Used for text search and evidence quotes. |
| `attributes` | object | no | Free-form extras. For `event` nodes please set `start_date` and `end_date` (see Dates). |
| `embedding` | list of numbers or `null` | no | Accepted but not used yet. |

### Edges
| Field | Type | Required | Meaning |
|---|---|---|---|
| `source` | string | **yes** | Node `id` of the subject (the attacker, the event, the person). |
| `target` | string | **yes** | Node `id` of the object (the place attacked, the event's location). An edge whose `source` or `target` doesn't exist is dropped at load time. |
| `type` | string | **yes** | Relation type in UPPER_SNAKE_CASE. See the vocabulary below. |
| `count` | integer ≥ 1 | no (default 1) | Number of mentions supporting the edge. Higher means better supported. |
| `start_date`, `end_date` | string or `null` | no, but important | When the relation held, as ISO dates. Partial dates are fine: `"1942"`, `"1942-06"`, `"1942-06-04"`. |
| `sources` | list of `{page, paragraph, url}` | no | Evidence for this edge. |
| `attributes`, `embedding` | | no | As for nodes. |

Unknown extra fields on nodes and edges are kept and ignored, so the format can grow.

### Relation vocabulary (WWII Pacific, open list)
Direction matters: `source` does `type` to `target`.

| Type | Direction | Meaning |
|------|-----------|---------|
| `OCCURRED_AT` | event → location | where an event happened |
| `PARTICIPATED_IN` | person/org → event | took part in |
| `COMMANDED` | person → org/event | command role |
| `ATTACKED`, `DEFENDED` | org → location | offensive / defensive action |
| `CAPTURED`, `OCCUPIED`, `LANDED_AT` | org → location | territorial change |
| `BOMBED` | org → location | air/naval bombardment |
| `SANK` | org → org/vessel | naval loss |
| `MEMBER_OF` | person → org | affiliation |
| `ALLIED_WITH` | org → org | alliance/cooperation |
| `LOCATED_IN` | location → location | containment: "who attacked Guadalcanal" also finds attacks on Henderson Field, which is `LOCATED_IN` Guadalcanal |
| `CO_MENTIONED` | any → any | fallback when only co-occurrence in the text is known |

### Dates
- An edge's `start_date`/`end_date` say when the relation held. They drive questions like "who attacked Guam in 1944".
- A date filter matches by interval overlap. An edge without dates is left out whenever a date filter is used.
- For events, also put `start_date`/`end_date` in `attributes`.

### What the agent needs from the graph, in priority order
1. **Battles, raids and landings as `event` nodes**, with `OCCURRED_AT` edges to their locations. Without them "where did the Battle of X happen" can't be answered.
2. **Dates** on edges, and in the attributes of event nodes.
3. **Geometry** on every location (lon/lat order, meridian-crossing shapes split).
4. **Aliases**: alternative names, transliterations, abbreviations.
5. **`sources` and `text`**, so answers can cite and quote Wikipedia.
6. **`LOCATED_IN` edges** for places inside other places.
7. **Clean ids**: unique, and every edge endpoint present.

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
| `graph_schema()` | Node/relation types with counts, date range and bbox. |
| `search_entities(query, type?, limit)` | Fuzzy name/alias → entity ids. |
| `get_entity(entity_id)` | Full node: geometry, attributes, sources, text excerpt. |
| `get_neighbors(entity_id, relation_types?, direction?, neighbor_type?, year?/date_from?/date_to?)` | One-hop neighbors with filters. |
| `find_relations(source_id?, target_id?, relation_type?, year?/dates?, include_sublocations)` | Edge query; empty endpoint = wildcard. |
| `get_locations(entity_id)` | Geolocated places linked to an event/org/person. |
| `entities_near(lat, lon, radius_km, type?)` | Radius search, nearest first. |
| `entities_in_bbox(min_lon, min_lat, max_lon, max_lat, type?)` | Map-region search. |
| `what_happened_at(lat, lon, radius_km)` | Nearby places → their events and relations, in time order. |
| `search_source_text(query, entity_ids?, k)` | BM25 over source text, with Wikipedia citations. |
| `to_geojson(entity_ids)` | FeatureCollection. Non-spatial entities are drawn at their linked places. |

How the example questions map to tools (examples, not a closed list):
- *"Show me places where the Battle of X happened"*: `search_entities` → `get_locations`
- *"Who attacked X in 1942?"*: `search_entities` → `find_relations(target_id, "ATTACKED", year=1942)`
- *"What happened at (x, y)?"*: `what_happened_at` (then `search_source_text` for detail)

## Output schema (for the UI)
### How to call the agent
```python
from geo_agent.agent import GeoAgent

agent = await GeoAgent.create()            # once at startup: loads the graph and the model
result = await agent.ask("Who attacked Guadalcanal in 1942?", thread_id="session-123")
```
From the command line, `GEO_AGENT_JSON=1 uv run geo-agent "question"` prints the same dict as JSON.

`ask()` always returns a dict and never raises for model or tool problems: errors come back in the `error` field. Use the same `thread_id` for one user's conversation. Follow-up questions, and answers to a clarification question, need it to keep context. Memory is per process and lost on restart.

### Fields
| Field | Type | Meaning |
|---|---|---|
| `answer` | string | Text for the chat panel. May contain Markdown (bold, lists). Empty when `error` is set. |
| `answer_ids` | list of strings | The entities that **answer the question**. "Who attacked X": only the attackers. "Where did X happen": the places. "What happened at (x, y)": the events. Highlight these. Empty for a clarification question, a "not in the graph" answer, or an error. |
| `context_ids` | list of strings | Other entities the answer mentions that help on the map: the place asked about, defenders, commanders, the related battle. Show these more quietly. |
| `entity_ids` | list of strings | `answer_ids` followed by `context_ids`, e.g. `["org:usmc", "loc:guadalcanal"]`. Kept so code written for the earlier output keeps working. All three lists hold only ids that the agent's graph queries actually returned; anything else the model named goes to `dropped_ids`. |
| `sources` | list of strings | Wikipedia page titles the answer relies on, e.g. `["Guadalcanal campaign"]`. Only pages that appeared in the agent's evidence; others go to `unverified_sources`. |
| `geojson` | GeoJSON FeatureCollection | Ready-to-draw map data built from the graph, never from the model (see below). Empty `features` when there's nothing to show. |
| `tool_calls` | list of `{name, args}` | The graph queries the agent ran. Useful for a "how I found this" panel or for debugging. |
| `run_id` | string | Id of this run's trace file entry. Show it in bug reports: `uv run geo-agent --trace <run_id>` replays what happened. |
| `confidence` | `{level, basis}` | `level` is `high`, `medium` or `low`: how well the graph supports the answer. Show it as a label, not a percentage; the thresholds aren't calibrated yet. `basis` has `max_edge_count`, `n_source_pages`, `uncertain_match_used`. |
| `interpretation` | list of strings | How names in the question were read when it wasn't literal, e.g. `"'Chungking' read as Chongqing (alias)"`. **Show these to the user**, so a wrong reading is visible. Empty when every name matched as typed. |
| `resolved_entities` | list of objects | One per final entity found by name: `id`, `name`, `typed` (the text searched), `matched_label`, `match` (`name`/`alias`/`fuzzy`), `score`, `quality` (`clear`/`uncertain`), `model_rewritten_query`, and `context_mismatch` when the question's year doesn't fit the event's dates. For debugging or a details panel. |
| `dropped_ids` | list of `{id, reason}` | Ids the model named but the evidence didn't support (`reason`: `unknown` or `not_in_tool_output`). Not drawn. |
| `unverified_sources` | list of strings | Pages the model cited that never appeared in its evidence. |
| `unverified_numbers` | list of numbers | Coordinates written in `answer` that match no value the graph returned. The text isn't changed; consider a small "unverified" marker. |
| `warnings` | list of strings | Codes for things worth flagging, empty when all went well. Answer quality: `low_confidence`, `uncertain_match_used` (a weak or ambiguous name match was used), `context_mismatch` (e.g. "Battle of Midway in 1944"; the battle was 1942), `model_rewritten_query` (the model searched a spelling the user didn't type), `ids_dropped`, `sources_unverified`, `text_coordinates_unverified`. Run health: `structured_output_failed` (plain text without ids), `repeated_tool_call`, `empty_resolution` (the first name lookup found nothing), `many_steps`. |
| `error` | `null` or `{type, message, stage}` | `null` on success. On failure: `stage` is `model_call` (the model provider failed or timed out), `tool_call`, `step_limit` (the agent looped too long), `post_processing` or `unknown`. `answer`, `entity_ids` and `sources` are then empty. |

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
- An org, person or event in `entity_ids` is drawn only at its linked places that are part of this answer's evidence. For "Who attacked Corregidor in 1942?", the Imperial Japanese Army is drawn at Corregidor, not at every place it fought.

### The three kinds of reply to handle
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
 "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [],
 "warnings": [], "error": null}
```

**2. Clarification question.** The name was ambiguous, so the agent asks instead of guessing. `entity_ids` and `features` are empty. Show the question, and send the user's reply with the **same `thread_id`**:
```json
{"answer": "Did you mean the Battle of Guam (1941) or the Battle of Guam (1944)?",
 "answer_ids": [], "context_ids": [], "entity_ids": [], "sources": [], "geojson": {"type": "FeatureCollection", "features": []},
 "tool_calls": [{"name": "search_entities", "args": {"query": "Battle of Guam"}}],
 "run_id": "…", "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": false}},
 "interpretation": [], "resolved_entities": [], "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [],
 "warnings": [], "error": null}
```
"Not in the graph" replies look the same: an explanation in `answer` and no `answer_ids`. They may still have `context_ids`, e.g. the place that was asked about.

**3. Error.** Show a friendly message and keep `run_id` for the bug report:
```json
{"answer": "", "answer_ids": [], "context_ids": [], "entity_ids": [], "sources": [],
 "geojson": {"type": "FeatureCollection", "features": []},
 "tool_calls": [], "run_id": "…", "warnings": [],
 "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": false}},
 "interpretation": [], "resolved_entities": [], "dropped_ids": [], "unverified_sources": [], "unverified_numbers": [],
 "error": {"type": "ModelDeadlineExceeded", "message": "model call exceeded 90s deadline 2 times", "stage": "model_call"}}
```

Every reply has all of these keys, including errors; list fields are empty and `confidence` is `low` when there is nothing to report. New keys may be added later; existing keys won't change name or type.

For a clarification reply, `confidence` is `low` but no `low_confidence` warning is raised: that warning is only for answers that have entities.

## Debugging a run
Every `ask()` appends events to `logs/trace-YYYY-MM-DD.jsonl`: `run_start` (question, model, graph hash, tools, step limit), `model_end` per step (latency, tokens, requested tools, text), `tool_end` / `tool_error` per tool call (args, status `ok`/`empty`/`error`, latency, result hash and preview), `structured_output`, `finalize` (claimed vs kept ids and sources, resolved entities, confidence), and `run_end` (outcome, failure stage, warnings, diagnostics).

```bash
uv run geo-agent --trace last          # timeline of the latest run (or a run id / 8-char prefix)
uv run geo-agent --replay <run_id>     # re-run its tool calls on the current graph, no model; compares result hashes
uv run geo-agent --report --since 7d   # outcomes, failures by stage, warnings, p50/p95 latency, tool usage, tokens
LOG_LEVEL=INFO uv run geo-agent "..."  # one console line per step and tool call
```
A question starting with `--` is read as a flag.

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `WARNING` | Console level |
| `GEO_AGENT_TRACE` | `1` | `0` turns trace files off |
| `GEO_AGENT_LOG_DIR` | `logs` | Where trace files go |
| `GEO_AGENT_TRACE_FULL` | `0` | `1` stores full tool results instead of 1000-character previews |
| `GEO_AGENT_TRACE_TEXT_CHARS` | `2000` | Cut for model text and reasoning |
| `GEO_AGENT_LOG_QUESTIONS` | `1` | `0` omits the question text |
| `GEO_AGENT_WARN_STEPS` | `10` | Threshold for the `many_steps` warning |
| `GEO_AGENT_MAX_STEPS` | `25` | Step limit per run (LangGraph recursion limit) |
| `GEO_AGENT_TIMEOUT` | `60` | Seconds per model request; a stalled request then fails as a `model_call` error instead of hanging |
| `GEO_AGENT_MAX_RETRIES` | `2` | Retries of a failed or timed-out model request (worst case ~3 × timeout per step) |
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
