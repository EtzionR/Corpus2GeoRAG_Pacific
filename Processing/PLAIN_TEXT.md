# How the Corpus text is cleaned

`Data/corpus_paragraphs.parquet` has one row per Article, sorted by `qid`:

| Column | What it holds |
|---|---|
| `qid` | The Article's Wikidata id |
| `text` | The article body as **Plain text**: `clean_text()` in `Processing/plain_text.py` |
| `part_of` | What the Article is part of, from its infobox: `part_of()` in the same file; null when the infobox has none |

Both are called from the Export cell of `crawl_corpus.ipynb`. Tests: `tests/test_plain_text.py`.

## What `text` looks like

```
The Battle of Midway was a major naval battle in the Pacific Theater of World War II that took place on
4–7 June 1942, six months after Japan's attack on Pearl Harbor …

Background

Before the battle, Japan desired to extend its Pacific defense perimeter …
```

Only the article body: prose paragraphs, section headings and table rows, separated by one blank line. No
infobox, no images, no link markup (the linked words stay), no references.

## The `part_of` column

The value of the infobox field `part_of` or `partof` (Wikipedia uses both spellings), as plain text:

| Article | `part_of` |
|---|---|
| Battle of Midway | `the Pacific War and World War II` |
| USS LST-908 | `LST Flotilla 14` |
| Western Australian emergency of March 19 | `the Pacific theatre of World War II` |

- A ship infobox has one section per career, and each can name a flotilla; the distinct values are joined with
  `; `: `LST Flotilla 5 (1943–1944); LST Flotilla 13 (1945)`. A list inside the field is joined the same way.
- Nested infoboxes and facts boxes not named `Infobox…` (`{{Operational plan}}`) are read too.
- Empty when the field is missing or holds only a placeholder comment (many military-base infoboxes have
  `part_of = <!-- for elements within a larger site -->`). Filled for 521 of the 5,692 Articles.

## What we keep in `text`

| In the wikitext | In the text |
|---|---|
| Prose paragraphs | Kept as paragraphs |
| Section headings (`== Service ==`) | A plain line: `Service` |
| Links `[[Battle of Midway\|Midway]]`, `[[Guadalcanal]]` | The shown words: `Midway`, `Guadalcanal` |
| External links `[http://… Title]` | `Title` (a bare URL is dropped) |
| Bold / italic `'''…'''`, `''…''` | The words, quotes removed |
| Bulleted / numbered lists | One line per item, bullet removed |
| Quotes (`<blockquote>`, `{{quote}}`, `{{blockquote}}`) | Their own paragraph |
| HTML entities (`&nbsp;`, `&mdash;`) | The character (non-breaking space becomes a plain space) |
| `<br>` | A line break |
| `<span>`, `<sup>`, `<small>`, `<div>` and similar wrappers | Their contents |

### Tables → `cell; cell; …` lines

A table becomes its caption on one line, then one line per row (header rows included), with the cells
joined by `; `. Cell styling (`style=…`, `scope=…`) and empty cells are removed.

```
List of Japanese operations during World War II
Operation; Year; Description
Operation FU; 1940; invasion of French Indo-China
Operation AI; 1941; attack on Pearl Harbor, Hawaii, United States
```

Many list Articles carry most of their content in tables, so tables are kept rather than dropped.

### Templates that carry words → those words

Templates are dropped unless they put words on the page. These are turned into their words:

| Template | Becomes |
|---|---|
| `{{USS\|Enterprise\|CV-6}}` (also HMS, HMAS, SS, MV, USNS, USCGC, HNLMS, … ) | `USS Enterprise (CV-6)` |
| `{{ship\|Japanese battleship\|Yamato}}`, `{{warship\|…}}` | `Japanese battleship Yamato` |
| `{{Jsub\|I-168}}`, `{{GS\|U-511}}` | `Japanese submarine I-168`, `German submarine U-511` |
| `{{sclass\|Fletcher\|destroyer}}` | `Fletcher-class destroyer` |
| `{{convert\|300\|nmi\|km}}`, `{{cvt\|…}}` | `300 nmi` (the source value and unit; the conversion is not computed) |
| `{{convert\|5\|to\|10\|mi}}` | `5 to 10 mi` |
| `{{nihongo\|Mitsuru Ushijima\|牛島 満\|Ushijima Mitsuru}}` | `Mitsuru Ushijima (Ushijima Mitsuru)` (kanji dropped) |
| `{{lang\|ja\|…}}`, `{{langx\|ja\|…}}`, `{{lang-zh\|t=…}}`, `{{zh\|t=…}}` | The foreign-language text |
| `{{birth date\|1887\|7\|31}}` (also death / start / end date, `…and age`) | `31 July 1887` |
| `{{date\|7 December 1941}}`, `{{circa\|1900}}`, `{{as of\|2015}}` | `7 December 1941`, `c. 1900`, `As of 2015` |
| `{{flag\|Empire of Japan\|name=Japan}}`, `{{flagcountry\|…}}` | `Japan` |
| `{{navy\|United States}}`, `{{army\|Empire of Japan}}` | `United States Navy`, `Imperial Japanese Army` |
| `{{GRT\|100,000}}`, `{{NRT\|…}}`, `{{DWT\|…}}` | `100,000 GRT` |
| `{{frac\|2\|1\|2}}` | `2 1/2` |
| `{{KIA}}` | ` (killed in action)` |
| `{{plainlist\|…}}`, `{{ubl\|…}}`, `{{hlist\|…}}`, `{{tree list\|…}}` | One item per line |
| `{{nowrap\|…}}`, `{{small\|…}}`, `{{abbr\|…}}`, `{{ill\|…}}`, `{{font color\|…}}` and other wrappers | Their text |
| `{{'}}`, `{{'s}}`, `{{nbsp}}`, `{{ndash}}`, `{{mdash}}`, `{{snd}}`, `{{okina}}`, `{{!}}` | The character(s) |

