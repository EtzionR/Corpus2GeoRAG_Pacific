"""GeoRAG agent: LLM setup, optional allowlisted MCP tools, agent loop and CLI.

Usage:
    uv run geo-agent "Who attacked Guadalcanal in 1942?"
    uv run geo-agent            # interactive session (keeps conversation memory)

Configuration (environment or .env):
    OPENROUTER_API_KEY   required
    GEO_AGENT_MODEL      OpenRouter model slug (default below)
    GRAPH_PATH           graph JSON (default data/sample_graph.json)
    MCP_CONFIG_PATH      MCP allowlist (default mcp_servers.json)
    GEO_AGENT_MAX_STEPS  LangGraph recursion limit per run (default 25)
    GEO_AGENT_TIMEOUT    seconds per model request before it fails (default 60)
    GEO_AGENT_MAX_RETRIES  retries of a failed or timed-out model request (default 2)
    GEO_AGENT_MODEL_DEADLINE  total seconds per model call, retried once (default 90)
    LOG_LEVEL            console level (default WARNING; INFO shows every step)
    Tracing variables (GEO_AGENT_TRACE, GEO_AGENT_LOG_DIR, ...) are documented in trace.py and README.

Design decisions:
- The graph tools are the pipeline. The agent answers only from graph evidence
  and makes no outbound calls to Wikipedia or geocoders.
- External MCP tools are opt-in. A server is loaded only if it is listed in the
  allowlist file, only its listed `allowed_tools` are kept, and a tool may never
  replace a built-in tool of the same name.
- The LLM returns a small structured answer (text + entity ids + sources). The
  GeoJSON for the map is then built in Python from those ids, so the UI gets
  valid geometry that the LLM never had to reproduce.
- Every `ask()` gets a `run_id` and a trace (see trace.py). Failures are
  returned as data with a failure stage (model_call, tool_call, step_limit,
  post_processing, unknown) instead of raising, so the UI always gets a reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import openai
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field

from rapidfuzz import fuzz

from geo_agent.graph_store import GraphStore, normalize_name
from geo_agent.tools import build_tools
from geo_agent.trace import RunTrace, TraceWriter, diagnostics, env_int, env_on, short_hash, tool_calls_of, trace_warnings

log = logging.getLogger("geo_agent")

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "z-ai/glm-5.2"  # chosen for cost; evals/baseline.json was run with it

SYSTEM_PROMPT = """You are a GEOINT/OSINT analyst assistant for the Pacific theatre of World War II.
You answer questions using ONLY a knowledge graph extracted from Wikipedia, accessed through your tools.
Nodes are entities (location, person, org, event, ...) and edges are typed relations
(e.g. ATTACKED, OCCURRED_AT, CAPTURED, COMMANDED) with dates and mention counts.

How to work:
- Resolve every name the user mentions to an entity id with search_entities before querying relations.
- Name matches come with `match` (name/alias/fuzzy) and `quality`:
  * quality "clear": go on with that entity.
  * quality "uncertain" (a weak fuzzy match, or results marked `ambiguous` that nearly tie) and the entity matters
    to the answer: do NOT answer. Reply with one short clarification question naming the candidates and what
    matched, e.g. "Did you mean the Battle of Guam (1941) or the Battle of Guam (1944)?", and return no entity_ids.
    Skip the question only if the user's question already settles it by year, type or context.
  * no result: say plainly that the graph has no such entity.
  * You may retry search_entities with another spelling or romanization. That spelling is only a query: a result
    counts only if the graph returns it, and it is judged by the same rule.
- Compose tools freely; there is no fixed list of question types. Typical patterns:
  * where did battle X happen -> search_entities -> get_locations
  * who did <relation> to place X in <year> -> search_entities -> find_relations(target_id=..., relation_type=..., year=...)
  * what happened at (lat, lon) -> what_happened_at (widen radius_km if nothing is found)
  * call graph_schema when you are unsure which relation or entity types exist.
- Back claims with evidence: prefer relations with higher counts, and use search_source_text for supporting text.
- Only put entity_ids and sources in the final answer that appeared in tool results; anything else is dropped.
- Quote coordinates only as returned by tools, in decimal degrees (e.g. 28.21, -177.37). Never estimate them.
- If the graph does not contain the answer, say so plainly. Never fill gaps from general knowledge.
- Coordinates are decimal degrees; a user's "(x, y)" is usually (lat, lon); state the interpretation you used.

