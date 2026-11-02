"""Unit tests for Processing/plain_text.py on small hand-written wikitext, plus a markup check on the Corpus."""
from pathlib import Path

import pytest
from plain_text import clean_text

CORPUS_PATH = Path(__file__).resolve().parent.parent / "Data" / "corpus_paragraphs.parquet"


def test_links_bold_and_refs_become_plain_prose():
    text = ("'''USS ''Enterprise''''' fought at [[Battle of Midway|Midway]] and [[Guadalcanal]].<ref>{{cite book"
            "|title=X}}</ref>{{sfn|Lundstrom|2005|p=3}} She sank{{citation needed|date=May 2024}}.<ref name=a/>")
    assert clean_text(text) == "USS Enterprise fought at Midway and Guadalcanal. She sank."


def test_word_bearing_templates_keep_their_words():
    text = ("{{USS|Enterprise|CV-6}} and {{ship|Japanese battleship|Yamato}} met a {{sclass|Fletcher|destroyer}} "
            "{{convert|300|nmi|km}} from {{nihongo|Admiral Yamamoto|山本|Yamamoto Isoroku}}{{'}}s base.")
    assert clean_text(text) == ("USS Enterprise (CV-6) and Japanese battleship Yamato met a Fletcher-class "
                                "destroyer 300 nmi from Admiral Yamamoto (Yamamoto Isoroku)'s base.")


def test_infobox_fields_become_label_lines():
    text = """{{Short description|Battle}}
{{Infobox military conflict
| conflict = Battle of Midway
| image = Midway.jpg
| date = 4–7 June 1942
| place = [[Midway Atoll]]
| commander1 = {{Plainlist|
* [[Chester W. Nimitz|Chester Nimitz]]
* [[Raymond A. Spruance|Raymond Spruance]]}}
| casualties2 = 3,057 {{KIA}}<ref>X</ref>
}}
The '''Battle of Midway''' was a naval battle."""
    assert clean_text(text) == """Conflict: Battle of Midway
Date: 4–7 June 1942
Place: Midway Atoll
Commander1: Chester Nimitz; Raymond Spruance
Casualties2: 3,057 (killed in action)

The Battle of Midway was a naval battle."""


def test_partof_is_labelled_part_of():
    assert clean_text("{{Infobox ship/career|partof='''LST Flotilla 14'''}}") == "Part of: LST Flotilla 14"
    assert clean_text("{{Infobox military conflict|part_of=the [[Pacific War]]}}") == "Part of: the Pacific War"


def test_nested_ship_infobox_sections_are_walked():
    text = """{{Infobox ship
|section1={{Infobox ship/image | image = A.jpg | image_caption = A}}
|section2={{Infobox ship/career
 | flag = {{USN flag|1945}}
 | name = ''LST-629''
 | laid_down = {{start date|1944|4|13}}
 }}
}}"""
    assert clean_text(text) == "Name: LST-629\nLaid down: 13 April 1944"


def test_generic_infobox_label_data_pairs():
    text = "{{Infobox|label1=Garrison|data1=[[Okinawa]]|label2=Empty|data2=|header3=Notes}}"
    assert clean_text(text) == "Garrison: Okinawa"


def test_headings_stay_and_reference_sections_are_cut():
    text = """Lead.

== Service ==
Body text.

=== 1944 ===
More.

== See also ==
* [[Other ship]]

== References ==
{{Reflist}}
=== Sources ===
* {{cite book|title=X}}

[[Category:Ships]]
[[File:Ship.jpg|thumb|A ship]]"""
    assert clean_text(text) == "Lead.\n\nService\n\nBody text.\n\n1944\n\nMore."


def test_table_rows_become_lines():
    text = """Operations:
{| class="wikitable"
|+ Japanese operations
|-
! scope="col" | Operation !! Year
|-
! scope="row" | [[Attack on Pearl Harbor|Operation AI]]
| 1941<ref>X</ref>
|-
| [[Battle of Midway|Operation MI]] || style="color:red" |
|}"""
    assert clean_text(text) == "Operations:\n\nJapanese operations\nOperation; Year\nOperation AI; 1941\nOperation MI"


def test_files_comments_and_entities_are_dropped():
    text = "Before.<!-- note -->\n[[File:X.jpg|thumb|cap [[Y]]]]\nWorld War&nbsp;II&mdash;the end."
    assert clean_text(text) == "Before.\n\nWorld War II—the end."


def test_unparsed_markup_is_still_removed():
    # A link spanning a line break is not parsed as a link; the leftovers pass still unwraps it.
    assert clean_text("at [[Mare Island\nNavy Yard]] on 3 July") == "at Mare Island Navy Yard on 3 July"


@pytest.mark.skipif(not CORPUS_PATH.exists(), reason="no Corpus export")
def test_corpus_has_no_markup_left():
    pq = pytest.importorskip("pyarrow.parquet")
    table = pq.read_table(CORPUS_PATH)
    assert table.column_names == ["qid", "text"]
    texts = table.column("text").to_pylist()
    marked = [q for q, t in zip(table.column("qid").to_pylist(), texts)
              if any(m in t for m in ("{{", "}}", "[[", "]]", "<ref", "'''", "Category:"))]
    assert not marked
    assert all(t.strip() for t in texts)