## What we remove from `text`

| In the wikitext | Why |
|---|---|
| Infoboxes (`{{Infobox …}}` and other facts boxes), including nested ship sections | Facts box beside the body, not the body; `part_of` is kept in its own column |
| References: `<ref>…</ref>`, `<ref name=… />`, `<references/>` | Citations, not article text |
| Footnote templates: `{{sfn}}`, `{{sfnp}}`, `{{sfnm}}`, `{{efn}}`, `{{refn}}`, `{{rp}}`, `{{r}}`, `{{#tag:ref}}` | Citations |
| Citation templates: `{{cite book}}`, `{{cite web}}`, `{{ISBN}}`, `{{OCLC}}`, `{{webarchive}}`, … | Citations |
| Maintenance tags: `{{citation needed}}`, `{{cn}}`, `{{clarify}}`, `{{dead link}}`, `{{page needed}}`, … | Editor notes |
| Page-setup templates: `{{Short description}}`, `{{Use dmy dates}}`, `{{DISPLAYTITLE}}`, `__NOTOC__` | Not content |
| Hatnotes: `{{Main}}`, `{{See also}}`, `{{Further}}` | Navigation |
| Navboxes, sidebars, campaign boxes, succession boxes, `{{Authority control}}`, `{{Commons category}}` | Navigation |
| Flag icons: `{{flagicon}}`, `{{flagdeco}}`, `{{USN flag}}`, `{{shipboxflag}}` | Images |
| `{{coord}}` | Coordinates, not prose |
| Ribbons, ship-class spec transclusions and any other template not listed above | No words of their own |
| Files and images: `[[File:…]]`, `[[Image:…]]`, `[[Media:…]]`, with their captions; `<gallery>` | Images |
| Categories: `[[Category:…]]` | Metadata |
| HTML comments `<!-- … -->` | Hidden in the article |
| `<math>`, `<chem>`, `<timeline>`, `<mapframe>`, `<graph>`, `<imagemap>`, `<score>`, `<syntaxhighlight>`, `<templatestyles>`, `<hr>` | Not prose |
| The closing sections, with their subsections: References, Notes, Footnotes, Citations, Sources, Bibliography, Works cited, General references, Cited sources, Endnotes, Explanatory notes, Notes and references, References and notes, Sources and further reading, Further reading, See also, External links, Gallery | Reference lists and links, not article text |

## Broken wikitext

About 2% of Articles have markup the parser cannot read (an unclosed bracket, a run of apostrophes such as
`''LST-1030'''s`, a link that spans a line break, a table opened by a template such as `{{avilisthead}}`).
Two things keep that markup out of the text:

- Bold and italic quotes are not parsed at all (`skip_style_tags`); they are removed afterwards. A stray quote
  run otherwise makes the parser give up on the whole template around it.
- A final pass cleans what is still left as text: `[[a|b]]` → `b`, stray `[[` / `]]` removed, any `{{…}}`
  removed, an unparsed `{| … |}` table removed, a table row `|a||b||c` → `a; b; c`, `|-` / `|}` lines removed,
  HTML entities decoded.

Whitespace is then tidied: runs of spaces become one, lines are trimmed, a space before `, . ; : )` and after
`(` is removed (left behind by dropped references), and more than one blank line becomes one.

A check over the whole Corpus (`test_corpus_has_no_markup_left`) finds no `{{`, `}}`, `[[`, `]]`, `<ref`, `'''` or
`Category:` in any Article, and no empty Article.

## Numbers (run of 2026-10-19, 5,692 Articles)

| | Wikitext | `text` (+ `part_of`) |
|---|---|---|
| Characters | 102.7 M | 53.5 M |
| Parquet file (ZSTD) | 25.5 MB | 14.9 MB |

## Changing the cleaning

Edit `Processing/plain_text.py`, add a case to `tests/test_plain_text.py`, and re-run the Export cell of
`crawl_corpus.ipynb` (it reads the Wikitext from `Data/interim/cache.sqlite`, so no network is needed).