Final answer fields (all required):
- answer: concise answer for the user, mentioning dates and places.
- answer_ids: ids of the entities that directly answer the question, and nothing else.
  * "who attacked/bombed/captured X": only the side that did it (the attackers), never the defenders.
  * "where did X happen" / "which places ...": the places.
  * "what happened at (lat, lon)" / "what happened in X": the events.
- context_ids: ids of other entities your answer mentions that help on the map: the place asked about,
  defenders, commanders, the battle behind a relation.
- sources: Wikipedia page titles you relied on.
Use [] for answer_ids and context_ids only in a clarification question; when the graph has no answer, answer_ids is [].
Take every id from tool results."""


class AgentAnswer(BaseModel):
    """Structured final answer returned by the LLM.

    Every field is required. With defaults, the JSON schema marks the lists
    optional, and GLM 5.2 sometimes sent only `answer`: correct text, nothing
    on the map (6 of 76 baseline/step-9 eval answers).

    Ids come in two roles (spec R24): `answer_ids` answer the question (for
    "who attacked X", the attackers), `context_ids` are mentioned entities that
    help on the map (the place, defenders, commanders). A flat list couldn't
    tell a defender the answer mentions from a wrong attacker.
    """

    answer: str = Field(description="Answer text for the user")
    answer_ids: list[str] = Field(
        description="Ids (from tool results) of the entities that directly answer the question: for 'who attacked X' "
                    "only the attackers, for 'where' the places, for 'what happened' the events. "
                    "[] only for a clarification question or when the graph has no answer.")
    context_ids: list[str] = Field(
        description="Ids (from tool results) of other entities the answer mentions that help on the map: the place "
                    "asked about, defenders, commanders, related battles. [] if none.")
    sources: list[str] = Field(
        description="Wikipedia page titles (from tool results) the answer relies on. [] only when there are no ids.")

    @property
    def entity_ids(self) -> list[str]:
        """Answer ids then context ids, without duplicates."""
        return list(dict.fromkeys(self.answer_ids + self.context_ids))


# ----------------------------------------------------------------------- LLM


def get_llm() -> ChatOpenAI:
    """Chat model served through OpenRouter's OpenAI-compatible API."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("OPENROUTER_API_KEY is not set (see .env.example)")
    return ChatOpenAI(
        model=os.getenv("GEO_AGENT_MODEL", DEFAULT_MODEL),
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=0,
        # Without these the OpenAI client waits up to 600 s per attempt, so one
        # stalled provider request could hang a run (or an eval) for ~30 minutes.
        timeout=float(os.getenv("GEO_AGENT_TIMEOUT", "60")),
        max_retries=env_int("GEO_AGENT_MAX_RETRIES", 2),
    )


class ModelDeadlineExceeded(TimeoutError):
    """A model call did not finish within the deadline, even after a retry."""


class ModelCallDeadline(AgentMiddleware):
    """Hard total deadline on each model call, with one retry.

    The client timeout is a read timeout: it resets whenever bytes arrive.
    OpenRouter keeps a non-streaming request alive with keep-alive bytes while
    the upstream model is slow, so a hung upstream never trips it. Observed in
    the baseline eval: the request stayed open for minutes while ~175 bytes
    trickled in every 20 s. This middleware cancels the call after `seconds`,
    retries once, then raises ModelDeadlineExceeded, which ask() returns as a
    `model_call` error.
    """

    def __init__(self, seconds: float, attempts: int = 2):
        super().__init__()
        self.seconds = seconds
        self.attempts = max(1, attempts)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        for attempt in range(1, self.attempts + 1):
            try:
                return await asyncio.wait_for(handler(request), self.seconds)
            except asyncio.TimeoutError:
                log.warning("model call exceeded %.0fs deadline (attempt %d/%d)", self.seconds, attempt, self.attempts)
        raise ModelDeadlineExceeded(f"model call exceeded {self.seconds:.0f}s deadline {self.attempts} times")


# ----------------------------------------------------------------------- MCP


def filter_mcp_tools(
    tools: list[BaseTool], allowed: list[str], reserved: set[str], skipped: list[str] | None = None
) -> list[BaseTool]:
    """Keep only allowlisted tools that don't collide with already-registered names.

    Names of rejected tools are appended to `skipped` when given (recorded in run traces).
    """
    kept = []
    for t in tools:
        if t.name not in allowed:
            log.info("MCP tool %s not in allowlist, skipped", t.name)
            if skipped is not None:
                skipped.append(t.name)
        elif t.name in reserved:
            log.warning("MCP tool %s collides with an existing tool, skipped", t.name)
            if skipped is not None:
                skipped.append(t.name)
        else:
            kept.append(t)
            reserved.add(t.name)
    return kept


