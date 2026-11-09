# Notes for the UI team: connecting the Streamlit app to the agent

**From:** the agent (Michael) · **Date:** 2026-10-13, updated 2026-11-09 · **About:** `App/app.py` at commit `9bc81e5` ("App: align with the Agent output contract"), rechecked at `2411e43`, and the agent in `Agent/`

## Summary
- **The app's latest version already does most of the integration well.**
  - Text is rendered as Markdown and names are HTML-escaped.
  - Each session gets its own `thread_id`.
  - All reply kinds are handled.
  - The GeoJSON is drawn directly, with meridian handling and clustering.
  - The sidebar shows real stats.
- **The agent now ships its own runner and mock mode,** built for exactly this: `geo_agent.runner.AgentRunner` and `GEO_AGENT_MOCK=1`. **Recommendation:** replace the app's own `AgentRunner` and `MockAgent` with them. This fixes timeouts and double submits, adds progress events, and keeps mock replies identical to real ones.
- **We ran your app end to end with the agent (2026-10-18).** We wrote the integration ourselves in a local copy, following this note, and nothing was pushed. It works in mock mode, with the real agent on the sample graph, and on the 465 MB draft graph. The one real trap we hit is in section 0.
- **The reply format grew to `schema_version` "1.4"** (1.2: `source_refs`, `bbox`, `center`, feature `geometry_source`, new error stages; 1.3: `ungrounded_names`; 1.4: `resolved_entities[].match` can be `nickname`, `hull_number` or `alias_part`). Nothing the app reads today changed name or type. Don't compare the version to a fixed string: additions are minor versions.
- **The graph is getting broader.** The team's next graph covers World War II beyond the Pacific (`metadata.scope`), and ids become Wikidata QIDs. Don't hard-code "Pacific" assumptions or id formats.
- **English only.**

## What changed for the app (2026-11-09, later)
`schema_version` is now "1.5". One new optional feature property, so nothing breaks:
- **`answer_for`** on a map feature lists the answer people or units shown there. A "who commanded at Leyte Gulf?" answer now highlights the gulf as `answer`, with `answer_for: [Halsey, Kurita, …]` (ids). Show their names in the popup ("Shown for").
- Sources on the real graph now carry page titles instead of `enwiki:<id>`, so the source links read as titles.

## Marking an area on the map (R102, 2026-11-09)
`schema_version` "1.6". The agent developer writes this part of `App/app.py`, as agreed, with **no change to the layout or sizes**: map left, chat right.
- **What the user does:** the map gets a draw control (circle, rectangle, polygon) inside the map frame. Drawing a shape asks "What happened in the marked area?" at once. While the shape stays marked, every typed question is sent with it ("who fought here in 1942?"). A line at the top of the chat box says an area is marked and offers "clear area"; New chat clears it too.
- **What the app sends:** `runner.submit(question, thread_id, area=...)` with a GeoJSON `Polygon` in lon/lat, or a circle as `{"type": "Point", "coordinates": [lon, lat], "radius_km": r}` (Leaflet gives the radius in metres).
- **What comes back:** a new key `area` (`{handle, kind, center, radius_km | bbox}` or null), and the area as the first map feature, with `type` and `role` `area`: draw it as a dashed outline without fill. An area the agent can't use (too large, a crossed outline) comes back as a short reply with warning `area_invalid`.
- `role` can now be `area` as well as `answer` and `context`; treat any other value as `context`.

## What changed for the app (2026-11-09)
The reply format is unchanged (`schema_version` stays "1.4"), so the app needs no code change.
- **Use the self-fixed graph:** set `GRAPH_PATH` to `data/partof521/graph.selffix.json`, a Git LFS file in `Agent/` (run `git lfs pull`; on Streamlit Cloud, make sure the deploy fetches LFS files). `Agent/graph_enrichment` builds it from the team's delivery; see the agent README, "Building the graph the agent should use". It loads in about 10 s. The sample graph stays the default, for tests and mock mode.
- **"Who commanded" and "which side" now have data.** Battles carry their two infobox sides, with commanders and units. The tool results the app shows under "graph queries" can include `side: 1` or `side: 2` on a relation. That number is the infobox column, not attacker or defender.
- **Entity cards have new attributes** (`birth_date`, `death_date`, `ner_type`, `wikidata_type`, `selffix`). The app doesn't render attributes today; if it ever does, leave out the internal ones (`selffix`, `merged_from`, `mention_types`).

