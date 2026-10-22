"""Unit tests for Processing/scope_rules.py on small hand-written page records."""
from scope_rules import (allied_only, anchor_scope, battle_grade, battle_scope, battle_sides, tag_page,
                         without_namesake)

BATTLES = {"Battle of Midway": "Q1", "Battle of Okinawa": "Q2", "Pacific War": "Q3"}
BY_QID = {q: t for t, q in BATTLES.items()}
ALIASES = {t: t for t in BATTLES}
UMBRELLA = {"Pacific War"}


def page(title="X", **kw):
    return {"title": title, "qid": kw.pop("qid", None), "infobox": None, "infobox_text": "", "infobox_years": [],
            "categories": [], "links": [], **kw}


def anchor(p, tag=None, claims=None):
    return anchor_scope(p, tag, claims or {}, BATTLES, BY_QID, ALIASES, UMBRELLA)


def test_battle_with_us_and_japan_in_infobox_is_in():
    p = page("Battle of Midway", infobox="military conflict", infobox_years=[1942],
             infobox_text="{{flag|United States|1912}} {{flag|Empire of Japan}}")
    assert battle_scope(p, {})[0] == "in"


def test_battle_outside_war_years_is_dated():
    p = page("USS Panay incident", infobox_years=[1937], infobox_text="United States Japan")
    assert battle_scope(p, {})[0] == "dated"


def test_media_infobox_wins():
    p = page("Letters from Iwo Jima", infobox="film", categories=["Category:Pacific War films"])
    assert tag_page(p, {}, {})[0] == "Media"


def test_wikidata_conflict_is_a_strong_anchor():
    p = page("Some admiral", qid="Q9")
    verdict, _, ties = anchor(p, "Person", {"Q9": {"P607": ["Q1"]}})
    assert (verdict, ties["grade"], ties["battles"]) == ("anchored", "strong", ["Battle of Midway"])


def test_umbrella_alone_is_gray():
    verdict, _, ties = anchor(page(links=["Pacific War"]))
    assert (verdict, ties["grade"]) == ("gray", "umbrella only")


def test_one_battle_link_is_gray_two_are_anchored():
    assert anchor(page(links=["Battle of Midway"]))[2]["grade"] == "one battle"
    assert anchor(page(links=["Battle of Midway", "Battle of Okinawa"]))[2]["grade"] == "links"


def test_umbrella_plus_one_battle_is_anchored_by_links():
    assert anchor(page(links=["Battle of Midway", "Pacific War"]))[0] == "anchored"


def test_person_significant_event_is_only_weak():
    claims = {"Q9": {"P793": ["Q1"]}}
    assert anchor(page(qid="Q9"), "Person", claims)[2]["grade"] == "one battle"
    assert anchor(page(qid="Q9"), "Ship", claims)[2]["grade"] == "strong"


def test_allied_only_unit_without_us_mention():
    p = page("2/30th Battalion (Australia)", categories=["Category:Infantry battalions of Australia in World War II",
                                                         "Category:Australian Army units"])
    assert allied_only(p, "The battalion fought the Japanese in Malaya in 1942. It surrendered at Singapore.")
    assert not allied_only(p, "The battalion fought alongside American troops at Buna.")


def test_namesake_section_removed():
    text = "Intro.\n== Namesake ==\nHe died at [[Battle of Midway]].\n== Service history ==\nAtlantic convoys.\n"
    own = without_namesake(text)
    assert "Midway" not in own and "Atlantic convoys" in own


def infobox(combatant1, combatant2, extra=""):
    return f"{{{{Infobox military conflict\n| combatant1 = {combatant1}\n| combatant2 = {combatant2}\n{extra}}}}}\nText.\n== History ==\n"


def test_sides_opposed():
    assert battle_sides(infobox("{{flag|United States|1912}}", "{{flag|Empire of Japan}}")) == "opposed"


def test_sides_us_only_in_air_support():
    """Japanese invasion of Burma: China with "United States (air support)"."""
    text = infobox("{{ubl|{{flag|China}}|{{flag|United States|1912}} (air support)}}", "{{flagcountry|Empire of Japan}}")
    assert battle_sides(text) == "support"


def test_sides_support_heading():
    """Jesselton revolt / coup in Indochina: a "Limited arms support:" or "Air support:" heading above the US flag."""
    text = infobox("{{flag|Kinabalu Guerrillas}} ---- '''Limited arms support:''' {{flagdeco|United States|1912}} [[USAFIP]]",
                   "{{flag|Empire of Japan}}")
    assert battle_sides(text) == "support"
    text = infobox("{{flag|Khmer Issarak}} ---- '''Air support:'''<br>{{flag|United States|1912}}", "{{flag|Empire of Japan}}")
    assert battle_sides(text) == "support"


def test_sides_casualty_claims_and_abda_are_not_us_forces():
    """Changde: "American and British claims" sits in the casualties; Malaya: the ABDA Command is not US forces."""
    text = infobox("{{flag|China}}", "{{flag|Empire of Japan}}", "| casualties2 = American and British claims: 40,000\n")
    assert battle_sides(text) == "not opposed"
    text = infobox("{{flag|United Kingdom}} [[American-British-Dutch-Australian Command|ABDA Command]]", "{{flag|Empire of Japan}}")
    assert battle_sides(text) == "not opposed"


def test_sides_unlisted():
    assert battle_sides("{{Infobox military operation\n| target = Kure\n}}\nText.") == "unlisted"


def test_grade_disagreement_goes_to_review():
    p = page("Battle of the Green Islands", categories=["Category:Battles of World War II involving the United States",
                                                       "Category:Battles of World War II involving Japan"])
    assert battle_grade(p, {}, "not opposed")[0] == "disagree"
    assert battle_grade(page("Malayan campaign"), {}, "not opposed")[0] == "out"
    assert battle_grade(page("Battle of West Hunan"), {}, "support")[0] == "peripheral"