async def load_mcp_tools(config_path: Path, reserved: set[str], skipped: list[str] | None = None) -> list[BaseTool]:
    """Load tools from the MCP servers listed in the allowlist file.

    File format:
        {"servers": {"<name>": {"transport": "stdio", "command": "...", "args": [...],
                                "allowed_tools": ["tool_a", "tool_b"]}}}
    Other keys besides `allowed_tools` / `enabled` are passed to
    langchain-mcp-adapters as the connection config. A server with no
    `allowed_tools`, or with `"enabled": false`, is ignored.
    """
    if not config_path.exists():
        return []
    servers: dict[str, dict[str, Any]] = json.loads(config_path.read_text()).get("servers", {})
    active = {n: c for n, c in servers.items() if c.get("enabled", True) and c.get("allowed_tools")}
    if not active:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient  # only needed when MCP is used

    connections = {n: {k: v for k, v in c.items() if k not in ("allowed_tools", "enabled")} for n, c in active.items()}
    client = MultiServerMCPClient(connections)
    tools: list[BaseTool] = []
    for name, cfg in active.items():
        try:
            server_tools = await client.get_tools(server_name=name)
        except Exception as exc:  # a broken optional server must not take the agent down
            log.warning("MCP server %s failed to load: %s", name, exc)
            if skipped is not None:
                skipped.append(f"{name}:*")
            continue
        tools += filter_mcp_tools(server_tools, cfg["allowed_tools"], reserved, skipped)
    return tools


# --------------------------------------------------------------------- agent


def classify_failure(exc: BaseException, trace: RunTrace) -> str:
    """Map an exception from the graph run to a failure stage (spec R14)."""
    if isinstance(exc, GraphRecursionError):
        return "step_limit"
    if trace.failed_stage:  # set by on_tool_error / on_llm_error
        return trace.failed_stage
    if isinstance(exc, (openai.APIError, httpx.HTTPError, TimeoutError, ConnectionError)):
        return "model_call"
    return "unknown"


# Evidence check (spec R8). Starting values, see spec Q11/Q12.
COORD_TOLERANCE = 0.01  # degrees: a coordinate in the answer must match a tool value this closely
MAX_FALLBACK_SOURCES = 5
HIGH_CONFIDENCE_EDGE_COUNT = 3  # spec R9 starting rule; not a probability, calibrate on the eval set
HIGH_CONFIDENCE_PAGES = 2
REWRITE_RATIO = 85  # typed text matching the question less than this was chosen by the model (spec R21)
_YEAR_RE = re.compile(r"\b(19[3-4]\d|1950)\b")  # years checked against event dates (spec R22)

# "9.43°S", "160.05 ° E", "-177.37°"
_DEGREE_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*°\s*([NSEW])?", re.IGNORECASE)
# "28.21, -177.37", "28.21 -177.37", "28.21/-177.37"; both numbers need a decimal part
_PAIR_RE = re.compile(r"(?<!\d)(?<!\d\.)(-?\d{1,3}\.\d+)\s*(?:,|/|\s)\s*(-?\d{1,3}\.\d+)(?!\.?\d)")


def turn_messages(messages: list[Any]) -> list[Any]:
    """Messages of the current turn: everything after the last user message."""
    last_user = max((i for i, m in enumerate(messages) if m.type == "human"), default=-1)
    return messages[last_user + 1:]


def _walk(data: Any):
    """Yield every dict nested in a JSON value."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _walk(v)
    elif isinstance(data, list):
        for v in data:
            yield from _walk(v)


def _numbers(data: Any):
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        yield float(data)
    elif isinstance(data, list):
        for v in data:
            yield from _numbers(v)


def tool_results(turn: list[Any]) -> list[tuple[str, Any]]:
    """(tool name, parsed JSON) for this turn's tool messages. Non-JSON content (e.g. MCP text) is skipped."""
    out = []
    for m in turn:
        if m.type != "tool":
            continue
        try:
            out.append((getattr(m, "name", None) or "", json.loads(m.content)))
        except (TypeError, ValueError):
            continue
    return out


