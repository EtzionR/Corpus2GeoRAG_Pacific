"""Tagging and scope rules: which kind of thing an Article is (Tag) and whether it is tied to US–Japan engagement.
Used by crawl_corpus.ipynb; tested by tests/test_scope_rules.py. Vocabulary: CONTEXT.md. Overview: RULES.md.
Pure functions over a page record (see task_fetch in the notebook), its Wikidata claims and class ancestors."""
import re

import mwparserfromhell

from evidence import sentences

BATTLE_TAGS = {"Battle", "Campaign", "Operation"}
WAR_YEARS = set(range(1941, 1946))

LINK_RE = re.compile(r"\[\[([^\]|#]+)")
NON_ARTICLE_LINK = re.compile(r"\s*(file|image|category|wikt|wiktionary|wikipedia|template|help|portal|s|commons|w|d)\s*:", re.I)

# P31 instance of · P607 conflict · P710 participant · P1344 participant in · P793 significant event
CLAIM_PROPS = ["P31", "P607", "P710", "P1344", "P793"]
ANCHOR_PROPS = ["P607", "P1344", "P793"]  # claims that tie an Article to an In-scope Battle

TAG_ROOTS = [
    ("Ship class", ["Q559026"]),
    ("Ship", ["Q11446", "Q1229765"]),
    ("Aircraft", ["Q11436", "Q15056995", "Q15056993"]),
    ("Weapon/Equipment", ["Q728", "Q15142894", "Q15142889", "Q18643213"]),
    ("Person", ["Q5"]),
    ("Military unit", ["Q176799"]),
    ("Campaign", ["Q831663"]),
    ("Battle", ["Q178561"]),
    ("Operation", ["Q645883"]),
    ("Location", ["Q2221906", "Q618123", "Q62447", "Q245016"]),
    ("Diplomacy/Document", ["Q131569", "Q49848", "Q2761147"]),
    ("Event", ["Q1190554", "Q1520311"]),
    ("Organization", ["Q43229"]),
]
ROOT_TAG = {r: tag for tag, roots in TAG_ROOTS for r in roots}
TAG_RANK = {tag: i for i, (tag, _) in enumerate(TAG_ROOTS)}
US_QIDS, JAPAN_QIDS = {"Q30"}, {"Q188712", "Q17"}
# Classes too broad to be anchored by their ties (a country or metropolis is tied to every battle fought there).
# Plain "city" is deliberately not here: Hiroshima, Rabaul, Myitkyina are battle sites.
TOO_BROAD_ROOTS = {"Q6256": "country", "Q1637706": "city with millions of inhabitants"}

