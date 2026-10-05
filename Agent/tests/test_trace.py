"""Tracing tests (spec R12-R16): fake chat model + real tools on the fixture. No network."""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import geo_agent.agent as agent_mod
from geo_agent.agent import GeoAgent
from geo_agent.graph_store import GraphStore
from geo_agent.trace import diagnostics, result_status, trace_warnings

GRAPH = Path(__file__).resolve().parent.parent / "data" / "sample_graph.json"
SECRET = "sk-or-v1-THIS-IS-A-FAKE-KEY-0123456789"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Traces go to a temp dir, and a fake key is set so .env is never used for it."""
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    for var in ("GEO_AGENT_TRACE", "GEO_AGENT_MAX_STEPS", "GEO_AGENT_WARN_STEPS", "GEO_AGENT_LOG_QUESTIONS", "GEO_AGENT_TRACE_FULL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "logs"


class FakeToolModel(FakeMessagesListChatModel):
    """Replays scripted AI messages; tool binding is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self


class FailingModel(FakeToolModel):
    def _generate(self, *args, **kwargs):
        raise TimeoutError("provider timed out")


def call(name, id, **args):
    return {"name": name, "args": args, "id": id}


def ai(*calls, usage=None):
    return AIMessage("", tool_calls=list(calls), usage_metadata=usage)


def final(answer="done", ids=()):
    return ai(call("AgentAnswer", "final", answer=answer, entity_ids=list(ids), sources=[]))


def ask(responses, question="q", model_cls=FakeToolModel):
    async def go():
        agent = await GeoAgent.create(graph_path=GRAPH, llm=model_cls(responses=responses))
        return await agent.ask(question)
    return asyncio.run(go())


def read_events(log_dir):
    return [json.loads(line) for f in sorted(Path(log_dir).glob("trace-*.jsonl")) for line in f.read_text().splitlines()]


USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
MIDWAY_RUN = [
    ai(call("search_entities", "c1", query="Battle of Midway"), usage=USAGE),
    ai(call("get_locations", "c2", entity_id="event:battle_of_midway")),
    final("Midway Atoll", ["loc:midway_atoll"]),
]


# R12: run identity and context


def test_run_start_context(isolated_env):
    out = ask([final()], question="Where is Midway?")
    start = read_events(isolated_env)[0]
    assert start["event"] == "run_start" and start["run_id"] == out["run_id"]
    assert start["question"] == "Where is Midway?" and start["thread_id"] == "default"
    assert start["model"] == "FakeToolModel" and start["base_url_host"] == "openrouter.ai"
    assert len(start["system_prompt_hash"]) == 12
    assert start["graph"]["nodes"] == 50 and start["graph"]["edges"] == 84 and len(start["graph"]["sha256"]) == 12
    assert "search_entities" in start["tools"] and start["mcp_loaded"] == [] and start["mcp_skipped"] == []
    assert start["step_limit"] == 25 and start["version"] == "0.1.0"


def test_question_can_be_omitted(isolated_env, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_LOG_QUESTIONS", "0")
    ask([final()], question="private question")
    assert "private question" not in (isolated_env / next(isolated_env.iterdir()).name).read_text()


def test_api_key_never_logged(isolated_env, caplog):
    caplog.set_level(logging.DEBUG)
    ask(MIDWAY_RUN)
    ask([final()], model_cls=FailingModel)
    text = "".join(f.read_text() for f in isolated_env.glob("*.jsonl"))
    assert SECRET not in text and SECRET not in caplog.text


# R13: structured trace


def test_event_order_end_to_end(isolated_env):
    out = ask(MIDWAY_RUN)
    events = read_events(isolated_env)
    assert [e["event"] for e in events] == [
        "run_start", "model_end", "tool_end", "model_end", "tool_end", "model_end", "structured_output", "run_end",
    ]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["run_id"] for e in events} == {out["run_id"]}

    m1, t1, _, t2 = events[1:5]
    assert (m1["step"], m1["input_tokens"], m1["output_tokens"]) == (1, 10, 5)
    assert m1["tool_calls_requested"] == [{"name": "search_entities", "args": {"query": "Battle of Midway"}, "id": "c1"}]
    assert (t1["name"], t1["args"], t1["status"], t1["step"]) == ("search_entities", {"query": "Battle of Midway"}, "ok", 1)
    assert (t2["name"], t2["status"]) == ("get_locations", "ok")
    assert len(t2["result_sha256"]) == 64 and t2["result_chars"] >= len(t2["result_preview"])

    end = events[-1]
    assert end["outcome"] == "ok" and end["failure_stage"] is None and end["warnings"] == []
    assert out["entity_ids"] == ["loc:midway_atoll"] and out["error"] is None and out["warnings"] == []