def collect_evidence(results: list[tuple[str, Any]]) -> dict[str, Any]:
    """Entity ids, source pages, coordinate values and relations seen in tool results."""
    ids: set[str] = set()
    pages: set[str] = set()
    numbers: list[float] = []
    edges: list[dict[str, Any]] = []
    for _, data in results:
        for d in _walk(data):
            if isinstance(d.get("id"), str):
                ids.add(d["id"])
            if isinstance(d.get("page"), str):
                pages.add(d["page"])
            if isinstance(d.get("sources"), list):
                pages.update(s for s in d["sources"] if isinstance(s, str))
            for key in ("lat", "lon"):
                if isinstance(d.get(key), (int, float)):
                    numbers.append(float(d[key]))
            if "coordinates" in d:
                numbers.extend(_numbers(d["coordinates"]))
            if isinstance(d.get("source"), dict) and isinstance(d.get("target"), dict) and "relation" in d:
                edges.append({"source": d["source"].get("id"), "target": d["target"].get("id"), "count": d.get("count", 1)})
    return {"ids": ids, "pages": pages, "numbers": numbers, "edges": edges}


def coordinates_in_text(text: str) -> list[float]:
    """Decimal-degree coordinates quoted in the answer, with S/W as negative.

    Recognized: numbers with a degree sign (optional hemisphere letter), and pairs
    of decimals separated by a comma, space or slash with |lat| <= 90 and
    |lon| <= 180. A lone decimal such as "20.5 km" and integers such as "1942"
    are not coordinates. Degrees-minutes-seconds is not recognized (spec C17).
    """
    found: list[float] = []
    spans = []
    for m in _DEGREE_RE.finditer(text):
        value = float(m.group(1))
        if (m.group(2) or "").upper() in ("S", "W"):
            value = -abs(value)
        if abs(value) <= 180:
            found.append(value)
        spans.append(m.span())
    rest = list(text)
    for a, b in spans:  # don't read degree-format numbers twice as a pair
        rest[a:b] = " " * (b - a)
    for m in _PAIR_RE.finditer("".join(rest)):
        lat, lon = float(m.group(1)), float(m.group(2))
        if abs(lat) <= 90 and abs(lon) <= 180:
            found += [lat, lon]
    return found


def search_hits(turn: list[Any]) -> list[dict[str, Any]]:
    """search_entities results of this turn, each with the query text that produced it (`typed`)."""
    queries = {c.get("id"): (c.get("args") or {}).get("query", "")
               for m in turn for c in getattr(m, "tool_calls", None) or [] if c["name"] == "search_entities"}
    hits = []
    for m in turn:
        if m.type != "tool" or getattr(m, "name", None) != "search_entities":
            continue
        try:
            results = json.loads(m.content)
        except (TypeError, ValueError):
            continue
        typed = queries.get(getattr(m, "tool_call_id", None), "")
        hits += [{**r, "typed": typed} for r in results if isinstance(r, dict) and "id" in r] if isinstance(results, list) else []
    return hits


def resolution_records(store: GraphStore, kept_ids: list[str], turn: list[Any], question: str) -> list[dict[str, Any]]:
    """How each final entity was found by name (spec R21), with the year check (spec R22).

    One record per final id that came from a search_entities call of this turn:
    the search returned it, and the agent then queried with it (as a tool
    argument) or no other tool returned it. A lower-ranked hit that reached the
    answer through another tool (loc:midway_atoll via get_locations after a
    search for "Battle of Midway") was not resolved from the name. When several
    searches returned an id, the best counts (clear first, then higher score).
    """
    queried: set[str] = set()
    for m in turn:
        for c in getattr(m, "tool_calls", None) or []:
            if c["name"] in ("search_entities", AgentAnswer.__name__):
                continue
            args = c.get("args") or {}
            queried |= {args[k] for k in ("entity_id", "source_id", "target_id") if isinstance(args.get(k), str)}
            queried |= {i for i in args.get("entity_ids") or [] if isinstance(i, str)}
    other_ids = collect_evidence([r for r in tool_results(turn) if r[0] != "search_entities"])["ids"]

    best: dict[str, dict[str, Any]] = {}
    for h in search_hits(turn):
        if h["id"] not in kept_ids or (h["id"] in other_ids and h["id"] not in queried):
            continue
        rank = (h.get("quality") == "clear", h.get("score", 0))
        if h["id"] not in best or rank > best[h["id"]]["_rank"]:
            best[h["id"]] = {**h, "_rank": rank}

    norm_question = normalize_name(question)
    years = sorted({int(y) for y in _YEAR_RE.findall(question)})
    records = []
    for node_id in kept_ids:
        if node_id not in best:
            continue
        h, node = best[node_id], store.nodes[node_id]
        typed = h["typed"]
        rec: dict[str, Any] = {
            "id": node_id,
            "name": node.name,
            "typed": typed,
            "matched_label": h.get("matched"),
            "match": h.get("match"),
            "score": h.get("score"),
            "quality": h.get("quality", "uncertain"),
            "model_rewritten_query": bool(typed) and fuzz.partial_ratio(normalize_name(typed), norm_question) < REWRITE_RATIO,
        }
        start, end = node.attributes.get("start_date"), node.attributes.get("end_date")
        if node.type == "event" and start and end:
            outside = [y for y in years if not int(str(start)[:4]) <= y <= int(str(end)[:4])]
            if outside:
                rec["context_mismatch"] = {"question_year": outside[0], "start_date": start, "end_date": end}
        records.append(rec)
    return records


