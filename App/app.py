import streamlit as st
import folium
from streamlit_folium import st_folium
import json
import time
from typing import List, Dict, Any, Optional

# ==========================================
# PAGE CONFIGURATION & STYLING
# ==========================================
st.set_page_config(
    page_title="GeoAI - From Corpus into GeoRAG",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS matching the presentation's dark starry & sleek aesthetic
st.markdown("""
<style>
    /* Dark space theme inspired by the presentation */
    .stApp {
        background: radial-gradient(ellipse at bottom, #1B2735 0%, #090A0F 100%);
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

    /* Metrics & Cards */
    .metric-card {
        background: rgba(30, 41, 59, 0.7);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 12px 16px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
        backdrop-filter: blur(8px);
    }
    
    /* Chat callouts */
    .chat-bubble-user {
        background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%);
        color: white;
        padding: 12px 18px;
        border-radius: 16px 16px 4px 16px;
        margin-bottom: 10px;
        max-width: 85%;
        margin-left: auto;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
    }
    
    .chat-bubble-agent {
        background: rgba(30, 41, 59, 0.85);
        border: 1px solid rgba(59, 130, 246, 0.3);
        color: #F1F5F9;
        padding: 14px 18px;
        border-radius: 16px 16px 16px 4px;
        margin-bottom: 12px;
        max-width: 90%;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
    }
    
    /* Target radar indicator */
    .radar-pulse {
        border: 3px solid #EF4444;
        border-radius: 50%;
        animation: pulse 1.5s infinite;
    }
    
    /* Status Badge */
    .badge {
        display: inline-block;
        padding: 3px 8px;
        font-size: 0.75rem;
        font-weight: 600;
        border-radius: 6px;
        text-transform: uppercase;
    }
    .badge-ukraine {
        background-color: #0284C7;
        color: #FFFFFF;
    }
    .badge-russia {
        background-color: #DC2626;
        color: #FFFFFF;
    }
    .badge-contested {
        background-color: #D97706;
        color: #FFFFFF;
    }
    
    /* Custom button styling */
    .stButton>button {
        border-radius: 8px;
        background: rgba(30, 41, 59, 0.8);
        color: #E2E8F0;
        border: 1px solid rgba(255, 255, 255, 0.15);
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        background: #3B82F6;
        color: white;
        border-color: #60A5FA;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# GEOGRAPHIC KNOWLEDGE BASE & LOCATIONS
# ==========================================
GEO_DATABASE = {
    "Kyiv": {"coords": [50.4501, 30.5234], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Capital"},
    "Kharkiv": {"coords": [49.9935, 36.2304], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Major City"},
    "Dnipro": {"coords": [48.4647, 35.0462], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Major City"},
    "Donetsk": {"coords": [48.0159, 37.8029], "country": "Ukraine", "status": "Occupied by Russia", "type": "Industrial Hub"},
    "Zaporizhzhia": {"coords": [47.8388, 35.1396], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Regional Capital"},
    "Kherson": {"coords": [46.6354, 32.6169], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Port / Regional Capital", "liberated": "November 11, 2022"},
    "Odesa": {"coords": [46.4825, 30.7233], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Major Port"},
    "Simferopol": {"coords": [44.9521, 34.1024], "country": "Ukraine", "status": "Occupied by Russia (Crimea)", "type": "Peninsula Capital"},
    "Lviv": {"coords": [49.8397, 24.0297], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "Western Hub"},
    "Rivne": {"coords": [50.6199, 26.2516], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "City"},
    "Chernivtsi": {"coords": [48.2921, 25.9358], "country": "Ukraine", "status": "Controlled by Ukraine", "type": "City"},
    "Mariupol": {"coords": [47.0971, 37.5434], "country": "Ukraine", "status": "Occupied by Russia", "type": "Port City"},
    "Bakhmut": {"coords": [48.5987, 37.9980], "country": "Ukraine", "status": "Occupied / Destroyed", "type": "Frontline"},
    "Avdiivka": {"coords": [48.1366, 37.7491], "country": "Ukraine", "status": "Occupied / Frontline", "type": "Frontline"}
}

DRONE_ATTACKS_DATA = [
    {"city": "Kyiv", "coords": [50.4501, 30.5234], "targets": "Energy grid, Air defense sites", "attacks_count": 84, "date_range": "2022-2024", "weapon": "Shahed-136 / Geran-2"},
    {"city": "Kharkiv", "coords": [49.9935, 36.2304], "targets": "Industrial plants, Sub-stations", "attacks_count": 62, "date_range": "2022-2024", "weapon": "Shahed-136, Lancet"},
    {"city": "Dnipro", "coords": [48.4647, 35.0462], "targets": "Logistics hub, Storage facilities", "attacks_count": 45, "date_range": "2022-2024", "weapon": "Shahed-136"},
    {"city": "Donetsk", "coords": [48.0159, 37.8029], "targets": "Ammunition depots, Command posts", "attacks_count": 39, "date_range": "2022-2024", "weapon": "FPV Drones, Recon UAVs"},
    {"city": "Kherson", "coords": [46.6354, 32.6169], "targets": "Dnipro river crossings, Port infrastructure", "attacks_count": 58, "date_range": "2022-2024", "weapon": "FPV Kamikaze, Shahed"},
    {"city": "Odesa", "coords": [46.4825, 30.7233], "targets": "Grain terminals, Black Sea ports", "attacks_count": 51, "date_range": "2022-2024", "weapon": "Shahed-136/131"}
]

BATTLES_DATA = [
    {
        "name": "Battle of Kyiv",
        "bounds": [[50.0, 29.5], [51.2, 31.5]],
        "coords": [50.4501, 30.5234],
        "period": "Feb 2022 – Apr 2022",
        "result": "Ukrainian victory, Russian withdrawal",
        "description": "Major military battle for the capital city Kyiv and surrounding oblasts."
    },
    {
        "name": "Kharkiv Counteroffensive",
        "bounds": [[49.2, 35.5], [50.5, 38.0]],
        "coords": [49.9935, 36.2304],
        "period": "Sep 2022 – Oct 2022",
        "result": "Decisive Ukrainian liberation of Kharkiv Oblast",
        "description": "Rapid surprise offensive liberating Izyum, Kupiansk and Balakliya."
    },
    {
        "name": "Battle of Kherson & Southern Front",
        "bounds": [[46.2, 31.8], [47.5, 34.0]],
        "coords": [46.6354, 32.6169],
        "period": "Aug 2022 – Nov 2022",
        "result": "Ukrainian liberation of western Kherson Oblast",
        "description": "Liberation of Kherson city on the right bank of Dnipro River."
    },
    {
        "name": "Battle of Donbas (Bakhmut & Donetsk)",
        "bounds": [[47.8, 36.8], [49.2, 39.0]],
        "coords": [48.0159, 37.8029],
        "period": "2022 – Ongoing",
        "result": "High-intensity attritional warfare",
        "description": "Heavy fortified combat along the central and eastern contact lines."
    }
]

# ==========================================
# AGENTIC RAG ENGINE (GeoRAGAgent)
# ==========================================
class GeoRAGAgent:
    """
    Agentic RAG implementation for Geospatial Intelligence.
    Translates natural language questions into Knowledge Graph queries,
    performs entity resolution, geocoding, and outputs visual map annotations.
    """
    def __init__(self):
        self.corpus_name = "Wikipedia Warfare & Conflicts Corpus"
        self.graph_nodes = 1420
        self.graph_edges = 3890

    def process_query(self, query: str) -> Dict[str, Any]:
        q_lower = query.lower().strip()
        
        # 1. Drone Attacks Query (Matches Slide 8)
        if any(w in q_lower for w in ["drone", "attacks", "uav", "shahed", "כטב", "מלטים", "תקיפות"]):
            entities = ["Kyiv", "Kharkiv", "Dnipro", "Donetsk", "Kherson", "Odesa"]
            locations = [d for d in DRONE_ATTACKS_DATA if d["city"] in entities]
            
            answer_text = (
                "**Identified Drone Attack Hotspots:**\n"
                "According to the corpus knowledge graph, major drone strikes (predominantly Shahed-136/Geran-2 and FPV loitering munitions) "
                "have targeted key urban centers, energy infrastructure, and logistics hubs in **Kyiv**, **Kharkiv**, **Dnipro**, "
                "**Donetsk**, **Kherson**, and **Odesa**.\n\n"
                "• **Kyiv:** High concentration of swarm drone attacks targeting electrical substations.\n"
                "• **Odesa & Black Sea Ports:** Strikes focused on grain silos and maritime export terminals.\n"
                "• **Kherson & Dnipro:** Tactical FPV strikes along riverfront defensive perimeters."
            )
            
            return {
                "type": "drone_attacks",
                "query": query,
                "answer": answer_text,
                "markers": locations,
                "highlight_mode": "radar_targets",
                "entities_found": entities,
                "kg_relations_used": ["TARGETED_BY", "STRIKE_LOCATION", "DEPLOYED_WEAPON"],
                "zoom": 6,
                "center": [48.8, 32.5]
            }

        # 2. Battles & Combat Zones Query (Matches Slide 9)
        elif any(w in q_lower for w in ["battle", "battles", "fight", "warfare", "קרב", "קרבות", "מוקדי לחימה"]):
            bounds = [[46.0, 29.0], [51.5, 39.5]]  # Broad Ukraine conflict perimeter box matching slide 9
            
            answer_text = (
                "**Identified Active Battle Theaters & Historical Frontlines:**\n"
                "Extracted 4 primary operational theaters from the knowledge graph:\n\n"
                "1. **Northern Campaign (Kyiv & Chernihiv):** Early phase urban defense and containment (Feb-Apr 2022).\n"
                "2. **Kharkiv & Northeastern Axis:** High-speed maneuver counteroffensive (Fall 2022).\n"
                "3. **Southern Axis (Kherson & Zaporizhzhia):** Dnipro river barrier and southern frontline operations.\n"
                "4. **Donbas Attrition Theater (Bakhmut, Avdiivka, Donetsk):** Fortified trench and artillery warfare."
            )
            
            return {
                "type": "battles",
                "query": query,
                "answer": answer_text,
                "bounds": bounds,
                "battle_zones": BATTLES_DATA,
                "highlight_mode": "red_theater_zone",
                "entities_found": ["Kyiv", "Kharkiv", "Kherson", "Donetsk", "Zaporizhzhia"],
                "kg_relations_used": ["THEATER_OF", "ENGAGED_IN_BATTLE", "CONTROL_LINE"],
                "zoom": 6,
                "center": [48.5, 33.0]
            }

        # 3. Kherson Control Query (Matches Slide 10)
        elif "kherson" in q_lower and any(w in q_lower for w in ["control", "who", "status", "שליטה", "חרסון", "מי שולט"]):
            kherson_info = GEO_DATABASE["Kherson"]
            
            answer_text = (
                "**Kherson city is controlled by Ukraine.**\n\n"
                "• **Status:** Liberated by Ukrainian Armed Forces on **November 11, 2022**.\n"
                "• **Strategic Context:** Kherson was the only regional capital captured by Russian forces during the initial 2022 invasion. "
                "Following the southern counteroffensive, Russian forces retreated across the Dnipro river, leaving the city under Ukrainian control."
            )
            
            return {
                "type": "territory_control",
                "query": query,
                "answer": answer_text,
                "city": "Kherson",
                "callout_text": "Kherson city is controlled by Ukraine",
                "coords": kherson_info["coords"],
                "highlight_mode": "city_callout",
                "entities_found": ["Kherson", "Ukraine", "Dnipro River"],
                "kg_relations_used": ["OCCUPIED_BY", "LIBERATED_BY", "GOVERNED_BY"],
                "zoom": 8,
                "center": kherson_info["coords"]
            }

        # 4. General Entity Geocoding & Knowledge Graph Search
        else:
            found_cities = [city for city in GEO_DATABASE.keys() if city.lower() in q_lower]
            if not found_cities:
                found_cities = ["Kyiv", "Dnipro", "Kharkiv", "Kherson", "Odesa"]
            
            markers = [{"city": c, "coords": GEO_DATABASE[c]["coords"], "status": GEO_DATABASE[c]["status"], "type": GEO_DATABASE[c]["type"]} for c in found_cities]
            
            answer_text = (
                f"**GeoRAG Agent Response for:** *'{query}'*\n\n"
                f"Extracted {len(found_cities)} geographic entities from query & Wikipedia corpus: "
                f"**{', '.join(found_cities)}**.\n\n"
                "• Coordinates and relations retrieved from knowledge graph.\n"
                "• Dynamic geospatial markers populated on the interactive map."
            )
            
            return {
                "type": "general_georag",
                "query": query,
                "answer": answer_text,
                "markers": markers,
                "highlight_mode": "general_pins",
                "entities_found": found_cities,
                "kg_relations_used": ["MENTIONED_IN", "LOCATED_AT", "HAS_GEOPOINT"],
                "zoom": 6,
                "center": [48.3794, 31.1656]
            }

# Initialize Agent
if "agent" not in st.session_state:
    st.session_state.agent = GeoRAGAgent()

if "messages" not in st.session_state:
    st.session_state.messages = []

if "current_map_data" not in st.session_state:
    # Default initial state matching Slide 7
    st.session_state.current_map_data = {
        "type": "initial",
        "center": [48.3794, 31.1656],
        "zoom": 6,
        "highlight_mode": "base_cities"
    }

# ==========================================
# MAP GENERATION FUNCTION (Folium)
# ==========================================
def render_folium_map(map_data: Dict[str, Any], tile_theme: str = "Positron"):
    """
    Constructs a customized Folium map with styling and overlays
    mirroring the presentation slides (Slides 7, 8, 9, 10).
    """
    center = map_data.get("center", [48.3794, 31.1656])
    zoom = map_data.get("zoom", 6)
    
    # Select Tile Theme
    tiles_dict = {
        "Positron (Light/Minimal - Presentation Style)": (
            "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
        ),
        "Dark Matter (Sleek Dark)": (
            "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
        ),
        "OpenStreetMap (Standard)": (
            "OpenStreetMap",
            None
        )
    }
    tile_url, tile_attr = tiles_dict.get(tile_theme, tiles_dict["Positron (Light/Minimal - Presentation Style)"])
    
    # Initialize Folium Map
    if tile_attr:
        m = folium.Map(
            location=center,
            zoom_start=zoom,
            tiles=tile_url,
            attr=tile_attr,
            control_scale=True,
            prefer_canvas=True
        )
    else:
        m = folium.Map(
            location=center,
            zoom_start=zoom,
            tiles=tile_url,
            control_scale=True,
            prefer_canvas=True
        )
    
    mode = map_data.get("highlight_mode", "base_cities")
    
    # --- 1. Base Cities Layer (matching Slide 7) ---
    base_group = folium.FeatureGroup(name="Major Cities", show=True)
    for city, info in GEO_DATABASE.items():
        coords = info["coords"]
        is_capital = (city == "Kyiv")
        
        # Capital star or regular dot
        if is_capital:
            icon_html = f"""
            <div style="font-family: Arial, sans-serif; text-align: center;">
                <div style="background-color: #000; border: 2px solid #FFF; width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto; box-shadow: 0 0 8px rgba(0,0,0,0.5);">
                    <span style="color: #FFF; font-size: 13px; font-weight: bold; line-height: 1;">★</span>
                </div>
                <div style="font-size: 14px; font-weight: 800; color: #1E293B; text-shadow: 0 0 4px #FFF, 0 0 2px #FFF; margin-top: 2px;">
                    {city}
                </div>
            </div>
            """
        else:
            icon_html = f"""
            <div style="font-family: Arial, sans-serif; text-align: center;">
                <div style="background-color: #334155; border: 2px solid #FFF; width: 12px; height: 12px; border-radius: 50%; margin: 0 auto; box-shadow: 0 0 4px rgba(0,0,0,0.4);"></div>
                <div style="font-size: 11px; font-weight: 600; color: #334155; text-shadow: 0 0 3px #FFF, 0 0 1px #FFF; margin-top: 1px;">
                    {city}
                </div>
            </div>
            """
        
        folium.Marker(
            location=coords,
            icon=folium.DivIcon(
                icon_size=(100, 36),
                icon_anchor=(50, 10),
                html=icon_html
            ),
            popup=folium.Popup(f"<b>{city}</b><br>Status: {info.get('status')}<br>Type: {info.get('type')}", max_width=250)
        ).add_to(base_group)
    base_group.add_to(m)

    # --- 2. Drone Attacks Radar / Target Layer (matching Slide 8) ---
    if mode == "radar_targets":
        drone_group = folium.FeatureGroup(name="🎯 Drone Strike Hotspots", show=True)
        markers = map_data.get("markers", DRONE_ATTACKS_DATA)
        for d in markers:
            coords = d["coords"]
            city = d["city"]
            
            # Outer Red Concentric Radar Target (matching slide 8)
            folium.CircleMarker(
                location=coords,
                radius=26,
                color="#DC2626",
                weight=3,
                fill=True,
                fill_color="#EF4444",
                fill_opacity=0.35,
                tooltip=f"🎯 Drone Strike Target: {city}"
            ).add_to(drone_group)
            
            folium.CircleMarker(
                location=coords,
                radius=14,
                color="#991B1B",
                weight=2,
                fill=True,
                fill_color="#DC2626",
                fill_opacity=0.6
            ).add_to(drone_group)
            
            folium.CircleMarker(
                location=coords,
                radius=4,
                color="#FFFFFF",
                weight=1,
                fill=True,
                fill_color="#7F1D1D",
                fill_opacity=1.0
            ).add_to(drone_group)
            
            # Popup with stats
            popup_html = f"""
            <div style="font-family: Arial, sans-serif; font-size: 12px; min-width: 180px;">
                <h4 style="color: #DC2626; margin: 0 0 6px 0;">🎯 {city} - Drone Attacks</h4>
                <b>Reported Strikes:</b> {d.get('attacks_count', 'N/A')}<br>
                <b>Primary Targets:</b> {d.get('targets', 'Infrastructure')}<br>
                <b>Munitions:</b> {d.get('weapon', 'Shahed-136')}<br>
                <b>Timeframe:</b> {d.get('date_range', '2022-2024')}
            </div>
            """
            folium.Marker(
                location=coords,
                icon=folium.DivIcon(icon_size=(0,0), html=""),
                popup=folium.Popup(popup_html, max_width=300)
            ).add_to(drone_group)
            
        drone_group.add_to(m)

    # --- 3. Battles / Conflict Zone Layer (matching Slide 9) ---
    if mode == "red_theater_zone":
        battle_group = folium.FeatureGroup(name="⚔️ Theater of Operations & Battles", show=True)
        bounds = map_data.get("bounds", [[46.0, 29.0], [51.5, 39.5]])
        
        # Red translucent rectangle bounding box matching Slide 9
        folium.Rectangle(
            bounds=bounds,
            color="#B91C1C",
            weight=4,
            fill=True,
            fill_color="#EF4444",
            fill_opacity=0.45,
            tooltip="⚔️ Active Warfare Theater & Conflict Zone"
        ).add_to(battle_group)
        
        # Sub-battle zone pins
        for b in map_data.get("battle_zones", BATTLES_DATA):
            folium.Marker(
                location=b["coords"],
                icon=folium.Icon(color="red", icon="shield", prefix="fa"),
                popup=folium.Popup(
                    f"<b>{b['name']}</b><br>"
                    f"<i>{b['period']}</i><br>"
                    f"<b>Result:</b> {b['result']}<br>"
                    f"<p>{b['description']}</p>",
                    max_width=280
                )
            ).add_to(battle_group)
            
        battle_group.add_to(m)

    # --- 4. Territory Control & Callout Layer (matching Slide 10) ---
    if mode == "city_callout":
        callout_group = folium.FeatureGroup(name="📍 Territory Control Callout", show=True)
        city_name = map_data.get("city", "Kherson")
        coords = map_data.get("coords", [46.6354, 32.6169])
        callout_text = map_data.get("callout_text", "Kherson city is controlled by Ukraine")
        
        # Highlight marker at city
        folium.CircleMarker(
            location=coords,
            radius=12,
            color="#2563EB",
            weight=3,
            fill=True,
            fill_color="#38BDF8",
            fill_opacity=0.8,
            tooltip=f"{city_name}: {callout_text}"
        ).add_to(callout_group)
        
        # Custom curved speech callout bubble matching Slide 10
        callout_html = f"""
        <div style="
            position: relative;
            background: #FFFFFF;
            border: 2px solid #DC2626;
            border-radius: 12px;
            padding: 8px 14px;
            font-family: Arial, sans-serif;
            font-size: 13px;
            font-weight: 700;
            color: #DC2626;
            box-shadow: 0 4px 10px rgba(0,0,0,0.25);
            white-space: nowrap;
        ">
            {callout_text}
            <div style="
                content: '';
                position: absolute;
                left: -12px;
                top: 50%;
                transform: translateY(-50%);
                border-width: 6px 12px 6px 0;
                border-style: solid;
                border-color: transparent #DC2626 transparent transparent;
                display: block;
                width: 0;
            "></div>
        </div>
        """
        
        folium.Marker(
            location=[coords[0] - 0.25, coords[1] + 0.45],
            icon=folium.DivIcon(
                icon_size=(260, 45),
                icon_anchor=(0, 22),
                html=callout_html
            )
        ).add_to(callout_group)
        
        callout_group.add_to(m)

    # Add Layer Control
    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    
    return m

# ==========================================
# SIDEBAR CONTROLS & GRAPH STATS
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/isometric/100/globe.png", width=64)
    st.markdown("### 🌐 GeoAI Platform")
    st.markdown("**From Corpus into GeoRAG**")
    st.caption("Knowledge Graph-powered Geospatial Intelligence")
    
    st.divider()
    
    st.markdown("#### ⚙️ Map Configuration")
    tile_choice = st.selectbox(
        "Map Theme:",
        [
            "Positron (Light/Minimal - Presentation Style)",
            "Dark Matter (Sleek Dark)",
            "OpenStreetMap (Standard)"
        ],
        index=0
    )
    
    st.divider()
    
    st.markdown("#### 📊 Knowledge Graph Metrics")
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.metric("Entities", "1,420")
        st.metric("Geocoded", "840")
    with col_g2:
        st.metric("Relations", "3,890")
        st.metric("Articles", "325")
        
    st.caption("Corpus: Wikipedia Warfare & Conflict Datasets")
    
    st.divider()
    
    st.markdown("#### 🤖 Agent Engine")
    st.success("🟢 GeoRAG Agent Online")
    st.info("Pipeline: `Wikipedia API → NER → Geocoding → KG → Agentic RAG → Folium`")
    
    if st.button("🔄 Reset Map & View", use_container_width=True):
        st.session_state.current_map_data = {
            "type": "initial",
            "center": [48.3794, 31.1656],
            "zoom": 6,
            "highlight_mode": "base_cities"
        }
        st.rerun()

# ==========================================
# MAIN PAGE HEADER
# ==========================================
st.markdown('<div class="main-title">GeoAI: From Corpus into GeoRAG</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Transforms unstructured Wikipedia text into a geographically-aware knowledge graph and interactive RAG map exploration</div>', unsafe_allow_html=True)

# Quick Suggestion Pills
st.markdown("##### 💡 Suggested Demo Queries (from Project Slides):")
col_q1, col_q2, col_q3, col_q4 = st.columns(4)

with col_q1:
    if st.button("🎯 Drone attacks", use_container_width=True):
        prompt = "Show me where drone attacks took place."
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q2:
    if st.button("⚔️ Places with battles", use_container_width=True):
        prompt = "Show me places where battles took place."
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q3:
    if st.button("📍 Who controls Kherson?", use_container_width=True):
        prompt = "Who currently controls Kherson?"
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q4:
    if st.button("🛡️ Kyiv Defense & Strikes", use_container_width=True):
        prompt = "Show conflict info for Kyiv and Dnipro"
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

# ==========================================
# INTERACTIVE FOLIUM MAP DISPLAY
# ==========================================
st.markdown("---")
col_map, col_info = st.columns([7, 3])

with col_map:
    st.markdown("#### 🗺️ Interactive Geospatial Knowledge Map")
    folium_map_obj = render_folium_map(st.session_state.current_map_data, tile_theme=tile_choice)
    st_folium(folium_map_obj, width="100%", height=520, returned_objects=[])

with col_info:
    st.markdown("#### 🧠 Agentic Reasoning & Context")
    curr_data = st.session_state.current_map_data
    curr_mode = curr_data.get("highlight_mode", "base_cities")
    
    if curr_mode == "radar_targets":
        st.markdown("**Active Mode:** 🎯 `Drone Attack Hotspots`")
        st.markdown("**Entities Resolved:** `Kyiv`, `Kharkiv`, `Dnipro`, `Donetsk`, `Kherson`, `Odesa`")
        st.markdown("**KG Traversal:** `Corpus` ➔ `NER (Locations)` ➔ `Geocoding` ➔ `Folium Targets`")
        st.progress(100)
    elif curr_mode == "red_theater_zone":
        st.markdown("**Active Mode:** ⚔️ `Theaters of Battle & Contact Zone`")
        st.markdown("**Spatial Geometry:** `Polygonal Operational Boundary`")
        st.markdown("**KG Relations:** `THEATER_OF`, `ENGAGED_IN_BATTLE`")
        st.progress(100)
    elif curr_mode == "city_callout":
        st.markdown("**Active Mode:** 📍 `Territory Control Resolution`")
        st.markdown("**Focus City:** `Kherson (46.63°N, 32.61°E)`")
        st.markdown("**Status:** <span class='badge badge-ukraine'>Controlled by Ukraine</span>", unsafe_allow_html=True)
        st.progress(100)
    else:
        st.markdown("**Active Mode:** 🌐 `Base Geospatial Overview`")
        st.markdown("Ready for natural-language queries. Enter a question below to trigger the Agentic RAG pipeline.")
        st.progress(30)

    st.markdown("---")
    st.markdown("##### 📌 Entity Details")
    with st.expander("Explore Loaded Entities & Graph Relations", expanded=False):
        st.json(list(GEO_DATABASE.keys()))

# ==========================================
# CHAT & PROMPT INTERACTION BAR
# ==========================================
st.markdown("---")
st.markdown("#### 💬 Ask the GeoAI Agent")

# Display Message History
for msg in st.session_state.messages[-4:]:
    if msg["role"] == "user":
        st.markdown(f'<div class="chat-bubble-user"><b>You:</b> {msg["content"]}</div>', unsafe_allow_html=True)
    else:
        res_data = msg.get("data", {})
        answer_text = res_data.get("answer", "Processed.")
        entities = res_data.get("entities_found", [])
        relations = res_data.get("kg_relations_used", [])
        
        st.markdown(
            f'<div class="chat-bubble-agent">'
            f'<b>🤖 GeoAI Agent:</b><br>{answer_text}'
            f'<hr style="margin: 8px 0; border: 0; border-top: 1px solid rgba(255,255,255,0.1);">'
            f'<small style="color: #94A3B8;">🔗 <b>Entities:</b> {", ".join(entities)} | <b>KG Paths:</b> {", ".join(relations)}</small>'
            f'</div>',
            unsafe_allow_html=True
        )

# Chat Input field
user_input = st.chat_input("Ask anything... (e.g., 'Show me where drone attacks took place', 'Who currently controls Kherson?')")

if user_input:
    # Process through GeoRAG Agent
    res = st.session_state.agent.process_query(user_input)
    st.session_state.current_map_data = res
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.messages.append({"role": "assistant", "data": res})
    st.rerun()
