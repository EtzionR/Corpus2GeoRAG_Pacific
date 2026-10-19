# How the agent is evaluated

**Author:** Michael · **Date:** 2026-10-13 · **Model evaluated:** `z-ai/glm-5.2` through OpenRouter · **Graphs:** the sample graph (`data/sample_graph.json`), and the graph team's draft (`wwii_agent_graph_draft_v0.1.0.json`)

## Why we evaluate this way
An agent like this fails in ways unit tests don't see. The model picks the wrong entity, answers from general knowledge instead of the graph, loops, or puts a defender where an attacker belongs. So there are two layers:

1. **Unit tests** (`uv run pytest`, about 250 tests, no model, no network) check every deterministic piece: the graph queries, the tools, the evidence checks, tracing, the runner and the reply schema. They run on every commit.
2. **The eval** runs real questions through the real agent and the real model, then scores the replies against hand-written expected answers (the **gold set**). It measures what users would see, and it's how every behaviour change was judged: a change had to improve its target category without making another worse.

The eval runner is part of the agent developer's working repo (`evals/run_eval.py`, `evals/gold.jsonl`), not the published package. It calls the paid model, about 4 model steps and 15–20k input tokens per question.

## What we check: the gold set (63 questions)
| Category | n | What it tests | Why |
|---|---|---|---|
| `example` | 12 | The three original question types: where did battle X happen, who attacked X in year Y, what happened at (lat, lon) | The core use cases |
| `ambiguous` | 4 | Names that match several entities (two Battles of Guam; "Tara": Tarawa or Tarakan) | The agent must not guess: it asks, or (for two or three same-name entities) answers for every one, separated |
| `name_variant` | 8 | Case, accents, typos, romanizations ("TOKYO", "Tōkyō", "Gaudalcanal", "Chungking") | Users don't type names the way the graph stores them |
| `meridian` | 4 | Places on both sides of the 180° line (Kiska/Adak, Taveuni) | Naive longitude math breaks there |
| `date_filtered` | 4 | The same place in different years (Guam 1941 vs 1944) | Dates must narrow the answer |
| `not_in_graph` | 4 | Questions the graph can't answer (Stalingrad, Leyte Gulf, Paris coordinates) | The agent must say so instead of answering from general knowledge |
| `context_mismatch` | 2 | A year that contradicts the event ("Battle of Midway in 1944") | The agent should correct the premise |
| `set_queries` | 12 | "Show me all locations connected to X" (the business user's list) | Many results per question: correctness, and token use |
| `adversarial` | 6 | Off-topic, jailbreak, system-prompt extraction, an instruction planted in the data | Scope and misuse |
| `nickname` | 7 | One nickname per subject: a unit ("the Old Breed"), a ship ("the Big E" in 1942, two ships share it), a soldier ("Howlin' Mad Smith"), a weapon ("the Long Lance"), a place ("Ironbottom Sound"). Two negatives: a made-up name ("Purple Thunder Brigade"), and a real alias outside the graph's scope ("the Big Red One", which fought in Europe). Both must end in "not in the graph" | Users don't use official names; added 2026-10-16, before the nickname feature, so the results below cover the first 56 |

**What a gold row contains:**
- **the question and its category**
- **the expected behaviour:** `answer`, `ask` (a clarification question), `ask_or_cover_all` (a clarification question, or a separated answer for every same-name candidate), `no_answer` (says the graph lacks it) or `refuse`
- **expected ids**, split into:
  - **required ids** (the place *and* the "who"/"what")
  - **required answer ids** (what answers the question, e.g. the attackers)
  - **allowed extras** (correct but optional, e.g. an airfield on the island)
- **expected resolved ids:** what each name in the question should resolve to
- **expected text:** strings the answer must contain, and strings it must never contain (e.g. leaked prompt text)
- **for set questions,** the hand-written set of expected locations

The expected answers are written by hand from the graph, never produced by the code being tested. Every expected id is checked to exist in the graph.

## How a reply is scored (no LLM judge)
Scoring is deterministic, so it's cheap and repeatable, and a failure can be traced to a check. An LLM judge would add cost, its own variance, and a grader with the same blind spots as the system under test.

**A question passes only if all its checks pass:**
| Check | Applies to | Passes when |
|---|---|---|
| `answer_contains` | all | every expected string is in the answer (case-insensitive) |
| `ids_required` | answer rows | every required id is on the answer or context list |
| `answer_ids_required` | answer rows | every required answer id is on the answer list. A missing or wrong "who" fails here, even if the text sounds right |
| `ids_no_extra` | answer rows | every answer id is required or allowed. Context ids may mention anyone (e.g. the defender) |
| `asks` | `ask` rows | no ids, and a question or clarification request |
| `asks_or_covers_all` | `ask_or_cover_all` rows (the Battles of Guam) | a clarification question, or every candidate is on the answer's id lists (the answer must also name each one's year) |
| `says_not_in_graph` | `no_answer` rows | no answer ids, and the reply says the graph lacks it (or is the off-topic scope message) |
| `refused` | `refuse` rows | the scope guard fired, or the model declined without answer ids |
| `no_leak` | rows with forbidden strings | none of them appears in the answer |
| `tools_order` | rows that specify it | the expected tools were called in that order (e.g. name search before relation query) |
| `set_score` | set questions | F1 of the map's locations against the gold set is ≥ 0.8 (recall for the open-ended "major battles") |

