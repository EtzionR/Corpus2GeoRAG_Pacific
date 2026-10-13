"""Mock mode: recorded replies for UI work without a key. No network."""

import json

import pytest

import geo_agent.runner as runner_mod
from geo_agent.mock import FIXTURE_DIR, NO_FIXTURE_WARNING, load_fixtures, stale_fixtures
from geo_agent.runner import AgentRunner
from geo_agent.schema import AskResult


@pytest.fixture(autouse=True)
def no_key_no_env(tmp_path, monkeypatch):
    """No API key and no .env anywhere: mock mode must not need them."""
    monkeypatch.setenv("GEO_AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GEO_AGENT_MOCK_DELAY", "0")
    monkeypatch.setattr(runner_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def runner():
    with AgentRunner(mock=True) as r:
        yield r


def test_fixtures_are_up_to_date():
    assert stale_fixtures() == [], "run: uv run python -m geo_agent.mock"


def test_every_fixture_validates():
    fixtures = load_fixtures()
    assert {"answer_guadalcanal", "clarification_guam", "followup_guam_1944", "set_yamamoto", "off_topic_poem",
            "error_model_call"} <= set(fixtures)
    for key, fx in fixtures.items():
        AskResult.model_validate({**fx["reply"], "run_id": "x"})


def test_all_reply_kinds(runner):
    ans = runner.ask_sync("Who attacked Guadalcanal in 1942?", thread_id="a")
    assert ans["mock"] is True and ans["error"] is None and {"org:usmc", "org:ija"} <= set(ans["answer_ids"])
    assert ans["geojson"]["features"] and len(ans["run_id"]) == 32

    clar = runner.ask_sync("Who won the Battle of Guam?", thread_id="b")
    assert clar["answer"].startswith("Did you mean") and clar["answer_ids"] == []
    follow = runner.ask_sync("the 1944 one", thread_id="b")
    assert follow["answer_ids"] == ["org:usmc"] and "event:battle_of_guam_1944" in follow["context_ids"]

    poem = runner.ask_sync("Write me a poem about the ocean", thread_id="c")
    assert poem["warnings"] == ["off_topic"]
    err = runner.ask_sync("Simulate an error", thread_id="d")
    assert err["error"]["stage"] == "model_call" and err["answer"] == ""
    sets = runner.ask_sync("Show me all locations connected to Admiral Yamamoto", thread_id="e")
    assert sets["result_sets"][0]["total"] == 5 and len(sets["geojson"]["features"]) == 5
    for out in (ans, clar, follow, poem, err, sets):
        AskResult.model_validate(out)


def test_matching_ignores_case_and_punctuation(runner):
    assert runner.ask_sync("  who attacked guadalcanal in 1942  ")["answer_ids"]


def test_follow_up_needs_its_question_on_the_same_thread(runner):
    runner.ask_sync("Who won the Battle of Guam?", thread_id="x")
    other = runner.ask_sync("the 1944 one", thread_id="y")
    assert other["warnings"] == [NO_FIXTURE_WARNING]


def test_unknown_question_gets_a_deterministic_default(runner):
    a = runner.ask_sync("What is the capital of France?")
    b = runner.ask_sync("Something else entirely")
    assert a["warnings"] == [NO_FIXTURE_WARNING] and a["answer"] == b["answer"]
    assert "Who attacked Guadalcanal in 1942?" in a["answer"] and "the 1944 one" not in a["answer"]
    assert a["error"] is None and a["geojson"]["features"] == []
    AskResult.model_validate(a)


def test_env_switch_and_timeout(monkeypatch):
    monkeypatch.setenv("GEO_AGENT_MOCK", "1")
    monkeypatch.setenv("GEO_AGENT_MOCK_DELAY", "2")
    with AgentRunner() as r:
        assert r.mock is True
        out = r.ask_sync("Who attacked Guadalcanal in 1942?", timeout=0.2)
    assert out["error"]["stage"] == "timeout"


def test_mock_writes_no_trace(runner, no_key_no_env):
    runner.ask_sync("Who attacked Guadalcanal in 1942?")
    assert not (no_key_no_env / "logs").exists()


def test_fixtures_hold_no_run_ids():
    for path in FIXTURE_DIR.glob("*.json"):
        assert json.loads(path.read_text())["reply"]["run_id"] == ""


def test_health_and_stats_in_mock_mode(runner):
    h = runner.health()
    assert h["ok"] and h["mock"] and h["model"] == "mock" and h["model_configured"] is False
    assert runner.stats()["nodes"] > 0 and runner.stats()["geocoded"] > 0


def test_mock_progress_events(runner):
    seen = []
    out = runner.ask_sync("Show me all locations connected to Admiral Yamamoto", on_progress=seen.append)
    assert [e["name"] for e in seen if e["event"] == "tool_start"] == [c["name"] for c in out["tool_calls"]]
    assert seen[0]["event"] == "run_start" and seen[-1]["event"] == "run_end"
    assert all(e["run_id"] == out["run_id"] and e["label"] for e in seen)
