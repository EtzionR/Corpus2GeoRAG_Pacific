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


# --- a marked map area (agent spec R102) ---------------------------------------------------------

PEARL_BOX = {"type": "Polygon", "coordinates": [[[-158.05, 21.30], [-157.90, 21.30], [-157.90, 21.42], [-158.05, 21.42], [-158.05, 21.30]]]}


def test_a_drawn_shape_becomes_the_agents_area():
    src = Path(APP).read_text(encoding="utf-8")
    ns: dict = {}
    # only the pure helper, without running the Streamlit page
    start = src.index("def area_from_drawing")
    exec("from typing import Any, Dict, Optional\n" + src[start:src.index("def take_new_drawing")], ns)
    f = ns["area_from_drawing"]
    circle = f({"type": "Feature", "geometry": {"type": "Point", "coordinates": [-157.95, 21.35]}, "properties": {"radius": 25000}})
    assert circle["geometry"] == {"type": "Point", "coordinates": [-157.95, 21.35], "radius_km": 25.0}
    assert f({"type": "Feature", "geometry": PEARL_BOX, "properties": {}})["geometry"] == PEARL_BOX
    assert f({"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}}) is None
    assert f({"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}) is None  # a marker


def test_a_marked_area_is_sent_drawn_and_cleared():
    at = new_app()
    at.session_state["area"] = {"geometry": PEARL_BOX, "label": "a marked polygon"}
    at.run()
    assert any("marked on the map" in c.value for c in at.caption)
    reply = ask(at, "What happened in the marked area?")
    assert reply["area"]["kind"] == "polygon" and reply["geojson"]["features"][0]["properties"]["role"] == "area"
    assert "event:attack_on_pearl_harbor" in reply["answer_ids"]
    at.button(key="clear_area").click().run()
    assert at.session_state.area is None and not any("marked on the map" in c.value for c in at.caption)


# --- operational answers on the map (agent schema 1.7) -------------------------------------------

def _map_helpers():
    """The App's map functions without running the page: its top part (constants, imports, the agent
    loader) and its helper functions, with no runner (names fall back to ids)."""
    src = Path(APP).read_text(encoding="utf-8")
    ns: dict = {"__file__": APP}
    exec(src[:src.index("agent = load_agent()")], ns)
    ns["runner"] = None
    exec(src[src.index("def finish_pending"):src.index("# A question that finished")], ns)
    return ns


def _map_html(at, question):
    reply = ask(at, question)
    assert reply["operational"], reply["answer"]
    m = _map_helpers()["render_agent_map"](reply, "OpenStreetMap (Standard)")
    return reply, _decoded(m.get_root().render())


def _decoded(page: str) -> str:
    """The page as the browser's scripts see it: folium writes marker HTML as JSON strings (\\u003c for <)."""
    return page.replace("\\u003c", "<").replace("\\u003e", ">").replace('\\"', '"').replace("\\u0026", "&")


def test_a_path_is_drawn_as_lettered_stops_and_a_line_with_arrows():
    at = new_app()
    reply, page = _map_html(at, "Show the path of the 1st Marine Division")
    assert "c2g-stop" in page and ">A</div>" in page and ">E</div>" in page
    assert "setText" in page and "➤" in page  # PolyLineTextPath: the direction along the route
    # Leaflet.TextPath only works on SVG lines: on a canvas map it throws and the whole map stays blank
    assert '"preferCanvas": false' in page

    strip = _map_helpers()["timeline_strip"](reply)
    assert strip.startswith("🧭 **A**") and "→ **B**" in strip and "Aug 1942" in strip and "date unknown" in strip


def test_maps_without_a_path_keep_the_canvas_renderer():
    at = new_app()
    reply = ask(at, "Where did the Battle of Midway happen?")
    page = _map_helpers()["render_agent_map"](reply, "OpenStreetMap (Standard)").get_root().render()
    assert '"preferCanvas": true' in page


def test_concurrency_sides_and_sequence_have_their_own_styles():
    at = new_app()
    _, page = _map_html(at, "What else happened during the Battle of Midway?")
    assert "c2g-concurrent" in page and "c2g-ongoing" in page and "c2g-asked" in page
    _, page = _map_html(at, "Who fought on each side at the Battle of Midway?")
    assert "#2563EB" in page and "#D97706" in page and "c2g-inferred" in page and "inferred: through a parent unit" in page
    _, page = _map_html(at, "What came after the Battle of Tarawa in the island-hopping campaign?")
    assert "c2g-sequence" in page and ">2</div>" in page and ">3</div>" in page


def test_operational_text_is_escaped_on_the_map():
    reply = {"geojson": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [160.0, -9.5]},
         "properties": {"id": "x", "name": XSS, "type": "location", "role": "answer", "state": "stop", "label": XSS, "date": XSS}}]},
        "operational": [{"family": "trajectory", "sides": [{"side": 1, "label": XSS}]}]}
    page = _decoded(_map_helpers()["render_agent_map"](reply, "OpenStreetMap (Standard)").get_root().render())
    assert "<script>alert(2)" not in page and "<img src=x" not in page and "&lt;script&gt;" in page