def test_tool_error_is_traced_and_run_ends(isolated_env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("spatial index broke")
    monkeypatch.setattr(GraphStore, "near", boom)
    out = ask([ai(call("entities_near", "c1", lat=28.2, lon=-177.4, radius_km=20)), final()])
    events = read_events(isolated_env)
    err = next(e for e in events if e["event"] == "tool_error")
    assert (err["name"], err["error_type"], err["message"]) == ("entities_near", "RuntimeError", "spatial index broke")
    assert "Traceback" in err["traceback"]
    assert events[-1]["event"] == "run_end" and events[-1]["failure_stage"] == "tool_call"
    assert out["error"]["stage"] == "tool_call" and out["tool_calls"][0]["name"] == "entities_near"


# R14: failure stages and warning codes


def loop_model():
    return [ai(call("graph_schema", f"c{i}")) for i in range(20)]


@pytest.mark.parametrize("stage", ["model_call", "tool_call", "step_limit", "structured_output", "post_processing"])
def test_failure_stages(isolated_env, monkeypatch, stage):
    responses, model_cls = [final()], FakeToolModel
    if stage == "model_call":
        model_cls = FailingModel
    elif stage == "tool_call":
        monkeypatch.setattr(GraphStore, "schema", lambda self: 1 / 0)
        responses = [ai(call("graph_schema", "c1")), final()]
    elif stage == "step_limit":
        monkeypatch.setenv("GEO_AGENT_MAX_STEPS", "4")
        responses = loop_model()
    elif stage == "structured_output":
        responses = [AIMessage("plain text answer")]
    elif stage == "post_processing":
        monkeypatch.setattr(agent_mod, "finalize_answer", lambda *a: 1 / 0)

    out = ask(responses, model_cls=model_cls)
    end = read_events(isolated_env)[-1]
    assert end["event"] == "run_end" and end["failure_stage"] == stage
    if stage == "structured_output":
        assert end["outcome"] == "ok_with_warnings" and out["error"] is None
        assert out["warnings"] == ["structured_output_failed"] and out["answer"] == "plain text answer"
    else:
        assert end["outcome"] == "error" and out["error"]["stage"] == stage and out["answer"] == ""


def test_unknown_stage(isolated_env):
    class Stub:
        async def ainvoke(self, *args, **kwargs):
            raise ValueError("something odd")
    out = asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), Stub()).ask("q"))
    assert out["error"] == {"type": "ValueError", "message": "something odd", "stage": "unknown"}
    assert read_events(isolated_env)[-1]["failure_stage"] == "unknown"


