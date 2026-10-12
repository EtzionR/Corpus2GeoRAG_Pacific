# Pacific War Wikipedia Corpus

Collects English Wikipedia articles about the war between the United States and Japan in WWII and hands them to a consumer as a single title → wikitext JSON.

## Language

### Scope

**US–Japan engagement**:
The subject of the corpus: WWII events, people, and things in which both the United States and Japan were directly involved.
_Avoid_: Pacific War (broader — includes UK/Australia/USSR-only fighting), Pacific theatre

### Sources

**Article**:
A single English Wikipedia page in the main namespace, identified by its canonical (post-redirect) title.
_Avoid_: Page, entry, document

**Wikitext**:
The raw, uncleaned MediaWiki markup of a whole Article, including infoboxes, templates and references.
_Avoid_: Content, text, body

**Category**:
A Wikipedia category (`Category:…`); part of a cyclic graph, used for discovery, never as a Tag.
_Avoid_: Tag, label

**Seed category**:
A Category from which discovery starts.

**Blocked category**:
A Category whose subtree is never entered during discovery (e.g. films, memorials).
_Avoid_: Excluded category, blacklist

### Classification

**Tag**:
One of a fixed, project-defined set of article kinds (Person, Battle, Ship, …) that decides whether an Article belongs in the Corpus.
_Avoid_: Category, label, type

The Tags are: **Person**, **Battle**, **Campaign** (a group of Battles), **Operation** (a named plan, executed or not), **Military unit**, **Ship** (one vessel), **Ship class**, **Aircraft**, **Weapon/Equipment**, **Location**, **Event** (non-combat, incl. war crimes), **Organization**, **Diplomacy/Document**, **Intelligence**. Films, books, games, memorials and veterans' groups are never in scope.

**Candidate**:
An Article found during discovery that has not yet been tagged or scope-checked.

**In-scope Battle**:
An Article tagged Battle, Campaign or Operation whose participants include both the United States and Japan; the anchor every other Tag is scope-checked against.
_Avoid_: Core battle, key event

**Anchored**:
Said of an Article that is tied to In-scope Battles strongly enough to be in scope on its own: its Wikidata conflict, participant-in or significant-event claims name one (significant event does not count for a Person), it sits in a Category named after one, or it links to at least two (at most one of them an Umbrella conflict).

**Umbrella conflict**:
An In-scope Battle so broad (Pacific War, Second Sino-Japanese War, Burma campaign, …) that a tie to it says nothing about US–Japan engagement; it is in the Corpus itself but can never be an Article's only anchor.
_Avoid_: Theatre, war (as anchor)

**Gray zone**:
Articles the rules cannot decide: tied only through an Umbrella conflict, linked to exactly one In-scope Battle, or Anchored but untagged. Their fate is decided by Relevance; those Relevance cannot call are decided by Override, except that an Article tied only through Umbrella conflicts is left out.

**Relevance**:
How much closer an Article's text is to known in-scope Articles than to known out-of-scope ones (e5 embedding nearest neighbours); decides the Gray zone and is recorded for every Article.
_Avoid_: Similarity score, confidence

**Override**:
A hand-made include or exclude decision for a specific Article that beats every rule.

**Gold set**:
A hand-curated list of Articles known to be in scope, used to suggest Seed categories and as a recall test the Corpus must pass.
_Avoid_: Test set, examples

### Output

**Corpus**:
The final deliverable handed to the consumer: a JSON object mapping each included Article title to its Wikitext.
_Avoid_: Dataset, dump, output