INFOBOX_TAGS = [
    (r"film|book|video game|television|\btv\b|album|song|musical|artwork|painting|comic|anime|manga|museum|memorial|monument|award|medal|sport|podcast|magazine|newspaper|\bplay\b|single", "Media"),
    (r"military conflict|battle", "Battle"),
    (r"military operation", "Operation"),
    (r"civilian attack|event|disaster|massacre|war crime|trial|court case|incident", "Event"),
    (r"ship class|ship/class", "Ship class"),
    (r"ship|submarine|boat", "Ship"),
    (r"aircraft|airliner", "Aircraft"),
    (r"weapon|firearm|gun|artillery|tank|\bafv\b|vehicle|rocket|missile|torpedo", "Weapon/Equipment"),
    (r"military unit|military formation|army unit", "Military unit"),
    (r"person|officeholder|military officer|scientist|royalty|politician|monarch|writer|engineer|criminal|spy|officer|noble|religious|artist|journalist|pilot|athlete", "Person"),
    (r"military installation|airport|island|atoll|settlement|country|former country|geographic|body of water|landform|mountain|protected area|nrhp|building|port|bay|river|lake|place|prison|structure", "Location"),
    (r"treaty|document|legislation|conference|summit|speech", "Diplomacy/Document"),
    (r"intelligence|cryptograph|cipher", "Intelligence"),
    (r"organi[sz]ation|government agency|agency|company|law enforcement|political party|government|ministry|project", "Organization"),
]
INTELLIGENCE_CATEGORY = re.compile(r"cryptograph|signals intelligence|code ?talkers|codebreak|espionage|intelligence (services|agencies|operations|gathering)|\bspies\b", re.I)
CATEGORY_TAGS = [
    (r"\bfilms?\b|novels?|video games|television|\bsongs?\b|books\b|comics|anime|manga|fiction|in popular culture|memorials|museums", "Media"),
    (r"-class |ship classes", "Ship class"),
    (r"\bships\b|submarines|destroyers|battleships|cruisers|aircraft carriers|shipwrecks|frigates|minesweepers|escort", "Ship"),
    (r"\baircraft\b|bombers|fighter aircraft|aviation units", "Aircraft"),
    (r"weapons|tanks|\bguns\b|artillery|torpedoes|\bbombs\b|firearms|rifles|machine guns|nuclear weapons", "Weapon/Equipment"),
    (r"campaigns", "Campaign"),
    (r"\boperations\b", "Operation"),
    (r"battles|conflicts in", "Battle"),
    (r"\bunits\b|divisions|regiments|battalions|squadrons|brigades|fleets|\bwings\b|\bgroups\b|\bcorps\b|formations", "Military unit"),
    (r"\bpeople\b|personnel|admirals|generals|officers|\bpilots\b|soldiers|sailors|marines|recipients|politicians|prime ministers|executed|prisoners of war|aces", "Person"),
    (r"treaties|conferences|declarations|documents|surrender|diplomacy|agreements|speeches", "Diplomacy/Document"),
    (r"war crimes|massacres|incidents|atrocities|disasters|events|internment|death marches|kamikaze", "Event"),
    (r"islands|atolls|airfields|airports|\bbases\b|installations|ports|geography|populated places|harbors|lagoons", "Location"),
    (r"organi[sz]ations|agencies|ministries|\bprojects\b|headquarters|companies", "Organization"),
]
INFOBOX_TAGS = [(re.compile(p, re.I), t) for p, t in INFOBOX_TAGS]
CATEGORY_TAGS = [(re.compile(p, re.I), t) for p, t in CATEGORY_TAGS]

US_TEXT = re.compile(r"united states|\bamerican\b", re.I)
US_ABBR = re.compile(r"\bU\.\s?S\.|\bUSA\b|\bUS\b|\bUSN\b|\bUSAAF\b|\bUSMC\b|\bUSAF\b")
JP_TEXT = re.compile(r"japan", re.I)
JP_ABBR = re.compile(r"\bIJN\b|\bIJA\b|\bJPN\b")

# An Allied force other than the US: tells a unit or person apart from US and Japanese ones by its categories.
ALLY_CATEGORY = re.compile(r"\b(Australian|Royal Australian|RAAF|British|Royal Navy|Royal Air Force|British Indian|Indian Army|"
                           r"Gurkhas?|New Zealand|Dutch|Netherlands|KNIL|French|Chinese|National Revolutionary Army|"
                           r"Kuomintang|Soviet|Red Army|Canadian)\b")
US_CATEGORY = re.compile(r"United States|\bAmerican\b|\bU\.S\.|USAAF")
JP_CATEGORY = re.compile(r"\bJapan\b|Japanese|Imperial")
US_MENTION = re.compile(r"United States|\bAmerican|\bU\.\s?S\.|\bUS (Army|Navy|Marines?|forces|troops)\b|\bUSN\b|\bUSAAF\b|"
                        r"\bUSMC\b|\bUSS [A-Z]|Fifth Air Force|Seventh Fleet|7th Fleet|MacArthur|Nimitz|Halsey|Stilwell")