def confidence(kept_ids: list[str], sources: list[str], edges: list[dict[str, Any]], uncertain_match_used: bool) -> dict[str, Any]:
    """Heuristic support level of an answer (spec R9).

    Supporting edges are relations seen in this turn's tool results that touch a
    final entity; source pages are the answer's verified `sources`.
    - high: an edge with count >= 3 and >= 2 distinct source pages
    - medium: at least one supporting edge or one source page
    - low: otherwise, or whenever an uncertain name match was used
    Thresholds are starting values (spec C5): show it as a label, never as a probability.
    """
    support = [e["count"] for e in edges if e["source"] in kept_ids or e["target"] in kept_ids]
    max_count = max(support, default=0)
    n_pages = len(set(sources))
    if uncertain_match_used:
        level = "low"
    elif max_count >= HIGH_CONFIDENCE_EDGE_COUNT and n_pages >= HIGH_CONFIDENCE_PAGES:
        level = "high"
    elif support or n_pages:
        level = "medium"
    else:
        level = "low"
    return {"level": level, "basis": {"max_edge_count": max_count, "n_source_pages": n_pages,
                                      "uncertain_match_used": uncertain_match_used}}


def finalize_answer(store: GraphStore, parsed: AgentAnswer, messages: list[Any]) -> dict[str, Any]:
    """Turn the model's parsed answer and this run's messages into the UI output.

    Pure function: no I/O, no LLM. Only this turn's tool results count as
    evidence (spec R8):
    - entity_ids keeps ids that exist in the graph and appeared in a tool
      result; the others go to dropped_ids with reason "unknown" or
      "not_in_tool_output" and are not drawn.
    - sources keeps claimed pages that appeared in a tool result; the others
      go to unverified_sources. If the model gave ids but no sources, the
      kept nodes' source pages are used (at most MAX_FALLBACK_SOURCES).
    - Coordinates quoted in the answer that match no tool value (within
      COORD_TOLERANCE) are listed in unverified_numbers. The text is not changed.
    It also records how names were resolved (spec R21, R22):
    - resolved_entities: per final entity found by search_entities, the typed
      query, matched label, match kind, score, quality, and whether the query
      was the model's own spelling (model_rewritten_query).
    - interpretation: a line per entity whose typed name differs from its graph
      name, e.g. "'Chungking' read as Chongqing (alias)", for the UI to show.
    - context_mismatch: a 1930-1950 year in the question outside a resolved
      event's dates. A warning only; the question may be about lead-up or aftermath.
    """
    turn = turn_messages(messages)
    evidence = collect_evidence(tool_results(turn))
    warnings: list[str] = []

    kept, dropped = [], []
    for i in parsed.entity_ids:  # answer ids first, then context ids, deduped
        if i not in store.nodes:
            dropped.append({"id": i, "reason": "unknown"})
        elif i not in evidence["ids"]:
            dropped.append({"id": i, "reason": "not_in_tool_output"})
        else:
            kept.append(i)
    answer_ids = [i for i in dict.fromkeys(parsed.answer_ids) if i in kept]
    context_ids = [i for i in kept if i not in answer_ids]

    sources = [p for p in dict.fromkeys(parsed.sources) if p in evidence["pages"]]
    unverified_sources = [p for p in dict.fromkeys(parsed.sources) if p not in evidence["pages"]]
    if kept and not parsed.sources:
        fallback = [s.page for i in kept for s in store.nodes[i].sources]
        sources = list(dict.fromkeys(fallback))[:MAX_FALLBACK_SOURCES]

    unverified_numbers = [x for x in coordinates_in_text(parsed.answer)
                          if not any(abs(x - n) <= COORD_TOLERANCE for n in evidence["numbers"])]

    question = next((str(m.content) for m in reversed(messages) if m.type == "human"), "")
    resolved = resolution_records(store, kept, turn, question)
    interpretation = [f"'{r['typed']}' read as {r['name']} ({r['match']})"
                      for r in resolved if r["typed"] and normalize_name(r["typed"]) != normalize_name(r["name"])]

    if dropped:
        warnings.append("ids_dropped")
    if unverified_sources:
        warnings.append("sources_unverified")
    if unverified_numbers:
        warnings.append("text_coordinates_unverified")
    uncertain_used = bool(kept) and any(r["quality"] != "clear" for r in resolved)
    conf = confidence(kept, sources, evidence["edges"], uncertain_used)
    if uncertain_used:
        warnings.append("uncertain_match_used")
    if any(r["model_rewritten_query"] for r in resolved):
        warnings.append("model_rewritten_query")
    if any("context_mismatch" in r for r in resolved):
        warnings.append("context_mismatch")
    if kept and conf["level"] == "low":  # clarification and not-in-graph replies have no ids: no warning
        warnings.append("low_confidence")

    # R23: a non-spatial entity (org, person, event) is drawn only at linked places that are
    # final ids or appeared in this turn's tool results, not at every place it ever touched.
    geojson = store.to_geojson(kept, restrict_to=set(kept) | evidence["ids"])
    for f in geojson["features"]:  # R24: highlight what answers the question
        props = f["properties"]
        props["role"] = "answer" if props["id"] in answer_ids or props.get("related_to") in answer_ids else "context"

    return {
        "answer": parsed.answer,
        "answer_ids": answer_ids,
        "context_ids": context_ids,
        "entity_ids": kept,
        "sources": sources,
        "geojson": geojson,
        "tool_calls": [
            {"name": c["name"], "args": c["args"]}
            for m in turn
            for c in getattr(m, "tool_calls", None) or []
            if c["name"] != AgentAnswer.__name__  # structured-output call, not a real tool
        ],
        "warnings": warnings,
        "dropped_ids": dropped,
        "unverified_sources": unverified_sources,
        "unverified_numbers": unverified_numbers,
        "resolved_entities": resolved,
        "interpretation": interpretation,
        "confidence": conf,
    }