@pytest.mark.parametrize(
    "code,responses,env",
    [
        ("repeated_tool_call", [ai(call("search_entities", "a", query="Midway")), ai(call("search_entities", "b", query="Midway")), final()], {}),
        ("empty_resolution", [ai(call("search_entities", "a", query="Leyte Gulf")), final()], {}),
        ("many_steps", [ai(call("graph_schema", "a")), ai(call("graph_schema", "b")), final()], {"GEO_AGENT_WARN_STEPS": "2"}),
    ],
)
def test_warning_codes(isolated_env, monkeypatch, code, responses, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = ask(responses)
    assert code in out["warnings"]
    end = read_events(isolated_env)[-1]
    assert code in end["warnings"] and end["outcome"] == "ok_with_warnings" and end["failure_stage"] is None


# R15: diagnostics (pure function over stub traces)


def ev(event, **fields):
    return {"event": event, **fields}


def test_diagnostics_from_stub_trace():
    events = [
        ev("model_end", latency_ms=100, input_tokens=50, output_tokens=7),
        ev("tool_end", name="find_relations", args={"target_id": "loc:x"}, latency_ms=3, status="empty"),
        ev("model_end", latency_ms=80, input_tokens=60, output_tokens=9),
        ev("tool_end", name="search_entities", args={"query": "X"}, latency_ms=2, status="ok"),
        ev("tool_end", name="search_entities", args={"query": "X"}, latency_ms=2, status="ok"),
        ev("model_end", latency_ms=50, input_tokens=None, output_tokens=None),
    ]
    d = diagnostics(events)
    assert d == {
        "steps": 3,
        "tool_sequence": ["find_relations", "search_entities", "search_entities"],
        "tool_counts": {"find_relations": 1, "search_entities": 2},
        "empty_results": 1,
        "repeated_calls": 1,
        "resolved_before_query": False,  # queried relations before resolving a name
        "model_ms": 230,
        "tools_ms": 7,
        "input_tokens": 110,
        "output_tokens": 16,
    }
    assert trace_warnings(events, warn_steps=10) == ["repeated_tool_call"]
    assert diagnostics([ev("model_end", latency_ms=1, input_tokens=1, output_tokens=1)])["resolved_before_query"] is None


def test_result_status():
    assert result_status('{"result": "no matching entity in graph"}') == "empty"
    assert result_status("[]") == "empty"
    assert result_status('{"error": "unknown entity_id x"}') == "error"
    assert result_status('[{"id": "loc:x"}]') == "ok"
    assert result_status("plain text from an MCP tool") == "ok"


# R16: console logging and switches


def test_unwritable_log_dir_does_not_fail_run(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(blocker / "logs"))
    out = ask(MIDWAY_RUN)
    assert out["error"] is None and out["entity_ids"] == ["loc:midway_atoll"]
    failures = [r for r in caplog.records if "trace write failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].levelno == logging.WARNING


def test_trace_can_be_turned_off(isolated_env, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_TRACE", "0")
    ask([final()])
    assert not isolated_env.exists()


def test_info_logs_steps_and_default_is_quiet(caplog):
    caplog.set_level(logging.INFO, logger="geo_agent")
    out = ask(MIDWAY_RUN)
    lines = [r.getMessage() for r in caplog.records]
    assert any("step 1 model" in l for l in lines) and any("tool search_entities ok" in l for l in lines)
    assert all(l.startswith(out["run_id"][:8]) for l in lines)

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="geo_agent")
    ask(MIDWAY_RUN)
    assert [r for r in caplog.records if r.name == "geo_agent"] == []


# R17-R19: viewer, replay, report

from datetime import datetime, timedelta, timezone

from geo_agent.tools import build_tools
from geo_agent.trace import find_run, load_events, parse_since, render_timeline, replay, summarize


def test_timeline_of_ok_and_failing_runs(isolated_env, monkeypatch):
    ask(MIDWAY_RUN, question="Where was the Battle of Midway?")
    ok_text = render_timeline(find_run(load_events(isolated_env), "last"))
    for expected in ("Question: Where was the Battle of Midway?", "Model: FakeToolModel", "Outcome: ok",
                     "Step 1", 'tool search_entities(query="Battle of Midway")  ok', "Answer: Midway Atoll"):
        assert expected in ok_text, expected

    monkeypatch.setattr(GraphStore, "near", lambda *a, **k: 1 / 0)
    out = ask([ai(call("search_entities", "a", query="Midway")),
               ai(call("entities_near", "b", lat=28.2, lon=-177.4, radius_km=20)), final()])
    fail_text = render_timeline(find_run(load_events(isolated_env), out["run_id"][:8]))
    assert "failure stage: tool_call" in fail_text
    assert "RAISED ZeroDivisionError" in fail_text
    assert "Last tool call: entities_near(lat=28.2, lon=-177.4, radius_km=20)" in fail_text


def test_find_run_not_found(isolated_env):
    ask([final()])
    events = load_events(isolated_env)
    assert find_run(events, "doesnotexist") is None and find_run([], "last") is None
    assert agent_mod.cli(["--trace", "doesnotexist"]) == 1
    assert agent_mod.cli(["--trace"]) == 0


def test_replay_matches_then_detects_graph_change(isolated_env, tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text(GRAPH.read_text())

    async def go():
        a = await GeoAgent.create(graph_path=graph, llm=FakeToolModel(responses=MIDWAY_RUN))
        return await a.ask("q")
    asyncio.run(go())
    run = find_run(load_events(isolated_env), "last")

    rows = replay(run, {t.name: t for t in build_tools(GraphStore.from_json(graph))})
    assert [r["status"] for r in rows] == ["match", "match"]

    # Edit the battle's OCCURRED_AT date: get_locations shows it, search_entities does not.
    data = json.loads(graph.read_text())
    edge = next(e for e in data["edges"] if e["source"] == "event:battle_of_midway" and e["type"] == "OCCURRED_AT")
    edge["end_date"] = "1942-06-08"
    graph.write_text(json.dumps(data))
    rows = replay(run, {t.name: t for t in build_tools(GraphStore.from_json(graph))})
    assert [(r["name"], r["status"]) for r in rows] == [("search_entities", "match"), ("get_locations", "mismatch")]
    assert rows[1]["detail"].startswith("result changed")


def run_events(rid, start, latency, outcome="ok", stage=None, warnings=(), tools=(), tokens=(0, 0)):
    """Stub run: run_start, tool events (name, status), run_end."""
    evs = [{"run_id": rid, "seq": 1, "ts": start.isoformat(), "event": "run_start"}]
    for i, (name, status) in enumerate(tools, 2):
        evs.append({"run_id": rid, "seq": i, "ts": start.isoformat(), "event": "tool_end", "name": name, "status": status})
    evs.append({"run_id": rid, "seq": 99, "ts": start.isoformat(), "event": "run_end", "outcome": outcome,
                "failure_stage": stage, "latency_ms": latency, "warnings": list(warnings),
                "diagnostics": {"steps": len(tools) + 1, "tool_sequence": [t for t, _ in tools],
                                "input_tokens": tokens[0], "output_tokens": tokens[1]}})
    return evs


def test_report_numbers():
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    events = (
        run_events("a", now - timedelta(hours=1), 1000, tools=[("search_entities", "ok"), ("find_relations", "empty")], tokens=(100, 10))
        + run_events("b", now - timedelta(hours=2), 3000, "ok_with_warnings", warnings=["empty_resolution"],
                     tools=[("search_entities", "empty")], tokens=(50, 5))
        + run_events("c", now - timedelta(hours=3), 2000, "error", "model_call", tokens=(20, 0))
        + run_events("old", now - timedelta(days=30), 9000, "error", "step_limit")
        + [{"run_id": "crashed", "seq": 1, "ts": (now - timedelta(minutes=5)).isoformat(), "event": "run_start"}]
    )
    r = summarize(events, since=parse_since("7d"), now=now)
    assert r["runs"] == 4 and r["unfinished"] == 1
    assert r["outcomes"] == {"ok": 1, "ok_with_warnings": 1, "error": 1}
    assert r["failures_by_stage"] == {"model_call": 1}
    assert r["warnings"] == {"empty_resolution": 1}
    assert (r["latency_ms_p50"], r["latency_ms_p95"]) == (2000, 3000)
    assert (r["mean_steps"], r["mean_tool_calls"]) == (2.0, 1.0)
    assert r["tool_usage"] == {"search_entities": 2, "find_relations": 1}
    assert r["empty_share"] == {"search_entities": 0.5, "find_relations": 1.0}
    assert (r["input_tokens"], r["output_tokens"]) == (170, 15)
    assert summarize(events, now=now)["failures_by_stage"] == {"model_call": 1, "step_limit": 1}
    with pytest.raises(ValueError):
        parse_since("7 days")


def test_report_cli(isolated_env, capsys):
    ask(MIDWAY_RUN)
    assert agent_mod.cli(["--report", "--since", "1d"]) == 0
    out = capsys.readouterr().out
    assert "Runs: 1" in out and "search_entities" in out


# R20: failure attribution (pure function of question record + trace events)

from geo_agent.trace import attribute_failure, resolved_ids


def stub_trace(*, outcome="ok", searches=(), tools=(), answer="Answer.", ids=(), dropped=None):
    """Minimal trace: search_entities results, other tool results, final answer."""
    evs, seq = [], 0

    def add(event, **f):
        nonlocal seq
        seq += 1
        evs.append({"run_id": "r", "seq": seq, "event": event, **f})

    for query, hit_ids in searches:
        add("tool_end", name="search_entities", args={"query": query}, status="ok" if hit_ids else "empty",
            result_preview=json.dumps([{"id": i} for i in hit_ids]))
    for name, args, result_ids in tools:
        add("tool_end", name=name, args=args, status="ok", result_preview=json.dumps([{"id": i} for i in result_ids]))
    add("structured_output", ok=True, answer={"answer": answer, "entity_ids": list(ids), "sources": []})
    if dropped is not None:
        add("finalize", kept_ids=[i for i in ids if i not in dropped], dropped_ids=[{"id": d, "reason": "not_in_tool_output"} for d in dropped])
    add("run_end", outcome=outcome)
    return evs


Q = {"expected_entity_ids": ["loc:midway_atoll"], "expected_resolved_ids": ["event:battle_of_midway"],
     "expected_behavior": "answer", "expected_tools": ["search_entities", "get_locations"]}
GOOD_TOOLS = [("get_locations", {"entity_id": "event:battle_of_midway"}, ["loc:midway_atoll"])]
GOOD_SEARCH = [("Battle of Midway", ["event:battle_of_midway"])]


@pytest.mark.parametrize(
    "expected_class,question,trace",
    [
        ("run_error", Q, stub_trace(outcome="error")),
        ("not_retrieved", Q, stub_trace(searches=GOOD_SEARCH, tools=[("get_locations", {"entity_id": "event:battle_of_midway"}, [])])),
        ("not_selected", Q, stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, ids=[])),
        ("dropped_by_check", Q, stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, ids=["loc:midway_atoll"], dropped=["loc:midway_atoll"])),
        ("extra_ids", Q, stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, ids=["loc:midway_atoll", "loc:tokyo"])),
        ("wrong_tools", Q, stub_trace(tools=GOOD_TOOLS, ids=["loc:midway_atoll"])),
        ("wrong_entity_accepted", {**Q, "expected_resolved_ids": ["loc:tarawa"]},
         stub_trace(searches=[("Tarawa", ["loc:tarakan", "loc:tarawa"])],
                    tools=[("get_locations", {"entity_id": "loc:tarakan"}, ["loc:tarakan"])], ids=["loc:tarakan"])),
        ("should_have_asked", {**Q, "expected_entity_ids": [], "expected_behavior": "ask"},
         stub_trace(searches=[("Guam", ["event:battle_of_guam_1941", "event:battle_of_guam_1944"])], ids=["event:battle_of_guam_1941"])),
        ("asked_unnecessarily", Q, stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, answer="Did you mean Midway Atoll?")),
    ],
)
def test_attribute_failure_classes(expected_class, question, trace):
    assert expected_class in attribute_failure(question, trace)