NAMESAKE = re.compile(r"^(=+)\s*(Namesakes?|Name|Naming|Etymology)\s*\1\s*$", re.M | re.I)
PACIFIC = re.compile(r"Pacific|Japan|Japanese|Okinawa|Leyte|Luzon|Philippines?|Guam|Saipan|Tinian|Iwo Jima|Ulithi|Manus|"
                     r"Eniwetok|Kwajalein|Marshall|Gilbert|Solomon|Guadalcanal|New Guinea|Hollandia|Aleutian|Kuril|Truk|"
                     r"Palau|Peleliu|Marianas|Formosa|Borneo|Rabaul|Bougainville|kamikaze", re.I)


def norm_title(t):
    t = t.replace("_", " ").split("#", 1)[0].strip()
    return t[:1].upper() + t[1:]


def article_links(wikitext):
    """Main-namespace link targets of a wikitext, normalised and de-duplicated."""
    return sorted({norm_title(l) for l in LINK_RE.findall(wikitext) if not NON_ARTICLE_LINK.match(l)})


def class_tag(cls, ancestors):
    tags = {ROOT_TAG[a] for a in ancestors.get(cls, []) if a in ROOT_TAG}
    return min(tags, key=TAG_RANK.get) if tags else None


def refine(tag, title):
    if tag in BATTLE_TAGS:
        if re.search(r"\bcampaign\b", title, re.I):
            return "Campaign"
        if title.startswith("Operation "):
            return "Operation"
    if tag in {"Ship", "Ship class"}:
        return "Ship class" if re.search(r"-class\b| class\b", title) else "Ship"
    return tag


def tag_page(p, claims, ancestors):
    """-> (primary tag, source, all tags)"""
    tags = []
    if p["infobox"]:
        tags += [(t, "infobox") for rx, t in INFOBOX_TAGS if rx.search(p["infobox"])][:1]
    if any(INTELLIGENCE_CATEGORY.search(c) for c in p["categories"]):
        tags.append(("Intelligence", "category"))
    for c in claims.get(p["qid"], {}).get("P31", []) if p["qid"] else []:
        if t := class_tag(c, ancestors):
            tags.append((t, "wikidata"))
    for cat in p["categories"]:
        tags += [(t, "category") for rx, t in CATEGORY_TAGS if rx.search(cat.removeprefix("Category:"))][:1]
    if not tags:
        return None, None, []
    all_tags = list(dict.fromkeys(refine(t, p["title"]) for t, _ in tags))
    # A Media infobox always wins; Media categories only win when nothing else tags the Article.
    if tags[0] == ("Media", "infobox") or all(t == "Media" for t, _ in tags):
        return "Media", "media", all_tags
    tag, source = next((t, s) for t, s in tags if t != "Media")
    return refine(tag, p["title"]), source, all_tags


def battle_scope(p, claims):
    """-> (verdict, reason); verdict in {"in", "dated", "no participants"}"""
    if p["infobox_years"] and not (set(p["infobox_years"]) & WAR_YEARS):
        return "dated", f"dated {p['infobox_years']}"
    signals = []
    txt = p["infobox_text"]
    if (US_TEXT.search(txt) or US_ABBR.search(txt)) and (JP_TEXT.search(txt) or JP_ABBR.search(txt)):
        signals.append("infobox")
    parts = set(claims.get(p["qid"], {}).get("P710", [])) if p["qid"] else set()
    if parts & US_QIDS and parts & JAPAN_QIDS:
        signals.append("wikidata P710")
    cats = " ".join(p["categories"])
    if "involving the United States" in cats and "involving Japan" in cats:
        signals.append("categories")
    return ("in", "US+Japan via " + ", ".join(signals)) if signals else ("no participants", "no US+Japan participants found")