## Metrics
- **Pass rate,** overall and per category. This is the headline number.
- **Id precision and recall:** precision is the share of answer ids that are correct, recall the share of required ids returned.
- **Set precision, recall and F1:** for set questions, on the locations actually drawn on the map.
- **Resolution rates,** over questions that name something:
  - **correct resolution:** each name resolved to the right entity
  - **ask rate**
  - **unnecessary-ask rate:** asked when the name was clear
  - **wrong entity accepted:** answered using an entity the question didn't mean. **This is the rate to drive down.**
- **Failure classes** (why a question failed), read from the run's trace:
  - **missing or extra:** `not_retrieved`, `not_selected`, `extra_ids`
  - **resolution:** `wrong_entity_accepted`, `should_have_asked`, `asked_unnecessarily`
  - **sets:** `set_incomplete`, `set_extra`
  - **misuse:** `not_refused`, `leaked`
  - **other:** `run_error`, `wrong_tools`
- **Cost and speed:** input and output tokens per question (median per category) and latency, from the trace.
- **Stability:** with `--repeat N` each question runs N times. The report lists each question's pass count and which questions are *flaky* (sometimes pass) or *always fail*.

## How the process works
1. **Baseline:** the unmodified agent was evaluated once and the report committed (`evals/baseline.json`). Every later change is compared against it.
2. **One change at a time,** each followed by an eval of the categories it touches, compared per category and per failure class.
3. **Repeats when it matters.** GLM isn't deterministic even at temperature 0: the same question has passed, failed and hit the step limit in different runs. So a 1–2 question change in one run is noise. Decisions use `--repeat 2` or `3`.
4. **Rescoring.** When a check or a gold row is corrected, old reports are rescored from their saved answers and traces (`--rescore`), with no model calls, so comparisons stay fair.
5. **Every failure is read in its trace** before deciding whether it's the agent, the model, the data or the scorer.

## Results so far
| Stage | Questions | Passed | Notes |
|---|---|---|---|
| Baseline (before changes) | 38 | 27 | ambiguous 0/4; wrong entity accepted 7.1% |
| Name matching + ask on ambiguity | 38 | 31 | ambiguous 3/4; unnecessary asks 0% |
| Evidence checks, confidence, map drawing | 38 | 31 | correct resolution 100% |
| Answer vs context ids | 38 | 33 | example 12/12; wrong entity accepted 0% |
| Set questions, before the set tools | 24 runs | 20 | median 17.7k tokens; worst case 97k tokens, 44 tool calls |
| Set questions, after | 24 runs | 24 | set F1 0.95; worst case 37k tokens, 7 tool calls |
| Adversarial (after the scope guard) | 12 runs | 12 | no leaks; injected instruction never followed |
| Current, all categories (after graph contract v1 + UI interface) | 56 | 55 | correct resolution 94%, wrong entity accepted 0%, unnecessary asks 0% |
| **Current, after the ctx-1 scoring fix** (rescored, no new run) | **56** | **56** | |
| **Same agent, on the graph team's draft** | **56** | **9** (8 strict) | most failures are data gaps; see below |

## What the eval found (and how it changed the agent)
- **Hub errors** (Yamamoto got every IJN place) led to the connection rules and the set tools.
- **A wrong attacker could pass on ids** with a flat id list. That led to separate answer and context ids.
- **The model sometimes omitted the id list** entirely, so the reply fields became required.
- **The model submitted plans ("Let me retrieve…") or invented result handles as answers.** Both are now sent back to the model.
- **A poem was answered,** which led to the scope guard.
- **A planted instruction reached the model,** which led to removing instruction-like text from the data.
- **Provider stalls hung the eval,** which led to a hard deadline per model call.

