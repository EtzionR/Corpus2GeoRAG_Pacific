"""AgentRunner: synchronous, thread-safe front end for the UI. Fake models, no network."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from geo_agent.schema import SCHEMA_VERSION
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import geo_agent.runner as runner_mod
from geo_agent.runner import AgentRunner

GRAPH = Path(__file__).resolve().parent.parent / "data" / "sample_graph.json"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    for var in ("GEO_AGENT_MOCK", "GEO_AGENT_RUN_TIMEOUT", "GEO_AGENT_TRACE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "logs"


def call(name, id, **args):
    return {"name": name, "args": args, "id": id}


def final(answer="done", ids=()):
    return AIMessage("", tool_calls=[call("AgentAnswer", "final", answer=answer, context_ids=[], answer_ids=list(ids),
                                          answer_sets=[], sources=[])])


class ScriptModel(FakeMessagesListChatModel):
    """Replays scripted AI messages, sleeping `delay` seconds (async) before each one."""

    delay: float = 0.0

    def bind_tools(self, tools, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


MIDWAY = [
    AIMessage("", tool_calls=[call("search_entities", "c1", query="Battle of Midway")]),
    AIMessage("", tool_calls=[call("get_locations", "c2", entity_id="event:battle_of_midway")]),
    final("At Midway Atoll.", ["loc:midway_atoll"]),
]


def read_events(log_dir):
    return [json.loads(line) for f in sorted(Path(log_dir).glob("trace-*.jsonl")) for line in f.read_text().splitlines()]


def test_ask_sync_from_plain_code_and_reuse():
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=MIDWAY)) as runner:
        for _ in range(2):  # the same loop and agent serve every question
            out = runner.ask_sync("Where was the Battle of Midway?", thread_id=runner.new_thread_id())
            assert out["error"] is None and out["answer_ids"] == ["loc:midway_atoll"]
        assert runner.loop.is_running() and runner._thread.daemon
    assert not runner._thread.is_alive() and runner.loop.is_closed()


def test_new_thread_ids_are_unique():
    assert len({AgentRunner.new_thread_id() for _ in range(50)}) == 50


def test_two_threads_run_concurrently():
    """Two conversations asked from two OS threads overlap on the one loop."""
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("ok")], delay=0.5)) as runner:
        results = {}

        def worker(name):
            results[name] = runner.ask_sync("q", thread_id=name)

        t0 = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert time.perf_counter() - t0 < 0.95  # sequential would take >= 1.0 s
        assert set(results) == {"a", "b"} and all(r["error"] is None for r in results.values())


def test_timeout_returns_error_result_and_traces_stage(isolated_env):
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("late")], delay=5)) as runner:
        t0 = time.perf_counter()
        out = runner.ask_sync("slow question", thread_id="t1", timeout=0.3)
        assert time.perf_counter() - t0 < 2
    assert out["error"]["stage"] == "timeout" and out["error"]["type"] == "RunTimeout"
    assert "0.3s" in out["error"]["message"] and out["answer"] == "" and out["geojson"]["features"] == []
    end = [e for e in read_events(isolated_env) if e["event"] == "run_end"][-1]
    assert end["run_id"] == out["run_id"] and end["failure_stage"] == "timeout"


def test_cancel_from_another_thread(isolated_env):
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("late")], delay=5)) as runner:
        pending = runner.submit("slow question", thread_id="t1")
        time.sleep(0.2)
        assert not pending.done()
        threading.Thread(target=runner.cancel, args=("t1",)).start()
        out = pending.result()
        assert out["error"]["stage"] == "cancelled" and pending.done()
        assert pending.cancel() is False  # already finished
    end = [e for e in read_events(isolated_env) if e["event"] == "run_end"][-1]
    assert end["failure_stage"] == "cancelled"


def test_thread_continues_on_a_fresh_conversation_after_a_stop():
    model = ScriptModel(responses=[final("late"), final("second")], delay=0.4)
    with AgentRunner(graph_path=GRAPH, llm=model) as runner:
        assert runner.ask_sync("q1", thread_id="t", timeout=0.1)["error"]["stage"] == "timeout"
        model.delay = 0
        out = runner.ask_sync("q2", thread_id="t")
        assert out["error"] is None
        assert runner._internal_thread("t").startswith("t~")


def test_closed_runner_rejects_questions():
    runner = AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final()]))
    runner.close()
    runner.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        runner.ask_sync("q")


def test_close_cancels_running_questions():
    runner = AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final()], delay=5))
    pending = runner.submit("q", thread_id="t")
    time.sleep(0.1)
    t0 = time.perf_counter()
    runner.close()
    assert time.perf_counter() - t0 < 3
    assert pending.result(0)["error"]["stage"] == "cancelled"


def test_runner_without_key_raises_config_error(monkeypatch):
    import geo_agent.agent as agent_mod
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(agent_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(agent_mod.AgentConfigError):
        AgentRunner(graph_path=GRAPH)
    assert not [t for t in threading.enumerate() if t.name == "geo-agent-loop" and t.is_alive()]


# Per-thread busy guard

def test_second_question_on_a_busy_thread_is_rejected():
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("first")], delay=0.5)) as runner:
        first = runner.submit("q", thread_id="t")
        time.sleep(0.05)
        assert runner.is_busy("t") and not runner.is_busy("other")
        t0 = time.perf_counter()
        dup = runner.ask_sync("q", thread_id="t")
        assert time.perf_counter() - t0 < 0.2  # returned at once
        assert dup["error"]["stage"] == "busy" and dup["error"]["type"] == "ThreadBusy" and dup["run_id"] != first.run_id
        assert first.result()["error"] is None  # the running question is unaffected
        assert not runner.is_busy("t")
        assert runner.ask_sync("q", thread_id="t")["error"] is None  # free again


def test_wait_policy_runs_after_the_first_finishes():
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("x")], delay=0.3)) as runner:
        first = runner.submit("q1", thread_id="t")
        time.sleep(0.05)
        second = runner.ask_sync("q2", thread_id="t", if_busy="wait")
        assert first.done() and second["error"] is None
        # the wait counts toward the timeout: too short a wait gives `busy`
        slow = runner.submit("q3", thread_id="t")
        time.sleep(0.05)
        assert runner.ask_sync("q4", thread_id="t", if_busy="wait", timeout=0.1)["error"]["stage"] == "busy"
        slow.result()


def test_busy_guard_is_per_thread_under_contention():
    """Eight OS threads double-submit on one conversation: exactly one runs."""
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final("x")], delay=0.3)) as runner:
        results, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(runner.ask_sync("q", thread_id="same"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stages = sorted((r["error"] or {}).get("stage", "ok") for r in results)
        assert stages == ["busy"] * 7 + ["ok"]


def test_get_runner_is_process_wide_and_thread_safe():
    from geo_agent.runner import get_runner
    llm = ScriptModel(responses=[final()])
    got = []
    threads = [threading.Thread(target=lambda: got.append(get_runner(GRAPH, llm=llm))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(r) for r in got}) == 1
    got[0].close()
    fresh = get_runner(GRAPH, llm=llm)  # a closed runner is replaced
    assert fresh is not got[0] and fresh.ask_sync("q")["error"] is None
    fresh.close()


def test_health_and_stats():
    graph = json.loads(GRAPH.read_text())
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final()], delay=0.3)) as runner:
        h = runner.health()
        assert h["ok"] and h["graph_loaded"] and h["model_configured"] and not h["mock"]
        assert h["model"] == "ScriptModel" and h["schema_version"] == SCHEMA_VERSION and h["version"] == "0.1.0"
        pending = runner.submit("q", thread_id="t")
        time.sleep(0.05)
        st = runner.stats()
        assert (st["nodes"], st["edges"]) == (len(graph["nodes"]), len(graph["edges"]))
        assert st["geocoded"] == sum(1 for n in graph["nodes"] if n.get("geometry"))
        assert st["node_types"]["location"] > 0 and st["running_questions"] == 1 and len(st["graph_sha256"]) == 12
        pending.result()
        assert runner.stats()["running_questions"] == 0
    assert runner.health()["ok"] is False


# Progress events (for st.status)

def test_progress_events_from_the_loop_thread():
    seen, threads = [], set()

    def on_progress(ev):
        threads.add(threading.current_thread().name)
        seen.append(ev)

    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=MIDWAY)) as runner:
        pending = runner.submit("Where was the Battle of Midway?", thread_id="t", on_progress=on_progress)
        out = pending.result()
    assert out["error"] is None and threads == {"geo-agent-loop"}
    kinds = [e["event"] for e in seen]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and seen[-1]["outcome"] == "ok"
    tools = [(e["event"], e["name"]) for e in seen if e["event"].startswith("tool_")]
    assert tools == [("tool_start", "search_entities"), ("tool_end", "search_entities"),
                     ("tool_start", "get_locations"), ("tool_end", "get_locations")]
    assert kinds.count("model_start") == kinds.count("model_end") == 3
    first_tool = next(e for e in seen if e["event"] == "tool_start")
    assert first_tool["label"] == "Looking up names in the graph" and first_tool["run_id"] == out["run_id"]
    assert next(e for e in seen if e["event"] == "model_end")["tools_requested"] == ["search_entities"]
    assert pending.progress == seen and pending.poll_progress() == seen and pending.poll_progress() == []


def test_failing_progress_callback_does_not_break_the_run():
    def boom(ev):
        raise RuntimeError("UI bug")

    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=MIDWAY)) as runner:
        out = runner.ask_sync("q", on_progress=boom)
    assert out["error"] is None and out["answer_ids"] == ["loc:midway_atoll"]


def test_deadline_is_enforced_while_the_ui_polls():
    """A UI loop that polls done()/poll_progress() and never calls result() still gets the timeout."""
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final()], delay=5)) as runner:
        pending = runner.submit("q", thread_id="t", timeout=0.3)
        t0 = time.perf_counter()
        while not pending.done() and time.perf_counter() - t0 < 3:
            pending.poll_progress()
            time.sleep(0.05)
        assert pending.done() and time.perf_counter() - t0 < 1.5
        assert pending.result()["error"]["stage"] == "timeout"
        assert [e["event"] for e in pending.progress][-1] == "run_end"


# Entity lookup for map clicks

def test_get_entity_feature():
    with AgentRunner(graph_path=GRAPH, llm=ScriptModel(responses=[final()])) as runner:
        loc = runner.get_entity_feature("loc:midway_atoll")
        assert (loc["name"], loc["type"]) == ("Midway Atoll", "location") and "Midway" in loc["aliases"]
        assert loc["feature"]["geometry"] == {"type": "Point", "coordinates": [-177.37, 28.21]}
        assert loc["feature"]["properties"]["id"] == "loc:midway_atoll" and loc["geojson"]["features"] == [loc["feature"]]
        assert loc["sources"][0]["page"] == "Midway Atoll" and set(loc["sources"][0]) <= {"page", "url", "paragraph"}
        assert loc["excerpt"] and len(loc["excerpt"]) <= 301

        event = runner.get_entity_feature("event:battle_of_midway")
        assert event["feature"] is None and event["attributes"]["start_date"].startswith("1942")
        places = {f["properties"]["id"]: f["properties"].get("related_to") for f in event["geojson"]["features"]}
        assert places and set(places.values()) == {"event:battle_of_midway"}

        assert runner.get_entity_feature("loc:atlantis") is None
        # instruction-like sentences in the source text never reach the excerpt (spec R32)
        nauru = runner.get_entity_feature("loc:nauru")
        assert "[instruction-like text removed]" in nauru["excerpt"]
        assert "ignore all previous instructions" not in nauru["excerpt"].lower()


def test_excerpt_cuts_at_a_word():
    from geo_agent.runner import excerpt
    assert excerpt("short text") == "short text"
    assert excerpt("first para.\n\nsecond") == "first para."
    cut = excerpt("word " * 100, 22)
    assert cut.endswith("…") and len(cut) <= 23 and not cut[:-1].endswith(" ")
