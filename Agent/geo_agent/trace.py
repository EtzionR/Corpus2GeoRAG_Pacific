"""Run tracing: one structured JSONL event stream per agent run (spec R12-R16).

A `RunTrace` is a LangChain callback handler created for each `GeoAgent.ask()`
call and passed in the run config. It turns model and tool callbacks into
events, numbers the steps, measures timings, and keeps the events in memory so
`ask()` can derive diagnostics and warnings from them. A `TraceWriter` appends
every event to `<log dir>/trace-YYYY-MM-DD.jsonl`.

Design decisions:
- Tracing goes through callbacks, so no tool and no `GraphStore` method changes.
  `run_inline = True` makes LangChain call the handler directly, which keeps
  events in execution order in async runs.
- Local files only, no hosted tracing service. Write failures are logged once
  and swallowed: tracing must never fail or delay a user request.
- Events hold the question, tool arguments, result previews and model text.
  They never hold environment values such as the API key.
- Diagnostics and warnings are pure functions of the event list, so tests can
  feed them stub traces.

Verified behavior of the installed LangChain versions (plan step 3a spike):
- Per model step: `on_chat_model_start`, then `on_llm_end` with usage in
  `message.usage_metadata` and the finish reason in `message.response_metadata`.
- Per tool call: `on_tool_start` (with `inputs` and `tool_call_id`), then
  `on_tool_end` (a ToolMessage) or `on_tool_error`. A tool exception stops the run.
- The structured final answer is requested as an `AgentAnswer` tool call but
  fires no tool callbacks; `ask()` emits the `structured_output` event itself.
- OpenRouter returned no reasoning content for the default model, so
  `reasoning` is usually null.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("geo_agent")

# Tools that query the graph after names are resolved (for `resolved_before_query`).
QUERY_TOOLS = {
    "get_neighbors", "find_relations", "get_locations", "entities_near",
    "entities_in_bbox", "what_happened_at", "search_source_text",
}

# Closed set of failure stages (spec R14).
FAILURE_STAGES = ("model_call", "tool_call", "step_limit", "structured_output", "post_processing", "unknown")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def env_on(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in ("0", "false", "no", "off", "")


def short_hash(data: str | bytes) -> str:
    """First 12 hex characters of the SHA-256 digest."""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()[:12]


def _text(content: Any) -> str:
    """Message or tool content as a string (content may be a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b if isinstance(b, str) else str(b.get("text", "")) for b in content if isinstance(b, (str, dict)))
    return json.dumps(content, ensure_ascii=False, default=str)


def _reasoning(message: Any) -> str | None:
    """Reasoning text when the provider returns it, else None."""
    extra = getattr(message, "additional_kwargs", {}) or {}
    for key in ("reasoning_content", "reasoning"):
        if extra.get(key):
            return _text(extra[key])
    if isinstance(getattr(message, "content", None), list):
        parts = [b.get("reasoning") or b.get("text", "") for b in message.content if isinstance(b, dict) and b.get("type") == "reasoning"]
        if parts:
            return "".join(parts)
    return None


