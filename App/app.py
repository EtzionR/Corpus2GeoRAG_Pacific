import asyncio
import copy
import html
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import folium
import streamlit as st
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# ==========================================
# PAGE CONFIGURATION & STYLING
# ==========================================
st.set_page_config(
    page_title="Corpus2GeoRAG: Pacific Theater (WWII) 🌊",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS matching the presentation's dark starry & sleek aesthetic.
# Static only: no data is ever inserted into an unsafe_allow_html block.
st.markdown("""
<style>
    /* Dark oceanic space theme */
    .stApp {
        background: radial-gradient(ellipse at bottom, #0F172A 0%, #030712 100%);
        color: #E0E6ED;
    }

    /* Main header */
    .main-title {
        font-size: 2.2rem;
        font-weight: 700;
        color: #FFFFFF;
        text-align: center;
        margin-bottom: 0.2rem;
        letter-spacing: 1px;
    }

    .subtitle {
        font-size: 1.05rem;
        color: #94A3B8;
        text-align: center;
        margin-bottom: 1.5rem;
    }

    /* Custom button styling */
    .stButton>button {
        border-radius: 8px;
        background: rgba(30, 41, 59, 0.85);
        color: #E2E8F0;
        border: 1px solid rgba(255, 255, 255, 0.15);
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        background: #0EA5E9;
        color: white;
        border-color: #38BDF8;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# AGENT CONTRACT (see Agent/README.md -> "Output schema (for the UI)")
# ==========================================
AGENT_DIR = Path(__file__).resolve().parent.parent / "Agent"
AGENT_CREATE_TIMEOUT_S = 180
# The agent bounds itself (model deadline x retries x steps); this only stops the UI from hanging forever.
AGENT_ASK_TIMEOUT_S = 600

DEFAULT_CENTER = [15.0, 150.0]
DEFAULT_ZOOM = 3

# The agent's fixed scope message; the mock reuses it so the UI sees the real text.
OFF_TOPIC_MESSAGE = (
    "I can only answer questions about the Pacific theatre of World War II, using the knowledge graph "
    "built from Wikipedia: places, battles, people and organizations, and how they are connected. "
    "For example: \"Where did the Battle of Midway happen?\" or \"Show me all locations connected to Admiral Yamamoto.\""
)

WARNING_LABELS = {
    "low_confidence": "Low confidence",
    "uncertain_match_used": "A weak or ambiguous name match was used",
    "context_mismatch": "The question's date doesn't fit the matched event",
    "model_rewritten_query": "The agent searched a spelling you didn't type",
    "ids_dropped": "Some entities were dropped (not supported by evidence)",
    "sources_unverified": "Some cited sources were not in the evidence",
    "text_coordinates_unverified": "Coordinates in the text could not be verified",
    "approximation_used": "A kind, country or concept was approximated",
    "result_set_unknown": "A result set could not be found",
    "off_topic": "Off-topic question",
    "instruction_text_removed": "Instruction-like text was removed from the sources",
    "structured_output_failed": "The agent replied without structured output",
    "repeated_tool_call": "The agent repeated a graph query",
    "empty_resolution": "The first name lookup found nothing",
    "many_steps": "The agent needed many steps",
    "token_budget": "The question used more tokens than the budget",
}

CONFIDENCE_COLORS = {"high": "green", "medium": "orange", "low": "red"}

ROLE_STYLES = {
    "answer": {"color": "#B91C1C", "fillColor": "#EF4444", "fillOpacity": 0.45, "weight": 3},
    "context": {"color": "#475569", "fillColor": "#94A3B8", "fillOpacity": 0.25, "weight": 2},
}

# Above this many point features the map clusters them (set questions can return up to 1,000).
CLUSTER_THRESHOLD = 30


def blank_reply(**fields: Any) -> Dict[str, Any]:
    """A reply with every contract key present, as the agent guarantees."""
    reply: Dict[str, Any] = {
        "answer": "",
        "answer_ids": [],
        "context_ids": [],
        "sources": [],
        "geojson": {"type": "FeatureCollection", "features": []},
        "tool_calls": [],
        "run_id": uuid.uuid4().hex,
        "confidence": {"level": "low", "basis": {"max_edge_count": 0, "n_source_pages": 0, "uncertain_match_used": False}},
        "interpretation": [],
        "resolved_entities": [],
        "dropped_ids": [],
        "unverified_sources": [],
        "unverified_numbers": [],
        "warnings": [],
        "result_sets": [],
        "dropped_sets": [],
        "error": None,
    }
    reply.update(fields)
    reply["entity_ids"] = list(reply["answer_ids"]) + list(reply["context_ids"])
    return reply


def error_reply(exc_type: str, message: str, stage: str) -> Dict[str, Any]:
    return blank_reply(error={"type": exc_type, "message": message, "stage": stage})


# ==========================================
# TEMPORARY MOCK AGENT
# Stand-in until the real Agent is ready. It returns the Agent's exact output schema
# (GeoJSON in [lon, lat]) so every UI path can be tested. Delete this section once
# the Agent loads; nothing else in the App depends on it.
# ==========================================
MOCK_PLACES: Dict[str, Dict[str, Any]] = {
    "loc:pearl_harbor": {"name": "Pearl Harbor", "aliases": ["pearl harbor"], "geometry": {"type": "Point", "coordinates": [-157.9492, 21.3649]}},
    "loc:midway_atoll": {"name": "Midway Atoll", "aliases": ["midway"], "geometry": {"type": "Point", "coordinates": [-177.3735, 28.2072]}},
    "loc:guadalcanal": {"name": "Guadalcanal", "aliases": ["guadalcanal"], "geometry": {"type": "Polygon", "coordinates": [[[159.6, -9.2], [160.9, -9.2], [160.9, -10.0], [159.6, -10.0], [159.6, -9.2]]]}},
    "loc:henderson_field": {"name": "Henderson Field", "aliases": ["henderson field"], "geometry": {"type": "Point", "coordinates": [160.05, -9.43]}},
    "loc:leyte_gulf": {"name": "Leyte Gulf", "aliases": ["leyte"], "geometry": {"type": "Point", "coordinates": [125.4167, 10.8333]}},
    "loc:okinawa": {"name": "Okinawa", "aliases": ["okinawa"], "geometry": {"type": "Point", "coordinates": [127.8056, 26.3344]}},
    "loc:iwo_jima": {"name": "Iwo Jima", "aliases": ["iwo jima"], "geometry": {"type": "Point", "coordinates": [141.3167, 24.7833]}},
    "loc:coral_sea": {"name": "Coral Sea", "aliases": ["coral sea"], "geometry": {"type": "Point", "coordinates": [152.0, -16.0]}},
    "loc:saipan": {"name": "Saipan", "aliases": ["saipan"], "geometry": {"type": "Point", "coordinates": [145.7480, 15.1789]}},
    "loc:manila": {"name": "Manila", "aliases": ["manila"], "geometry": {"type": "Point", "coordinates": [120.9842, 14.5995]}},
    "loc:tokyo": {"name": "Tokyo", "aliases": ["tokyo"], "geometry": {"type": "Point", "coordinates": [139.6503, 35.6762]}},
    "loc:hiroshima": {"name": "Hiroshima", "aliases": ["hiroshima"], "geometry": {"type": "Point", "coordinates": [132.4553, 34.3853]}},
    "loc:nagasaki": {"name": "Nagasaki", "aliases": ["nagasaki"], "geometry": {"type": "Point", "coordinates": [129.8779, 32.7503]}},
}

MOCK_BATTLES = [
    ("event:battle_of_midway", "Battle of Midway", "loc:midway_atoll"),
    ("event:guadalcanal_campaign", "Guadalcanal campaign", "loc:guadalcanal"),
    ("event:battle_of_leyte_gulf", "Battle of Leyte Gulf", "loc:leyte_gulf"),
    ("event:battle_of_okinawa", "Battle of Okinawa", "loc:okinawa"),
    ("event:battle_of_iwo_jima", "Battle of Iwo Jima", "loc:iwo_jima"),
    ("event:battle_of_the_coral_sea", "Battle of the Coral Sea", "loc:coral_sea"),
    ("event:battle_of_saipan", "Battle of Saipan", "loc:saipan"),
    ("event:battle_of_manila", "Battle of Manila", "loc:manila"),
]

MOCK_WAR_WORDS = ["war", "battle", "attack", "bomb", "where", "who", "located", "connected", "campaign", "invasion", "fleet", "navy", "army"]


def _mock_feature(loc_id: str, role: str, related_to: Optional[str] = None,
                  via: Optional[str] = None, strength: Optional[int] = None) -> Dict[str, Any]:
    place = MOCK_PLACES[loc_id]
    props: Dict[str, Any] = {"id": loc_id, "name": place["name"], "type": "location", "role": role}
    if related_to:
        props["related_to"] = related_to
    if via:
        props["via"] = via
        props["strength"] = strength or 1
    return {"type": "Feature", "geometry": place["geometry"], "properties": props}


def _mock_fc(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _mock_confidence(level: str, edges: int, pages: int) -> Dict[str, Any]:
    return {"level": level, "basis": {"max_edge_count": edges, "n_source_pages": pages, "uncertain_match_used": False}}


class MockAgent:
    """Mimics GeoAgent.ask(); same signature, same output keys."""

    async def ask(self, question: str, thread_id: str = "default") -> Dict[str, Any]:
        q = question.lower()

        if "simulate error" in q:
            return error_reply("ModelDeadlineExceeded", "model call exceeded 90s deadline 2 times", "model_call")

        if "xss test" in q:
            # Untrusted text in the answer and in a place name: both must show as plain text.
            evil = "<img src=x onerror=alert('xss')>"
            feature = _mock_feature("loc:midway_atoll", "answer")
            feature["properties"]["name"] = evil
            return blank_reply(answer=f"Escaping test: {evil} <script>alert('xss')</script> **bold stays Markdown**",
                               answer_ids=["loc:midway_atoll"], geojson=_mock_fc([feature]),
                               confidence=_mock_confidence("low", 1, 1))

        if "guam" in q:
            return blank_reply(
                answer="Did you mean the Battle of Guam (1941) or the Battle of Guam (1944)?",
                tool_calls=[{"name": "search_entities", "args": {"query": "Battle of Guam"}}],
            )

        if "yamamoto" in q:
            paths = [
                ("loc:pearl_harbor", "Isoroku Yamamoto -COMMANDED-> Attack on Pearl Harbor -OCCURRED_AT-> Pearl Harbor", 6),
                ("loc:midway_atoll", "Isoroku Yamamoto -COMMANDED-> Battle of Midway -OCCURRED_AT-> Midway Atoll", 8),
                ("loc:guadalcanal", "Isoroku Yamamoto -COMMANDED-> Guadalcanal campaign -OCCURRED_AT-> Guadalcanal", 3),
            ]
            ids = [p[0] for p in paths]
            return blank_reply(
                answer=("**3 locations** are connected to Admiral Isoroku Yamamoto, through the operations he commanded: "
                        "Pearl Harbor (1941), Midway Atoll (1942) and Guadalcanal (1942-43)."),
                answer_ids=ids, context_ids=["per:yamamoto"], sources=["Isoroku Yamamoto", "Battle of Midway"],
                geojson=_mock_fc([_mock_feature(i, "answer", "per:yamamoto", v, s) for i, v, s in paths]),
                tool_calls=[{"name": "search_entities", "args": {"query": "Yamamoto"}},
                            {"name": "connected_locations", "args": {"entity_id": "per:yamamoto"}}],
                confidence=_mock_confidence("high", 8, 2),
                interpretation=["'Admiral Yamamoto' read as Isoroku Yamamoto"],
                result_sets=[{"handle": "rs-mock01", "label": "locations connected to Isoroku Yamamoto",
                              "total": 3, "on_map": 3, "map_truncated": False}],
            )

        if "guadalcanal" in q and "attack" in q:
            return blank_reply(
                answer=("The **US Marine Corps** attacked Guadalcanal from 7 August 1942; the **Imperial Japanese Army** "
                        "attacked Henderson Field in September-October 1942."),
                answer_ids=["org:usmc", "org:ija"], context_ids=["loc:guadalcanal", "loc:henderson_field"],
                sources=["Guadalcanal campaign"],
                geojson=_mock_fc([_mock_feature("loc:guadalcanal", "answer", "org:usmc"),
                                  _mock_feature("loc:henderson_field", "answer", "org:ija")]),
                tool_calls=[{"name": "search_entities", "args": {"query": "Guadalcanal"}},
                            {"name": "find_relations", "args": {"target_id": "loc:guadalcanal", "relation_type": "ATTACKED", "year": 1942}}],
                confidence=_mock_confidence("high", 5, 2),
            )

        if "battle" in q and any(w in q for w in ["major", "battles", "all"]):
            features = [_mock_feature(loc, "answer", ev, f"{name} -OCCURRED_AT-> {MOCK_PLACES[loc]['name']}", 5)
                        for ev, name, loc in MOCK_BATTLES]
            return blank_reply(
                answer=("The graph has **8 major battles** of the Pacific War. The most important: the Battle of Midway, "
                        "the Guadalcanal campaign and the Battle of Leyte Gulf. All of them are on the map."),
                answer_ids=[ev for ev, _, _ in MOCK_BATTLES], sources=["Pacific War"],
                geojson=_mock_fc(features),
                tool_calls=[{"name": "find_entities", "args": {"type": "event", "kind": "battle"}}],
                confidence=_mock_confidence("medium", 4, 1),
                warnings=["approximation_used"],
                interpretation=["'major' ranked by how much the graph says about each battle (no importance attribute)"],
                result_sets=[{"handle": "rs-mock02", "label": "major battles of the Pacific War",
                              "total": 8, "on_map": 8, "map_truncated": False}],
            )

        found = [loc for loc, place in MOCK_PLACES.items() if any(a in q for a in place["aliases"])]
        if found:
            battle = next(((ev, name) for ev, name, loc in MOCK_BATTLES if loc in found and "battle" in q), None)
            related = battle[0] if battle else None
            names = ", ".join(MOCK_PLACES[f]["name"] for f in found)
            interpretation = (["'Midway' read as Midway Atoll (alias)"]
                              if "loc:midway_atoll" in found and "midway atoll" not in q else [])
            return blank_reply(
                answer=(f"The {battle[1]} happened at **{names}**." if battle else f"Found in the graph: **{names}**."),
                answer_ids=found + ([related] if related else []),
                sources=[battle[1]] if battle else [MOCK_PLACES[f]["name"] for f in found],
                geojson=_mock_fc([_mock_feature(f, "answer", related) for f in found]),
                tool_calls=[{"name": "search_entities", "args": {"query": names}}],
                confidence=_mock_confidence("high" if battle else "medium", 3, 1),
                interpretation=interpretation,
            )

        if any(w in q for w in MOCK_WAR_WORDS):
            return blank_reply(answer="I couldn't find this in the knowledge graph.",
                               tool_calls=[{"name": "search_source_text", "args": {"query": question}}])

        return blank_reply(answer=OFF_TOPIC_MESSAGE, warnings=["off_topic"])


# ==========================================
# AGENT RUNNER (one long-lived event loop per process)
# The agent is async and its model client is bound to the loop it was created on,
# so every call goes through the same background loop instead of asyncio.run().
# ==========================================
class AgentRunner:
    def __init__(self):
        self.mode = "mock"
        self.reason = ""
        self.graph_stats: Optional[Dict[str, Any]] = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="geo-agent-loop", daemon=True)
        self._thread.start()
        try:
            self.agent = self._run(self._create_agent(), AGENT_CREATE_TIMEOUT_S)
            self.mode = "agent"
            self.graph_stats = self._read_graph_stats()
        except Exception as exc:
            self.agent = MockAgent()
            self.reason = f"{type(exc).__name__}: {exc}"

    @staticmethod
    async def _create_agent():
        if str(AGENT_DIR) not in sys.path:
            sys.path.insert(0, str(AGENT_DIR))
        from geo_agent.agent import GeoAgent
        try:
            return await GeoAgent.create()
        except SystemExit as exc:
            # create() calls sys.exit() when OPENROUTER_API_KEY is missing; a SystemExit
            # escaping a task would stop the background loop, so turn it into an error.
            raise RuntimeError(str(exc)) from None

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def _read_graph_stats(self) -> Optional[Dict[str, Any]]:
        try:
            store = self.agent.store
            schema = store.schema()
            return {"nodes": schema["node_count"], "edges": schema["edge_count"],
                    "node_types": schema["node_types"], "geocoded": len(store.geoms)}
        except Exception:
            return None

    def ask(self, question: str, thread_id: str) -> Dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(self.agent.ask(question, thread_id=thread_id), self._loop)
        try:
            return future.result(timeout=AGENT_ASK_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            return error_reply("UITimeout", f"no reply within {AGENT_ASK_TIMEOUT_S}s", "unknown")
        except Exception as exc:  # ask() returns errors as data; this is only a safety net
            return error_reply(type(exc).__name__, str(exc), "unknown")

    def close(self):
        self._loop.call_soon_threadsafe(self._loop.stop)


@st.cache_resource(show_spinner="Loading the GeoRAG agent…")
def get_runner() -> AgentRunner:
    return AgentRunner()


# ==========================================
# SESSION STATE
# ==========================================
if "thread_id" not in st.session_state:
    st.session_state.thread_id = uuid.uuid4().hex  # one conversation per browser session

if "messages" not in st.session_state:
    st.session_state.messages = []

if "last_reply" not in st.session_state:
    st.session_state.last_reply = None  # latest agent reply (reasoning panel)

if "map_reply" not in st.session_state:
    st.session_state.map_reply = None  # latest reply that had map features


def reset_conversation():
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.messages = []
    st.session_state.last_reply = None
    st.session_state.map_reply = None


# ==========================================
# REPLY HELPERS
# ==========================================
def reply_features(res: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not res:
        return []
    return (res.get("geojson") or {}).get("features") or []


def reply_kind(res: Dict[str, Any]) -> str:
    """One of: error, off_topic, clarification (also "not in the graph"), answer."""
    if res.get("error"):
        return "error"
    if "off_topic" in (res.get("warnings") or []):
        return "off_topic"
    if not res.get("answer_ids"):
        return "clarification"
    return "answer"


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~:$])")


def md_text(value: Any) -> str:
    """Escape untrusted short text (names, page titles) so Markdown shows it literally."""
    return _MD_SPECIAL.sub(r"\\\1", str(value))


def confidence_line(res: Dict[str, Any]) -> Optional[str]:
    level = ((res.get("confidence") or {}).get("level") or "").lower()
    if level not in CONFIDENCE_COLORS:
        return None
    return f":{CONFIDENCE_COLORS[level]}[●] Confidence: **{level}**"


def result_set_lines(res: Dict[str, Any]) -> List[str]:
    lines = []
    for rs in res.get("result_sets") or []:
        line = f"**{rs.get('total', '?')}** — {md_text(rs.get('label', 'result set'))}"
        if rs.get("map_truncated"):
            line += f" (map shows {rs.get('on_map', '?')})"
        lines.append(line)
    return lines


def warning_labels(res: Dict[str, Any]) -> List[str]:
    return [WARNING_LABELS.get(w, w) for w in res.get("warnings") or [] if w not in ("off_topic", "low_confidence")]


def render_reply(res: Dict[str, Any]):
    """Render one agent reply inside a chat message. The answer is untrusted Markdown: never raw HTML."""
    kind = reply_kind(res)

    if kind == "error":
        err = res.get("error") or {}
        st.error("The agent couldn't answer this question. Please try again or rephrase it.")
        st.caption(f"Stage: {md_text(err.get('stage', 'unknown'))} · run\\_id: `{res.get('run_id', '')}`")
        return

    if kind == "off_topic":
        st.info(res.get("answer") or OFF_TOPIC_MESSAGE)
        return

    st.markdown(res.get("answer") or "")

    if kind == "clarification":
        st.caption("↩️ Reply below to continue — the agent keeps this conversation's context.")

    for line in res.get("interpretation") or []:
        st.caption(f"🔎 {md_text(line)}")

    meta = [c for c in [confidence_line(res)] if c] + result_set_lines(res)
    if meta:
        st.markdown(" · ".join(meta))

    if res.get("sources"):
        st.caption("📚 Sources: " + ", ".join(md_text(s) for s in res["sources"]))

    labels = warning_labels(res)
    if res.get("unverified_numbers"):
        labels.append("Unverified coordinates in the text: " + ", ".join(str(n) for n in res["unverified_numbers"]))
    if labels:
        st.caption("⚠️ " + " · ".join(md_text(l) for l in labels))

    with st.expander("How I found this"):
        st.caption(f"run\\_id: `{res.get('run_id', '')}`")
        st.json(res.get("tool_calls") or [], expanded=False)
        if res.get("dropped_ids") or res.get("unverified_sources"):
            st.json({"dropped_ids": res.get("dropped_ids") or [],
                     "unverified_sources": res.get("unverified_sources") or []}, expanded=False)


def handle_question(question: str):
    with st.spinner("The agent is searching the knowledge graph…"):
        res = get_runner().ask(question, st.session_state.thread_id)
    st.session_state.messages.append({"role": "user", "content": question})
    st.session_state.messages.append({"role": "assistant", "data": res})
    st.session_state.last_reply = res
    if reply_features(res):
        st.session_state.map_reply = res
    st.rerun()


# ==========================================
# MAP (draws the agent's GeoJSON as-is: [lon, lat], handled by Folium)
# ==========================================
def _iter_positions(coords: Any) -> Iterator[Tuple[float, float]]:
    if isinstance(coords, (list, tuple)) and len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
        yield float(coords[0]), float(coords[1])
    elif isinstance(coords, (list, tuple)):
        for c in coords:
            yield from _iter_positions(c)


def _shift_lon(coords: Any) -> Any:
    """Move western-hemisphere longitudes past 180° so a Pacific set is drawn contiguously."""
    if isinstance(coords, (list, tuple)) and len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
        lon = coords[0] + 360 if coords[0] < 0 else coords[0]
        return [lon, *coords[1:]]
    return [_shift_lon(c) for c in coords]


def prepare_geojson(fc: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[List[List[float]]]]:
    """Display copy of the FeatureCollection plus its bounds ([[s, w], [n, e]]).

    - Every property is HTML-escaped: folium's GeoJson tooltip/popup inserts them with innerHTML.
    - Missing tooltip/popup fields get "" so folium can render every feature.
    - If the features straddle the 180° meridian, longitudes are shifted for display only.
    """
    fc = copy.deepcopy(fc)
    features = [f for f in fc.get("features") or [] if f.get("geometry")]
    positions = [p for f in features for p in _iter_positions(f["geometry"].get("coordinates"))]
    if not positions:
        return {"type": "FeatureCollection", "features": []}, None

    lons = [p[0] for p in positions]
    shifted = [lon + 360 if lon < 0 else lon for lon in lons]
    use_shift = (max(shifted) - min(shifted)) < (max(lons) - min(lons))
    if use_shift:
        for f in features:
            f["geometry"]["coordinates"] = _shift_lon(f["geometry"]["coordinates"])
        lons = shifted

    for f in features:
        props = f.get("properties") or {}
        role = props.get("role") if props.get("role") in ROLE_STYLES else "context"
        strength = props.get("strength")
        f["properties"] = {key: html.escape(str(value)) for key, value in props.items()}
        f["properties"].setdefault("name", "")
        f["properties"].setdefault("via", "")
        f["properties"].setdefault("related_to", "")
        f["properties"]["role"] = role  # whitelisted, used by the style function
        f["properties"]["_radius"] = 6 + min(int(strength), 10) if isinstance(strength, (int, float)) else 8

    lats = [p[1] for p in positions]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    return {"type": "FeatureCollection", "features": features}, bounds


def _feature_style(feature: Dict[str, Any]) -> Dict[str, Any]:
    props = feature.get("properties") or {}
    style = dict(ROLE_STYLES.get(props.get("role"), ROLE_STYLES["context"]))
    style["radius"] = props.get("_radius", 8)
    return style


def render_agent_map(res: Optional[Dict[str, Any]], tile_theme: str) -> folium.Map:
    tiles = {
        "OpenStreetMap (Standard)": "OpenStreetMap",
        "CartoDB Positron (Minimal)": "CartoDB positron",
        "CartoDB Dark Matter (Dark)": "CartoDB dark_matter",
    }.get(tile_theme, "OpenStreetMap")

    fc, bounds = prepare_geojson((res or {}).get("geojson") or {})
    single_point = bool(bounds) and bounds[0] == bounds[1]
    m = folium.Map(location=bounds[0] if single_point else DEFAULT_CENTER,
                   zoom_start=6 if single_point else DEFAULT_ZOOM, tiles=tiles,
                   control_scale=True, prefer_canvas=True, world_copy_jump=True)
    if not fc["features"]:
        return m

    layer = folium.GeoJson(
        fc,
        name="Agent answer",
        marker=folium.CircleMarker(radius=8, fill=True),
        style_function=_feature_style,
        highlight_function=lambda _: {"weight": 4, "fillOpacity": 0.7},
        tooltip=folium.GeoJsonTooltip(fields=["name"], labels=False),
        popup=folium.GeoJsonPopup(fields=["name", "role", "related_to", "via"],
                                  aliases=["Name", "Role", "Drawn for", "Connected via"]),
    )
    n_points = sum(1 for f in fc["features"] if f["geometry"].get("type") == "Point")
    if n_points > CLUSTER_THRESHOLD:
        cluster = MarkerCluster(name="Agent answer (clustered)")
        layer.add_to(cluster)
        cluster.add_to(m)
    else:
        layer.add_to(m)

    if bounds and not single_point:  # a single point keeps the readable zoom set above
        m.fit_bounds(bounds, padding=(30, 30), max_zoom=8)
    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    return m


# ==========================================
# SIDEBAR CONTROLS & GRAPH STATS
# ==========================================
runner = get_runner()

with st.sidebar:
    st.image("https://img.icons8.com/isometric/100/globe.png", width=64)
    st.markdown("### 🌊 Corpus2GeoRAG_Pacific")
    st.markdown("**Pacific & Asian Theaters (WWII)**")
    st.caption("Geospatial RAG on Wikipedia Pacific War & Maritime Datasets")

    st.divider()

    st.markdown("#### ⚙️ Map Configuration")
    tile_choice = st.selectbox(
        "Map Theme:",
        [
            "OpenStreetMap (Standard)",
            "CartoDB Positron (Minimal)",
            "CartoDB Dark Matter (Dark)"
        ],
        index=0
    )

    st.divider()

    st.markdown("#### 🤖 Agent Engine")
    if runner.mode == "agent":
        st.success("🟢 GeoRAG Agent connected")
    else:
        st.warning("🟠 Demo mode — the real Agent is not loaded. Answers come from a local mock.")
        st.caption(f"Reason: {md_text(runner.reason)}")
        if st.button("🔌 Retry Agent connection", use_container_width=True):
            runner.close()
            get_runner.clear()
            st.rerun()

    if runner.graph_stats:
        st.markdown("#### 📊 Knowledge Graph")
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.metric("Entities", f"{runner.graph_stats['nodes']:,}")
            st.metric("Geocoded", f"{runner.graph_stats['geocoded']:,}")
        with col_g2:
            st.metric("Relations", f"{runner.graph_stats['edges']:,}")
        with st.expander("Entity types"):
            st.json(runner.graph_stats["node_types"])

    st.info("Pipeline: `Pacific Wikipedia Corpus → NER → Geocoding → KG → Agentic RAG → Folium`")

    if st.button("🔄 New conversation & reset map", use_container_width=True):
        reset_conversation()
        st.rerun()

# ==========================================
# MAIN PAGE HEADER
# ==========================================
st.markdown('<div class="main-title">Corpus2GeoRAG: Pacific Theater (WWII) 🌊</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Transforms unstructured Wikipedia Pacific War corpora into a geographically-aware knowledge graph and interactive GeoRAG exploration</div>', unsafe_allow_html=True)

# Suggested questions: the Agent's example questions, same path as the chat input
SUGGESTED_QUESTIONS = [
    ("📍 Where was the Battle of Midway?", "Show me places where the Battle of Midway happened"),
    ("⚔️ Who attacked Guadalcanal in 1942?", "Who attacked Guadalcanal in 1942?"),
    ("🧭 Yamamoto's locations", "Show me all locations connected to Admiral Yamamoto"),
    ("🗺️ Major Pacific battles", "Show me major battles in the Pacific War on the map"),
]

st.markdown("##### 💡 Suggested questions:")
for col, (label, question) in zip(st.columns(len(SUGGESTED_QUESTIONS)), SUGGESTED_QUESTIONS):
    with col:
        if st.button(label, use_container_width=True):
            handle_question(question)

# ==========================================
# INTERACTIVE FOLIUM MAP DISPLAY
# ==========================================
st.markdown("---")
col_map, col_info = st.columns([7, 3])

with col_map:
    st.markdown("#### 🗺️ Interactive Geospatial Knowledge Map (Pacific Theater)")
    folium_map_obj = render_agent_map(st.session_state.map_reply, tile_theme=tile_choice)
    st_folium(folium_map_obj, width="100%", height=530, returned_objects=[])
    st.caption("🔴 Answer · ⚪ Context")

with col_info:
    st.markdown("#### 🧠 Agentic Reasoning & Context")
    last = st.session_state.last_reply
    if not last:
        st.markdown("Ready for natural-language Pacific War questions. Ask below or pick a suggestion.")
    else:
        kind = reply_kind(last)
        st.markdown(f"**Reply type:** `{kind}`")
        conf = confidence_line(last)
        if conf and kind in ("answer", "clarification"):
            st.markdown(conf)
        for line in result_set_lines(last):
            st.markdown(f"**Result set:** {line}")
        if last.get("interpretation"):
            st.markdown("**How names were read:**")
            for line in last["interpretation"]:
                st.markdown(f"- {md_text(line)}")
        n_features = len(reply_features(last))
        st.markdown(f"**Answer entities:** {len(last.get('answer_ids') or [])} · "
                    f"**Context:** {len(last.get('context_ids') or [])} · **On map:** {n_features}")
        if kind in ("answer", "clarification") and not n_features and st.session_state.map_reply:
            st.caption("This reply has nothing to draw; the map still shows the previous answer.")
        if last.get("tool_calls"):
            st.markdown("**Graph queries:** " + " ➔ ".join(f"`{md_text(t.get('name', '?'))}`" for t in last["tool_calls"]))
        if last.get("resolved_entities"):
            with st.expander("Resolved entities"):
                st.json(last["resolved_entities"], expanded=False)

# ==========================================
# CHAT & PROMPT INTERACTION BAR
# ==========================================
st.markdown("---")
st.markdown("#### 💬 Ask the Pacific GeoAI Agent")

# Message history: user text and agent answers are rendered as Markdown, never as raw HTML
for msg in st.session_state.messages[-6:]:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.text(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🤖"):
            render_reply(msg.get("data") or {})

user_input = st.chat_input("Ask anything about the Pacific War... (e.g., 'Where did the Battle of Midway happen?', 'Who attacked Guadalcanal in 1942?')")

if user_input:
    handle_question(user_input)