def error_result(run_id: str, exc: BaseException, stage: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Output returned instead of raising when a run fails (spec R10)."""
    return {
        "answer": "",
        "answer_ids": [],
        "context_ids": [],
        "entity_ids": [],
        "sources": [],
        "geojson": {"type": "FeatureCollection", "features": []},
        "tool_calls": tool_calls,
        "run_id": run_id,
        "warnings": [],
        "dropped_ids": [],
        "unverified_sources": [],
        "unverified_numbers": [],
        "resolved_entities": [],
        "interpretation": [],
        "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": False}},
        "error": {"type": type(exc).__name__, "message": str(exc), "stage": stage},
    }


class GeoAgent:
    """Wires the graph, tools and LLM together. `ask()` returns a UI-ready dict."""

    def __init__(
        self,
        store: GraphStore,
        agent: Any,
        *,
        model: str = "",
        base_url: str = OPENROUTER_BASE_URL,
        graph_path: str | Path | None = None,
        tool_names: list[str] | None = None,
        mcp_loaded: list[str] | None = None,
        mcp_skipped: list[str] | None = None,
    ):
        self.store = store
        self.agent = agent
        self.max_steps = env_int("GEO_AGENT_MAX_STEPS", 25)
        self.writer = TraceWriter.from_env()
        self.log_questions = env_on("GEO_AGENT_LOG_QUESTIONS", True)
        graph = Path(graph_path) if graph_path else None
        # Run context recorded in every run_start event (spec R12); computed once.
        self.context = {
            "model": model,
            "base_url_host": urlparse(base_url).hostname,
            "system_prompt_hash": short_hash(SYSTEM_PROMPT),
            "graph": {
                "path": str(graph) if graph else None,
                "nodes": len(store.nodes),
                "edges": len(store.edges),
                "sha256": short_hash(graph.read_bytes()) if graph and graph.exists() else None,
            },
            "tools": tool_names or [],
            "mcp_loaded": mcp_loaded or [],
            "mcp_skipped": mcp_skipped or [],
            "step_limit": self.max_steps,
            "version": _package_version(),
        }

    @classmethod
    async def create(
        cls,
        graph_path: str | Path | None = None,
        mcp_config: str | Path | None = None,
        llm: BaseChatModel | None = None,
    ) -> "GeoAgent":
        """Build the agent. `llm` overrides the OpenRouter model (tests pass a fake one)."""
        load_dotenv()
        graph_path = Path(graph_path or os.getenv("GRAPH_PATH", ROOT / "data" / "sample_graph.json"))
        mcp_config = Path(mcp_config or os.getenv("MCP_CONFIG_PATH", ROOT / "mcp_servers.json"))

        store = GraphStore.from_json(graph_path)
        tools = build_tools(store)
        builtin = [t.name for t in tools]
        skipped: list[str] = []
        mcp_tools = await load_mcp_tools(mcp_config, set(builtin), skipped)
        tools += mcp_tools

        agent = create_agent(
            llm or get_llm(),
            tools,
            system_prompt=SYSTEM_PROMPT,
            response_format=AgentAnswer,
            middleware=[ModelCallDeadline(float(os.getenv("GEO_AGENT_MODEL_DEADLINE", "90")))],
            checkpointer=InMemorySaver(),  # per-thread conversation memory
        )
        return cls(
            store,
            agent,
            model=os.getenv("GEO_AGENT_MODEL", DEFAULT_MODEL) if llm is None else type(llm).__name__,
            graph_path=graph_path,
            tool_names=builtin,
            mcp_loaded=[t.name for t in mcp_tools],
            mcp_skipped=skipped,
        )

    async def ask(self, question: str, thread_id: str = "default") -> dict[str, Any]:
        """Run one question and return the UI output.

        Keys: answer, entity_ids, sources, geojson, tool_calls, run_id, warnings,
        error (null on success). A failed run returns an error result instead of
        raising (spec R10). Every run leaves a trace ending in `run_end` (spec R13).
        """
        run_id = uuid.uuid4().hex
        trace = RunTrace(run_id, self.writer)
        trace.emit("run_start", thread_id=thread_id,
                   question=question if self.log_questions else None, **self.context)
        t0 = time.perf_counter()
        out: dict[str, Any] | None = None
        warnings: list[str] = []
        stage: str | None = None
        try:
            try:
                result = await self.agent.ainvoke(
                    {"messages": [{"role": "user", "content": question}]},
                    config={
                        "configurable": {"thread_id": thread_id},
                        "callbacks": [trace],
                        "recursion_limit": self.max_steps,
                    },
                )
            except Exception as exc:
                stage = classify_failure(exc, trace)
                log.error("%s run failed at %s", run_id[:8], stage, exc_info=exc)
                out = error_result(run_id, exc, stage, tool_calls_of(trace.events))
                return out

            parsed: AgentAnswer | None = result.get("structured_response")
            messages = result["messages"]
            if parsed is None:  # fall back to plain text if structured output failed
                raw = str(messages[-1].content) if messages else ""
                trace.emit("structured_output", ok=False, raw_text=raw[: trace.text_chars])
                warnings.append("structured_output_failed")
                parsed = AgentAnswer(answer=raw, answer_ids=[], context_ids=[], sources=[])
            else:
                trace.emit("structured_output", ok=True, answer=parsed.model_dump())

            try:
                out = finalize_answer(self.store, parsed, messages)
            except Exception as exc:
                stage = "post_processing"
                log.error("%s post-processing failed", run_id[:8], exc_info=exc)
                out = error_result(run_id, exc, stage, tool_calls_of(trace.events))
                return out

            warnings += out.pop("warnings")
            warnings += [w for w in trace_warnings(trace.events) if w not in warnings]
            trace.emit(
                "finalize",
                claimed_ids=parsed.entity_ids, kept_ids=out["entity_ids"], dropped_ids=out["dropped_ids"],
                answer_ids=out["answer_ids"], context_ids=out["context_ids"],
                claimed_sources=parsed.sources, kept_sources=out["sources"],
                unverified_sources=out["unverified_sources"], unverified_numbers=out["unverified_numbers"],
                resolved_entities=out["resolved_entities"], interpretation=out["interpretation"],
                confidence=out["confidence"],
                warnings=warnings,
            )
            out.update(run_id=run_id, warnings=warnings, error=None)
            return out
        finally:
            # Always close the trace, even on KeyboardInterrupt (out stays None then).
            error = (out or {}).get("error") if out is not None else {"type": "Interrupted", "message": "run did not finish", "stage": "unknown"}
            if error:
                outcome, failure_stage = "error", error["stage"]
            elif warnings:
                outcome = "ok_with_warnings"
                failure_stage = "structured_output" if "structured_output_failed" in warnings else None
            else:
                outcome, failure_stage = "ok", None
            trace.emit("run_end", outcome=outcome, failure_stage=failure_stage, error=error,
                       latency_ms=round((time.perf_counter() - t0) * 1000),
                       diagnostics=diagnostics(trace.events), warnings=warnings)


def _package_version() -> str | None:
    try:
        return metadata.version("geo-agent")
    except metadata.PackageNotFoundError:
        return None


# ----------------------------------------------------------------------- CLI


def _print(res: dict[str, Any]) -> None:
    if res.get("error"):
        err = res["error"]
        print(f"\nError at stage {err['stage']}: {err['type']}: {err['message']}  (run {res['run_id'][:8]})")
        return
    print(f"\n{res['answer']}\n")
    if res["sources"]:
        print("Sources:", ", ".join(res["sources"]))
    print("Tools:", " -> ".join(c["name"] for c in res["tool_calls"]) or "none")
    print(f"Map: {len(res['geojson']['features'])} feature(s)", [f["properties"]["name"] for f in res["geojson"]["features"]])
    if res.get("warnings"):
        print("Warnings:", ", ".join(res["warnings"]))
    print(f"Run: {res['run_id']}")


async def _run(argv: list[str]) -> int:
    agent = await GeoAgent.create()
    if argv:
        res = await agent.ask(" ".join(argv))
        if os.getenv("GEO_AGENT_JSON"):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            _print(res)
        return 1 if res.get("error") else 0
    thread = str(uuid.uuid4())
    print("GeoRAG agent. Ask about the WWII Pacific theatre (empty line to quit).")
    while question := input("\n> ").strip():
        _print(await agent.ask(question, thread_id=thread))
    return 0


USAGE = """usage:
  geo-agent "question"              ask one question
  geo-agent                         interactive session
  geo-agent --trace [RUN_ID|last]   print the timeline of a recorded run
  geo-agent --replay RUN_ID         re-run a run's tool calls on the current graph (no model)
  geo-agent --report [--since 7d]   summary over trace files"""


def _debug_command(argv: list[str]) -> int:
    """--trace / --replay / --report: read local trace files, no model, no network."""
    from geo_agent.trace import find_run, load_events, parse_since, render_report, render_timeline, replay, summarize

    log_dir = os.getenv("GEO_AGENT_LOG_DIR", "logs")
    events = load_events(log_dir)
    flag, rest = argv[0], argv[1:]

    if flag == "--report":
        since = None
        if rest[:1] == ["--since"] and len(rest) == 2:
            try:
                since = parse_since(rest[1])
            except ValueError as exc:
                print(exc)
                return 2
        elif rest:
            print(USAGE)
            return 2
        print(render_report(summarize(events, since)))
        return 0

    if flag == "--replay" and len(rest) != 1:
        print(USAGE)
        return 2
    run_id = rest[0] if rest else "last"
    run = find_run(events, run_id)
    if run is None:
        print(f"run {run_id!r} not found (or ambiguous) in {log_dir}/")
        return 1
    if flag == "--trace":
        print(render_timeline(run))
        return 0

    # --replay
    graph_path = Path(os.getenv("GRAPH_PATH", ROOT / "data" / "sample_graph.json"))
    store = GraphStore.from_json(graph_path)
    recorded = next((e.get("graph", {}).get("sha256") for e in run if e["event"] == "run_start"), None)
    print(f"Replaying run {run[0]['run_id']}: graph sha recorded {recorded}, current {short_hash(graph_path.read_bytes())}")
    rows = replay(run, {t.name: t for t in build_tools(store)})
    for r in rows:
        print(f"  #{r['seq']:<3} {r['name']:<20} {r['status']:<9} {r['detail']}")
    mismatches = sum(r["status"] == "mismatch" for r in rows)
    print(f"{len(rows)} call(s), {mismatches} mismatch(es)")
    return 1 if mismatches else 0


def cli(argv: list[str]) -> int:
    """Entry point logic; returns the exit status."""
    load_dotenv()
    if argv and argv[0] in ("--trace", "--replay", "--report"):
        return _debug_command(argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return asyncio.run(_run(argv))


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