def test_attribute_clean_run_has_no_class():
    trace = stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, ids=["loc:midway_atoll"])
    assert attribute_failure(Q, trace) == []
    assert resolved_ids(trace) == {"event:battle_of_midway"}


def test_resolved_ids_ignore_unused_candidates():
    trace = stub_trace(searches=[("Guam", ["loc:guam", "event:battle_of_guam_1941"])],
                       tools=[("find_relations", {"target_id": "loc:guam"}, [])], ids=["loc:guam"])
    assert resolved_ids(trace) == {"loc:guam"}


def test_allowed_extra_ids_are_not_flagged():
    q = {**Q, "allowed_extra_ids": ["event:battle_of_midway"]}
    trace = stub_trace(searches=GOOD_SEARCH, tools=GOOD_TOOLS, ids=["loc:midway_atoll", "event:battle_of_midway"])
    assert attribute_failure(q, trace) == []
    assert "extra_ids" in attribute_failure(Q, trace)  # same ids without the allowance


# Model call deadline (hung upstream that keeps the connection alive)


class HangingThenOkModel(FakeToolModel):
    """First call never returns (like a hung upstream); later calls replay responses."""

    calls: int = 0

    async def _agenerate(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(30)
        return self._generate(*args, **kwargs)


class AlwaysHangingModel(FakeToolModel):
    async def _agenerate(self, *args, **kwargs):
        await asyncio.sleep(30)


def test_hung_model_call_is_retried_then_succeeds(isolated_env, monkeypatch, caplog):
    monkeypatch.setenv("GEO_AGENT_MODEL_DEADLINE", "0.2")
    out = ask([final("ok after retry")], model_cls=HangingThenOkModel)
    assert out["error"] is None and out["answer"] == "ok after retry"
    assert any("exceeded" in r.getMessage() for r in caplog.records)


def test_hung_model_call_becomes_model_call_error(isolated_env, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_MODEL_DEADLINE", "0.2")
    out = ask([final()], model_cls=AlwaysHangingModel)
    assert out["error"]["stage"] == "model_call" and out["error"]["type"] == "ModelDeadlineExceeded"
    end = read_events(isolated_env)[-1]
    assert end["event"] == "run_end" and end["failure_stage"] == "model_call"


def test_resolved_ids_ignore_fuzzy_candidates_reached_by_other_tools():
    """Observed on ex-where-2: loc:guadalcanal was a lower search hit for "Guadalcanal campaign"
    and reached the final ids via get_locations; only the queried event is the resolution."""
    trace = stub_trace(
        searches=[("Guadalcanal campaign", ["event:guadalcanal_campaign", "loc:guadalcanal"])],
        tools=[("get_locations", {"entity_id": "event:guadalcanal_campaign"}, ["loc:guadalcanal", "loc:tulagi"])],
        ids=["event:guadalcanal_campaign", "loc:guadalcanal", "loc:tulagi"],
    )
    assert resolved_ids(trace) == {"event:guadalcanal_campaign"}


def test_resolved_ids_fall_back_to_final_ids_without_follow_up_queries():
    trace = stub_trace(searches=[("Iwo Jima", ["loc:iwo_jima", "event:battle_of_iwo_jima"])], ids=["loc:iwo_jima"])
    assert resolved_ids(trace) == {"loc:iwo_jima"}


def test_resolving_an_allowed_related_entity_is_not_wrong():
    """Baseline case nv-1: "TOKYO" resolved to loc:tokyo, and the agent also queried the Doolittle Raid (allowed)."""
    q = {"expected_entity_ids": ["loc:tokyo", "org:usaaf"], "allowed_extra_ids": ["event:doolittle_raid"],
         "expected_resolved_ids": ["loc:tokyo"], "expected_behavior": "answer"}
    trace = stub_trace(searches=[("TOKYO", ["loc:tokyo", "event:doolittle_raid"])],
                       tools=[("find_relations", {"target_id": "loc:tokyo"}, ["org:usaaf"]),
                              ("get_entity", {"entity_id": "event:doolittle_raid"}, ["event:doolittle_raid"])],
                       ids=["loc:tokyo", "org:usaaf"])
    assert "wrong_entity_accepted" not in attribute_failure(q, trace)
