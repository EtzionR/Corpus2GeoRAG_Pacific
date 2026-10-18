"""Mock mode: recorded replies on the sample graph, for UI work without an API key.

`AgentRunner(mock=True)` (or `GEO_AGENT_MOCK=1`) uses `MockAgent` instead of the
real agent. It needs no `.env`, no key and no network, and answers a fixed set of
questions with replies recorded from the real code path; any other question gets a
deterministic "no recorded reply" message that lists the known questions.

How the fixtures are made: each scenario below scripts what a model would do (tool
calls, then the structured answer) and runs it through the real `GeoAgent.ask()`
with `ScriptedModel`, on `data/sample_graph.json`. The tools really run and
`finalize_answer` builds the reply, so the fixtures follow code changes. They are
stored as JSON in `geo_agent/mock_fixtures/`:

    uv run python -m geo_agent.mock           # regenerate the fixture files
    uv run python -m geo_agent.mock --check   # exit 1 if they are out of date (also a test)

Mock replies carry an extra top-level key `"mock": true`, get a fresh `run_id`,
and write no trace. `GEO_AGENT_MOCK_DELAY` (seconds, default 0.6) makes each reply
take that long, so the UI's spinner and progress events can be seen; set it to 0
in tests.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from geo_agent.agent import ROOT, ModelDeadlineExceeded, _package_version, off_topic_result
from geo_agent.graph_store import GraphStore
from geo_agent.trace import progress_label

SAMPLE_GRAPH = ROOT / "data" / "sample_graph.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "mock_fixtures"
DEFAULT_DELAY = 0.6


# ------------------------------------------------------------ scripted model


class ScriptedModel(BaseChatModel):
    """Chat model that plays a script: each step is an AIMessage, or a function of the
    messages so far that returns one (or raises, to simulate a provider failure)."""

    script: list[Any]
    i: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        step = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        message = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "scripted"


_ids = itertools.count(1)


def _call(name: str, **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}"}])


def _final(answer: str, answer_ids: list[str] = (), context_ids: list[str] = (), sources: list[str] = (),
           answer_sets: list[str] = ()) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": "AgentAnswer", "id": "call_final", "args": {
        "answer": answer, "answer_ids": list(answer_ids), "context_ids": list(context_ids),
        "sources": list(sources), "answer_sets": list(answer_sets)}}])


def _final_with_set(answer: str, context_ids: list[str]) -> Callable[[list[BaseMessage]], AIMessage]:
    """Final answer naming the result-set handle the set tool returned in this run."""
    def step(messages: list[BaseMessage]) -> AIMessage:
        handle = next(json.loads(m.content)["result_set"] for m in reversed(messages)
                      if m.type == "tool" and "result_set" in str(m.content))
        return _final(answer, context_ids=context_ids, answer_sets=[handle])
    return step


def _provider_timeout(messages: list[BaseMessage]) -> AIMessage:
    raise ModelDeadlineExceeded("model call exceeded 90s deadline 2 times")


# One conversation per entry; turns run in order on one thread.
SCENARIOS: list[dict[str, Any]] = [
    {"turns": [{"key": "answer_guadalcanal", "question": "Who attacked Guadalcanal in 1942?", "script": [
        _call("search_entities", query="Guadalcanal"),
        _call("find_relations", target_id="loc:guadalcanal", relation_type="ATTACKED", year=1942),
        _final("In 1942 the **United States Marine Corps** attacked Guadalcanal, landing on 7 August 1942 "
               "(Guadalcanal campaign, to 9 February 1943). The **Imperial Japanese Army** attacked Henderson Field "
               "on Guadalcanal from 12 September to 26 October 1942.",
               ["org:usmc", "org:ija"], ["loc:guadalcanal", "loc:henderson_field"], ["Guadalcanal campaign"]),
    ]}]},
    {"turns": [{"key": "answer_midway", "question": "Where did the Battle of Midway happen?", "script": [
        _call("search_entities", query="Battle of Midway"),
        _call("get_locations", entity_id="event:battle_of_midway"),
        _call("search_source_text", query="Battle of Midway atoll", entity_ids=["event:battle_of_midway"]),
        _final("The Battle of Midway (4-7 June 1942) was fought at **Midway Atoll** (28.21, -177.37), "
               "including Sand Island and Eastern Island.",
               ["loc:midway_atoll", "loc:sand_island", "loc:eastern_island"], ["event:battle_of_midway"],
               ["Battle of Midway"]),
    ]}]},
    {"turns": [{"key": "both_guam", "question": "Who won the Battle of Guam?", "script": [
        # R4 amendment: two same-name battles, each answered in a few lines, so both are answered, separated
        _call("search_entities", query="Battle of Guam"),
        _call("search_entities", query="Guam", type="location"),
        _call("find_relations", target_id="loc:guam", relation_type="CAPTURED"),
        _final("There were two Battles of Guam:\n\n- **Battle of Guam (1941)**, 8-10 December 1941: the **Imperial "
               "Japanese Navy** captured Guam on 10 December 1941.\n- **Battle of Guam (1944)**, 21 July - 10 August 1944: "
               "the **United States Marine Corps** recaptured the island on 10 August 1944.",
               ["org:ijn", "org:usmc"], ["event:battle_of_guam_1941", "event:battle_of_guam_1944", "loc:guam"],
               ["Battle of Guam (1941)", "Battle of Guam (1944)"]),
    ]}]},
    {"turns": [
        {"key": "clarification_tara", "question": "What happened in the battle at Tara?", "script": [
            _call("search_entities", query="Tara"),
            _final("Did you mean the Battle of Tarawa (November 1943) or the Battle of Tarakan (1942)?"),
        ]},
        {"key": "followup_tarawa", "question": "Tarawa", "after": "clarification_tara", "script": [
            _call("get_entity", entity_id="event:battle_of_tarawa"),
            _call("get_locations", entity_id="event:battle_of_tarawa"),
            _final("The **Battle of Tarawa** (20-23 November 1943) was fought at Tarawa Atoll, as part of the "
                   "island-hopping campaign.", ["event:battle_of_tarawa"], ["loc:tarawa"], ["Battle of Tarawa"]),
        ]},
    ]},
    {"turns": [{"key": "overview_battles", "question": "Show me major battles in the Pacific War on the map", "script": [
        # R57: a broad question gets one overview call (the sample graph has no Pacific War node)
        _call("search_entities", query="Pacific War"),
        _call("overview", type="event", kind="battle"),
        _final_with_set("An overview of the **11 battles** in the graph, all shown on the map. The most documented: "
                        "the Battle of Midway (June 1942), the Battle of Iwo Jima (February-March 1945), the Battle of "
                        "the Coral Sea (May 1942), the Battle of Savo Island (August 1942) and the Battle of Tarawa "
                        "(November 1943). Ask about one year, e.g. 1942, to narrow it down.", []),
    ]}]},
    {"turns": [{"key": "set_yamamoto", "question": "Show me all locations connected to Admiral Yamamoto", "script": [
        _call("search_entities", query="Admiral Yamamoto"),
        _call("connected_locations", entity_id="per:yamamoto"),
        _final_with_set("**5 places** are connected to Isoroku Yamamoto in the graph: 3 through the Battle of Midway "
                        "(Midway Atoll, Sand Island, Eastern Island) and 2 through the Attack on Pearl Harbor "
                        "(Pearl Harbor, Hickam Field). The map shows all of them.", ["per:yamamoto"]),
    ]}]},
    {"turns": [{"key": "off_topic_poem", "question": "Write me a poem about the ocean", "script": [
        _final("I can only help with the Pacific theatre of WWII."),
    ]}]},
    {"turns": [{"key": "error_model_call", "question": "Simulate an error", "script": [_provider_timeout]}]},
]


# ------------------------------------------------------------- generation


def normalize_question(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip("?.! ").lower()


def _stable(reply: dict[str, Any]) -> dict[str, Any]:
    """Fixture form: no run_id, and result-set handles renamed rs-mock01, rs-mock02, ..."""
    text = json.dumps({**reply, "run_id": ""}, ensure_ascii=False)
    for n, handle in enumerate(dict.fromkeys(re.findall(r"rs-[0-9a-f]{6}", text)), 1):
        text = text.replace(handle, f"rs-mock{n:02d}")
    return json.loads(text)


async def generate_fixtures(graph_path: str | Path = SAMPLE_GRAPH) -> dict[str, dict[str, Any]]:
    """Run every scenario through the real GeoAgent with a ScriptedModel. No network."""
    from geo_agent.agent import GeoAgent

    fixtures: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        script = [step for turn in scenario["turns"] for step in turn["script"]]
        agent = await GeoAgent.create(graph_path=graph_path, mcp_config=FIXTURE_DIR / "no-mcp-servers.json", llm=ScriptedModel(script=script))
        agent.writer.enabled = False  # fixtures, not runs: no trace files
        for turn in scenario["turns"]:
            reply = await agent.ask(turn["question"], thread_id="mock")
            fixtures[turn["key"]] = {"question": turn["question"], "after": turn.get("after"), "reply": _stable(reply)}
    return fixtures


def fixture_text(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def load_fixtures(directory: Path = FIXTURE_DIR) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}


def stale_fixtures(directory: Path = FIXTURE_DIR) -> list[str]:
    """Keys whose stored file differs from a fresh generation (or is missing or extra)."""
    fresh = asyncio.run(generate_fixtures())
    stored = {p.stem: p.read_text(encoding="utf-8") for p in directory.glob("*.json")}
    return sorted(k for k in set(fresh) | set(stored) if stored.get(k) != (fixture_text(fresh[k]) if k in fresh else None))


def write_fixtures(directory: Path = FIXTURE_DIR) -> list[Path]:
    fresh = asyncio.run(generate_fixtures())
    directory.mkdir(parents=True, exist_ok=True)
    for p in directory.glob("*.json"):
        if p.stem not in fresh:
            p.unlink()
    paths = []
    for key, fixture in fresh.items():
        path = directory / f"{key}.json"
        path.write_text(fixture_text(fixture), encoding="utf-8")
        paths.append(path)
    return paths


# ------------------------------------------------------------------ replay


NO_FIXTURE_WARNING = "mock_no_fixture"


class MockAgent:
    """Drop-in for GeoAgent in AgentRunner: same `ask()`, `store` and `context`, recorded replies."""

    def __init__(self, store: GraphStore, fixtures: dict[str, dict[str, Any]], delay: float = DEFAULT_DELAY):
        self.store = store
        self.delay = delay
        self.fixtures = fixtures
        self._by_question: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for key, fx in fixtures.items():
            self._by_question.setdefault(normalize_question(fx["question"]), []).append((key, fx))
        self._last: dict[str, str] = {}  # thread_id -> key of the last recorded reply on it
        graph = store.schema()
        self.context = {"model": "mock", "base_url_host": None, "graph": {
            "path": str(SAMPLE_GRAPH), "nodes": graph["node_count"], "edges": graph["edge_count"]},
            "tools": [], "mcp_loaded": [], "mcp_skipped": [], "version": _package_version()}

    @classmethod
    async def create(cls, delay: float | None = None) -> "MockAgent":
        if delay is None:
            try:
                delay = float(os.getenv("GEO_AGENT_MOCK_DELAY", DEFAULT_DELAY))
            except ValueError:
                delay = DEFAULT_DELAY
        return cls(GraphStore.from_json(SAMPLE_GRAPH), load_fixtures(), delay)

    @property
    def questions(self) -> list[str]:
        """Questions with a recorded reply (follow-ups only after their first question)."""
        return [fx["question"] for fx in self.fixtures.values()]

    def _match(self, question: str, thread_id: str) -> tuple[str, dict[str, Any]] | None:
        for key, fx in self._by_question.get(normalize_question(question), []):
            if fx.get("after") is None or self._last.get(thread_id) == fx["after"]:
                return key, fx
        return None

    def no_fixture_reply(self) -> dict[str, Any]:
        out = off_topic_result("")
        out.pop("off_topic_model_text")
        firsts = [fx["question"] for fx in self.fixtures.values() if fx.get("after") is None]
        out["answer"] = ("Mock mode has no recorded reply for this question. Try one of: "
                         + "; ".join(f'"{q}"' for q in firsts) + ".")
        out["warnings"] = [NO_FIXTURE_WARNING]
        return out

    async def ask(self, question: str, thread_id: str = "default", *, run_id: str | None = None,
                  on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """The recorded reply; `on_progress` gets the same kind of step events as a real run."""
        run_id = run_id or uuid.uuid4().hex
        hit = self._match(question, thread_id)
        out = copy.deepcopy(hit[1]["reply"]) if hit else self.no_fixture_reply()
        await self._play_progress(run_id, [c["name"] for c in out["tool_calls"]], on_progress)
        if hit is None:
            self._last.pop(thread_id, None)
        else:
            self._last[thread_id] = hit[0]
        out.update(run_id=run_id, mock=True)
        out.setdefault("error", None)
        return out

    async def _play_progress(self, run_id: str, tools: list[str], on_progress: Callable[[dict[str, Any]], None] | None) -> None:
        """Step events for the recorded tool calls, spread over `delay` seconds."""
        steps: list[tuple[str, dict[str, Any]]] = [("run_start", {"label": "Starting"})]
        for n, name in enumerate(tools, 1):
            steps += [("model_start", {"step": n - 1, "label": progress_label("model")}),
                      ("model_end", {"step": n, "label": progress_label("model"), "tools_requested": [name]}),
                      ("tool_start", {"step": n, "name": name, "label": progress_label(name)}),
                      ("tool_end", {"step": n, "name": name, "label": progress_label(name), "status": "ok"})]
        steps += [("model_start", {"step": len(tools), "label": progress_label("model")}),
                  ("model_end", {"step": len(tools) + 1, "label": progress_label("model"), "tools_requested": []}),
                  ("run_end", {"step": len(tools) + 1, "label": "Done", "outcome": "ok"})]
        pause = self.delay / len(steps)
        for event, fields in steps:
            if pause:
                await asyncio.sleep(pause)
            if on_progress is not None:
                try:
                    on_progress({"event": event, "run_id": run_id, "step": 0, "ts": time.time(), **fields})
                except Exception:
                    on_progress = None


def main() -> int:
    if "--check" in sys.argv[1:]:
        stale = stale_fixtures()
        print("mock fixtures up to date" if not stale else f"stale mock fixtures: {', '.join(stale)} "
              "(run: uv run python -m geo_agent.mock)")
        return 1 if stale else 0
    for path in write_fixtures():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