## What changed for the app (2026-11-05)
The reply format is unchanged (`schema_version` stays "1.4"), so nothing has to change. Worth knowing:
- **New warning `empty_answer`:** the model returned nothing, and `answer` is a fixed sentence saying the agent couldn't finish. Treat it as a failed answer: suggest asking in a narrower way.
- **New warning `raw_answer`:** the model twice returned raw data instead of an answer, and `answer` is the same fixed sentence as for `empty_answer`. Handle it the same way.
- **New warning `tool_call_cap`:** the agent reached its limit on tool calls, so the answer may be incomplete. This is rare; no good answer so far has come near it.
- **Questions outside 1938 to 1948 get a "that's outside my window" answer,** with nothing on the map. Events outside those years are no longer in the graph the agent uses.
- **A war as a whole has no places.** MacArthur's map now shows campaigns and battles, not continents.
- **Battles can appear as markers** (`type: "event"`) as well as places. Both are correct.
- **Loading a real graph is slower:** about 25 seconds and 2.3 GB for the current 460 MB checkpoint. Keep showing a loading state, and load it once per process.

## 0. What we learned running your app with the agent (2026-10-18)
- **Keep the running question in `st.session_state` and check it on every rerun.** If the app polls the answer only inside the function that handles the question, any click while the agent works (a sidebar button, a suggestion) reruns the script and the answer is lost. Store the `PendingAnswer` from `submit()` in session state, and at the top of every run, if one is pending, poll it and render it when it's done.
- **Disable input while a question runs:** the chat input and the suggestion buttons, using `is_busy(thread_id)` or the pending answer. A Stop button can call `cancel()`.
- **Fall back to mock mode visibly:** with no key, or if startup fails, show the reason in the sidebar instead of silently switching.
- **Two new warnings deserve a visible note:**
  - `nothing_on_map`: the answer has entities, but none has a place in the graph. Say "no location in the graph for this answer" instead of showing an empty map.
  - `ungrounded_names`: the answer names entities no tool returned, i.e. from the model's own knowledge. Show them as "not from the graph".
- **Answers may now cover several same-name entities** (both Battles of Guam, clearly separated), and **broad questions get an overview** ("major battles of the Pacific War": hundreds of items on the map, the text gives the main groups and ways to narrow). Both render like any answer with a result set.
- **Links:** show `source_refs` and `sources` as links, allowing only `http(s)` URLs, and escape Markdown in their titles.
- **Measured on the draft graph:**
  - load 32.5 s, about 3 GB of memory
  - answers in 6–28 s
  - a 1,000-feature map across the 180° meridian renders in 0.1 s (358 KB) with clustering

## 1. Switch to the agent's runner
| Today in `app.py` | With `geo_agent.runner.AgentRunner` |
|---|---|
| Timeout: `future.cancel()` and an error with stage `unknown`; the agent run keeps going on the shared loop | Stage `timeout` (`GEO_AGENT_RUN_TIMEOUT`, default 120 s); the run is cancelled and the conversation restarts cleanly (a half-finished turn would be rejected by the model provider) |
| Two questions can run at once on one conversation (double submit, rerun) | One question per `thread_id` at a time; a second gets stage `busy` (or waits with `if_busy="wait"`); disable the input with `is_busy(tid)` |
| `_create_agent` catches `SystemExit` for a missing key | The agent raises `geo_agent.agent.AgentConfigError`, a normal exception |
| No progress during the 3–35 s wait | `submit()` + `poll_progress()` gives step events ("Searching the graph…") for `st.status` |
| `_read_graph_stats` reads `agent.store` internals | `health()` and `stats()` (graph size, geocoded count, model, version, schema version) |
| `MockAgent`: hand-written replies that drift from the real schema (no `schema_version`, `source_refs`, `bbox`) | `GEO_AGENT_MOCK=1`: replies recorded from the real agent code, regenerated whenever the agent changes |
| `sys.path` insert of `AGENT_DIR` | Install the agent into the app's environment (`pip install -e ../Agent`, or uv) |

```python
import time, streamlit as st
from geo_agent.runner import AgentRunner
from geo_agent.agent import AgentConfigError

@st.cache_resource(show_spinner="Loading the GeoRAG agent…")
def get_runner():
    try:
        return AgentRunner()                    # real agent; needs OPENROUTER_API_KEY server-side
    except AgentConfigError:
        return AgentRunner(mock=True)           # no key: recorded replies (res["mock"] is True)

r = get_runner()
if q := st.chat_input("Ask about World War II", disabled=r.is_busy(st.session_state.thread_id)):
    p = r.submit(q, st.session_state.thread_id)
    with st.status("Searching the graph…") as status:
        while not p.done():
            for ev in p.poll_progress(): status.update(label=ev["label"])
            time.sleep(0.2)
    res = p.result()                            # always a dict, never an exception
```

