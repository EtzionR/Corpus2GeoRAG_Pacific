"""Unit tests for Processing/plain_text.py on small hand-written wikitext, plus a markup check on the Corpus."""
from pathlib import Path

import pytest
from plain_text import clean_text, part_of

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


MIDWAY = """{{Short description|Battle}}
{{Infobox military conflict
| conflict = Battle of Midway
| partof = the [[Pacific War]] and [[World War II]]<ref>X</ref>
| image = Midway.jpg
| date = {{start date|1942|6|4}}
| commander1 = {{Plainlist|
* [[Chester W. Nimitz|Chester Nimitz]]
* [[Raymond A. Spruance|Raymond Spruance]]}}
}}
The '''Battle of Midway''' was a naval battle."""


def test_infobox_is_left_out_of_the_body():
    assert clean_text(MIDWAY) == "The Battle of Midway was a naval battle."


def test_part_of_comes_from_either_field_name():
    assert part_of(MIDWAY) == "the Pacific War and World War II"
    assert part_of("{{Infobox ship/career|part_of='''LST Flotilla 14'''}}") == "LST Flotilla 14"


def test_part_of_reads_nested_ship_sections_and_joins_distinct_values():
    text = """{{Infobox ship
|section2={{Infobox ship/career | name = LST-629 | partof = LST Flotilla 7 }}
|section3={{Infobox ship/career | hide_header = yes | partof = LST Flotilla 7 }}
|section4={{Infobox ship/career | country = Singapore | partof = {{ubl|191 Squadron|[[RSN Navy|RSN]]}} }}
}}"""
    assert part_of(text) == "LST Flotilla 7; 191 Squadron; RSN"


def test_no_part_of_is_none():
    assert part_of("{{Infobox person|name=X|part_of=}} Text with [[part of]] a link.") is None


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
    assert table.column_names == ["qid", "text", "part_of"]
    texts = table.column("text").to_pylist()
    marked = [q for q, t in zip(table.column("qid").to_pylist(), texts)
              if any(m in t for m in ("{{", "}}", "[[", "]]", "<ref", "'''", "Category:"))]
    assert not marked
    assert all(t.strip() for t in texts)
