"""End-to-end tests of the Streamlit App against the agent's mock mode.

No API key, network or .env: with no OPENROUTER_API_KEY the App falls back to
the agent's recorded replies (Agent/geo_agent/mock_fixtures). Run from the repo root:

    pip install -r App/requirements.txt pytest
    pytest App/tests -q
"""

import os
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
APP = str(APP_DIR / "app.py")
AGENT_DIR = APP_DIR.parent / "Agent"

# Before the App (and the agent) are loaded: mock mode, fast replies, no trace files.
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ["GEO_AGENT_MOCK_DELAY"] = "0"
os.environ["GEO_AGENT_TRACE"] = "0"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

from geo_agent.schema import SCHEMA_VERSION, AskResult  # noqa: E402

XSS = "<img src=x onerror=alert(1)> <script>alert(2)</script>"


def new_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception, at.exception
    return at


def suggestion_buttons(at: AppTest):
    return [b for b in at.button if b.key and b.key.startswith("suggestion_")]


def ask(at: AppTest, question: str) -> dict:
    at.chat_input[0].set_value(question).run()
    assert not at.exception, at.exception
    assert at.session_state.pending is None, "the answer should be collected in the same run"
    return at.session_state.last_reply


def kind(reply: dict) -> str:
    if reply["error"]:
        return "error"
    if "off_topic" in reply["warnings"]:
        return "off_topic"
    return "answer" if reply["answer_ids"] else "clarification"


def test_mock_mode_looks_like_the_real_app():
    at = new_app()
    assert not at.warning and not at.error, "no status box in mock mode"
    assert any("Demo mode" in c.value for c in at.caption), "the chat notes it is a demo"
    assert not at.chat_input[0].disabled


def test_every_suggestion_button_gets_its_recorded_reply():
    at = new_app()
    labels = [b.label for b in suggestion_buttons(at)]
    assert len(labels) == 4, "the same four suggestions as with the real agent"
    for label in labels:
        at = new_app()
        next(b for b in suggestion_buttons(at) if b.label == label).click().run()
        assert not at.exception, (label, at.exception)
        reply = at.session_state.last_reply
        assert reply.get("mock") is True, label
        assert "mock_no_fixture" not in reply["warnings"], label


def test_clarification_follow_up_keeps_the_conversation():
    at = new_app()
    thread_id = at.session_state.thread_id
    assert kind(ask(at, "What happened in the battle at Tara?")) == "clarification"
    assert kind(ask(at, "Tarawa")) == "answer", "the follow-up is answered in the same conversation"
    assert at.session_state.thread_id == thread_id
    assert len(at.session_state.messages) == 4


def test_all_reply_kinds_match_the_agent_contract():
    at = new_app()
    questions = {
        "Who attacked Guadalcanal in 1942?": "answer",
        "Where did the Battle of Midway happen?": "answer",
        "Show me all locations connected to Admiral Yamamoto": "answer",
        "Write me a poem about the ocean": "off_topic",
        "Simulate an error": "error",
        "a question with no recording": "clarification",
    }
    for question, expected in questions.items():
        reply = ask(at, question)
        AskResult.model_validate(reply)
        assert reply["schema_version"] == SCHEMA_VERSION
        assert kind(reply) == expected, question
    assert any(reply["run_id"] in c.value for c in at.caption), "an error shows its run_id"


def test_answers_draw_on_the_map_and_link_sources():
    at = new_app()
    reply = ask(at, "Where did the Battle of Midway happen?")
    assert reply["geojson"]["features"]
    assert at.session_state.map_reply is reply
    sources = [c.value for c in at.caption if c.value.startswith("📚 Sources:")]
    assert sources and "https://en.wikipedia.org/wiki/" in sources[-1]


def test_untrusted_text_is_never_rendered_as_html():
    at = new_app()
    ask(at, XSS)
    assert any(XSS in t.value for t in at.text), "the user's question is shown as plain text"
    html_blocks = [m.value for m in at.markdown if m.proto.allow_html]
    assert not any("onerror" in v or "<script" in v for v in html_blocks)


def test_new_conversation_resets_thread_and_history():
    at = new_app()
    ask(at, "Who attacked Guadalcanal in 1942?")
    old_thread = at.session_state.thread_id
    next(b for b in at.button if "New chat" in b.label).click().run()
    assert at.session_state.thread_id != old_thread
    assert at.session_state.messages == []
    assert at.session_state.map_reply is None


@pytest.mark.parametrize("n", [1000])
def test_large_set_across_the_meridian_renders_quickly(n):
    features = [{"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [175.0 + (i % 10) if i % 2 else -175.0 + (i % 10), (i % 40) - 20.0]},
                 "properties": {"id": f"Q{i}", "name": f"<b>Place {i}</b>", "type": "location", "role": "answer"}}
                for i in range(n)]
    reply = {"answer": "", "answer_ids": [f["properties"]["id"] for f in features], "context_ids": [], "entity_ids": [],
             "warnings": [], "error": None, "geojson": {"type": "FeatureCollection", "features": features}}
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["map_reply"] = reply
    at.session_state["last_reply"] = reply
    started = time.time()
    at.run()
    assert not at.exception, at.exception
    assert time.time() - started < 15
