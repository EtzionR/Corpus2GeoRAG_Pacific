# Unsupervised event discovery: project method and input contract

## Scope and terms

The goal is to discover **historical event instances** in the full Pacific War
Wikipedia corpus and export supported instances to the Agent graph. The method
must not require event labels from this project for training. General-purpose
parsers, language representations, and dictionaries may have been trained
elsewhere; their use must be recorded. Wikipedia links and event pages are
optional external evidence (**distant supervision**), not part of the strictly
unsupervised text-discovery claim.

Three objects must remain distinct:

- **Event mention:** one passage asserts or describes an occurrence, such as an
  attack, landing, sinking, declaration, or battle. It has a source span.
- **Event instance:** the particular occurrence that one or more mentions
  describe. It has a stable local ID even if it has no Wikipedia page.
- **Event type:** a reusable class such as *naval attack*. A type cluster is not
  an event instance and cannot answer when or where one battle happened.

An event is an occurrence or bounded process situated in historical time. It
may have an actor, affected entity, place, and time, but not every field is
required to *detect a candidate*. States such as “X was based at Y” are
relations; changes such as “X moved to Y” are event candidates. A plan, denial,
prediction, or hypothetical attack must not become an asserted attack. A war,
campaign, battle, and individual raid may each be events at different levels;
their part-of relation does not make them the same instance.

## Current files and entity types

`Data/DATA.json` currently maps **428 Wikipedia article titles to article
text**. It is the available text corpus, not a list of 428 event pages.
`Data/entities_table.csv` has **1,002 raw NER mentions**, all from the single
source article `Attack on Pearl Harbor`. It is a worked example of an upstream
handoff, **not entity coverage for the full corpus**. Its columns are
`entity_group, score, word, start, end, Source, Index, Text`. `Source` names
the article containing the mention; `Index` identifies its text chunk;
`start`/`end` locate the mention within that chunk. `score` concerns the NER
label, not entity identity, event truth, or geocoding quality.

| Raw label | Current example rows | Meaning at handoff | Graph treatment |
| --- | ---: | --- | --- |
| `LOC` | 495 | Candidate geographic mention | Resolve to `location` only after type and identity checks. |
| `ORG` | 108 | Candidate organization or military unit | Resolve to `org` when supported. |
| `PER` | 129 | Candidate person | Resolve to `person` when supported. |
| `MISC` | 270 | Mixed residual label: may include operations, wars, ships, nationalities, and other expressions | Inspect context; do not map all rows to one graph type. |

These are **mention labels**, not validated identity types. For example, the
first Pearl Harbor chunk labels `Empire` and `Fleet` as `LOC`, `of Japan` as
`ORG`, and `US Pacific` as `MISC`. Those fragments must not be promoted to
canonical entities without checking the full phrase and source. The current
CSV has no `event` or `time` label. That does not mean events and dates are
absent from the article text. The Agent currently documents `location`,
`person`, `org`, and `event` nodes; it permits more node types, but a new type
needs an explicit downstream use case and validation.

For the complete handoff, upstream extraction must cover every relevant
source article, or supply a manifest that identifies which pages have no
entity output. Unknown coverage must remain unknown. Identity normalization
creates stable person, organization, place, and other supported IDs from raw
mentions before event arguments are linked to those IDs. Event discovery must
also read the full text: an entity table alone cannot supply all event
predicates, dates, negation, or mention boundaries.

## Original Wikipedia pages and provenance

Store two different page roles:

1. **Source page:** the article whose text contains the evidence. Keep its
   title, URL, paragraph/chunk index, sentence or character offsets, and text.
2. **Referent page:** a Wikipedia page about the identified entity or event,
   if one exists and the match is supported. Keep its resolved title, URL,
   page ID or Wikidata ID when verified, and the evidence for the link.

