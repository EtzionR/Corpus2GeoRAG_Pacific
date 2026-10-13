"""Synchronous front end for GeoAgent, for in-process callers such as the Streamlit UI.

`GeoAgent` is async, and its HTTP client is bound to the event loop it was
created on. A Streamlit script is synchronous and reruns on every interaction,
so calling `asyncio.run(agent.ask(...))` per question would build a new loop
each time and break the client. `AgentRunner` owns one long-lived event loop on
a background daemon thread, creates the agent on it, and lets any thread submit
questions:

    from geo_agent.runner import AgentRunner
    runner = AgentRunner()                       # once per process (st.cache_resource)
    result = runner.ask_sync("Who attacked Guadalcanal in 1942?", thread_id=tid)

Design decisions:
- `ask_sync` never raises for run problems: like `GeoAgent.ask()`, it returns the
  output dict, with `error.stage` `timeout` when the question took longer than
  `timeout` seconds (it is then cancelled), `cancelled` after `cancel()`.
- A stopped run may leave a half-finished turn in the conversation memory (a tool
  call with no result), which the model provider would reject on the next turn.
  So after a timeout or cancel, that `thread_id` continues on a fresh internal
  conversation: the next question works, but the earlier turns are forgotten.
- The runner is thread-safe and meant to be shared by the whole process; questions
  on different threads run concurrently on the one loop.
- One question at a time per `thread_id`. A Streamlit double submit or rerun must not
  run two turns of one conversation at once (they would interleave in its memory).
  By default a second question on a busy thread returns at once with an error result,
  stage `busy`, and is not run (`if_busy="reject"`): the UI is still waiting for the
  first answer, so running the duplicate would only show a second, stale reply.
  `if_busy="wait"` instead waits for the running question (within the timeout) and
  then runs; if the wait runs out it also returns `busy`. A busy result writes no trace.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

from geo_agent.agent import GeoAgent, error_result, stopped_result
from geo_agent.graph_store import GraphStore
from geo_agent.schema import SCHEMA_VERSION
from geo_agent.trace import env_on


class ThreadBusy(Exception):
    """Another question is still running in this conversation; stage `busy`."""


BUSY_MESSAGE = "another question is still running in this conversation; wait for its answer"

log = logging.getLogger("geo_agent")

DEFAULT_TIMEOUT = 120.0  # seconds per question; eval runs take 3-35 s, rare outliers ~100 s
STOP_GRACE = 5.0  # seconds to wait for a cancelled run to unwind and close its trace


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


EXCERPT_CHARS = 300


def excerpt(text: str, chars: int = EXCERPT_CHARS) -> str:
    """The first paragraph, cut at a word boundary to at most `chars` characters."""
    first = text.strip().split("\n\n", 1)[0].strip()
    if len(first) <= chars:
        return first
    cut = first[:chars].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def entity_card(store: GraphStore, entity_id: str, excerpt_chars: int = EXCERPT_CHARS) -> dict[str, Any] | None:
    """See AgentRunner.get_entity_feature."""
    node = store.nodes.get(entity_id)
    if node is None:
        return None
    geojson = store.to_geojson([entity_id])
    own = next((f for f in geojson["features"] if f["properties"]["id"] == entity_id), None)
    return {
        "id": node.id,
        "name": node.name,
        "type": node.type,
        "aliases": list(node.aliases),
        "attributes": dict(node.attributes),
        "excerpt": excerpt(store.safe_text.get(entity_id, ""), excerpt_chars),
        "sources": [s.model_dump(include={"page", "url", "paragraph"}) for s in node.sources],
        "feature": own,
        "geojson": geojson,
    }


class PendingAnswer:
    """Handle on a submitted question. Thread-safe; `result()` may be called from any thread.

    The time limit is enforced on the event loop from the moment of submit, so a UI that
    polls `done()` / `poll_progress()` instead of blocking in `result()` gets the same
    `timeout` result.
    """

    def __init__(self, runner: "AgentRunner", question: str, thread_id: str, run_id: str, timeout: float,
                 on_progress: Callable[[dict[str, Any]], None] | None = None):
        self.runner = runner
        self.question = question
        self.thread_id = thread_id
        self.run_id = run_id
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._on_progress = on_progress
        self._events: list[dict[str, Any]] = []
        self._read = 0
        self._events_lock = threading.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._stop: str | None = None  # "timeout" or "cancelled" once a stop was requested

    def done(self) -> bool:
        """True once the run has finished (answered, failed or stopped)."""
        return self.future is not None and self.future.done()

    def cancel(self) -> bool:
        """Stop the run. `result()` then returns an error result with stage `cancelled`.

        Returns False if the run had already finished.
        """
        return self._request_stop("cancelled")

    def result(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the output dict. Never raises for run problems.

        Without `timeout`, waits until the run ends; the runner stops it at the deadline
        set at submit (result `error.stage == "timeout"`). With `timeout`, waits at most
        that long and then stops the run the same way.
        """
        assert self.future is not None
        wait = max(self.deadline - time.monotonic(), 0.0) + STOP_GRACE if timeout is None else timeout
        try:
            return self.future.result(wait)
        except concurrent.futures.TimeoutError:
            self._request_stop("timeout")
        try:
            return self.future.result(STOP_GRACE)
        except concurrent.futures.TimeoutError:  # the run ignores cancellation; report it stopped anyway
            log.warning("%s run did not stop within %.0fs after %s", self.run_id[:8], STOP_GRACE, self._stop)
            return stopped_result(self.run_id, self._stop or "timeout", [])

    def poll_progress(self) -> list[dict[str, Any]]:
        """Progress events that arrived since the last call (for a polling UI loop)."""
        with self._events_lock:
            new, self._read = self._events[self._read:], len(self._events)
        return new

    @property
    def progress(self) -> list[dict[str, Any]]:
        """All progress events so far."""
        with self._events_lock:
            return list(self._events)

    # ------------------------------------------------------------ internals

    def _progress(self, event: dict[str, Any]) -> None:
        """Hand the event to the loop thread. LangChain calls tool callbacks of sync tools on
        executor threads; queueing every event on the loop keeps one thread and their order."""
        try:
            self.runner.loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop closed
            pass

    def _deliver(self, event: dict[str, Any]) -> None:
        """On the loop thread: buffer the event, then call the user's callback (errors logged)."""
        with self._events_lock:
            self._events.append(event)
        if self._on_progress is not None:
            try:
                self._on_progress(event)
            except Exception as exc:
                log.warning("on_progress failed (%s: %s); disabled for this question", type(exc).__name__, exc)
                self._on_progress = None

    def _request_stop(self, reason: str) -> bool:
        if self.done():
            return False
        if self._stop is None:
            self._stop = reason
        try:
            self.runner.loop.call_soon_threadsafe(self._cancel_on_loop)
        except RuntimeError:  # loop already closed
            return False
        return True

    def _cancel_on_loop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel(self._stop)
        # If the task hasn't started yet, _run sees self._stop and returns at once.

    def _deadline_on_loop(self) -> None:
        if self._stop is None:
            self._stop = "timeout"
        self._cancel_on_loop()

    async def _run(self, agent: Any) -> dict[str, Any]:
        self._task = asyncio.current_task()
        if self._stop:
            return stopped_result(self.run_id, self._stop, [])
        internal = self.runner._internal_thread(self.thread_id)
        timer = asyncio.get_running_loop().call_later(max(self.deadline - time.monotonic(), 0.0), self._deadline_on_loop)
        try:
            return await agent.ask(self.question, internal, run_id=self.run_id, on_progress=self._progress)
        except asyncio.CancelledError as exc:
            self.runner._reset_thread(self.thread_id)
            out = getattr(exc, "geo_result", None)
            reason = self._stop or "cancelled"
            if out is None or out["error"]["stage"] != reason:
                out = stopped_result(self.run_id, reason, out["tool_calls"] if out else [])
            if reason == "timeout":
                out["error"]["message"] = f"the question took longer than {self.timeout:g}s and was stopped"
            return out
        except Exception as exc:  # GeoAgent.ask returns errors as data; this is a safety net
            log.error("%s run raised outside GeoAgent.ask", self.run_id[:8], exc_info=exc)
            return error_result(self.run_id, exc, "unknown", [])
        finally:
            timer.cancel()