COMBATANT_PARAM = re.compile(r"^combatant(\d)")
US_SIDE = re.compile(r"United States|\bUSA\b|\bU\.S\.|\bUS\b|\bAmerican\b|USAAF|\bUSN\b|\bUSMC\b")
JP_SIDE = re.compile(r"Japan")
ABDA = re.compile(r"American[- ]British[- ]Dutch[- ]Australian|\bABDA\w*", re.I)  # a joint command, not US forces
# A side's entry in a supporting role only: "(air support)", "Limited arms support:", "American volunteers"…
SUPPORT = re.compile(r"support|advis|volunteer|\bclaims?\b|limited|lend|suppl(y|ies)|logistic|\baid\b", re.I)
ENTRY_BREAK = re.compile(r"(?=\{\{\s*flag)|<br\s*/?>|\n|\*")


def battle_sides(wikitext):
    """How the infobox's combatant fields line up the US and Japan.
    -> "opposed" (on opposite sides, both as main combatants) | "support" (opposite sides, one only in a supporting
    role) | "not opposed" (combatants listed, but not the US against Japan) | "unlisted" (no combatant fields)."""
    lead = wikitext.split("\n==", 1)[0]
    sides = {}
    for tpl in mwparserfromhell.parse(lead).filter_templates(recursive=False):
        if str(tpl.name).strip().lower().startswith("infobox"):
            for param in tpl.params:
                if m := COMBATANT_PARAM.match(str(param.name).strip()):
                    sides.setdefault(m.group(1), []).append(str(param.value))
            break
    if not any(v.strip() for vals in sides.values() for v in vals):
        return "unlisted"

    def presence(rx, text):
        """"main" if an entry names the force outside a supporting role, "support" if only in one, else None."""
        found, in_support = None, False
        for piece in ENTRY_BREAK.split(ABDA.sub("", text)):
            if "----" in piece:  # a divider starts a new group of entries
                in_support = False
                piece = piece.split("----")[-1]
            names = rx.search(piece)
            supporting = in_support or bool(SUPPORT.search(piece))
            if not names and SUPPORT.search(piece):  # a heading such as "'''Air support:'''"
                in_support = True
            if names:
                if not supporting:
                    return "main"
                found = "support"
        return found

    texts = {k: " ".join(v) for k, v in sides.items()}
    best = None
    for us_side, us_text in texts.items():
        for jp_side, jp_text in texts.items():
            if us_side == jp_side:
                continue
            us, jp = presence(US_SIDE, us_text), presence(JP_SIDE, jp_text)
            if us == jp == "main":
                return "opposed"
            if us and jp:
                best = "support"
    return best or "not opposed"


def battle_grade(p, claims, sides):
    """Classification of a battle that the discovery check (battle_scope) let in, now reading its combatant fields.
    -> (grade, reason); grade in {"in", "peripheral", "disagree", "out"}.
    peripheral: the US or Japan took part only in a supporting role; stays in the Corpus, but like an Umbrella
    conflict it can never be an Article's only anchor. disagree: the combatant fields say no, Wikidata or the
    categories say yes; left for e5 and review."""
    p710 = set(claims.get(p["qid"], {}).get("P710", [])) if p["qid"] else set()
    wikidata = bool(p710 & US_QIDS and p710 & JAPAN_QIDS)
    cats = " ".join(p["categories"])
    categories = "involving the United States" in cats and "involving Japan" in cats
    if sides == "opposed":
        return "in", "US vs Japan in the combatant fields"
    if sides == "support":
        return "peripheral", "US or Japan only in a supporting role (combatant fields)"
    if sides == "not opposed":
        if wikidata or categories:
            return "disagree", ("combatant fields list no US vs Japan, but "
                                + " and ".join(s for s, ok in (("Wikidata P710", wikidata), ("categories", categories)) if ok)
                                + " do")
        return "out", "combatant fields list no US vs Japan"
    return "in", "no combatant fields; " + battle_scope(p, claims)[1]