- `on_progress` callbacks run on the agent's thread, so don't call `st.*` from them. `poll_progress()` is the safe way.
- `r.get_entity_feature(entity_id)` returns name, type, excerpt, sources and a GeoJSON feature for a clicked map object. Render the excerpt as plain text.
- `geo_agent.schema.AskResult.model_validate(res)` checks a reply against the contract. It's useful in a test, and `res["schema_version"]` tells you which version you got.

## 2. Use the new reply fields
- **`bbox` and `center`:** the agent now sends the map extent. `prepare_geojson` computes bounds itself, which is fine; you can use `res["bbox"]` instead. A box crossing the 180° meridian has `min_lon > max_lon` (add 360 to `max_lon` for Leaflet).
- **`source_refs`:** a list of `{provider, record_id, url, page}`. Wikidata and EventKG records go here, while `sources` stays Wikipedia page titles only. Show both, with links.
- **Feature `geometry_source`:** `direct` (the entity's own coordinate, e.g. a battle site) or `related` (drawn at a linked place, named in `related_to`). Consider a different marker style.
- **Error stages:** `timeout`, `cancelled` and `busy` join `model_call`, `tool_call`, `step_limit`, `post_processing` and `unknown`. After `timeout`/`cancelled`, tell the user the conversation context was reset.
- **New warnings worth a small note in the UI:**
  - `approximation_used`: a kind, country or concept was guessed; see `interpretation`
  - `instruction_text_removed`: suspicious text was removed from the source data
  - `token_budget`: the answer may be incomplete
  - `result_set_unknown`
  - `nothing_on_map` and `ungrounded_names` (see section 0)

## 3. Keep doing (already right, please don't regress)
- `answer` and user text go through `st.markdown` without `unsafe_allow_html`. Every GeoJSON property is HTML-escaped before Folium uses it.
- `folium.GeoJson` gets the agent's `[lon, lat]` coordinates directly, with no hand conversion.
- One runner per process (`st.cache_resource`) and one `thread_id` per session, so a clarification reply goes on the same `thread_id`.
- `interpretation`, `confidence.level` (shown as a label), `result_sets` totals and `warnings` are displayed.

## 4. Smaller items
- **`App/README.md` describes the old demo:** radar markers, bounding boxes, scenario buttons with fixed content. Update it to describe the agent-driven app.
- **Repo hygiene:** `App/__pycache__/` and `.idea/` are committed although `.gitignore` lists them; `App/a.txt` looks like a placeholder; the project PDF is duplicated in `App/`.
- **Python version:** the committed `.pyc` suggests Python 3.14. The agent is tested on 3.11–3.13. Please confirm that shapely, langchain and rapidfuzz install in your environment, and on Streamlit Cloud (the live app).
- **Memory on Streamlit Cloud:** with the team's full draft graph the agent needs about 3 GB of memory and about 20 s to load. Streamlit Community Cloud may not have that much. The sample graph and mock mode are small. If the full graph is needed online, we may need a separate host or a smaller graph export.
- **The key:** `OPENROUTER_API_KEY` belongs in Streamlit secrets or a server-side `.env`, never in the repo or in browser code.

## Acceptance checklist
1. Every reply kind renders, including a `busy` or `timeout` error, and a clarification continues on the same `thread_id`.
2. A `<script>` or `<img onerror=…>` payload in a question, an answer or a node name shows as text.
3. Features land at the right place, including a set that crosses the 180° meridian.
4. A set question with hundreds of places stays responsive.
5. Mock mode runs with no `.env`, and its replies have `"mock": true` and the agent's current `schema_version`. Its recorded questions:
   - "Who attacked Guadalcanal in 1942?" and "Where did the Battle of Midway happen?"
   - "What happened in the battle at Tara?", then "Tarawa" on the same `thread_id`
   - "Who won the Battle of Guam?" (both battles)
   - "Show me major battles in the Pacific War on the map" (overview)
   - "Show me all locations connected to Admiral Yamamoto"
   - "Write me a poem about the ocean"
   - "Simulate an error"
6. Errors show their `run_id`, which the agent developer can replay with `uv run geo-agent --trace <run_id>`.
7. A click while the agent is working doesn't lose the answer (section 0).

**When the text is right but the map is empty,** see "Debugging a run" in `Agent/README.md`. Send us the `run_id`.

The full reply schema is in `Agent/README.md` ("Output schema" and "Using the agent from the UI") and machine-readable in `Agent/geo_agent/schemas/ask_result.schema.json`.