def result_status(text: str) -> str:
    """Classify a tool result: `error`, `empty` ("no matching ..." or []), or `ok`."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return "ok" if text.strip() else "empty"
    if data in ([], {}):
        return "empty"
    if isinstance(data, dict) and "error" in data:
        return "error"
    if isinstance(data, dict) and set(data) == {"result"}:
        return "empty"  # tools answer {"result": "no matching ..."} when nothing is found
    return "ok"


# ---------------------------------------------------------------- writer


class TraceWriter:
    """Appends events as JSON lines to the daily trace file. Never raises."""

    def __init__(self, log_dir: str | Path, enabled: bool = True):
        self.log_dir = Path(log_dir)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._warned = False

    @classmethod
    def from_env(cls) -> "TraceWriter":
        return cls(os.getenv("GEO_AGENT_LOG_DIR", "logs"), env_on("GEO_AGENT_TRACE", True))

    def path_for(self, ts: str) -> Path:
        return self.log_dir / f"trace-{ts[:10]}.jsonl"

    def write(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self._lock:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with self.path_for(event["ts"]).open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
        except Exception as exc:  # tracing must never break a run
            if not self._warned:
                self._warned = True
                log.warning("trace write failed (%s: %s); continuing without trace files", type(exc).__name__, exc)


# --------------------------------------------------------------- handler


class RunTrace(BaseCallbackHandler):
    """Callback handler that records one agent run as trace events."""

    run_inline = True  # keep events in execution order in async runs

    def __init__(self, run_id: str, writer: TraceWriter | None = None):
        self.run_id = run_id
        self.writer = writer
        self.text_chars = env_int("GEO_AGENT_TRACE_TEXT_CHARS", 2000)
        self.full_results = env_on("GEO_AGENT_TRACE_FULL", False)
        self.events: list[dict[str, Any]] = []
        self.step = 0
        self.failed_stage: str | None = None  # set by model/tool error callbacks
        self._seq = 0
        self._lock = threading.Lock()
        self._model_t0: dict[UUID, float] = {}
        self._tools: dict[UUID, tuple[str, Any, float]] = {}

    # ----------------------------------------------------------- emitting

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "run_id": self.run_id, "seq": self._seq, "event": event, **fields}
            self.events.append(ev)
        if self.writer is not None:
            self.writer.write(ev)
        self._log(ev)
        return ev

    def _log(self, ev: dict[str, Any]) -> None:
        """One readable console line per event (spec R16)."""
        rid, kind = self.run_id[:8], ev["event"]
        if kind == "run_start":
            log.info("%s run_start model=%s question=%r", rid, ev.get("model"), ev.get("question"))
        elif kind == "model_end":
            asked = ", ".join(c["name"] for c in ev["tool_calls_requested"]) or "final text"
            log.info("%s step %d model %dms -> %s", rid, ev["step"], ev["latency_ms"], asked)
        elif kind == "tool_end":
            log.info("%s step %d tool %s %s %dms", rid, ev["step"], ev["name"], ev["status"], ev["latency_ms"])
        elif kind == "tool_error":
            log.error("%s step %d tool %s raised %s: %s\n%s", rid, ev["step"], ev["name"], ev["error_type"], ev["message"], ev["traceback"])
        elif kind == "finalize" and ev.get("dropped_ids"):
            log.warning("%s dropped ids not supported by tool output: %s", rid,
                        ", ".join(f"{d['id']} ({d['reason']})" for d in ev["dropped_ids"]))
        elif kind == "structured_output" and not ev["ok"]:
            log.warning("%s structured output failed; using plain-text answer", rid)
        elif kind == "run_end":
            if ev["outcome"] == "error":
                log.error("%s run failed at %s: %s", rid, ev["failure_stage"], (ev.get("error") or {}).get("message"))
            elif ev["warnings"]:
                log.warning("%s run finished with warnings: %s", rid, ", ".join(ev["warnings"]))
            else:
                log.info("%s run ok in %dms", rid, ev["latency_ms"])

    # ---------------------------------------------------------- callbacks

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._model_t0[run_id] = time.perf_counter()

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._model_t0[run_id] = time.perf_counter()

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        t0 = self._model_t0.pop(run_id, None)
        self.step += 1
        gen = response.generations[0][0] if response.generations and response.generations[0] else None
        msg = getattr(gen, "message", None)
        usage = getattr(msg, "usage_metadata", None) or {}
        finish = (getattr(msg, "response_metadata", None) or {}).get("finish_reason") or (getattr(gen, "generation_info", None) or {}).get("finish_reason")
        reasoning = _reasoning(msg) if msg is not None else None
        self.emit(
            "model_end",
            step=self.step,
            latency_ms=round((time.perf_counter() - t0) * 1000) if t0 else None,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            finish_reason=finish,
            tool_calls_requested=[{"name": c["name"], "args": c["args"], "id": c.get("id")} for c in getattr(msg, "tool_calls", None) or []],
            text=_text(getattr(msg, "content", "") or getattr(gen, "text", ""))[: self.text_chars],
            reasoning=reasoning[: self.text_chars] if reasoning else None,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._model_t0.pop(run_id, None)
        self.failed_stage = "model_call"

    def on_tool_start(self, serialized: Any, input_str: str, *, run_id: UUID, inputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "unknown"
        self._tools[run_id] = (name, inputs if inputs is not None else input_str, time.perf_counter())

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        name, args, t0 = self._tools.pop(run_id, (kwargs.get("name") or "unknown", None, time.perf_counter()))
        text = _text(getattr(output, "content", output))
        self.emit(
            "tool_end",
            step=self.step,
            name=name,
            args=args,
            latency_ms=round((time.perf_counter() - t0) * 1000),
            status=result_status(text),
            result_chars=len(text),
            result_sha256=hashlib.sha256(text.encode()).hexdigest(),
            result_preview=text if self.full_results else text[:1000],
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        name, args, _ = self._tools.pop(run_id, (kwargs.get("name") or "unknown", None, 0.0))
        self.failed_stage = "tool_call"
        self.emit(
            "tool_error",
            step=self.step,
            name=name,
            args=args,
            error_type=type(error).__name__,
            message=str(error),
            traceback="".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )


# ------------------------------------------------- pure trace analysis


def tool_calls_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Executed tool calls (name, args) in order, including ones that raised."""
    return [{"name": e["name"], "args": e["args"]} for e in events if e["event"] in ("tool_end", "tool_error")]


