"""The text the AI check reads for an Article. Used by crawl_corpus.ipynb (e5); experiments that must read the
same text import it from here.
Changing evidence_text() means bumping E5_TEXT in crawl_corpus.ipynb, so cached vectors are recomputed."""
import re

import mwparserfromhell

REF_RE = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
WAR_SENTENCE = re.compile(
    r"Japan|Japanese|Pacific|Imperial|kamikaze|Okinawa|Leyte|Guadalcanal|Midway|Iwo Jima|Saipan|Philippine|"
    r"Solomon|New Guinea|Marianas|Truk|Pearl Harbor|Burma|China|Chinese|Atlantic|Europe|German|Normandy|"
    r"Mediterranean|U-boat|Italy", re.I)


def sentences(wikitext):
    """The Article's plain-text sentences, refs and markup removed."""
    body = re.sub(r"\s+", " ", mwparserfromhell.parse(REF_RE.sub("", wikitext)).strip_code())
    return re.split(r"(?<=[.!?])\s+", body)


def evidence_text(title, wikitext, max_sentences=25, max_chars=2500, prefix="passage: "):
    """What the AI check reads: the first two sentences plus every sentence naming a theatre or belligerent,
    from the whole Article (the lead alone often never says where the subject fought).
    `prefix` is e5's passage marker; pass "" for a reader that does not use one."""
    sents = sentences(wikitext)
    picked = sents[:2] + [s for s in sents[2:] if WAR_SENTENCE.search(s)]
    return f"{prefix}{title}. " + " ".join(picked[:max_sentences])[:max_chars]
