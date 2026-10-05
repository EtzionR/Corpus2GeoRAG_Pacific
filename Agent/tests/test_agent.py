"""GeoAgent.ask() output and error handling (spec R10, R11). Stub agents, no network."""

import asyncio
from pathlib import Path

import httpx
import openai
import pytest
from langgraph.errors import GraphRecursionError

import geo_agent.agent as agent_mod
from geo_agent.agent import GeoAgent
from geo_agent.graph_store import GraphStore

GRAPH = Path(__file__).resolve().parent.parent / "data" / "sample_graph.json"
OUTPUT_KEYS = {"answer", "entity_ids", "sources", "geojson", "tool_calls", "run_id", "warnings", "error"}


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))


class RaisingAgent:
    def __init__(self, exc):
        self.exc = exc

    async def ainvoke(self, *args, **kwargs):
        raise self.exc


REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


@pytest.mark.parametrize(
    "exc,stage",
    [
        (GraphRecursionError("Recursion limit of 25 reached"), "step_limit"),
        (openai.APIConnectionError(request=REQUEST), "model_call"),
        (openai.APITimeoutError(request=REQUEST), "model_call"),
        (httpx.ConnectError("refused", request=REQUEST), "model_call"),
        (ValueError("tool blew up outside the loop"), "unknown"),
    ],
)
def test_failures_return_error_result(exc, stage):
    out = asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(exc)).ask("q"))
    assert set(out) >= OUTPUT_KEYS
    assert out["error"] == {"type": type(exc).__name__, "message": str(exc), "stage": stage}
    assert out["answer"] == "" and out["entity_ids"] == [] and out["sources"] == []
    assert out["geojson"] == {"type": "FeatureCollection", "features": []}
    assert out["tool_calls"] == [] and len(out["run_id"]) == 32


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_interrupts_are_not_swallowed(exc):
    with pytest.raises(type(exc)):
        asyncio.run(GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(exc)).ask("q"))


def test_cli_exits_nonzero_on_error(monkeypatch, capsys):
    async def fake_create(cls, *args, **kwargs):
        return GeoAgent(GraphStore.from_json(GRAPH), RaisingAgent(TimeoutError("slow")))
    monkeypatch.setattr(GeoAgent, "create", classmethod(fake_create))
    assert asyncio.run(agent_mod._run(["Who attacked Midway?"])) == 1
    assert "Error at stage model_call" in capsys.readouterr().out


def test_model_requests_have_a_timeout(monkeypatch):
    """A stalled provider request must fail (model_call) instead of hanging the run."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.delenv("GEO_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("GEO_AGENT_MAX_RETRIES", raising=False)
    llm = agent_mod.get_llm()
    assert (llm.request_timeout, llm.max_retries) == (60.0, 2)
    monkeypatch.setenv("GEO_AGENT_TIMEOUT", "15")
    monkeypatch.setenv("GEO_AGENT_MAX_RETRIES", "0")
    llm = agent_mod.get_llm()
    assert (llm.request_timeout, llm.max_retries) == (15.0, 0)


def test_env_example_holds_no_secrets():
    """.env.example is committed; real values belong in the gitignored .env."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    values = dict(line.split("=", 1) for line in example.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert values["OPENROUTER_API_KEY"] == "", "remove the API key from .env.example (put it in .env)"