def diagnostics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Run summary for `run_end` (spec R15)."""
    models = [e for e in events if e["event"] == "model_end"]
    tools = [e for e in events if e["event"] in ("tool_end", "tool_error")]
    sequence = [e["name"] for e in tools]
    keys = [(e["name"], json.dumps(e["args"], sort_keys=True, default=str)) for e in tools]
    first_query = next((i for i, n in enumerate(sequence) if n in QUERY_TOOLS), None)
    first_search = next((i for i, n in enumerate(sequence) if n == "search_entities"), None)
    return {
        "steps": len(models),
        "tool_sequence": sequence,
        "tool_counts": dict(Counter(sequence)),
        "empty_results": sum(e.get("status") == "empty" for e in tools),
        "repeated_calls": len(keys) - len(set(keys)),
        "resolved_before_query": None if first_query is None else (first_search is not None and first_search < first_query),
        "model_ms": sum(e["latency_ms"] or 0 for e in models),
        "tools_ms": sum(e.get("latency_ms") or 0 for e in tools),
        "input_tokens": sum(e["input_tokens"] or 0 for e in models),
        "output_tokens": sum(e["output_tokens"] or 0 for e in models),
    }


def trace_warnings(events: list[dict[str, Any]], warn_steps: int | None = None) -> list[str]:
    """Warning codes derivable from the trace alone (spec R14)."""
    warn_steps = env_int("GEO_AGENT_WARN_STEPS", 10) if warn_steps is None else warn_steps
    diag = diagnostics(events)
    warnings = []
    if diag["repeated_calls"]:
        warnings.append("repeated_tool_call")
    first_search = next((e for e in events if e["event"] == "tool_end" and e["name"] == "search_entities"), None)
    if first_search is not None and first_search["status"] == "empty":
        warnings.append("empty_resolution")
    if diag["steps"] > warn_steps:
        warnings.append("many_steps")
    return warnings


# ------------------------------------------- reading traces back (R17-R19)


def load_events(log_dir: str | Path) -> list[dict[str, Any]]:
    """All events from the trace files in `log_dir`; unreadable lines are skipped."""
    events = []
    for path in sorted(Path(log_dir).glob("trace-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def group_runs(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Events grouped by run_id, each run ordered by seq; runs ordered by start time."""
    runs: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        runs.setdefault(e["run_id"], []).append(e)
    for evs in runs.values():
        evs.sort(key=lambda e: e["seq"])
    return dict(sorted(runs.items(), key=lambda kv: kv[1][0]["ts"]))


def find_run(events: list[dict[str, Any]], run_id: str = "last") -> list[dict[str, Any]] | None:
    """Events of one run: "last", a full run_id, or a unique prefix (e.g. the 8 chars in logs)."""
    runs = group_runs(events)
    if not runs:
        return None
    if run_id == "last":
        return list(runs.values())[-1]
    matches = [rid for rid in runs if rid.startswith(run_id)]
    return runs[matches[0]] if len(matches) == 1 else None


def parse_since(value: str | None) -> timedelta | None:
    """'7d', '12h', '30m' -> timedelta. None or '' -> None (no filter)."""
    if not value:
        return None
    units = {"d": "days", "h": "hours", "m": "minutes"}
    if value[-1] not in units or not value[:-1].isdigit():
        raise ValueError(f"bad --since value {value!r}; use e.g. 7d, 12h, 30m")
    return timedelta(**{units[value[-1]]: int(value[:-1])})


def _args(args: Any) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in args.items())
    return str(args)


def _indent(text: str, width: int = 300) -> str:
    text = text.replace("\n", " ")
    return "      " + (text[:width] + " ..." if len(text) > width else text)