The source page is not automatically the referent page. An article about a
person can mention several battles; an article about a battle can mention
people and places. A Wikipedia page title is also not proof that every
sentence on that page describes the title event. A URL constructed from a
title points to the **current** page; it does not identify the exact revision
used to create `DATA.json`. The current files do not provide source revision
IDs or the original hyperlink targets. If these can be recovered from the
upstream crawl, preserve the page ID, revision ID, collection time, resolved
redirect, and original link target. Otherwise mark revision and link target
as unknown; do not invent them. See the [MediaWiki revisions API](https://www.mediawiki.org/wiki/API:Revisions)
and the [WEC Wikipedia link extraction method](https://aclanthology.org/2021.naacl-main.198/).

Verified live page examples relevant to the Pearl Harbor sample follow. A
live page is a **candidate referent**, not proof that the local text came from
its current revision or that an NER fragment was linked to it.

| Role in example | Wikipedia page | In `DATA.json` now? |
| --- | --- | --- |
| Evidence source and candidate attack instance | [Attack on Pearl Harbor](https://en.wikipedia.org/wiki/Attack_on_Pearl_Harbor) | Yes |
| Geopolitical actor | [Empire of Japan](https://en.wikipedia.org/wiki/Empire_of_Japan) | Yes |
| Military organization | [United States Pacific Fleet](https://en.wikipedia.org/wiki/United_States_Pacific_Fleet) | No |
| Place | [Pearl Harbor](https://en.wikipedia.org/wiki/Pearl_Harbor) | No |
| Wider place | [Oʻahu](https://en.wikipedia.org/wiki/Oahu) | No |
| Wider place | [Hawaii](https://en.wikipedia.org/wiki/Hawaii) | No |
| Person, mentioned later in the sample article | [Isoroku Yamamoto](https://en.wikipedia.org/wiki/Isoroku_Yamamoto) | No |
| Separate event mentioned in the opening chunk | [United States declaration of war on Japan](https://en.wikipedia.org/wiki/United_States_declaration_of_war_on_Japan) | No |

The corpus can mention a referent whose own page is outside the corpus.
Absence of a referent page from `DATA.json` is not evidence that the entity or
event is absent from the text.

## Discovery procedure

### 1. Prepare evidence, not event labels

For each source page, keep paragraph and sentence boundaries, original text,
and stable offsets. Attach raw entity mentions and resolved entity IDs where
available. Extract raw temporal expressions from text and retain their spans;
normalize the historical event time separately from article publication or
edit time. Preserve uncertainty and date precision. Optional raw Wikipedia
links can provide direct candidate referent pages.

### 2. Generate event mentions from sentence structure

Use two complementary paths without training on project event labels:

- Extract predicate–argument claims with a dependency parser or OpenIE tool.
  Keep the action phrase, grammatical roles, and original sentence.
- Examine short dependency paths among co-mentioned entities and time
  expressions. Retain a path only when its words support an occurrence or
  transition. This can recover event mentions that a simple subject–verb–
  object triple misses.

Also check event-noun phrases such as “the invasion of X” and “the sinking of
Y”; verbs alone are insufficient. Do **not** generate the Cartesian product
of all entities in a paragraph. Mere co-occurrence is only a retrieval clue.
The extraction and entity-path design follows [Rusu et al.](https://aclanthology.org/W14-2905.pdf).
[Stanford OpenIE](https://stanfordnlp.github.io/CoreNLP/openie.html) and
[MinIE](https://github.com/uma-pi1/minie) are public candidate extractors;
neither creates final historical event identities by itself.

For every candidate, record its exact source span, action or event noun,
arguments and roles, raw time/place expressions, source page, and extraction
method. Record whether the passage asserts an occurrence, negates it, reports
a plan, quotes a claim, or is uncertain. Keep an `unresolved` state when this
cannot be decided. A sentence can yield several candidates; one candidate
can have no resolved named entity yet.

### 3. Separate event types from event instances

If types are needed for search or graph analysis, cluster event expressions
from the corpus. [ETypeClus](https://aclanthology.org/2021.emnlp-main.441/)
([code](https://github.com/mickeysjm/ETypeClus)) is a public, corpus-label-free
method for this task. A discovered type is optional metadata on an instance.
Never use a type-cluster ID as the historical event ID.

### 4. Resolve mentions to event instances

Compare candidate mentions using action meaning, normalized participant
identities and their roles, **historical** date intervals, place identities,
and local context. Compatible missing fields are unknown, not matches. Strong
positive evidence includes a verified link to the same event page. Strong
conflicts include incompatible dates or places, opposed participant roles,
different event granularity, and explicit references to separate subevents.
No single shared participant or place is sufficient to merge two events.
Avoid transitive merges in which A resembles B and B resembles C but A and C
conflict. Keep ambiguous candidates as separate or unresolved instances.
The separation of mention extraction from unsupervised instance coreference
is supported by [Bejan and Harabagiu](https://aclanthology.org/J14-2004/).

Wikipedia event pages and links can anchor known named instances as in
[WEC](https://aclanthology.org/2021.naacl-main.198/) and its
[extraction repository](https://github.com/AlonEirew/extract-wec). This is
**external/distant supervision** and must be reported separately from the
text-only unsupervised path. Events without a page still receive local IDs.
An event page may describe a larger campaign while a passage describes one
battle; check granularity before linking or merging them.

### 5. Validate and export

Retain each event's supporting mentions and source locations, canonical name
and aliases when supported, normalized start/end date with precision, place
links, participants and roles, and unresolved fields. Export only supported
event instances as `event` nodes. The Agent contract in `Agent/README.md`
requires dates in event attributes, `OCCURRED_AT` edges to location nodes,
and source evidence. Other edges, including participation and attack
relations, need their own textual support. Geocode resolved locations; do not
invent a point from an event name. Validate IDs, edge endpoints, date
intervals, geometry, and source references before export.

## Pearl Harbor worked example, not a corpus assumption

The local opening chunk of `Attack on Pearl Harbor` describes a Japanese
attack on the US Pacific Fleet at Pearl Harbor on 7 December 1941. A text
extractor should produce an **attack mention** with action, actor, target,
place, time, and source span. It also says the US declared war on Japan the
next day. That is a **second event candidate**, with a different action and
date. The first chunk's current NER fragments do not by themselves establish
either event. A verified link to the attack's Wikipedia page can help resolve
the first instance; the source sentence remains the evidence for its fields.
The second event can be represented even though its own page is not in the
428-article corpus.

## Acceptance checks

- Run over the **full supplied corpus**, and report exact article, paragraph,
  and sentence denominators. Report entity-handoff coverage separately.
- Produce inspectable mention records before any event-instance merge. Every
  exported event and relation must trace to a source span.
- Distinguish text-only candidates, Wikipedia-anchored candidates, rejected
  candidates, and unresolved cases. Do not call Wikipedia anchors strictly
  unsupervised.
- Audit a page-level sample from across the corpus, including articles about
  people, places, organizations, and events. Measure candidate precision,
  missed event mentions, false merges, split duplicates, date/place errors,
  and unsupported Agent edges. Manual evaluation does not make the discovery
  model supervised unless its labels are used to train or tune that model.
- Check that different events in one passage stay separate and that repeated
  mentions of one occurrence can share an instance ID. Keep war/campaign/
  battle/subevent levels distinct.

This document specifies a proposed project pipeline. The individual cited
methods were evaluated on other corpora; their combined performance on this
Pacific War corpus has not yet been measured.