def participants_known(p, claims):
    """A battle's belligerents are listed (a flag template in the infobox, or Wikidata P710), so a missing US+Japan
    signal is meaningful. Operation infoboxes often list only commanders and units (Operation Matterhorn), and
    cancelled or proposed operations stay unknown: those fall back to anchoring."""
    if any(re.search(r"cancelled|proposed", c, re.I) for c in p["categories"]):
        return False
    return bool(re.search(r"\{\{\s*flag", p["infobox_text"], re.I)) or bool(claims.get(p["qid"], {}).get("P710"))


def allied_only(p, wikitext):
    """A subject of a non-US Allied force (by its categories), not US or Japanese itself, whose article never
    mentions the US: Australian, British, Indian, Chinese, Dutch… units and people fighting Japan without the US."""
    def identity(rx):
        return any(rx.search(c) for c in p["categories"] if not re.search(r"relations|involving", c))
    return (identity(ALLY_CATEGORY) and not identity(US_CATEGORY) and not identity(JP_CATEGORY)
            and not US_MENTION.search(" ".join(sentences(wikitext))))


def without_namesake(wikitext):
    """The wikitext with its Namesake section(s) removed: a ship's namesake often fought battles the ship never saw."""
    m = NAMESAKE.search(wikitext)
    while m:
        level = len(m.group(1))
        nxt = re.compile(rf"^={{1,{level}}}[^=].*?={{1,{level}}}\s*$", re.M).search(wikitext, m.end())
        wikitext = wikitext[:m.start()] + (wikitext[nxt.start():] if nxt else "")
        m = NAMESAKE.search(wikitext)
    return wikitext


def too_broad(p, claims, ancestors):
    """-> the TOO_BROAD_ROOTS class name the Article is an instance of, or None"""
    for c in claims.get(p["qid"], {}).get("P31", []) if p["qid"] else []:
        for a in ancestors.get(c, []):
            if a in TOO_BROAD_ROOTS:
                return TOO_BROAD_ROOTS[a]
    return None


def anchor_scope(p, tag, claims, battles, battle_by_qid, battle_aliases, umbrella, min_links=2):
    """-> (verdict, reason, ties); verdict in {"anchored", "gray", "none"}.
    ties = {"grade": "strong" | "links" | "one battle" | "umbrella only" | "none", "battles": [In-scope Battles]}.
    `umbrella`: In-scope Battles that can never be an Article's only anchor."""
    wd, weak = {}, set()  # In-scope Battle -> the first Wikidata claim (ANCHOR_PROPS order) naming it
    for prop in ANCHOR_PROPS:
        for q in claims.get(p["qid"], {}).get(prop, []) if p["qid"] else []:
            if q not in battle_by_qid:
                continue
            if prop == "P793" and tag == "Person":  # a person's "significant event" is often one they only witnessed
                weak.add(battle_by_qid[q])
            else:
                wd.setdefault(battle_by_qid[q], prop)
    in_cat = {c.removeprefix("Category:") for c in p["categories"]} & set(battles)
    links = {battle_aliases[l] for l in p["links"] if l in battle_aliases} - {p["title"]}
    strong = (set(wd) | in_cat) - umbrella
    if strong:
        how = "/".join(sorted({wd[b] for b in strong if b in wd})) or "category"
        return "anchored", f"{how} → " + "; ".join(sorted(strong))[:200], {"grade": "strong", "battles": sorted(strong)}
    ties = set(wd) | weak | in_cat | links
    specific = ties - umbrella
    if len(ties) >= min_links and specific:
        return ("anchored", f"tied to {len(ties)} in-scope battles: " + "; ".join(sorted(ties)[:5]),
                {"grade": "links", "battles": sorted(ties)})
    if ties:
        why = "umbrella only" if not specific else "one battle"
        return "gray", f"{why}: " + "; ".join(sorted(ties)[:5]), {"grade": why, "battles": sorted(ties)}
    return "none", "not tied to any in-scope battle", {"grade": "none", "battles": []}