def render_timeline(events: list[dict[str, Any]]) -> str:
    """Readable timeline of one run (spec R17)."""
    start = next((e for e in events if e["event"] == "run_start"), {})
    end = next((e for e in events if e["event"] == "run_end"), None)
    graph = start.get("graph") or {}
    lines = [
        f"Run {events[0]['run_id']}  {start.get('ts', '')}",
        f"Question: {start.get('question') if start.get('question') is not None else '(not stored)'}",
        f"Model: {start.get('model')} @ {start.get('base_url_host')}   step limit {start.get('step_limit')}",
        f"Graph: {graph.get('path')} ({graph.get('nodes')} nodes, {graph.get('edges')} edges, sha {graph.get('sha256')})",
    ]
    if end:
        lines.append(f"Outcome: {end['outcome']}" + (f"   failure stage: {end['failure_stage']}" if end.get("failure_stage") else "")
                     + f"   {end['latency_ms']} ms")
    else:
        lines.append("Outcome: run did not finish (no run_end event)")
    lines.append("")

    last_tool = None
    for e in events:
        kind = e["event"]
        if kind == "model_end":
            tokens = f"tokens {e.get('input_tokens')}/{e.get('output_tokens')}" if e.get("input_tokens") is not None else "tokens n/a"
            asked = ", ".join(f"{c['name']}({_args(c['args'])})" for c in e["tool_calls_requested"]) or "no tool calls"
            lines.append(f"Step {e['step']}  model {e.get('latency_ms')} ms  {tokens}  finish={e.get('finish_reason')}")
            lines.append(f"   requests: {asked}")
            if e.get("text"):
                lines.append("   text:")
                lines.append(_indent(e["text"]))
            if e.get("reasoning"):
                lines.append("   reasoning:")
                lines.append(_indent(e["reasoning"]))
        elif kind == "tool_end":
            last_tool = e
            lines.append(f"   tool {e['name']}({_args(e['args'])})  {e['status']}  {e['latency_ms']} ms  {e['result_chars']} chars")
            lines.append(_indent(e["result_preview"], 200))
        elif kind == "tool_error":
            last_tool = e
            lines.append(f"   tool {e['name']}({_args(e['args'])})  RAISED {e['error_type']}: {e['message']}")
        elif kind == "structured_output":
            lines.append("")
            if e["ok"]:
                lines.append(f"Answer: {e['answer']['answer']}")
                if "answer_ids" in e["answer"]:
                    lines.append(f"   answer_ids: {e['answer']['answer_ids']}   context_ids: {e['answer']['context_ids']}"
                                 f"   sources: {e['answer']['sources']}")
                else:
                    lines.append(f"   entity_ids: {e['answer']['entity_ids']}   sources: {e['answer']['sources']}")
            else:
                lines.append("Structured output failed; plain-text answer:")
                lines.append(_indent(e.get("raw_text") or ""))
        elif kind == "finalize":  # added in plan step 9
            if e.get("dropped_ids"):
                lines.append(f"Dropped ids: {e['dropped_ids']}")
            if e.get("confidence"):
                lines.append(f"Confidence: {e['confidence'].get('level')}  {e['confidence'].get('basis')}")
            if e.get("interpretation"):
                lines.append(f"Interpretation: {e['interpretation']}")

    if end:
        lines.append("")
        if end.get("error"):
            lines.append(f"Error: {end['error']['type']}: {end['error']['message']}")
            if last_tool:
                lines.append(f"Last tool call: {last_tool['name']}({_args(last_tool['args'])})")
        if end.get("warnings"):
            lines.append(f"Warnings: {', '.join(end['warnings'])}")
        d = end.get("diagnostics") or {}
        lines.append(f"Diagnostics: {d.get('steps')} steps, tools {d.get('tool_sequence')}, "
                     f"empty {d.get('empty_results')}, repeated {d.get('repeated_calls')}, "
                     f"resolved_before_query {d.get('resolved_before_query')}, "
                     f"model {d.get('model_ms')} ms, tools {d.get('tools_ms')} ms, "
                     f"tokens {d.get('input_tokens')}/{d.get('output_tokens')}")
    return "\n".join(lines)


