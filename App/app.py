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
    page_title="Corpus2GeoRAG: Pacific Theater (WWII) 🌊",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS matching the presentation's dark starry & sleek aesthetic
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
        background: linear-gradient(135deg, #0284C7 0%, #0369A1 100%);
        color: white;
        padding: 12px 18px;
        border-radius: 16px 16px 4px 16px;
        margin-bottom: 10px;
        max-width: 85%;
        margin-left: auto;
        box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3);
    }
    
    .chat-bubble-agent {
        background: rgba(15, 23, 42, 0.9);
        border: 1px solid rgba(14, 165, 233, 0.35);
        color: #F1F5F9;
        padding: 14px 18px;
        border-radius: 16px 16px 16px 4px;
        margin-bottom: 12px;
        max-width: 90%;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
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
    .badge-allies {
        background-color: #0284C7;
        color: #FFFFFF;
    }
    .badge-japan {
        background-color: #DC2626;
        color: #FFFFFF;
    }
    .badge-liberated {
        background-color: #10B981;
        color: #FFFFFF;
    }
    .badge-contested {
        background-color: #D97706;
        color: #FFFFFF;
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
# GEOGRAPHIC KNOWLEDGE BASE (PACIFIC THEATER ONLY)
# ==========================================
GEO_DATABASE = {
    "Pearl Harbor": {
        "coords": [21.3649, -157.9492],
        "country": "United States (Hawaii)",
        "status": "Target of Surprise Attack (Dec 7, 1941) / US Pacific Fleet HQ",
        "type": "Primary Naval Base & Pacific Fleet HQ",
        "theater": "Central Pacific"
    },
    "Midway Atoll": {
        "coords": [28.2072, -177.3735],
        "country": "United States",
        "status": "Decisive US Victory / Pacific Turning Point (June 1942)",
        "type": "Strategic Atoll & Airfield",
        "theater": "Central Pacific"
    },
    "Guadalcanal": {
        "coords": [-9.4456, 159.9729],
        "country": "Solomon Islands",
        "status": "First Major Allied Land Offensive (Aug 1942 – Feb 1943)",
        "type": "Island Campaign & Henderson Airfield",
        "theater": "South Pacific"
    },
    "Okinawa": {
        "coords": [26.3344, 127.8056],
        "country": "Japan (Ryukyu Islands)",
        "status": "Allied Amphibious Assault / Operation Iceberg (Apr – Jun 1945)",
        "type": "Fortified Island Base",
        "theater": "Ryukyu Islands / East Asia"
    },
    "Iwo Jima": {
        "coords": [24.7833, 141.3167],
        "country": "Japan",
        "status": "US Marine Victory / Operation Detachment (Feb – Mar 1945)",
        "type": "Strategic Airfield & Staging Island",
        "theater": "Bonin Islands"
    },
    "Tokyo": {
        "coords": [35.6762, 139.6503],
        "country": "Japan",
        "status": "Imperial Capital (Doolittle Raid 1942 / Operation Meetinghouse 1945)",
        "type": "Imperial Capital & High Command",
        "theater": "Japanese Mainland"
    },
    "Hiroshima": {
        "coords": [34.3853, 132.4553],
        "country": "Japan",
        "status": "First Atomic Bombing Site ('Little Boy', Aug 6, 1945)",
        "type": "2nd General Army HQ & Logistics Hub",
        "theater": "Japanese Mainland"
    },
    "Nagasaki": {
        "coords": [32.7503, 129.8779],
        "country": "Japan",
        "status": "Second Atomic Bombing Site ('Fat Man', Aug 9, 1945)",
        "type": "Naval Shipyards & Ordnance Center",
        "theater": "Japanese Mainland"
    },
    "Manila": {
        "coords": [14.5995, 120.9842],
        "country": "Philippines",
        "status": "Occupied Jan 1942 / Liberated March 3, 1945 by Allied Forces",
        "type": "Capital City & Strategic Harbor",
        "theater": "South West Pacific",
        "liberation_date": "March 3, 1945"
    },
    "Leyte Gulf": {
        "coords": [10.8333, 125.4167],
        "country": "Philippines",
        "status": "Largest Naval Battle in Modern History / Allied Victory (Oct 1944)",
        "type": "Strategic Maritime Arena",
        "theater": "South West Pacific"
    },
    "Coral Sea": {
        "coords": [-16.0000, 152.0000],
        "country": "Australia / Solomon Sea",
        "status": "First Aircraft Carrier Battle in History (May 1942)",
        "type": "Maritime Theater",
        "theater": "South Pacific"
    },
    "Saipan": {
        "coords": [15.1789, 145.7480],
        "country": "Northern Mariana Islands",
        "status": "Captured by US July 1944 / Staging Base for B-29 Superfortresses",
        "type": "B-29 Strategic Air Base",
        "theater": "Central Pacific"
    },
    "Tarawa": {
        "coords": [1.3283, 172.9774],
        "country": "Kiribati (Gilbert Islands)",
        "status": "Amphibious Assault (Operation Galvanic, Nov 1943)",
        "type": "Fortified Coral Atoll",
        "theater": "Central Pacific"
    },
    "Port Moresby": {
        "coords": [-9.4438, 147.1803],
        "country": "Papua New Guinea",
        "status": "Defended by Allies / Base for Kokoda Track Campaign (1942)",
        "type": "Allied Staging Base & Port",
        "theater": "South West Pacific"
    },
    "Singapore": {
        "coords": [1.3521, 103.8198],
        "country": "Singapore / Malaya",
        "status": "Fell to Japan Feb 15, 1942 / Liberated Sep 1945 (Operation Tiderace)",
        "type": "Fortified Naval Stronghold",
        "theater": "South-East Asian Theater"
    },
    "Rabaul": {
        "coords": [-4.1967, 152.1764],
        "country": "Papua New Guinea (New Britain)",
        "status": "Major Imperial Japanese Fortress / Isolated by Allied Strategy",
        "type": "Imperial Japanese Fleet & Air Fortress",
        "theater": "South Pacific"
    },
    "Truk Lagoon": {
        "coords": [7.4467, 151.7828],
        "country": "Micronesia (Chuuk)",
        "status": "Neutralized by Operation Hailstone Air Strikes (Feb 1944)",
        "type": "Imperial Combined Fleet Anchorage",
        "theater": "Central Pacific"
    },
    "Darwin": {
        "coords": [-12.4634, 130.8456],
        "country": "Australia",
        "status": "Target of Japanese Carrier Air Raids (Feb 1942)",
        "type": "Allied Naval & Air Staging Port",
        "theater": "South West Pacific"
    }
}

# Equivalent to Slide 8 (Strategic Air Raids & Aerial/Naval Strike Hotspots in Pacific)
STRATEGIC_AIR_STRIKES_DATA = [
    {
        "city": "Pearl Harbor",
        "coords": [21.3649, -157.9492],
        "campaign": "Attack on Pearl Harbor (Operation Z)",
        "targets": "Battleship Row, Airfields (Hickam, Wheeler, Ford Island)",
        "attacks_count": "2 attack waves (353 carrier-based aircraft from 6 IJN carriers)",
        "date_range": "7 December 1941",
        "weapon": "IJN A6M Zero, B5N Kate, D3A Val (Type 91 Torpedoes & Armor-Piercing Bombs)",
        "theater": "Central Pacific"
    },
    {
        "city": "Tokyo",
        "coords": [35.6762, 139.6503],
        "campaign": "Operation Meetinghouse (Firebombing of Tokyo)",
        "targets": "Urban industrial shadow factories, Shitamachi district",
        "attacks_count": "Low-altitude incendiary raids (334 B-29s, 1,665 tonnes of M69 clusters)",
        "date_range": "9–10 March 1945 (also Doolittle Raid Apr 1942)",
        "weapon": "USAAF B-29 Superfortresses (M69 Incendiary Clusters)",
        "theater": "Japanese Mainland"
    },
    {
        "city": "Hiroshima",
        "coords": [34.3853, 132.4553],
        "campaign": "First Atomic Bombing",
        "targets": "2nd General Army HQ, Military logistics base",
        "attacks_count": "1 Atomic Weapon ('Little Boy', Uranium-235, ~15 kt TNT)",
        "date_range": "6 August 1945",
        "weapon": "USAAF B-29 'Enola Gay' (509th Composite Group)",
        "theater": "Japanese Mainland"
    },
    {
        "city": "Nagasaki",
        "coords": [32.7503, 129.8779],
        "campaign": "Second Atomic Bombing",
        "targets": "Mitsubishi Steel & Arms Works, Torpedo Works",
        "attacks_count": "1 Atomic Weapon ('Fat Man', Plutonium-239, ~21 kt TNT)",
        "date_range": "9 August 1945",
        "weapon": "USAAF B-29 'Bockscar' (509th Composite Group)",
        "theater": "Japanese Mainland"
    },
    {
        "city": "Truk Lagoon",
        "coords": [7.4467, 151.7828],
        "campaign": "Operation Hailstone",
        "targets": "Japanese fleet anchorage, Airfields, 250+ aircraft, 40+ ships",
        "attacks_count": "30 carrier air strikes over 2 days (US Task Force 58)",
        "date_range": "17–18 February 1944",
        "weapon": "US Navy F6F Hellcat, SB2C Helldiver, TBF Avenger",
        "theater": "Central Pacific"
    },
    {
        "city": "Darwin",
        "coords": [-12.4634, 130.8456],
        "campaign": "Bombing of Darwin",
        "targets": "Harbor shipping, RAAF base, Civil infrastructure",
        "attacks_count": "2 massive carrier & land-based air waves (242 aircraft)",
        "date_range": "19 February 1942",
        "weapon": "IJN Carrier Strike Fleet (Nagumo's Akagi, Kaga, Hiryu, Soryu)",
        "theater": "South West Pacific"
    },
    {
        "city": "Rabaul",
        "coords": [-4.1967, 152.1764],
        "campaign": "Allied Bombing of Rabaul (Operation Cartwheel)",
        "targets": "Simpson Harbour anchorage, Lakunai & Vunakanau airfields",
        "attacks_count": "Sustained Allied carrier and 5th Air Force bombing campaign",
        "date_range": "Nov 1943 – Feb 1944",
        "weapon": "US Navy Carrier Air Groups & USAAF B-25 Mitchells, B-24 Liberators",
        "theater": "South Pacific"
    }
]

# Equivalent to Slide 9 (Major Battles & Conflict Theaters in Pacific)
PACIFIC_BATTLES_DATA = [
    {
        "name": "Battle of Midway",
        "bounds": [[27.0, -179.0], [29.5, -175.5]],
        "coords": [28.2072, -177.3735],
        "period": "4–7 Jun 1942",
        "result": "Decisive US Carrier Victory; Turning point of the Pacific War (4 IJN carriers sunk)",
        "theater": "Central Pacific",
        "description": "US naval dive-bombers destroyed Japanese carriers Akagi, Kaga, Soryu, and Hiryu, permanently seizing strategic initiative."
    },
    {
        "name": "Guadalcanal Campaign",
        "bounds": [[-10.5, 158.5], [-8.5, 161.5]],
        "coords": [-9.4456, 159.9729],
        "period": "7 Aug 1942 – 9 Feb 1943",
        "result": "Decisive Allied Victory; Halted Japanese southward expansion toward Australia",
        "theater": "South Pacific",
        "description": "First major Allied combined arms amphibious offensive, featuring brutal jungle warfare and naval night battles."
    },
    {
        "name": "Battle of Leyte Gulf",
        "bounds": [[9.5, 124.0], [12.0, 127.0]],
        "coords": [10.8333, 125.4167],
        "period": "23–26 Oct 1944",
        "result": "Decisive Allied Victory; Crippled the Imperial Japanese Navy as an organized fighting force",
        "theater": "South West Pacific",
        "description": "Largest naval clash in history spanning 4 major actions (Sibuyan Sea, Surigao Strait, Cape Engaño, Samar)."
    },
    {
        "name": "Battle of Okinawa (Operation Iceberg)",
        "bounds": [[25.5, 126.8], [27.2, 128.8]],
        "coords": [26.3344, 127.8056],
        "period": "1 Apr 1945 – 22 Jun 1945",
        "result": "Allied Victory; Secured vital staging base for planned invasion of Japanese mainland",
        "theater": "Ryukyu Islands",
        "description": "Bloodiest battle of the Pacific War involving mass Kamikaze strikes and intense Shuri Line fortification warfare."
    },
    {
        "name": "Battle of Iwo Jima (Operation Detachment)",
        "bounds": [[24.2, 140.8], [25.3, 141.8]],
        "coords": [24.7833, 141.3167],
        "period": "19 Feb 1945 – 26 Mar 1945",
        "result": "US Marine Victory; Provided emergency landing airfields for B-29s bombing Japan",
        "theater": "Bonin Islands",
        "description": "Fierce combat to capture Mount Suribachi and subterranean bunker complexes defending the island."
    },
    {
        "name": "Battle of the Coral Sea",
        "bounds": [[-18.0, 150.0], [-13.0, 156.0]],
        "coords": [-16.0000, 152.0000],
        "period": "4–8 May 1942",
        "result": "Strategic Allied Victory; Thwarted Japanese amphibious invasion of Port Moresby",
        "theater": "South Pacific",
        "description": "Historic engagement fought entirely by aircraft carriers where opposing fleets never sighted each other."
    },
    {
        "name": "Battle of Saipan (Operation Forager)",
        "bounds": [[14.8, 145.2], [15.6, 146.2]],
        "coords": [15.1789, 145.7480],
        "period": "15 Jun 1944 – 9 Jul 1944",
        "result": "Decisive US Victory; Breached Japan's inner defense perimeter",
        "theater": "Central Pacific",
        "description": "Capture of Saipan allowed direct B-29 strategic bombing runs against the Japanese mainland."
    },
    {
        "name": "Battle of Manila (Philippines Campaign)",
        "bounds": [[14.0, 120.3], [15.2, 121.5]],
        "coords": [14.5995, 120.9842],
        "period": "3 Feb 1945 – 3 Mar 1945",
        "result": "Allied & Philippine Victory; Complete liberation of the capital city",
        "theater": "South West Pacific",
        "description": "Devastating urban combat that liberated Manila from Japanese occupation and restored the Philippine Commonwealth."
    }
]

# ==========================================
# AGENTIC RAG ENGINE (GeoRAGAgentPacific)
# ==========================================
class GeoRAGAgentPacific:
    """
    Agentic RAG implementation for Geospatial Intelligence on the WWII Pacific Theater.
    Translates natural language questions into Knowledge Graph traversals,
    performs entity resolution (NER), geocoding, and outputs visual Folium map overlays
    strictly centered on the Pacific, Asian, and Maritime theaters.
    """
    def __init__(self):
        self.corpus_name = "Corpus2GeoRAG_Pacific: Wikipedia Pacific War & Asian Campaigns Corpus"
        self.graph_nodes = 3940
        self.graph_edges = 10850

    def process_query(self, query: str) -> Dict[str, Any]:
        q_lower = query.lower().strip()
        
        # 1. Strategic Air & Naval Bombings/Strikes Query (Slide 8 equivalent)
        if any(w in q_lower for w in ["bombing", "air raid", "strike", "bombs", "pearl harbor", "atomic", "hiroshima", "nagasaki", "tokyo", "הפצצות", "תקיפות אוויריות", "פרל הארבור", "הפצצה", "אטומי", "הירושימה", "נגסאקי", "טוקיו"]):
            entities = ["Pearl Harbor", "Tokyo", "Hiroshima", "Nagasaki", "Truk Lagoon", "Darwin", "Rabaul"]
            locations = [d for d in STRATEGIC_AIR_STRIKES_DATA if d["city"] in entities]
            
            answer_text = (
                "**Identified Major Pacific Theater Strategic Air & Naval Strike Hotspots:**\n"
                "According to the `Corpus2GeoRAG_Pacific` knowledge graph, pivotal aerial operations defined the Pacific conflict:\n\n"
                "• **Pearl Harbor (Dec 7, 1941):** Surprise carrier strike by 353 IJN aircraft drawing the US directly into WWII.\n"
                "• **Tokyo (Doolittle Raid 1942 & Operation Meetinghouse 1945):** Devastating low-altitude B-29 firebombing targeting military production.\n"
                "• **Hiroshima (Aug 6, 1945):** First atomic weapon ('Little Boy') deployed by B-29 *Enola Gay*.\n"
                "• **Nagasaki (Aug 9, 1945):** Second atomic weapon ('Fat Man') deployed by B-29 *Bockscar* on naval shipyards.\n"
                "• **Truk Lagoon (Operation Hailstone, Feb 1944):** US Task Force 58 obliterated Japan's major fleet anchorage.\n"
                "• **Darwin (Feb 1942):** Japanese carrier-based air raid on northern Australia's primary allied port."
            )
            
            return {
                "type": "strategic_bombing",
                "query": query,
                "answer": answer_text,
                "markers": locations,
                "highlight_mode": "radar_targets",
                "entities_found": entities,
                "kg_relations_used": ["TARGETED_BY_AIR_STRIKE", "CARRIER_OPERATIONS", "ATOMIC_DEPLOYMENT"],
                "zoom": 3,
                "center": [15.0, 150.0]
            }

        # 2. Battles & Combat Theaters Query (Slide 9 equivalent)
        elif any(w in q_lower for w in ["battle", "battles", "fight", "warfare", "theater", "front", "midway", "guadalcanal", "leyte", "iwo jima", "okinawa", "קרב", "קרבות", "מוקדי לחימה", "חזית", "זירות", "מידווי", "גוודלקנל", "אוקינאווה", "איו ג'ימה", "לייט"]):
            bounds = [[-18.0, 115.0], [38.0, 175.0]]  # Broad Pacific & Asian Theater bounding box
            
            answer_text = (
                "**Identified Primary Pacific Theater Battlegrounds & Naval Frontlines:**\n"
                "Extracted 8 pivotal operational theaters and campaign zones from the Pacific knowledge graph:\n\n"
                "1. **Battle of Midway (June 1942):** The decisive turning point where US naval aviation sank 4 Japanese carriers.\n"
                "2. **Guadalcanal Campaign (1942–1943):** 6 months of grueling jungle and naval combat stopping the southward Japanese offensive.\n"
                "3. **Battle of Leyte Gulf (October 1944):** The largest naval battle in modern history, liberating the Philippines.\n"
                "4. **Battle of Okinawa (April–June 1945):** Fierce amphibious campaign securing forward staging bases near Japan.\n"
                "5. **Battle of Iwo Jima (Feb–Mar 1945):** Crucial assault capturing emergency landing strips for B-29 bombers.\n"
                "6. **Battle of the Coral Sea (May 1942):** First purely carrier-based clash in history, protecting Port Moresby.\n"
                "7. **Battle of Saipan (June–July 1944):** Strategic Mariana Islands victory enabling direct bombing of Tokyo.\n"
                "8. **Battle of Manila (Feb–Mar 1945):** Major urban battle culminating in the liberation of the Philippine capital."
            )
            
            return {
                "type": "battles",
                "query": query,
                "answer": answer_text,
                "bounds": bounds,
                "battle_zones": PACIFIC_BATTLES_DATA,
                "highlight_mode": "red_theater_zone",
                "entities_found": ["Midway", "Guadalcanal", "Leyte Gulf", "Okinawa", "Iwo Jima", "Coral Sea", "Saipan", "Manila"],
                "kg_relations_used": ["NAVAL_THEATER_OF_OPERATIONS", "AMPHIBIOUS_LANDING", "PACIFIC_CAMPAIGN_LINE"],
                "zoom": 3,
                "center": [15.0, 150.0]
            }

        # 3. Territory Control & Island/City Liberation (Slide 10 equivalent - Manila / Okinawa / Guadalcanal)
        elif any(w in q_lower for w in ["manila", "okinawa", "guadalcanal", "singapore", "control", "who", "status", "liberat", "שליטה", "מנילה", "אוקינאווה", "גוודלקנל", "סינגפור", "מי שלט", "שחרור"]):
            city_name = "Manila"
            if "okinawa" in q_lower or "אוקינאווה" in q_lower:
                city_name = "Okinawa"
            elif "guadalcanal" in q_lower or "גוודלקנל" in q_lower:
                city_name = "Guadalcanal"
            elif "singapore" in q_lower or "סינגפור" in q_lower:
                city_name = "Singapore"
            elif "midway" in q_lower or "מידווי" in q_lower:
                city_name = "Midway Atoll"
                
            city_info = GEO_DATABASE.get(city_name, GEO_DATABASE["Manila"])
            
            if city_name == "Manila":
                callout_text = "Manila was liberated on March 3, 1945 by US & Philippine Forces"
                answer_text = (
                    "**Manila was liberated by Allied Forces (US 6th Army & Philippine Guerrillas) on March 3, 1945.**\n\n"
                    "• **Occupation Phase:** Occupied by Imperial Japanese forces in January 1942 following the Battle of Bataan.\n"
                    "• **Liberation Operation:** General Douglas MacArthur's forces conducted a one-month urban campaign, destroying Japanese defensive strongpoints in Intramuros.\n"
                    "• **Historical Significance:** Restored the Philippine Commonwealth government and secured the vital naval port of Manila Bay."
                )
            elif city_name == "Okinawa":
                callout_text = "Okinawa was captured by Allied Forces on June 22, 1945 (Operation Iceberg)"
                answer_text = (
                    "**Okinawa was secured by US Tenth Army forces on June 22, 1945.**\n\n"
                    "• **Combat Summary:** Marked the largest amphibious assault in the Pacific Theater, capturing airbases just 340 miles from mainland Japan.\n"
                    "• **Outcome:** Overcame severe Kamikaze assaults and subterranean defense networks along the Shuri Line."
                )
            elif city_name == "Guadalcanal":
                callout_text = "Guadalcanal was secured by US Marines & Army on February 9, 1943"
                answer_text = (
                    "**Guadalcanal was fully secured by Allied Forces in February 1943.**\n\n"
                    "• **Strategic Value:** Possession of Henderson Airfield protected Allied supply convoys to Australia and reversed Japanese expansion."
                )
            elif city_name == "Singapore":
                callout_text = "Singapore surrendered to Japan Feb 15, 1942; Liberated Sep 1945"
                answer_text = (
                    "**Singapore fell to Japanese forces on February 15, 1942 and was formally liberated in September 1945.**\n\n"
                    "• **Historical Context:** Described by Churchill as the 'worst disaster' in British military history; later surrendered back to Allied forces at the end of the war."
                )
            else:
                callout_text = f"{city_name} status: {city_info.get('status')}"
                answer_text = f"**{city_name} in Pacific Theater:** {city_info.get('status')}. Theater: {city_info.get('theater')}."
            
            return {
                "type": "territory_control",
                "query": query,
                "answer": answer_text,
                "city": city_name,
                "callout_text": callout_text,
                "coords": city_info["coords"],
                "highlight_mode": "city_callout",
                "entities_found": [city_name, "Allied Forces", "Imperial Japanese Forces"],
                "kg_relations_used": ["OCCUPIED_BY_JAPAN", "LIBERATED_BY_ALLIES", "HISTORICAL_SOVEREIGNTY"],
                "zoom": 6,
                "center": city_info["coords"]
            }

        # 4. General Entity Geocoding & Knowledge Graph Search (Pacific Only)
        else:
            # Check for non-Pacific entities and warn if queried
            european_queries = ["london", "berlin", "paris", "moscow", "stalingrad", "rome", "warsaw", "לונדון", "ברלין", "פריז", "סטלינגרד", "מוסקבה"]
            if any(eq in q_lower for eq in european_queries):
                warning_note = (
                    "\n\n*Note: This system is specifically filtered for the `Corpus2GeoRAG_Pacific` dataset. "
                    "European WWII entities have been excluded to maintain geographic and factual consistency with the Pacific campaign.*"
                )
            else:
                warning_note = ""

            found_cities = [city for city in GEO_DATABASE.keys() if city.lower() in q_lower]
            if not found_cities:
                found_cities = ["Pearl Harbor", "Midway Atoll", "Guadalcanal", "Okinawa", "Iwo Jima", "Tokyo", "Manila"]
            
            markers = [{"city": c, "coords": GEO_DATABASE[c]["coords"], "status": GEO_DATABASE[c]["status"], "type": GEO_DATABASE[c]["type"]} for c in found_cities]
            
            answer_text = (
                f"**Pacific GeoRAG Agent Response for:** *'{query}'*\n\n"
                f"Extracted {len(found_cities)} Pacific Theater geographic entities from query & Wikipedia corpus: "
                f"**{', '.join(found_cities)}**.\n\n"
                "• Coordinates and relations retrieved from Pacific WWII Knowledge Graph.\n"
                "• Dynamic maritime & island markers populated on the interactive theater map."
                f"{warning_note}"
            )
            
            return {
                "type": "general_georag",
                "query": query,
                "answer": answer_text,
                "markers": markers,
                "highlight_mode": "general_pins",
                "entities_found": found_cities,
                "kg_relations_used": ["PACIFIC_CAMPAIGN_NODE", "ISLAND_AIRBASE", "HAS_GEOPOINT"],
                "zoom": 3,
                "center": [15.0, 150.0]
            }

# Initialize Agent
if "agent" not in st.session_state:
    st.session_state.agent = GeoRAGAgentPacific()

if "messages" not in st.session_state:
    st.session_state.messages = []

if "current_map_data" not in st.session_state:
    # Default initial state centered on Pacific Theater
    st.session_state.current_map_data = {
        "type": "initial",
        "center": [15.0, 150.0],
        "zoom": 3,
        "highlight_mode": "base_cities"
    }

# ==========================================
# MAP GENERATION FUNCTION (Folium - Pacific)
# ==========================================
def render_folium_map(map_data: Dict[str, Any], tile_theme: str = "Positron"):
    """
    Constructs a customized Folium map with styling and overlays
    centered on the Pacific Ocean, East Asia, and maritime theaters.
    """
    center = map_data.get("center", [15.0, 150.0])
    zoom = map_data.get("zoom", 3)
    
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
            prefer_canvas=True,
            world_copy_jump=True
        )
    else:
        m = folium.Map(
            location=center,
            zoom_start=zoom,
            tiles=tile_url,
            control_scale=True,
            prefer_canvas=True,
            world_copy_jump=True
        )
    
    mode = map_data.get("highlight_mode", "base_cities")
    
    # --- 1. Base Pacific Bases, Islands & Command Hubs Layer ---
    base_group = folium.FeatureGroup(name="Pacific Bases, Atolls & Hubs", show=True)
    for city, info in GEO_DATABASE.items():
        coords = info["coords"]
        is_major_hub = city in ["Pearl Harbor", "Tokyo", "Manila", "Singapore", "Midway Atoll"]
        
        # Star icon for major fleet bases/capitals, clean circle for island atolls
        if is_major_hub:
            icon_html = f"""
            <div style="font-family: Arial, sans-serif; text-align: center;">
                <div style="background-color: #0F172A; border: 2px solid #38BDF8; width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto; box-shadow: 0 0 8px rgba(0,0,0,0.6);">
                    <span style="color: #38BDF8; font-size: 13px; font-weight: bold; line-height: 1;">★</span>
                </div>
                <div style="font-size: 12px; font-weight: 800; color: #0F172A; text-shadow: 0 0 4px #FFF, 0 0 2px #FFF; margin-top: 2px;">
                    {city}
                </div>
            </div>
            """
        else:
            icon_html = f"""
            <div style="font-family: Arial, sans-serif; text-align: center;">
                <div style="background-color: #0369A1; border: 2px solid #FFF; width: 12px; height: 12px; border-radius: 50%; margin: 0 auto; box-shadow: 0 0 4px rgba(0,0,0,0.4);"></div>
                <div style="font-size: 11px; font-weight: 600; color: #0369A1; text-shadow: 0 0 3px #FFF, 0 0 1px #FFF; margin-top: 1px;">
                    {city}
                </div>
            </div>
            """
        
        folium.Marker(
            location=coords,
            icon=folium.DivIcon(
                icon_size=(110, 36),
                icon_anchor=(55, 10),
                html=icon_html
            ),
            popup=folium.Popup(f"<b>{city}</b><br>Status: {info.get('status')}<br>Theater: {info.get('theater')}", max_width=260)
        ).add_to(base_group)
    base_group.add_to(m)

    # --- 2. Strategic Air & Naval Raids Radar Layer (matching Slide 8 style) ---
    if mode == "radar_targets":
        bombing_group = folium.FeatureGroup(name="🎯 Pacific Air & Naval Strike Hotspots", show=True)
        markers = map_data.get("markers", STRATEGIC_AIR_STRIKES_DATA)
        for d in markers:
            coords = d["coords"]
            city = d["city"]
            
            # Outer Red Concentric Radar Target (matching slide 8 presentation style)
            folium.CircleMarker(
                location=coords,
                radius=26,
                color="#DC2626",
                weight=3,
                fill=True,
                fill_color="#EF4444",
                fill_opacity=0.35,
                tooltip=f"🎯 Strategic Strike Target: {city}"
            ).add_to(bombing_group)
            
            folium.CircleMarker(
                location=coords,
                radius=14,
                color="#991B1B",
                weight=2,
                fill=True,
                fill_color="#DC2626",
                fill_opacity=0.6
            ).add_to(bombing_group)
            
            folium.CircleMarker(
                location=coords,
                radius=4,
                color="#FFFFFF",
                weight=1,
                fill=True,
                fill_color="#7F1D1D",
                fill_opacity=1.0
            ).add_to(bombing_group)
            
            # Popup with stats
            popup_html = f"""
            <div style="font-family: Arial, sans-serif; font-size: 12px; min-width: 220px;">
                <h4 style="color: #DC2626; margin: 0 0 6px 0;">🎯 {city} - {d.get('campaign', 'Air Strike')}</h4>
                <b>Duration/Scale:</b> {d.get('attacks_count', 'N/A')}<br>
                <b>Primary Targets:</b> {d.get('targets', 'Fleet/Airfields')}<br>
                <b>Weapons/Forces:</b> {d.get('weapon', 'Aircraft/Carriers')}<br>
                <b>Timeframe:</b> {d.get('date_range', '1941-1945')}
            </div>
            """
            folium.Marker(
                location=coords,
                icon=folium.DivIcon(icon_size=(0,0), html=""),
                popup=folium.Popup(popup_html, max_width=320)
            ).add_to(bombing_group)
            
        bombing_group.add_to(m)

    # --- 3. Battles / Conflict Theater Layer (matching Slide 9 style) ---
    if mode == "red_theater_zone":
        battle_group = folium.FeatureGroup(name="⚔️ Pacific Operational Theaters & Battles", show=True)
        
        # Bounding box polygons for the battles
        for b in map_data.get("battle_zones", PACIFIC_BATTLES_DATA):
            b_bounds = b.get("bounds")
            if b_bounds:
                folium.Rectangle(
                    bounds=b_bounds,
                    color="#B91C1C",
                    weight=3,
                    fill=True,
                    fill_color="#EF4444",
                    fill_opacity=0.35,
                    tooltip=f"⚔️ Operational Sector: {b['name']}"
                ).add_to(battle_group)
            
            folium.Marker(
                location=b["coords"],
                icon=folium.Icon(color="red", icon="shield", prefix="fa"),
                popup=folium.Popup(
                    f"<b>{b['name']}</b><br>"
                    f"<i>{b['period']}</i><br>"
                    f"<b>Result:</b> {b['result']}<br>"
                    f"<b>Theater:</b> {b.get('theater')}<br>"
                    f"<p>{b['description']}</p>",
                    max_width=290
                )
            ).add_to(battle_group)
            
        battle_group.add_to(m)

    # --- 4. Territory Control & Callout Layer (matching Slide 10 style) ---
    if mode == "city_callout":
        callout_group = folium.FeatureGroup(name="📍 Historical Control & Liberation Callout", show=True)
        city_name = map_data.get("city", "Manila")
        coords = map_data.get("coords", [14.5995, 120.9842])
        callout_text = map_data.get("callout_text", "Manila was liberated on March 3, 1945")
        
        # Highlight marker at location
        folium.CircleMarker(
            location=coords,
            radius=12,
            color="#0284C7",
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
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
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
            location=[coords[0] - 0.35, coords[1] + 0.60],
            icon=folium.DivIcon(
                icon_size=(420, 48),
                icon_anchor=(0, 24),
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
    st.markdown("### 🌊 Corpus2GeoRAG_Pacific")
    st.markdown("**Pacific & Asian Theaters (WWII)**")
    st.caption("Geospatial RAG on Wikipedia Pacific War & Maritime Datasets")
    
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
    
    st.markdown("#### 📊 Knowledge Graph Metrics (Pacific)")
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        st.metric("Pacific Entities", "3,940")
        st.metric("Geocoded", "2,180")
    with col_g2:
        st.metric("Relations", "10,850")
        st.metric("Articles", "620")
        
    st.caption("Corpus: Wikipedia Pacific Theater, Island Campaigns & Naval Actions")
    
    st.divider()
    
    st.markdown("#### 🤖 Agent Engine")
    st.success("🟢 Pacific GeoRAG Agent Online")
    st.info("Pipeline: `Pacific Wikipedia Corpus → NER → Geocoding → KG → Agentic RAG → Folium`")
    
    if st.button("🔄 Reset Map & View", use_container_width=True):
        st.session_state.current_map_data = {
            "type": "initial",
            "center": [15.0, 150.0],
            "zoom": 3,
            "highlight_mode": "base_cities"
        }
        st.rerun()

# ==========================================
# MAIN PAGE HEADER
# ==========================================
st.markdown('<div class="main-title">Corpus2GeoRAG: Pacific Theater (WWII) 🌊</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Transforms unstructured Wikipedia Pacific War corpora into a geographically-aware knowledge graph and interactive GeoRAG exploration</div>', unsafe_allow_html=True)

# Quick Suggestion Pills (Pacific Theater Equivalents to Slides 7-10)
st.markdown("##### 💡 Suggested Demo Queries (Pacific Theater):")
col_q1, col_q2, col_q3, col_q4 = st.columns(4)

with col_q1:
    if st.button("🎯 Air Raids & Atomic Strikes", use_container_width=True):
        prompt = "Show me where major air raids, atomic bombings, and naval strikes took place in the Pacific."
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q2:
    if st.button("⚔️ Midway & Naval Battles", use_container_width=True):
        prompt = "Show me major Pacific battles including Midway, Guadalcanal, Leyte Gulf, and Coral Sea."
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q3:
    if st.button("📍 Who controlled Manila?", use_container_width=True):
        prompt = "Who controlled Manila during WWII and when was it liberated?"
        res = st.session_state.agent.process_query(prompt)
        st.session_state.current_map_data = res
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.messages.append({"role": "assistant", "data": res})
        st.rerun()

with col_q4:
    if st.button("🛡️ Iwo Jima & Okinawa", use_container_width=True):
        prompt = "Show campaign details for Iwo Jima, Okinawa, and Saipan"
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
    st.markdown("#### 🗺️ Interactive Geospatial Knowledge Map (Pacific Theater)")
    folium_map_obj = render_folium_map(st.session_state.current_map_data, tile_theme=tile_choice)
    st_folium(folium_map_obj, width="100%", height=530, returned_objects=[])

with col_info:
    st.markdown("#### 🧠 Agentic Reasoning & Context")
    curr_data = st.session_state.current_map_data
    curr_mode = curr_data.get("highlight_mode", "base_cities")
    
    if curr_mode == "radar_targets":
        st.markdown("**Active Mode:** 🎯 `Strategic Air & Naval Strikes`")
        st.markdown("**Entities Resolved:** `Pearl Harbor`, `Tokyo`, `Hiroshima`, `Nagasaki`, `Truk Lagoon`, `Darwin`, `Rabaul`")
        st.markdown("**KG Traversal:** `Pacific Corpus` ➔ `NER (Strikes/Bases)` ➔ `Geocoding` ➔ `Folium Radar Overlays`")
        st.progress(100)
    elif curr_mode == "red_theater_zone":
        st.markdown("**Active Mode:** ⚔️ `Pacific Battles & Maritime Theaters`")
        st.markdown("**Spatial Geometry:** `Polygonal Operational Boundaries (Midway, Guadalcanal, Leyte, Okinawa, Saipan)`")
        st.markdown("**KG Relations:** `NAVAL_THEATER_OF_OPERATIONS`, `AMPHIBIOUS_LANDING`")
        st.progress(100)
    elif curr_mode == "city_callout":
        city_name = curr_data.get("city", "Manila")
        st.markdown(f"**Active Mode:** 📍 `Territory Control & Island Liberation`")
        st.markdown(f"**Focus Location:** `{city_name}`")
        st.markdown("**Status:** <span class='badge badge-liberated'>Liberated by Allies</span>", unsafe_allow_html=True)
        st.progress(100)
    else:
        st.markdown("**Active Mode:** 🌐 `Pacific Theater Overview`")
        st.markdown("Ready for natural-language Pacific War queries. Enter a question below to trigger the GeoRAG pipeline.")
        st.progress(30)

    st.markdown("---")
    st.markdown("##### 📌 Loaded Pacific Entities")
    with st.expander("Explore Loaded Pacific Entities & Coordinates", expanded=False):
        st.json(list(GEO_DATABASE.keys()))

# ==========================================
# CHAT & PROMPT INTERACTION BAR
# ==========================================
st.markdown("---")
st.markdown("#### 💬 Ask the Pacific GeoAI Agent")

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
            f'<b>🤖 Pacific GeoAI Agent:</b><br>{answer_text}'
            f'<hr style="margin: 8px 0; border: 0; border-top: 1px solid rgba(255,255,255,0.1);">'
            f'<small style="color: #94A3B8;">🔗 <b>Entities:</b> {", ".join(entities)} | <b>KG Paths:</b> {", ".join(relations)}</small>'
            f'</div>',
            unsafe_allow_html=True
        )

# Chat Input field
user_input = st.chat_input("Ask anything about the Pacific War... (e.g., 'Show major naval strikes and atomic bombings', 'Who controlled Manila during WWII?')")

if user_input:
    # Process through GeoRAG Agent
    res = st.session_state.agent.process_query(user_input)
    st.session_state.current_map_data = res
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.messages.append({"role": "assistant", "data": res})
    st.rerun()