class AgentRunner:
    """Owns an event loop thread and one GeoAgent; answers questions synchronously.

    Arguments are passed to `GeoAgent.create` (`llm` overrides the OpenRouter model,
    tests pass a fake one). `timeout` is the default per-question limit in seconds
    (env GEO_AGENT_RUN_TIMEOUT, default 120). Raises AgentConfigError when the agent
    can't be built (e.g. no API key).

    `mock=True` (default: env GEO_AGENT_MOCK) answers from recorded replies on the
    sample graph instead (see geo_agent/mock.py): no key, no .env, no network;
    `graph_path`, `mcp_config` and `llm` are then ignored.
    """

    def __init__(
        self,
        graph_path: str | Path | None = None,
        mcp_config: str | Path | None = None,
        llm: BaseChatModel | None = None,
        *,
        timeout: float | None = None,
        start_timeout: float = 120.0,
        mock: bool | None = None,
    ):
        load_dotenv()  # GEO_AGENT_MOCK may be set in .env; a missing .env is fine
        self.mock = env_on("GEO_AGENT_MOCK", False) if mock is None else mock
        self.timeout = _env_float("GEO_AGENT_RUN_TIMEOUT", DEFAULT_TIMEOUT) if timeout is None else timeout
        self.loop = asyncio.new_event_loop()
        self._mutex = threading.Lock()
        self._aliases: dict[str, str] = {}  # user thread_id -> internal conversation id after a stop
        self._pending: set[PendingAnswer] = set()
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="geo-agent-loop", daemon=True)
        self._thread.start()
        try:
            if self.mock:
                from geo_agent.mock import MockAgent
                create = MockAgent.create()
            else:
                create = GeoAgent.create(graph_path=graph_path, mcp_config=mcp_config, llm=llm)
            self.agent: Any = asyncio.run_coroutine_threadsafe(create, self.loop).result(start_timeout)
        except BaseException:
            self.close()
            raise

    # ----------------------------------------------------------------- public

    @staticmethod
    def new_thread_id() -> str:
        """A fresh conversation id: one per chat session (st.session_state)."""
        return uuid.uuid4().hex

    def submit(self, question: str, thread_id: str = "default", *, timeout: float | None = None,
               if_busy: str = "reject", on_progress: Callable[[dict[str, Any]], None] | None = None) -> PendingAnswer:
        """Start a question without waiting. Use `.result()`, `.done()`, `.poll_progress()` or `.cancel()`.

        If `thread_id` already has a running question, `if_busy="reject"` (default) returns a
        finished handle whose result is a `busy` error; `"wait"` waits up to `timeout` for it.

        `on_progress(event)` gets short step events while the question runs (run_start,
        model_start/model_end, tool_start/tool_end/tool_error, run_end; each with `label`,
        e.g. "Looking up names in the graph"). It is called on the runner's loop thread: keep
        it fast and don't call Streamlit from it; put events on a queue, or poll
        `pending.poll_progress()` from the script thread instead. Its errors are ignored.
        """
        if if_busy not in ("reject", "wait"):
            raise ValueError("if_busy must be 'reject' or 'wait'")
        limit = self.timeout if timeout is None else timeout
        pending = PendingAnswer(self, question, thread_id, uuid.uuid4().hex, limit, on_progress)
        deadline = pending.deadline
        while True:
            with self._mutex:
                if self._closed:
                    raise RuntimeError("AgentRunner is closed")
                running = next((p for p in self._pending if p.thread_id == thread_id and not p.done()), None)
                if running is None:
                    self._pending.add(pending)
                    break
            left = deadline - time.monotonic()
            if if_busy == "reject" or left <= 0:
                pending.future = concurrent.futures.Future()
                pending.future.set_result(error_result(pending.run_id, ThreadBusy(BUSY_MESSAGE), "busy", []))
                return pending
            concurrent.futures.wait([running.future], timeout=left)  # type: ignore[list-item]
        pending.future = asyncio.run_coroutine_threadsafe(pending._run(self.agent), self.loop)
        pending.future.add_done_callback(lambda _: self._finished(pending))
        return pending

    def ask_sync(self, question: str, thread_id: str = "default", *, timeout: float | None = None,
                 if_busy: str = "reject", on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Ask a question and wait for the output dict (same keys as `GeoAgent.ask`). Never raises for run problems."""
        return self.submit(question, thread_id, timeout=timeout, if_busy=if_busy, on_progress=on_progress).result()

    def is_busy(self, thread_id: str) -> bool:
        """True while a question of this conversation is running (e.g. to disable the input box)."""
        with self._mutex:
            return any(p.thread_id == thread_id and not p.done() for p in self._pending)

    def cancel(self, thread_id: str) -> bool:
        """Cancel the running question(s) of a conversation; True if one was running."""
        with self._mutex:
            running = [p for p in self._pending if p.thread_id == thread_id]
        return any([p.cancel() for p in running])

    def get_entity_feature(self, entity_id: str, excerpt_chars: int = EXCERPT_CHARS) -> dict[str, Any] | None:
        """Details of one entity for a map click or a details panel; None for an unknown id.

        Reads the loaded graph directly (no model, no network, safe from any thread).
        Keys: id, name, type, aliases, attributes, excerpt (start of the source text, with
        instruction-like sentences removed; untrusted, render as plain text), sources
        ([{page, url, paragraph}]), feature (the entity's own GeoJSON Feature, None for
        events, people and orgs) and geojson (FeatureCollection: the entity itself, or the
        places it is linked to, with `related_to` set).
        """
        return entity_card(self.agent.store, entity_id, excerpt_chars)

    def health(self) -> dict[str, Any]:
        """Quick status for a sidebar or readiness check. Cheap; no model call."""
        ctx = self.agent.context
        loop_ok = self.loop.is_running() and not self._closed
        graph_loaded = len(self.agent.store.nodes) > 0
        return {
            "ok": loop_ok and graph_loaded,
            "mock": self.mock,
            "loop_running": loop_ok,
            "graph_loaded": graph_loaded,
            "model": ctx.get("model"),
            "model_configured": not self.mock,  # a real agent only exists when the model could be set up
            "version": ctx.get("version"),
            "schema_version": SCHEMA_VERSION,
            "timeout_s": self.timeout,
        }

    def stats(self) -> dict[str, Any]:
        """Graph and runner numbers: node/edge counts, geocoded places, types, extent, running questions."""
        store = self.agent.store
        schema = store.schema()
        graph = self.agent.context.get("graph", {})
        with self._mutex:
            running = sum(not p.done() for p in self._pending)
        return {
            "nodes": schema["node_count"],
            "edges": schema["edge_count"],
            "geocoded": len(store.geoms),
            "node_types": schema["node_types"],
            "relation_types": schema["relation_types"],
            "date_range": schema["date_range"],
            "graph_bbox": schema["bbox"],
            "graph_path": graph.get("path"),
            "graph_sha256": graph.get("sha256"),
            "running_questions": running,
            "mock": self.mock,
            "model": self.agent.context.get("model"),
            "version": self.agent.context.get("version"),
            "schema_version": SCHEMA_VERSION,
        }

    def close(self, timeout: float = 10.0) -> None:
        """Cancel running questions, stop the loop and join its thread. Idempotent."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            running = list(self._pending)
        for p in running:
            p.cancel()
        for p in running:
            if p.future is not None:
                try:
                    p.future.result(STOP_GRACE)
                except Exception:
                    pass
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)

    def __enter__(self) -> "AgentRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- internals

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for t in tasks:
                t.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def _finished(self, pending: PendingAnswer) -> None:
        with self._mutex:
            self._pending.discard(pending)

    def _internal_thread(self, thread_id: str) -> str:
        with self._mutex:
            return self._aliases.get(thread_id, thread_id)

    def _reset_thread(self, thread_id: str) -> None:
        """Continue `thread_id` on a fresh conversation after a stopped run (see module doc)."""
        with self._mutex:
            self._aliases[thread_id] = f"{thread_id}~{uuid.uuid4().hex[:8]}"


_shared: dict[tuple[Any, ...], AgentRunner] = {}
_shared_lock = threading.Lock()


def get_runner(graph_path: str | Path | None = None, mcp_config: str | Path | None = None, **kwargs: Any) -> AgentRunner:
    """The process-wide runner for this configuration, created on first use.

    Thread-safe: concurrent first calls build one runner. A runner that was closed is
    replaced. In Streamlit, `st.cache_resource` around `AgentRunner(...)` does the same
    job; this is for callers without such a cache. Shared runners close at exit.
    """
    key = (str(graph_path) if graph_path else None, str(mcp_config) if mcp_config else None,
           tuple(sorted((k, repr(v)) for k, v in kwargs.items())))
    with _shared_lock:
        runner = _shared.get(key)
        if runner is None or runner._closed:
            runner = _shared[key] = AgentRunner(graph_path, mcp_config, **kwargs)
        return runner


@atexit.register
def _close_shared() -> None:
    for runner in list(_shared.values()):
        runner.close(timeout=2)