def replay(events: list[dict[str, Any]], tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-run the recorded tool calls against `tools` (name -> tool) without a model (spec R18).

    Each row says whether the new result hash matches the recorded one. A
    recorded tool_error matches when the call raises again.
    """
    rows = []
    for e in events:
        if e["event"] not in ("tool_end", "tool_error"):
            continue
        row: dict[str, Any] = {"seq": e["seq"], "name": e["name"], "args": e["args"]}
        tool = tools.get(e["name"])
        if tool is None:
            rows.append({**row, "status": "skipped", "detail": "not a built-in tool (MCP or removed)"})
            continue
        try:
            text = _text(tool.invoke(e["args"]))
        except Exception as exc:
            same = e["event"] == "tool_error"
            rows.append({**row, "status": "match" if same else "mismatch", "detail": f"raised {type(exc).__name__}: {exc}"})
            continue
        if e["event"] == "tool_error":
            rows.append({**row, "status": "mismatch", "detail": "recorded an error, now returns a result"})
            continue
        same = hashlib.sha256(text.encode()).hexdigest() == e["result_sha256"]
        rows.append({**row, "status": "match" if same else "mismatch",
                     "detail": "" if same else f"result changed: {text[:200]}"})
    return rows


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def summarize(events: list[dict[str, Any]], since: timedelta | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Aggregate report over runs (spec R19). `since` keeps runs started within that window."""
    runs = group_runs(events)
    if since is not None:
        cutoff = (now or datetime.now(timezone.utc)) - since
        runs = {rid: evs for rid, evs in runs.items() if datetime.fromisoformat(evs[0]["ts"]) >= cutoff}
    ends = [next((e for e in evs if e["event"] == "run_end"), None) for evs in runs.values()]
    finished = [e for e in ends if e is not None]
    tool_events = [e for evs in runs.values() for e in evs if e["event"] in ("tool_end", "tool_error")]
    tool_counts = Counter(e["name"] for e in tool_events)
    empty_counts = Counter(e["name"] for e in tool_events if e.get("status") == "empty")
    latencies = [e["latency_ms"] for e in finished]
    diags = [e.get("diagnostics") or {} for e in finished]
    n = len(finished)
    return {
        "runs": len(runs),
        "unfinished": len(ends) - n,
        "outcomes": dict(Counter(e["outcome"] for e in finished)),
        "failures_by_stage": dict(Counter(e["failure_stage"] for e in finished if e.get("failure_stage"))),
        "warnings": dict(Counter(w for e in finished for w in e.get("warnings") or [])),
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "mean_steps": round(sum(d.get("steps", 0) for d in diags) / n, 2) if n else None,
        "mean_tool_calls": round(sum(len(d.get("tool_sequence", [])) for d in diags) / n, 2) if n else None,
        "tool_usage": dict(tool_counts.most_common()),
        "empty_share": {name: round(empty_counts[name] / count, 3) for name, count in tool_counts.items()},
        "input_tokens": sum(d.get("input_tokens", 0) for d in diags),
        "output_tokens": sum(d.get("output_tokens", 0) for d in diags),
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [f"Runs: {report['runs']}" + (f" ({report['unfinished']} unfinished)" if report["unfinished"] else "")]
    for key in ("outcomes", "failures_by_stage", "warnings"):
        lines.append(f"{key.replace('_', ' ').capitalize()}: " + (", ".join(f"{k} {v}" for k, v in report[key].items()) or "none"))
    lines.append(f"Latency: p50 {report['latency_ms_p50']} ms, p95 {report['latency_ms_p95']} ms")
    lines.append(f"Per run: {report['mean_steps']} model steps, {report['mean_tool_calls']} tool calls")
    lines.append("Tools (calls, empty share):")
    for name, count in report["tool_usage"].items():
        lines.append(f"   {name:<20} {count:>5}   {report['empty_share'][name]:.0%}")
    lines.append(f"Tokens: {report['input_tokens']} in, {report['output_tokens']} out")
    return "\n".join(lines)


# ---------------------------------------------- eval failure attribution (R20)

_ID_RE = re.compile(r'"id":\s*"([^"]+)"')
_ARG_ID_KEYS = ("entity_id", "source_id", "target_id")
FAILURE_CLASSES = (
    "run_error", "not_retrieved", "not_selected", "dropped_by_check", "extra_ids", "wrong_tools",
    "wrong_entity_accepted", "should_have_asked", "asked_unnecessarily",
)


def _claimed(answer: dict[str, Any], key: str) -> list[str]:
    """Ids from a structured answer; traces from before spec R24 only have `entity_ids`."""
    if key == "entity_ids" and "entity_ids" not in answer:
        return list(dict.fromkeys((answer.get("answer_ids") or []) + (answer.get("context_ids") or [])))
    return list(answer.get(key) or answer.get("entity_ids") or [])


def final_answer(events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """(answer text, final entity ids) of a run, from its trace."""
    fin = next((e for e in events if e["event"] == "finalize"), None)
    so = next((e for e in events if e["event"] == "structured_output"), None)
    text = ""
    if so is not None:
        text = so["answer"]["answer"] if so["ok"] else so.get("raw_text") or ""
    if fin is not None:  # kept ids after the evidence check (plan step 9)
        return text, list(fin.get("kept_ids") or [])
    return text, _claimed(so["answer"], "entity_ids") if so is not None and so["ok"] else []


def final_answer_ids(events: list[dict[str, Any]]) -> list[str]:
    """The run's answer ids (spec R24); runs from before R24 count all final ids as answer ids."""
    fin = next((e for e in events if e["event"] == "finalize"), None)
    if fin is not None and "answer_ids" in fin:
        return list(fin["answer_ids"])
    so = next((e for e in events if e["event"] == "structured_output"), None)
    if fin is None and so is not None and so["ok"] and "answer_ids" in so["answer"]:
        return list(so["answer"]["answer_ids"])
    return final_answer(events)[1]


def retrieved_ids(events: list[dict[str, Any]]) -> set[str]:
    """Every entity id that appears in a tool result of the run."""
    return {m for e in events if e["event"] == "tool_end" for m in _ID_RE.findall(e.get("result_preview") or "")}


def resolved_ids(events: list[dict[str, Any]]) -> set[str]:
    """Entities a name resolved to: search_entities hits the agent then queried with.

    A hit counts when it is passed as an argument (entity_id, source_id,
    target_id, entity_ids) to a later tool call. Only when no hit is used that
    way (the model answered straight from the search) do hits in the final ids
    count. Final ids alone are not enough: a lower-ranked fuzzy candidate (e.g.
    loc:guadalcanal for "Guadalcanal campaign") can legitimately reach the final
    ids through another tool without being what the name resolved to.
    This approximates spec R21's resolution record until plan step 9 adds it.
    """
    candidates: set[str] = set()
    queried: set[str] = set()
    for e in events:
        if e["event"] == "tool_end" and e["name"] == "search_entities":
            candidates |= set(_ID_RE.findall(e.get("result_preview") or ""))
        elif e["event"] in ("tool_end", "tool_error") and isinstance(e.get("args"), dict):
            args = e["args"]
            queried |= {args[k] for k in _ARG_ID_KEYS if isinstance(args.get(k), str)}
            queried |= {i for i in args.get("entity_ids") or [] if isinstance(i, str)}
    return (candidates & queried) or (candidates & set(final_answer(events)[1]))


def asked(answer: str, final_ids: list[str]) -> bool:
    """A reply that asks the user back: no ids and a question mark."""
    return not final_ids and "?" in answer


def attribute_failure(question: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    """Failure classes of one eval question from its trace (spec R20). Pure function."""
    end = next((e for e in events if e["event"] == "run_end"), None)
    if end is None or end["outcome"] == "error":
        return ["run_error"]
    expected = set(question["expected_entity_ids"])  # required ids, checked on answer + context ids
    acceptable = expected | set(question.get("allowed_extra_ids") or [])
    expected_resolved = set(question.get("expected_resolved_ids") or [])
    behavior = question["expected_behavior"]
    answer, final_ids = final_answer(events)
    final = set(final_ids)
    answer_ids = set(final_answer_ids(events))
    retrieved = retrieved_ids(events)
    dropped = {d["id"] if isinstance(d, dict) else d
               for e in events if e["event"] == "finalize" for d in e.get("dropped_ids") or []}
    sequence = [e["name"] for e in events if e["event"] in ("tool_end", "tool_error")]
    did_ask = asked(answer, final_ids)

    classes = []
    if expected - retrieved:
        classes.append("not_retrieved")
    if (expected & retrieved) - final - dropped:
        classes.append("not_selected")
    if expected & dropped:
        classes.append("dropped_by_check")
    if answer_ids - acceptable:  # spec R24: context ids may name anyone the answer mentions
        classes.append("extra_ids")
    expected_tools = question.get("expected_tools")
    if expected_tools:
        it = iter(sequence)
        if not all(name in it for name in expected_tools):
            classes.append("wrong_tools")
    if not did_ask and final and resolved_ids(events) - (expected_resolved | acceptable):
        classes.append("wrong_entity_accepted")
    if behavior == "ask" and not did_ask:
        classes.append("should_have_asked")
    if behavior == "answer" and did_ask:
        classes.append("asked_unnecessarily")
    return classes
