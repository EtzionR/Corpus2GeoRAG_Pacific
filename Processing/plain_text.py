"""An Article's wikitext as plain text, for the Corpus export in crawl_corpus.ipynb.
Infobox fields become `Label: value` lines and table rows become `cell; cell; …` lines where they sit; prose keeps
its paragraphs and section headings; references, citations, files, categories and the closing reference sections
are dropped.
Templates that carry words (ship names, unit conversions, Japanese names, dates…) are turned into those words;
any other template is dropped. What is kept and what is removed: PLAIN_TEXT.md."""
import html
import re

import mwparserfromhell
from mwparserfromhell.nodes import (Argument, Comment, ExternalLink, Heading, HTMLEntity, Tag, Template, Text,
                                    Wikilink)

DROP_SECTIONS = {
    "references", "notes", "footnotes", "citations", "sources", "bibliography", "see also", "further reading",
    "external links", "notes and references", "references and notes", "works cited", "general references",
    "cited sources", "endnotes", "explanatory notes", "sources and further reading", "gallery"}
SKIP_INFOBOX_PARAMS = re.compile(
    r"image|caption|alt$|map|module|footnotes|size|logo|ensign|badge|flag|hide|signature|upright|pushpin|relief|"
    r"embed|colou?r|style|width|label_position|onlysourced|qid|lang$", re.I)
DROP_TAGS = {"ref", "references", "gallery", "timeline", "mapframe", "maplink", "graph", "imagemap",
             "score", "hr", "syntaxhighlight", "templatestyles", "math", "chem"}
NON_TEXT_LINK = re.compile(r"^\s*(file|image|category|media)\s*:", re.I)
# Markup the parser left as text (an unclosed bracket nearby makes it give up on a region).
LEFTOVER_TABLE = re.compile(r"^\s*\{\|.*?^\s*\|\}", re.M | re.S)
LEFTOVER_LINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]]*)\]\]")
LEFTOVER_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
LEFTOVER_ROW = re.compile(r"^\s*[|!](?![-}+])")  # a table row whose table a template opened ({{avilisthead}})
INFOBOX_ROW = re.compile(r"(label|data|header)(\d+)$")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]

SHIP_PREFIXES = {"uss", "usns", "usat", "uscgc", "usc&gs", "hms", "hmas", "hmcs", "hmnzs", "hmis", "hnlms", "hnoms",
                 "ss", "ms", "mv", "rms", "rv", "sms", "ins", "jds", "js", "frs", "rocs", "rokn", "brp", "hmt", "hmhs"}
SHIP_KIND = {"jsub": "Japanese submarine", "gs": "German submarine"}
CONSTANTS = {"'": "'", "'s": "'s", "'\"": "'\"", "\"'": "\"'", "!": "|", "nbsp": " ", "spaces": " ",
             "ndash": "–", "mdash": "—", "snd": " – ", "spaced ndash": " – ", "kia": " (killed in action)",
             "okina": "ʻ"}
FIRST_PARAM = {"nowrap", "nobr", "nobold", "small", "smaller", "big", "midsize", "lower", "abbr", "sic", "em",
               "var", "mvar", "chem2", "date", "ill", "interlanguage link", "nihongo2", "marriage", "visible anchor",
               "anchor", "noitalic", "tooltip", "lang-ja", "transl"}
LISTS = {"plainlist", "flatlist", "ubl", "unbulletedlist", "hlist", "bulletedlist", "treelist", "collapsiblelist"}
DATES = {"birth date", "death date", "start date", "end date", "birth date and age", "death date and age",
         "start date and age", "end date and age", "dob", "film date", "birth-date", "death-date"}
SERVICES = {"navy": "Navy", "naval": "Navy", "army": "Army", "air force": "Air Force"}
IMPERIAL_JAPAN = {"Navy": "Imperial Japanese Navy", "Army": "Imperial Japanese Army",
                  "Air Force": "Imperial Japanese Army Air Service"}


