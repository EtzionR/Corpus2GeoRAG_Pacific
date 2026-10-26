import base64
import copy
import html
import importlib.util
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import folium
import streamlit as st
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# ==========================================
# PAGE CONFIGURATION & STYLING
# ==========================================
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

st.set_page_config(
    page_title="Corpus2GeoRAG: Pacific Theater (WWII)",
    page_icon=ASSETS_DIR / "logo_icon.svg",
    layout="wide",
    initial_sidebar_state="collapsed"
)
st.logo(str(ASSETS_DIR / "logo_lockup.svg"), icon_image=str(ASSETS_DIR / "logo_mark.svg"), size="large")

# Custom CSS matching the presentation's dark starry & sleek aesthetic.
# Static only: no data is ever inserted into an unsafe_allow_html block.
st.markdown("""
<style>
    /* Dark oceanic space theme */
    .stApp {
        background: radial-gradient(ellipse at bottom, #0F172A 0%, #030712 100%);
        color: #E0E6ED;
    }

    /* Less empty space above the header */
    .block-container {
        padding-top: 4.5rem;  /* clears Streamlit's 3.75rem top bar so the logo isn't cut */
    }

    /* Main header: logo beside the title, one compact row */
    .main-header {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.8rem;
    }

    .main-title {
        font-size: 1.9rem;
        font-weight: 700;
        color: #FFFFFF;
        letter-spacing: 1px;
    }

    .subtitle {
        font-size: 0.9rem;
        color: #94A3B8;
        text-align: center;
        margin-bottom: 0.1rem;
    }

    /* Top bar: centered and narrower than the page */
    .st-key-top_bar {
        max-width: 960px;
        margin: 0 auto;
    }

    .pipeline-line {
        font-size: 0.8rem;
        color: #94A3B8;
        text-align: center;
        margin-bottom: 0.5rem;
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
# AGENT CONTRACT (see Agent/README.md -> "Output schema" and "Using the agent from the UI",
# and Agent/docs/UI_TEAM.md)
# ==========================================
AGENT_DIR = Path(__file__).resolve().parent.parent / "Agent"

# Settings the agent reads from the environment; on Streamlit Cloud they come from st.secrets.
SECRET_ENV_KEYS = ("OPENROUTER_API_KEY", "GEO_AGENT_MODEL", "GEO_AGENT_MOCK", "GRAPH_PATH", "GEO_AGENT_RUN_TIMEOUT")

POLL_INTERVAL_S = 0.2

DEFAULT_CENTER = [15.0, 150.0]
DEFAULT_ZOOM = 3

# The same buttons with the real agent and in mock mode: each question has a recorded
# reply in Agent/geo_agent/mock_fixtures/*.json, so the demo answers every one.
SUGGESTED_QUESTIONS = [
    ("📍 Where was the Battle of Midway?", "Where did the Battle of Midway happen?"),
    ("⚔️ Who attacked Guadalcanal in 1942?", "Who attacked Guadalcanal in 1942?"),
    ("🧭 Yamamoto's locations", "Show me all locations connected to Admiral Yamamoto"),
    ("🗺️ Major Pacific battles", "Show me major battles in the Pacific War on the map"),
]
SUGGESTIONS_PER_ROW = 4

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
    "instruction_text_removed": "Instruction-like text was removed from the sources",
    "structured_output_failed": "The agent replied without structured output",
    "repeated_tool_call": "The agent repeated a graph query",
    "empty_resolution": "The first name lookup found nothing",
    "many_steps": "The agent needed many steps",
    "token_budget": "The question used more tokens than the budget; the answer may be incomplete",
    "mock_no_fixture": "Mock mode has no recorded reply for this question",
}
# Shown in their own way, not in the generic warning line.
SPECIAL_WARNINGS = {"off_topic", "low_confidence", "nothing_on_map", "ungrounded_names"}

ERROR_MESSAGES = {
    "busy": "Another question is still running in this conversation. Wait for its answer.",
    "timeout": "The question took too long and was stopped. The conversation continues without its earlier context.",
    "cancelled": "The question was stopped. The conversation continues without its earlier context.",
}

CONFIDENCE_COLORS = {"high": "green", "medium": "orange", "low": "red"}

ROLE_STYLES = {
    "answer": {"color": "#991B1B", "fillColor": "#B91C1C", "fillOpacity": 0.45, "weight": 3},
    "context": {"color": "#475569", "fillColor": "#94A3B8", "fillOpacity": 0.25, "weight": 2},
}
ROLE_LABELS = {"answer": "Answer", "context": "Context"}

# Above this many point features the map clusters them (set questions can return up to 1,000).
CLUSTER_THRESHOLD = 30

# Map icon standard: one icon per entity type. A place drawn for a person, event or org
# (geometry_source "related") shows that entity's icon. Inner SVG of a 24x24 stroke icon.
ENTITY_TYPES = ["location", "event", "person", "org", "other"]
TYPE_ALIASES = {"misc": "other"}  # the 10K graph says MISC where the sample says other
TYPE_LABELS = {"location": "Locations", "event": "Events", "person": "People", "org": "Organizations",
               "other": "Other"}
TYPE_SINGULAR = {"location": "Location", "event": "Event", "person": "Person", "org": "Organization",
                 "other": "Other"}
TYPE_ICONS = {
    "location": '<path d="M12 21s-6-5.6-6-10.2a6 6 0 1 1 12 0C18 15.4 12 21 12 21z"/><circle cx="12" cy="10.8" r="2.2"/>',
    "event": '<path d="M5 5l11 11"/><path d="M19 5L8 16"/><path d="M14 18l4-4"/><path d="M10 18l-4-4"/>'
             '<path d="M17 17l2.5 2.5"/><path d="M7 17l-2.5 2.5"/>',
    "person": '<circle cx="12" cy="8" r="3.6"/><path d="M5 20.5c0-3.9 3.1-7 7-7s7 3.1 7 7"/>',
    "org": '<path d="M6 21V3.5"/><path d="M6 4h11.5l-2.3 4 2.3 4H6"/>',
    "other": '<path d="M12 3.5l8.5 8.5-8.5 8.5L3.5 12z"/>',
}
MARKER_SIZE = 30

# Styles for the type markers and the legend, added to the map's own page.
MAP_CSS = """
<style>
.c2g-marker { width: 30px; height: 30px; box-sizing: border-box; border-radius: 50%;
  display: flex; align-items: center; justify-content: center; border: 2.5px solid; background: transparent;
  filter: drop-shadow(0 0 1px #fff) drop-shadow(0 0 1.5px #fff); }  /* hollow; a white halo keeps it readable on any tile */
.c2g-marker svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2.4;
  stroke-linecap: round; stroke-linejoin: round; }
.c2g-answer { color: #B91C1C; border-color: #B91C1C; }
.c2g-context { color: #475569; border-color: #64748B; }
.c2g-related { border-style: dashed; }
.c2g-legend { position: fixed; right: 10px; bottom: 26px; z-index: 1000; background: rgba(13,20,22,.92);
  color: #E6EEEA; border-radius: 8px; padding: 8px 10px; font: 12px/1.35 system-ui, sans-serif;
  box-shadow: 0 1px 6px rgba(0,0,0,.4); }
.c2g-legend-row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
.c2g-legend .c2g-marker { width: 20px; height: 20px; border-width: 1.5px; filter: none; }
.c2g-legend .c2g-answer { color: #F87171; border-color: #F87171; }
.c2g-legend .c2g-marker svg { width: 12px; height: 12px; }
.c2g-legend-note { color: #8FA39B; margin-top: 5px; border-top: 1px solid #23343A; padding-top: 5px; }
</style>
"""


# ==========================================
# AGENT CONNECTION
# The agent ships its own runner (geo_agent.runner.AgentRunner): one background event loop,
# per-question timeout, one question at a time per conversation, progress events, and a
# mock mode with replies recorded from the real code. The app only picks the mode.
# ==========================================
def _load_secrets_into_env():
    try:
        for key in SECRET_ENV_KEYS:
            if key in st.secrets and key not in os.environ:
                os.environ[key] = str(st.secrets[key])
    except Exception:  # no secrets file: environment / Agent/.env only
        pass


def _ensure_agent_importable():
    """Use the installed geo_agent package, or the Agent folder next to the App in this repo."""
    if importlib.util.find_spec("geo_agent") is None and AGENT_DIR.is_dir():
        sys.path.insert(0, str(AGENT_DIR))


@st.cache_resource(show_spinner="Loading the GeoRAG agent…")
def load_agent() -> Dict[str, Any]:
    """One runner per process. mode: "agent", "mock" or "unavailable"; reason says why it isn't "agent"."""
    _load_secrets_into_env()
    try:
        _ensure_agent_importable()
        from geo_agent.agent import AgentConfigError
        from geo_agent.runner import AgentRunner
    except Exception as exc:
        return {"runner": None, "mode": "unavailable",
                "reason": f"the agent package could not be imported ({type(exc).__name__}: {exc})"}

    try:
        runner = AgentRunner()
        if runner.mock:
            return {"runner": runner, "mode": "mock", "reason": "GEO_AGENT_MOCK is set"}
        return {"runner": runner, "mode": "agent", "reason": ""}
    except AgentConfigError as exc:
        reason = str(exc)
    except Exception as exc:
        reason = f"the agent failed to start ({type(exc).__name__}: {exc})"

    try:
        return {"runner": AgentRunner(mock=True), "mode": "mock", "reason": reason}
    except Exception as exc:
        return {"runner": None, "mode": "unavailable",
                "reason": f"{reason}; mock mode also failed ({type(exc).__name__}: {exc})"}


agent = load_agent()
runner = agent["runner"]

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

if "pending" not in st.session_state:
    # The running question's PendingAnswer. Kept here so a click while the agent works
    # (which reruns the script) doesn't lose the answer: every run checks it.
    st.session_state.pending = None


def finish_pending():
    res = st.session_state.pending.result()
    st.session_state.pending = None
    st.session_state.messages.append({"role": "assistant", "data": res})
    st.session_state.last_reply = res
    if reply_features(res):
        st.session_state.map_reply = res


def submit_question(question: str):
    if runner is None or st.session_state.pending is not None:
        return
    st.session_state.messages.append({"role": "user", "content": question})
    st.session_state.pending = runner.submit(question, st.session_state.thread_id)
    st.rerun()


def reset_conversation():
    if st.session_state.pending is not None:
        st.session_state.pending.cancel()
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.messages = []
    st.session_state.last_reply = None
    st.session_state.map_reply = None
    st.session_state.pending = None


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


def safe_url(url: Any) -> Optional[str]:
    """The URL if it is http(s), percent-encoded so it can't break out of a Markdown link."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return quote(url.strip(), safe=":/?#@!$&'*+,;=%~-._")


def md_link(label: Any, url: Any) -> str:
    target = safe_url(url)
    return f"[{md_text(label)}]({target})" if target else md_text(label)


def wikipedia_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")


def source_links(res: Dict[str, Any]) -> List[str]:
    """Wikipedia pages (`sources`) plus other provenance records (`source_refs`), as safe links."""
    links, seen = [], set()
    for ref in res.get("source_refs") or []:
        page = ref.get("page") or ""
        label = page or f"{ref.get('provider', 'record')} {ref.get('record_id', '')}".strip()
        provider = (ref.get("provider") or "").lower()
        if provider and provider != "wikipedia":
            label = f"{ref.get('provider')}: {label}"
        key = (label, ref.get("url"))
        if key not in seen:
            seen.add(key)
            links.append(md_link(label, ref.get("url")))
    ref_pages = {ref.get("page") for ref in res.get("source_refs") or [] if (ref.get("provider") or "").lower() == "wikipedia"}
    for title in res.get("sources") or []:
        if title not in ref_pages:
            links.append(md_link(title, wikipedia_url(title)))
    return links


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
    return [WARNING_LABELS.get(w, w) for w in res.get("warnings") or [] if w not in SPECIAL_WARNINGS]


def render_reply(res: Dict[str, Any]):
    """Render one agent reply inside a chat message. The answer is untrusted Markdown: never raw HTML."""
    kind = reply_kind(res)
    if res.get("mock"):
        st.caption("🧪 Recorded demo reply (mock mode)")

    if kind == "error":
        err = res.get("error") or {}
        stage = err.get("stage", "unknown")
        message = ERROR_MESSAGES.get(stage, "The agent couldn't answer this question. Please try again or rephrase it.")
        if stage == "model_call" and ("Authentication" in str(err.get("type")) or "401" in str(err.get("message"))):
            message = "The model provider rejected the API key. Check OPENROUTER_API_KEY in Agent/.env or the app's secrets."
        (st.warning if stage in ERROR_MESSAGES else st.error)(message)
        st.caption(f"Stage: {md_text(stage)} · run\\_id: `{res.get('run_id', '')}`")
        return

    if kind == "off_topic":
        st.info(res.get("answer") or "")
        return

    st.markdown(res.get("answer") or "")

    if kind == "clarification":
        st.caption("↩️ Reply below to continue — the agent keeps this conversation's context.")

    for line in res.get("interpretation") or []:
        st.caption(f"🔎 {md_text(line)}")

    meta = [c for c in [confidence_line(res)] if c] + result_set_lines(res)
    if meta:
        st.markdown(" · ".join(meta))

    if "nothing_on_map" in (res.get("warnings") or []):
        st.caption("🗺️ No location in the graph for this answer.")

    if res.get("ungrounded_names"):
        st.caption("❔ Not from the graph (the model's own knowledge): "
                   + ", ".join(md_text(n) for n in res["ungrounded_names"]))

    links = source_links(res)
    if links:
        st.caption("📚 Sources: " + " · ".join(links))

    labels = warning_labels(res)
    if res.get("unverified_numbers"):
        labels.append("Unverified coordinates in the text: " + ", ".join(str(n) for n in res["unverified_numbers"]))
    if labels:
        st.caption("⚠️ " + " · ".join(md_text(l) for l in labels))

    with st.expander("How I found this"):
        st.caption(f"run\\_id: `{res.get('run_id', '')}` · schema {md_text(res.get('schema_version', '?'))}")
        st.json(res.get("tool_calls") or [], expanded=False)
        if res.get("dropped_ids") or res.get("unverified_sources"):
            st.json({"dropped_ids": res.get("dropped_ids") or [],
                     "unverified_sources": res.get("unverified_sources") or []}, expanded=False)


def entity_label(res: Dict[str, Any], entity_id: str) -> str:
    """A display name for an id, from this reply's features or resolved names."""
    for f in reply_features(res):
        props = f.get("properties") or {}
        if props.get("id") == entity_id:
            return str(props.get("name") or entity_id)
    for ent in res.get("resolved_entities") or []:
        if ent.get("id") == entity_id:
            return str(ent.get("name") or entity_id)
    return entity_id


def render_entity_card(entity_id: str):
    """Details of one entity from the loaded graph. The excerpt is source text: shown as plain text."""
    card = runner.get_entity_feature(entity_id) if runner else None
    if not card:
        st.caption("No details in the graph for this entity.")
        return
    st.markdown(f"**{md_text(card.get('name', entity_id))}** · {md_text(card.get('type', ''))}")
    if card.get("excerpt"):
        st.text(card["excerpt"])
    sources = [md_link(s.get("page") or "source", s.get("url")) for s in card.get("sources") or []]
    if sources:
        st.caption("📚 " + " · ".join(sources))


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
        related = props.get("geometry_source") == "related"
        f["properties"] = {key: html.escape(str(value)) for key, value in props.items()}
        for key in ("name", "via", "related_to", "geometry_source"):
            f["properties"].setdefault(key, "")
        f["properties"]["role"] = role  # whitelisted, used by the style function
        f["properties"]["_dash"] = "6 4" if related else ""
        f["properties"]["_radius"] = 6 + min(int(strength), 10) if isinstance(strength, (int, float)) else 8

    lats = [p[1] for p in positions]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    return {"type": "FeatureCollection", "features": features}, bounds


def _feature_style(feature: Dict[str, Any]) -> Dict[str, Any]:
    props = feature.get("properties") or {}
    style = dict(ROLE_STYLES.get(props.get("role"), ROLE_STYLES["context"]))
    style["radius"] = props.get("_radius", 8)
    if props.get("_dash"):  # drawn at a linked place, not the entity's own geometry
        style["dashArray"] = props["_dash"]
    return style


def _entity_type(raw: Any) -> str:
    """One of ENTITY_TYPES for a graph type string (anything unknown is "other")."""
    t = str(raw or "").strip().lower()
    t = TYPE_ALIASES.get(t, t)
    return t if t in ENTITY_TYPES else "other"


def _marker_html(etype: str, role: str, related: bool) -> str:
    classes = f"c2g-marker c2g-{role}" + (" c2g-related" if related else "")
    return f'<div class="{classes}"><svg viewBox="0 0 24 24" aria-hidden="true">{TYPE_ICONS[etype]}</svg></div>'


def _legend_html(counts: Dict[str, int], has_related: bool) -> str:
    """Static legend: whitelisted labels, integer counts and our own icons only."""
    rows = "".join(
        f'<div class="c2g-legend-row">{_marker_html(t, "answer", False)}<span>{TYPE_LABELS[t]} · {counts[t]}</span></div>'
        for t in ENTITY_TYPES if counts.get(t))
    note = ('<div class="c2g-legend-note">Red: answer · gray: context'
            + ("<br>Dashed: shown at a linked place" if has_related else "") + "</div>")
    return f'<div class="c2g-legend">{rows}{note}</div>'


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

    # Each feature gets the type of what it stands for: its own type, or, for a place drawn
    # on behalf of a person, event or org, that entity's type and name (from the graph).
    cards: Dict[str, Optional[Dict[str, Any]]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {t: [] for t in ENTITY_TYPES}
    for f in fc["features"]:
        props = f["properties"]
        related_id = html.unescape(props.get("related_to") or "")
        etype = _entity_type(html.unescape(props.get("type") or ""))
        props["drawn_for"] = "—"
        props["type_label"] = TYPE_SINGULAR[etype]
        if related_id:
            if related_id not in cards:
                cards[related_id] = runner.get_entity_feature(related_id) if runner else None
            card = cards[related_id] or {}
            rtype = _entity_type(card.get("type") or related_id.split(":")[0])
            etype = rtype  # the icon shows what the place stands for
            props["drawn_for"] = html.escape(f"{card.get('name') or related_id} ({TYPE_SINGULAR[rtype]})")
            props["type_label"] = "Location (linked place)"
        props["_type"] = etype
        props["role_label"] = ROLE_LABELS[props["role"]]
        props["via"] = props.get("via") or "—"
        groups[etype].append(f)

    n_points = sum(1 for f in fc["features"] if f["geometry"].get("type") in ("Point", "MultiPoint"))
    tooltip_fields = dict(fields=["name"], labels=False)
    popup_fields = dict(fields=["name", "type_label", "role_label", "drawn_for", "via"],
                        aliases=["Name", "Type", "Role", "Shown for", "Connected via"])

    # One toggleable layer per type; inside it, one GeoJson per marker look (role x linked place).
    for etype in ENTITY_TYPES:
        feats = groups[etype]
        if not feats:
            continue
        group = folium.FeatureGroup(name=f"{TYPE_LABELS[etype]} ({len(feats)})", show=True).add_to(m)
        parent = MarkerCluster().add_to(group) if n_points > CLUSTER_THRESHOLD else group
        points = [f for f in feats if f["geometry"].get("type") in ("Point", "MultiPoint")]
        shapes = [f for f in feats if f not in points]
        for role in ROLE_LABELS:
            for related in (False, True):
                subset = [f for f in points if f["properties"]["role"] == role and bool(f["properties"]["_dash"]) == related]
                if not subset:
                    continue
                icon = folium.DivIcon(html=_marker_html(etype, role, related),
                                      icon_size=(MARKER_SIZE, MARKER_SIZE),
                                      icon_anchor=(MARKER_SIZE // 2, MARKER_SIZE // 2))
                folium.GeoJson(
                    {"type": "FeatureCollection", "features": subset},
                    marker=folium.Marker(icon=icon),
                    tooltip=folium.GeoJsonTooltip(**tooltip_fields),
                    popup=folium.GeoJsonPopup(**popup_fields),
                ).add_to(parent)
        if shapes:
            folium.GeoJson(
                {"type": "FeatureCollection", "features": shapes},
                style_function=_feature_style,
                highlight_function=lambda _: {"weight": 4, "fillOpacity": 0.7},
                tooltip=folium.GeoJsonTooltip(**tooltip_fields),
                popup=folium.GeoJsonPopup(**popup_fields),
            ).add_to(group)

    counts = {t: len(groups[t]) for t in ENTITY_TYPES}
    has_related = any(f["properties"]["_dash"] for f in fc["features"])
    m.get_root().header.add_child(folium.Element(MAP_CSS))
    m.get_root().html.add_child(folium.Element(_legend_html(counts, has_related)))

    if bounds and not single_point:  # a single point keeps the readable zoom set above
        m.fit_bounds(bounds, padding=(30, 30), max_zoom=8)
    folium.LayerControl(position="topright", collapsed=False).add_to(m)
    return m

    layer = folium.GeoJson(
        fc,
        name="Agent answer",
        marker=folium.CircleMarker(radius=8, fill=True),
        style_function=_feature_style,
        highlight_function=lambda _: {"weight": 4, "fillOpacity": 0.7},
        tooltip=folium.GeoJsonTooltip(fields=["name"], labels=False),
        popup=folium.GeoJsonPopup(fields=["name", "role", "related_to", "geometry_source", "via"],
                                  aliases=["Name", "Role", "Drawn for", "Geometry", "Connected via"]),
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


# A question that finished while the script wasn't watching (e.g. during a rerun) is collected here.
if st.session_state.pending is not None and st.session_state.pending.done():
    finish_pending()

busy = st.session_state.pending is not None or (runner is not None and runner.is_busy(st.session_state.thread_id))

# ==========================================
# MAIN PAGE HEADER
# ==========================================
_LOGO_B64 = base64.b64encode((ASSETS_DIR / "logo_mark.svg").read_bytes()).decode("ascii")
st.markdown(f'<div class="main-header"><img src="data:image/svg+xml;base64,{_LOGO_B64}" alt="" width="60" height="60">'
            '<span class="main-title">Pacific Theater (WWII)</span></div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Transforms unstructured Wikipedia Pacific War corpora into a geographically-aware knowledge graph and interactive GeoRAG exploration</div>', unsafe_allow_html=True)

# ==========================================
# AGENT STATUS: shown only when something is broken (agent unavailable, failed health check).
# Mock mode looks the same as the real agent; the chat only notes it in one line.
# ==========================================
tile_choice = "OpenStreetMap (Standard)"

health = runner.health() if runner else {}
if agent["mode"] == "unavailable" or (agent["mode"] == "agent" and not health.get("ok")):
    with st.container(border=True, key="top_bar"):
        col_status, col_retry = st.columns([6, 2])
        with col_status:
            if agent["mode"] == "agent":
                st.warning("🟠 GeoRAG Agent loaded, but its health check failed")
            else:
                st.error("🔴 Agent unavailable — questions can't be answered.")
            if agent["reason"]:
                st.caption(f"Reason: {md_text(agent['reason'])}")
        with col_retry:
            if agent["mode"] != "agent":
                if st.button("🔌 Retry Agent", use_container_width=True, disabled=busy):
                    if runner is not None:
                        runner.close()
                    load_agent.clear()
                    st.rerun()

st.markdown('<div class="pipeline-line">Pipeline: Pacific Wikipedia Corpus → NER → Geocoding → KG → Agentic RAG → Folium</div>',
            unsafe_allow_html=True)

st.markdown("##### 💡 Suggested questions:")
suggestions = SUGGESTED_QUESTIONS

for row_start in range(0, len(suggestions), SUGGESTIONS_PER_ROW):
    row = suggestions[row_start:row_start + SUGGESTIONS_PER_ROW]
    for i, (col, (label, question)) in enumerate(zip(st.columns(SUGGESTIONS_PER_ROW), row), start=row_start):
        with col:
            if st.button(label, key=f"suggestion_{i}_{question}", use_container_width=True,
                         disabled=busy or runner is None):
                submit_question(question)

# ==========================================
# INTERACTIVE FOLIUM MAP (LEFT) & CHAT (RIGHT)
# ==========================================
MAP_HEIGHT = 720
CHAT_HEIGHT = MAP_HEIGHT - 240  # leaves room for the chat heading, the input box and the New chat button

col_map, col_chat = st.columns([8, 4])

with col_chat:
    st.markdown("#### 💬 Ask the GeoAI Agent")
    chat_box = st.container(height=CHAT_HEIGHT, border=True)
    with chat_box:
        if not st.session_state.messages:
            st.caption("Ask a question in English below or pick a suggestion. Answers appear here, and their places on the map.")
            if agent["mode"] == "mock":
                st.caption("Demo mode: recorded answers to the suggested questions.")
        # Message history: user text and agent answers are rendered as Markdown, never as raw HTML
        for msg in st.session_state.messages[-20:]:
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.text(msg["content"])
            else:
                with st.chat_message("assistant", avatar="🤖"):
                    render_reply(msg.get("data") or {})

    user_input = st.chat_input(
        "Ask anything...",
        disabled=busy or runner is None,
    )
    if user_input:
        submit_question(user_input)
    if st.button("New chat", help="Start a new conversation", use_container_width=True):
        reset_conversation()
        st.rerun()

with col_map:
    folium_map_obj = render_agent_map(st.session_state.map_reply, tile_theme=tile_choice)
    map_state = st_folium(folium_map_obj, width="100%", height=MAP_HEIGHT, key="agent_map",
                          returned_objects=["last_active_drawing"])
    st.caption("Icons show what each place stands for: a place, event, person, organization or other · "
               "click one for its details · use the box at the top right to show or hide a type")

    # A click on a map feature opens it in the entity details panel below. The feature's
    # properties were HTML-escaped for the map, so the id is unescaped back.
    clicked = ((map_state or {}).get("last_active_drawing") or {}).get("properties") or {}
    clicked_id = html.unescape(str(clicked.get("id") or ""))
    if clicked_id and clicked_id != st.session_state.get("map_clicked_id"):
        st.session_state.map_clicked_id = clicked_id
        st.session_state.entity_choice = clicked_id

# ==========================================
# AGENTIC REASONING & CONTEXT (UNDER THE CHAT AND THE MAP)
# ==========================================
st.markdown("---")
st.markdown("#### 🧠 Agentic Reasoning & Context")
last = st.session_state.last_reply
if not last:
    st.markdown("Ready for natural-language questions about World War II (in English).")
else:
    col_reason, col_entity = st.columns([3, 2])
    with col_reason:
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
        if "nothing_on_map" in (last.get("warnings") or []):
            st.caption("No location in the graph for this answer.")
        elif kind in ("answer", "clarification") and not n_features and st.session_state.map_reply:
            st.caption("This reply has nothing to draw; the map still shows the previous answer.")
        if last.get("tool_calls"):
            st.markdown("**Graph queries:** " + " ➔ ".join(f"`{md_text(t.get('name', '?'))}`" for t in last["tool_calls"]))
        if last.get("resolved_entities"):
            with st.expander("Resolved entities"):
                st.json(last["resolved_entities"], expanded=False)

    with col_entity:
        # The reply's entities plus the places on the map (they can come from an earlier reply).
        map_ids = [str((f.get("properties") or {}).get("id")) for f in reply_features(st.session_state.map_reply)
                   if (f.get("properties") or {}).get("id")]
        entity_ids = list(dict.fromkeys((last.get("entity_ids") or []) + map_ids))[:100]
        if entity_ids and runner is not None:
            st.markdown("##### 🔍 Entity details")
            # No widget key: a new map click changes `index`, which makes a fresh widget showing it.
            focus = st.session_state.get("entity_choice")
            map_reply = st.session_state.map_reply or {}
            labels = {i: entity_label(last, i) if entity_label(last, i) != i else entity_label(map_reply, i)
                      for i in entity_ids}
            chosen = st.selectbox("Entity", entity_ids,
                                  index=entity_ids.index(focus) if focus in entity_ids else 0,
                                  format_func=labels.get, label_visibility="collapsed")
            render_entity_card(chosen)

# The running question: show progress in the chat until it's done. This runs last so the
# rest of the page is already drawn. A click meanwhile reruns the script, and the next run
# picks the question up again from session state.
pending = st.session_state.pending
if pending is not None:
    with chat_box:
        with st.chat_message("assistant", avatar="🤖"):
            if st.button("⏹ Stop", key="stop_question"):
                pending.cancel()
            events = pending.progress
            with st.status(events[-1].get("label", "Working…") if events else "The agent is working…") as status:
                while not pending.done():
                    for event in pending.poll_progress():
                        if event.get("label"):
                            status.update(label=event["label"])
                    time.sleep(POLL_INTERVAL_S)
                status.update(label="Done", state="complete")
    finish_pending()
    st.rerun()
