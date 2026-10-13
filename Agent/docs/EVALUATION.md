# How the agent is evaluated

**Author:** Michael · **Date:** 2026-10-13 · **Model evaluated:** `z-ai/glm-5.2` through OpenRouter · **Graph:** the sample graph (`data/sample_graph.json`)

## Why we evaluate this way
An agent like this fails in ways unit tests don't see. The model picks the wrong entity, answers from general knowledge instead of the graph, loops, or puts a defender where an attacker belongs. So there are two layers:

1. **Unit tests** (`uv run pytest`, about 250 tests, no model, no network) check every deterministic piece: the graph queries, the tools, the evidence checks, tracing, the runner and the reply schema. They run on every commit.
2. **The eval** runs real questions through the real agent and the real model, then scores the replies against hand-written expected answers (the **gold set**). It measures what users would see, and it's how every behaviour change was judged: a change had to improve its target category without making another worse.

The eval runner is part of the agent developer's working repo (`evals/run_eval.py`, `evals/gold.jsonl`), not the published package. It calls the paid model, about 4 model steps and 15–20k input tokens per question.

## What we check: the gold set (56 questions)
| Category | n | What it tests | Why |
|---|---|---|---|
| `example` | 12 | The three original question types: where did battle X happen, who attacked X in year Y, what happened at (lat, lon) | The core use cases |
| `ambiguous` | 4 | Names that match several entities (two Battles of Guam; "Tara": Tarawa or Tarakan) | The agent must ask, not guess |
| `name_variant` | 8 | Case, accents, typos, romanizations ("TOKYO", "Tōkyō", "Gaudalcanal", "Chungking") | Users don't type names the way the graph stores them |
| `meridian` | 4 | Places on both sides of the 180° line (Kiska/Adak, Taveuni) | Naive longitude math breaks there |
| `date_filtered` | 4 | The same place in different years (Guam 1941 vs 1944) | Dates must narrow the answer |
| `not_in_graph` | 4 | Questions the graph can't answer (Stalingrad, Leyte Gulf, Paris coordinates) | The agent must say so instead of answering from general knowledge |
| `context_mismatch` | 2 | A year that contradicts the event ("Battle of Midway in 1944") | The agent should correct the premise |
| `set_queries` | 12 | "Show me all locations connected to X" (the business user's list) | Many results per question: correctness, and token use |
| `adversarial` | 6 | Off-topic, jailbreak, system-prompt extraction, an instruction planted in the data | Scope and misuse |

**What a gold row contains:**
- **the question and its category**
- **the expected behaviour:** `answer`, `ask` (a clarification question), `no_answer` (says the graph lacks it) or `refuse`
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
| **Current, all categories** (after graph contract v1 + UI interface) | **56** | **55** | correct resolution 94%, wrong entity accepted 0%, unnecessary asks 0% |

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
- **Small graph.** The eval runs on the 75-node sample graph, so the results show the agent's logic, not its quality on the team's 146k-node graph, whose data gaps (unnamed places, missing geometry) dominate there. A gold set for the full graph is the next step once the data issues are fixed.
- **One model.** Results hold for `z-ai/glm-5.2`; a different model needs its own baseline.
- **Scoring by phrases.** "Says not in the graph" and "refused" are recognised by phrase patterns. They were widened twice after misses and can still miss new wording.
- **Two judgment cases** still fail on a strict reading of the gold set. Both need a decision on what the right answer is:
  - **ctx-1, "What happened in the Battle of Midway in 1944?"** The agent correctly says the battle was in 1942, but lists the battle under context instead of answer.
  - **amb-2, "Who commanded the US forces in the Battle of Guam?"** The gold expects a clarification question ("which battle?"), but the graph has no commander for either battle, so "the graph doesn't say" is arguably the better reply.
- **Confidence thresholds aren't calibrated yet.** `high` / `medium` / `low` are starting values; show them as labels, never as probabilities.

## Running it
```bash
uv run python evals/run_eval.py                                 # all 56 questions (paid)
uv run python evals/run_eval.py --ids ex-who-1,amb-1 --repeat 3 # a subset, three times each
uv run python evals/run_eval.py --rescore evals/baseline.json   # rescore a saved report, no model calls
```