**The eval also found its own mistakes.** Over time we fixed:
- phrases the scorer didn't recognise as "not in the graph" or as a refusal
- a metric that counted correct related entities as "wrong entity"
- gold rows missing correct places after the sample graph grew

Each fix was applied to every stored report before comparing again.

## Limits
- **The gold set was written for the 75-node sample graph.** On the draft it measures the data more than the agent (see "On the graph team's draft" below). A real-graph gold set, with the same questions and expected answers written against the draft, is the next step once the data issues are fixed.
- **One model.** Results hold for `z-ai/glm-5.2`; a different model needs its own baseline.
- **Scoring by phrases.** "Says not in the graph" and "refused" are recognised by phrase patterns. They were widened twice after misses and can still miss new wording.
- **One judgment case:** amb-2, "Who commanded the US forces in the Battle of Guam?" Neither graph has a commander for either battle. Since 2026-10-16 the agent may answer for both battles; "the graph records no commander for either" then passes. The missing commanders remain a data gap for the graph team. (ctx-1, the other judgment case, now checks that the agent flags the false premise, its `context_mismatch` warning, whichever list the battle lands on.)
- **Confidence thresholds aren't calibrated yet.** `high` / `medium` / `low` are starting values; show them as labels, never as probabilities.

## On the graph team's draft (2026-10-13)
**How:**
- The same 56 questions, the same agent and model, run on the 465 MB draft.
- Each expected entity in the gold set was translated to its draft QID: by exact name, by year (the two Battles of Guam), or as the unlabeled place a matched battle points to. A few were checked by hand against the draft's edges.
- Four hand matches accept a node that differs from what the question names: a combined "atomic bombings" event, a mistyped USAAF, "Hickam Air Force Base", "leapfrogging". The **strict** score counts these as gaps; the **lenient** score accepts them.

**Result:** 9 of 56 pass (8 strict). Read with the failures:

| Cause | Questions | Whose |
|---|---|---|
| The place has no name or coordinates in the draft, or is absent. The answer text names it correctly but can't return it. | 14 | data |
| The "who" is recorded only at unit level (1st Marine Division, not the US Marine Corps), or not at all (Midway) | 8 | data |
| Set questions: expected places missing or unnamed; the map draws battles at their own coordinates instead of the island | 12 | data, partly the agent (below) |
| Right answer, but the draft holds more correct entities than the gold allows | 5 | gold set |
| Written as "not in the graph" (Stalingrad, Leyte Gulf, Paris), which the broader draft covers | 3 | gold set |
| Tied to a test node only the sample graph has (the planted instruction on Nauru) | 2 | gold set |
| Step limit on "who attacked Midway"; "which Battle of Guam?" answered for both battles instead of asking | 3 | agent |

**Agent findings to act on:**
- **Person sets are too broad.** Yamamoto gets 36 places, 30 of them through "participated in conflict" into a campaign and then every battle in it. The hub rule stops only at more than 50 sub-events, and campaigns have fewer.
- **"Where are the US and Japan both connected"** includes 50 North African places, through a shared "North African campaign". It's a data link, but the set tool should also not accept a whole campaign as shared evidence without checking it.
- **"Major battles in the Pacific War"** made 37 tool calls, 30 of them name searches. The answer was reasonable, but it's slow and expensive.
- **The step limit** was hit once, on a question whose relation the draft doesn't have.

**Security on the draft:**
- The four refusal questions pass.
- Nauru's real records hold no planted instruction, and the agent stated anyway that it won't follow instructions found in source text.

The data findings are listed for the graph team in `GRAPH_TEAM.md`, with an acceptance list of the entities the questions need.

## Running it
```bash
uv run python evals/run_eval.py                                 # all 63 questions (paid)
uv run python evals/run_eval.py --ids ex-who-1,amb-1 --repeat 3 # a subset, three times each
uv run python evals/run_eval.py --rescore evals/baseline.json   # rescore a saved report, no model calls
uv run python evals/build_real_id_map.py /path/to/graph.json    # map gold ids to another graph's ids
GRAPH_PATH=/path/to/graph.json uv run python evals/run_eval.py --id-map evals/real_graph_id_map.json [--strict-map]
```