def clean_text(wikitext):
    """The Article as plain text: infobox lines, then paragraphs separated by blank lines."""
    out = leftovers(render(parse(wikitext).nodes, top=True))
    lines = [leftover_row(line) for line in out.split("\n") if not re.match(r"^\s*\|[-}+]", line)]
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in lines]
    lines = [re.sub(r" ([,.;:)])", r"\1", re.sub(r"\( ", "(", line)) for line in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse(wikitext):
    # Bold/italic quotes stay as text and are removed by leftovers(): a stray run like ''LST-1030'''s
    # otherwise makes the parser give up on the whole template around it.
    return mwparserfromhell.parse(wikitext, skip_style_tags=True)


def leftovers(text):
    """Strip markup the parser did not recognise, and decode HTML entities."""
    text = LEFTOVER_TABLE.sub("", text)
    text = LEFTOVER_LINK.sub(lambda m: re.sub(r"\s+", " ", m[1]), text)
    text = text.replace("[[", "").replace("]]", "")  # unbalanced brackets in the source
    while LEFTOVER_TEMPLATE.search(text):
        text = LEFTOVER_TEMPLATE.sub("", text)
    return html.unescape(re.sub(r"__[A-Z]+__|'{2,}", "", text))


def leftover_row(line):
    """`|a||b||c` (or `!a!!b`) left as text -> `a; b; c`; any other line is returned as is."""
    if not LEFTOVER_ROW.match(line):
        return line
    cells = (cell.rsplit("|", 1)[-1].strip() for cell in re.split(r"\|\||!!", line.strip()[1:]))
    return "; ".join(c for c in cells if c)


def render(nodes, top=False):
    """Plain text of a node list. At the top level, closing reference sections are skipped."""
    parts, skip_level = [], None
    for node in nodes:
        if isinstance(node, Heading):
            title = re.sub(r"\s+", " ", render(node.title.nodes)).strip()
            if skip_level is not None and node.level > skip_level:
                continue
            skip_level = node.level if top and title.lower() in DROP_SECTIONS else None
            if skip_level is None:
                parts.append(f"\n\n{title}\n\n")
        elif skip_level is not None:
            continue
        elif isinstance(node, Text):
            parts.append(str(node.value))
        elif isinstance(node, Wikilink):
            if not NON_TEXT_LINK.match(str(node.title)):
                parts.append(render(node.text.nodes) if node.text else str(node.title).split("#")[0].lstrip(":"))
        elif isinstance(node, ExternalLink):
            parts.append(render(node.title.nodes) if node.title else "")
        elif isinstance(node, HTMLEntity):
            parts.append(node.normalize())
        elif isinstance(node, Tag):
            parts.append(render_tag(node))
        elif isinstance(node, Template):
            parts.append(render_template(node))
        elif not isinstance(node, (Comment, Argument)):
            parts.append(str(node))
    return "".join(parts)


def render_tag(tag):
    name = str(tag.tag).strip().lower()
    if name in DROP_TAGS:
        return ""
    if name == "br":
        return "\n"
    if name == "table":
        return "\n\n" + "\n".join(table_lines(tag)) + "\n\n"
    if name in ("blockquote", "poem"):
        return f"\n\n{render(tag.contents.nodes)}\n\n" if tag.contents else ""
    return render(tag.contents.nodes) if tag.contents else ""  # b, i, span, sup, li markers…


def table_lines(table):
    """The caption, then one `cell; cell; …` line per row (header rows included, empty cells left out)."""
    lines, row = [], []
    for node in table.contents.nodes if table.contents else []:
        if not isinstance(node, Tag):
            continue
        name = str(node.tag).strip().lower()
        if str(node).startswith("|+"):  # the parser reads a |+ caption as a cell starting with "+"
            lines.append(table_row([node]).lstrip("+ "))
        elif name in ("td", "th"):  # cells before the first |- belong to an implicit first row
            row.append(node)
        elif name == "tr":
            lines += [table_row(row), table_row([c for c in (node.contents.nodes if node.contents else [])
                                                 if isinstance(c, Tag) and str(c.tag).strip().lower() in ("td", "th")])]
            row = []
    lines.append(table_row(row))
    return [line for line in lines if line]


def table_row(cells):
    texts = (re.sub(r"\s+", " ", leftovers(render(c.contents.nodes))).strip() if c.contents else "" for c in cells)
    return "; ".join(t for t in texts if t)


def positional(tpl):
    return [render(p.value.nodes).strip() for p in tpl.params if not p.showkey]


def named(tpl, *names):
    for n in names:
        if tpl.has(n):
            return render(tpl.get(n).value.nodes).strip()
    return ""


def render_template(tpl):
    name = re.sub(r"\s+", " ", str(tpl.name).replace("_", " ")).strip().lower()
    args = positional(tpl)
    arg = lambda i: args[i] if len(args) > i else ""
    if name.startswith("infobox"):
        return "\n\n" + "\n".join(infobox_lines(tpl)) + "\n\n"
    if name in CONSTANTS:
        return CONSTANTS[name]
    if name in FIRST_PARAM:
        return arg(0)
    if name in SHIP_PREFIXES:  # {{USS|Enterprise|CV-6}} -> USS Enterprise (CV-6)
        return ship(str(tpl.name).strip(), arg(0), arg(1))
    if name in ("ship", "warship"):  # {{ship|Japanese battleship|Yamato}}
        return ship(arg(0), arg(1), arg(2))
    if name in SHIP_KIND:
        return ship(SHIP_KIND[name], arg(0), arg(1))
    if name in ("sclass", "sclass-", "sclass2"):  # {{sclass|Fletcher|destroyer}} -> Fletcher-class destroyer
        return f"{arg(0)}-class {arg(1)}".strip()
    if name in ("convert", "cvt"):
        return convert(args)
    if name == "nihongo":  # {{nihongo|English|漢字|rōmaji}} -> English (rōmaji)
        english, romaji = arg(0), arg(2)
        return f"{english} ({romaji})" if english and romaji else english or romaji or arg(1)
    if name in ("lang", "langx"):
        return arg(1)
    if name.startswith("lang-") or name == "zh":
        return named(tpl, "t", "s", "text", "1") or arg(0)
    if name in ("blockquote", "quote", "quotation"):
        return f"\n\n{named(tpl, 'text', 'quote') or arg(0)}\n\n"
    if name.replace(" ", "") in LISTS:  # {{ubl|a|b}} or {{Plain list|* a\n* b}} -> one item per line
        return "\n".join(item for a in args for item in a.split("\n") if item.strip())
    if name in DATES:
        return date(args)
    if name in ("circa", "c."):
        return f"c. {arg(0)}"
    if name == "as of":
        return f"As of {arg(0)}"
    if name in ("flag", "flagcountry", "flagu", "flagdeco country"):
        return named(tpl, "name") or arg(0)
    if name in SERVICES:
        service = SERVICES[name]
        return IMPERIAL_JAPAN[service] if arg(0) in ("Empire of Japan", "Japan") else f"{arg(0)} {service}"
    if name in ("grt", "nrt", "dwt"):
        return f"{arg(0)} {name.upper()}"
    if name == "frac":
        return f"{arg(0)} {arg(1)}/{arg(2)}" if len(args) == 3 else "/".join(args)
    if name == "font color":
        return args[-1] if args else ""
    return ""  # citations, footnotes, flags, navboxes, maintenance tags…


def ship(prefix, name, ident):
    return f"{prefix} {name}" + (f" ({ident})" if ident else "")


def convert(args):
    """{{convert|300|nmi|km}} -> 300 nmi; {{convert|5|to|10|mi}} -> 5 to 10 mi."""
    if len(args) >= 4 and args[1] in ("-", "–", "to", "and", "or", "by", "x", "×", "+", "to(-)", "and(-)"):
        return f"{args[0]} {args[1]} {args[2]} {args[3]}"
    return " ".join(args[:2])


def date(args):
    """{{birth date|1887|7|31}} -> 31 July 1887. Text dates pass through."""
    y, m, d = (args + ["", "", ""])[:3]
    if not (y.isdigit() and m.isdigit() and 1 <= int(m) <= 12):
        return y
    return " ".join(p for p in (str(int(d)) if d.isdigit() else "", MONTHS[int(m) - 1], y) if p)


def infobox_lines(tpl):
    """`Label: value` for every filled infobox field; nested infoboxes (ship sections) are walked in place."""
    lines, row_labels = [], {}
    for p in tpl.params:
        nested = [t for t in p.value.filter_templates(recursive=False)
                  if str(t.name).strip().lower().startswith("infobox")]
        if nested:
            for t in nested:
                lines += infobox_lines(t)
            continue
        key = str(p.name).strip()
        if not p.showkey or SKIP_INFOBOX_PARAMS.search(key):
            continue
        value = "; ".join(v for v in (re.sub(r"^[*#:;\s]+|\s+$", "", line)
                                      for line in leftovers(render(p.value.nodes)).split("\n")) if v)
        value = re.sub(r":;", ":", re.sub(r"\s+", " ", value)).strip(" ;,")
        row = INFOBOX_ROW.match(key)  # generic {{Infobox}}: label7 = Garrison, data7 = … -> Garrison: …
        if row and row[1] == "label":
            row_labels[row[2]] = value
        elif row and row[1] == "data" and row_labels.get(row[2]) and value:
            lines.append(f"{row_labels[row[2]]}: {value}")
        elif value and not row:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return lines
