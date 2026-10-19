"""Review and explore the Pacific War Corpus.

Run from the repo root:  .venv/bin/streamlit run App/corpus_review_app.py
(The project UI is App/app.py; this is the corpus review app the Processing docs call "the app".)
Reads the outputs of Processing/crawl_corpus.ipynb (Data/interim/) and writes hand decisions to
Processing/config/overrides.yaml. Vocabulary: see CONTEXT.md.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import altair as alt
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "Data" / "interim"
CONFIG = ROOT / "Processing" / "config"
OVERRIDES_PATH = CONFIG / "overrides.yaml"
AUDIT_PATH = CONFIG / "corpus_audit.yaml"
EVAL_PATH = CONFIG / "eval_set.yaml"
EVAL_DRAFTS_PATH = ROOT / "Processing" / "reports" / "eval_log.tsv"
CORPUS_PATH = ROOT / "Data" / "corpus.json"
AUDIT_LABELS = ["keep", "marginal", "wrong"]

st.set_page_config(page_title="Pacific War Corpus", page_icon="⚓", layout="wide")


# ---------- data ----------
def mtime(path):
    return path.stat().st_mtime if path.exists() else 0


@st.cache_data
def load_rows(_stamp):
    frames = []
    for name in ("tagged", "excluded", "untagged"):
        path = INTERIM / f"{name}.jsonl"
        if path.exists():
            df = pd.read_json(path, lines=True)
            frames.append(df)
    rows = pd.concat(frames, ignore_index=True)
    rows["primary_tag"] = rows["primary_tag"].fillna("(untagged)")
    rows["wikipedia"] = "https://en.wikipedia.org/wiki/" + rows["title"].map(lambda t: quote(t.replace(" ", "_")))
    return rows


@st.cache_data
def load_knn(_stamp):
    path = INTERIM / "knn_gray_zone.csv"
    return pd.read_csv(path)[["title", "knn_score"]] if path.exists() else pd.DataFrame(columns=["title", "knn_score"])


@st.cache_data
def load_knn_results(_stamp):
    path = INTERIM / "knn_results.json"
    return json.loads(path.read_text()) if path.exists() else None


@st.cache_resource
def cache_db():
    return sqlite3.connect(f"file:{INTERIM / 'cache.sqlite'}?mode=ro", uri=True, check_same_thread=False)


def page_record(title):
    row = cache_db().execute("SELECT value FROM kv WHERE ns='page' AND key=?", (title,)).fetchone()
    return json.loads(row[0]) if row else {}


def wikitext(title):
    row = cache_db().execute("SELECT wikitext FROM wikitext WHERE title=?", (title,)).fetchone()
    return row[0] if row else ""


def load_overrides():
    data = yaml.safe_load(OVERRIDES_PATH.read_text()) or {}
    return {"include": list(data.get("include") or []), "exclude": list(data.get("exclude") or [])}


def save_overrides(include, exclude):
    header = [l for l in OVERRIDES_PATH.read_text().splitlines() if l.startswith("#")]
    body = yaml.safe_dump({"include": sorted(set(include)), "exclude": sorted(set(exclude))},
                          allow_unicode=True, sort_keys=False)
    OVERRIDES_PATH.write_text("\n".join(header) + "\n" + body)


def load_audit():
    """{stratum: {title: label or None}} from corpus_audit.yaml (drawn by Processing/audit_sample.py)."""
    return (yaml.safe_load(AUDIT_PATH.read_text()) or {}) if AUDIT_PATH.exists() else {}


def save_audit(audit):
    header = [l for l in AUDIT_PATH.read_text().splitlines() if l.startswith("#")]
    AUDIT_PATH.write_text("\n".join(header) + "\n" + yaml.safe_dump(audit, allow_unicode=True, sort_keys=False))


def load_eval():
    """{section: {title: label or None}} from eval_set.yaml (drawn by Processing/eval_sample.py)."""
    return (yaml.safe_load(EVAL_PATH.read_text()) or {}) if EVAL_PATH.exists() else {}


def save_eval(sections):
    header = [l for l in EVAL_PATH.read_text().splitlines() if l.startswith("#")]
    EVAL_PATH.write_text("\n".join(header) + "\n" + yaml.safe_dump(sections, allow_unicode=True, sort_keys=False))


def eval_drafts():
    """Claude's draft label and reason per title (reports/eval_log.tsv)."""
    if not EVAL_DRAFTS_PATH.exists():
        return pd.DataFrame(columns=["title", "draft", "why"])
    return pd.read_csv(EVAL_DRAFTS_PATH, sep="\t", keep_default_na=False)[["title", "draft", "why"]]


def gold_titles():
    gold = yaml.safe_load((CONFIG / "gold_set.yaml").read_text())
    return {t: tag for tag, titles in gold.items() for t in titles}


if not (INTERIM / "tagged.jsonl").exists():
    st.error("No pipeline output found. Run `Processing/crawl_corpus.ipynb` first.")
    st.stop()

stamp = mtime(INTERIM / "tagged.jsonl")
rows = load_rows(stamp)
if "knn_score" not in rows:  # runs before kNN joined the pipeline: the experiment's scores
    rows = rows.merge(load_knn(mtime(INTERIM / "knn_gray_zone.csv")), on="title", how="left")
if "knn_neighbours" not in rows:
    rows["knn_neighbours"] = [[] for _ in range(len(rows))]
overrides = load_overrides()
# The Gray zone, plus recovery candidates: left out by the rules, but e5 and kNN both say related.
gray = rows[(rows["rule_verdict"] == "gray") | (rows["decided_by"] == "recovery")].copy()
gray["decision"] = gray["decided_by"].map({"review": "review", "override": "override", "recovery": "recovery"}).fillna(
    gray["in_scope"].map({True: "accept", False: "reject"}))


# ---------- sidebar ----------
with st.sidebar:
    st.title("⚓ Pacific War Corpus")
    st.caption("US–Japan engagement in WWII · English Wikipedia")
    st.markdown(f"**Pipeline output:** {pd.Timestamp(stamp, unit='s'):%Y-%m-%d %H:%M}")
    pending = len(set(overrides["include"]) | set(overrides["exclude"])) - int((rows["decided_by"] == "override").sum())
    st.markdown(f"**Overrides:** {len(overrides['include'])} include · {len(overrides['exclude'])} exclude")
    if pending > 0:
        st.warning(f"{pending} override(s) not applied yet. Re-run the pipeline.")
    if st.button("▶ Re-run pipeline", width="stretch",
                 help="Executes crawl_corpus.ipynb (~12 s when everything is cached)"):
        with st.spinner("Running crawl_corpus.ipynb…"):
            proc = subprocess.run([sys.executable, str(ROOT / "Processing" / "run_notebook.py"),
                                   str(ROOT / "Processing" / "crawl_corpus.ipynb")],
                                  capture_output=True, text=True, cwd=ROOT)
        if proc.returncode == 0:
            st.cache_data.clear()
            st.success("Pipeline finished.")
            st.rerun()
        else:
            st.error("Pipeline failed")
            st.code(proc.stderr[-3000:])
    st.divider()
    st.caption("Docs: `Processing/PIPELINE.md` · `CONTEXT.md` · `Processing/reports/knn_experiment.md`")

overview_tab, review_tab, eval_tab, audit_tab, explore_tab, experiments_tab = st.tabs(
    ["📊 Overview", "⏳ Review", "📏 Evaluation", "🧾 Audit", "🔎 Explore", "🧪 Experiments"])


# ---------- overview ----------
with overview_tab:
    corpus = rows[rows["in_scope"]]
    by_rules = int(((rows["decided_by"] == "rules") & rows["in_scope"]).sum())
    by_ai = int(((rows["decided_by"] == "e5") & rows["in_scope"]).sum())
    by_override = int(((rows["decided_by"] == "override") & rows["in_scope"]).sum())
    waiting = int((gray["decision"] == "review").sum())
    gold = gold_titles()
    in_gold = sum(t in set(corpus["title"]) for t in gold)

    st.markdown(
        "#### The Corpus\n"
        "The **Corpus** is the deliverable: `Data/corpus.json`, one JSON file mapping each English Wikipedia "
        "article title to its full, raw article text (wikitext), for every article about the war between the "
        "**United States and Japan in WWII** (battles, people, ships, units, aircraft, weapons, places, events).")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("In the Corpus", f"{len(corpus):,}",
              help=f"Articles in corpus.json now: {by_rules:,} by the rules + {by_ai:,} by the AI check"
                   + (f" + {by_override:,} by your overrides" if by_override else "")
                   + f". The {waiting:,} articles waiting for review are not included yet.")
    c2.metric("Examined", f"{len(rows):,}",
              help="Every article found in the Wikipedia topic categories and through Wikidata, downloaded and classified.")
    c3.metric("Waiting for review", f"{waiting:,}",
              help="Articles the AI check could not call either way (the review band). Left out of the Corpus "
                   "until a person decides. Use the Review tab.")
    c4.metric("Gold recall", f"{in_gold}/{len(gold)}",
              help="A hand-picked must-have list (Midway, Nimitz, Yamato, Enola Gay…). Every one must be in the Corpus.")
    c5.metric("Corpus file", f"{CORPUS_PATH.stat().st_size / 1e6:,.0f} MB" if CORPUS_PATH.exists() else "–",
              help="Size of Data/corpus.json.")

    with st.expander(f"Where do the {len(corpus):,} articles come from?", expanded=True):
        breakdown = pd.DataFrame([
            {"stage": "Examined", "articles": len(rows), "meaning": "Found in Wikipedia topic categories or via Wikidata, downloaded and classified"},
            {"stage": "Clearly in by the rules", "articles": by_rules, "meaning": "Battles where both the US and Japan fought, and articles strongly tied to them"},
            {"stage": "Clearly out by the rules", "articles": int(((rows["decided_by"] == "rules") & ~rows["in_scope"]).sum()), "meaning": "Media, other wars and theatres, or no tie to any US–Japan battle"},
            {"stage": "Unsure, passed to the AI check", "articles": len(gray), "meaning": "Weak tie: only to a whole war, or to a single battle"},
            {"stage": "  ↳ accepted by the AI check", "articles": by_ai, "meaning": "Confidently about US–Japan fighting"},
            {"stage": "  ↳ rejected by the AI check", "articles": int(((rows["decided_by"] == "e5") & ~rows["in_scope"]).sum()), "meaning": "Confidently not about US–Japan fighting"},
            {"stage": "  ↳ waiting for human review", "articles": waiting, "meaning": "Too close to call; left out until reviewed"},
            {"stage": "Changed by your overrides", "articles": int((rows["decided_by"] == "override").sum()), "meaning": "Hand decisions from overrides.yaml (applied on the last pipeline run)"},
        ])
        st.dataframe(breakdown, hide_index=True, width="stretch",
                     column_config={"articles": st.column_config.NumberColumn(format="%d")})
        st.markdown(f"**In the Corpus = {by_rules:,} (rules) + {by_ai:,} (AI check)"
                    + (f" + {by_override:,} (overrides)" if by_override else "") + f" = {len(corpus):,}**")
        st.caption("Terms: **Article**, one Wikipedia page · **Rules**, fixed checks on battle participants and ties to "
                   "battles · **AI check**, an e5 language model reads each unsure article's war-related sentences · "
                   "**Review band**, articles the AI could not call · **Override**, a hand decision that beats every rule. "
                   "Full explanation: `Processing/PIPELINE.md`.")

    left, right = st.columns(2)
    with left:
        st.subheader("How every article was decided")
        flow = pd.DataFrame({
            "step": ["Rules", "Rules", "AI check", "AI check", "Human review", "Overrides", "Overrides"],
            "outcome": ["Included", "Left out", "Included", "Left out", "Waiting", "Included", "Left out"],
            "count": [
                int(((rows["decided_by"] == "rules") & rows["in_scope"]).sum()),
                int(((rows["decided_by"] == "rules") & ~rows["in_scope"]).sum()),
                int(((rows["decided_by"] == "e5") & rows["in_scope"]).sum()),
                int(((rows["decided_by"] == "e5") & ~rows["in_scope"]).sum()),
                int((rows["decided_by"] == "review").sum()),
                int(((rows["decided_by"] == "override") & rows["in_scope"]).sum()),
                int(((rows["decided_by"] == "override") & ~rows["in_scope"]).sum()),
            ]})
        st.altair_chart(alt.Chart(flow).mark_bar().encode(
            x=alt.X("count:Q", title="articles"), y=alt.Y("step:N", sort=["Rules", "AI check", "Human review", "Overrides"], title=None),
            color=alt.Color("outcome:N", scale=alt.Scale(domain=["Included", "Left out", "Waiting"],
                                                          range=["#2f9e44", "#e03131", "#f08c00"])),
            tooltip=["step", "outcome", "count"]), width="stretch")
    with right:
        st.subheader("What the Corpus contains")
        tags = corpus["primary_tag"].value_counts().rename_axis("tag").reset_index(name="articles")
        st.altair_chart(alt.Chart(tags).mark_bar(color="#1c7ed6").encode(
            x="articles:Q", y=alt.Y("tag:N", sort="-x", title=None), tooltip=["tag", "articles"]), width="stretch")

    missing = [t for t in gold if t not in set(corpus["title"])]
    if missing:
        st.error("Gold articles missing from the Corpus: " + ", ".join(missing))
    else:
        st.success(f"All {len(gold)} gold-set articles are in the Corpus.")


# ---------- review ----------
with review_tab:
    st.subheader("Gray zone review")
    st.caption("Articles the rules could not settle, and **recovery** candidates: Articles the rules tied to no battle "
               "although e5 and kNN both say related. Mark **keep** or **drop** and save; decisions go to "
               "`overrides.yaml`, beat every rule, and also recalibrate the AI check on the next pipeline run. "
               "*Nearest decided* shows the past decisions an Article looks like.")
    f1, f2, f3, f4 = st.columns([2, 2, 2, 3])
    decisions = f1.multiselect("AI decision", ["review", "recovery", "accept", "reject", "override"],
                               default=["review", "recovery"])
    tag_filter = f2.multiselect("Tag", sorted(gray["primary_tag"].unique()))
    hide_done = f3.toggle("Hide already overridden", value=True)
    search = f4.text_input("Search title", placeholder="e.g. USS, Maru, Division")

    view = gray[gray["decision"].isin(decisions)]
    if tag_filter:
        view = view[view["primary_tag"].isin(tag_filter)]
    done = set(overrides["include"]) | set(overrides["exclude"])
    if hide_done:
        view = view[~view["title"].isin(done)]
    if search:
        view = view[view["title"].str.contains(search, case=False, regex=False)]
    view = view.sort_values("relevance", ascending=False)

    band_total = int((gray["decision"] == "review").sum())
    band_done = int(gray[(gray["decision"] == "review") & gray["title"].isin(done)].shape[0])
    st.progress(band_done / max(band_total, 1), text=f"Review band: {band_done:,} of {band_total:,} decided")
    rec_total = int((gray["decision"] == "recovery").sum())
    rec_done = int(gray[(gray["decision"] == "recovery") & gray["title"].isin(done)].shape[0])
    st.progress(rec_done / max(rec_total, 1), text=f"Recovery candidates: {rec_done:,} of {rec_total:,} decided")

    page_size = 50
    pages = max(1, -(-len(view) // page_size))
    page = st.number_input(f"Page (of {pages}) · {len(view):,} articles", 1, pages, 1)
    chunk = view.iloc[(page - 1) * page_size: page * page_size].copy()
    chunk["override"] = chunk["title"].map(
        lambda t: "keep" if t in overrides["include"] else "drop" if t in overrides["exclude"] else "")
    chunk["lead"] = chunk["title"].map(lambda t: page_record(t).get("lead_text", "")[:220])
    chunk["nearest"] = chunk["knn_neighbours"].map(lambda n: " · ".join(n) if isinstance(n, list) else "")

    edited = st.data_editor(
        chunk[["override", "title", "wikipedia", "primary_tag", "relevance", "knn_score", "decision", "nearest", "reason",
               "lead"]],
        column_config={
            "override": st.column_config.SelectboxColumn("Your call", options=["", "keep", "drop"], width="small"),
            "title": st.column_config.TextColumn("Article", disabled=True, width="medium"),
            "wikipedia": st.column_config.LinkColumn("Link", display_text="open ↗", width="small"),
            "primary_tag": st.column_config.TextColumn("Tag", disabled=True, width="small"),
            "relevance": st.column_config.ProgressColumn("AI score", min_value=-0.05, max_value=0.12, format="%.3f"),
            "knn_score": st.column_config.ProgressColumn("kNN", min_value=0.0, max_value=1.0, format="%.2f"),
            "decision": st.column_config.TextColumn("AI decision", disabled=True, width="small"),
            "nearest": st.column_config.TextColumn("Nearest decided", disabled=True, width="medium"),
            "reason": st.column_config.TextColumn("Why it is unsure", disabled=True),
            "lead": st.column_config.TextColumn("Opening text", disabled=True, width="large"),
        },
        hide_index=True, width="stretch", height=560, key=f"editor-{page}-{len(view)}")

    if st.button("💾 Save decisions", type="primary"):
        include, exclude = set(overrides["include"]), set(overrides["exclude"])
        for _, r in edited.iterrows():
            include.discard(r["title"]); exclude.discard(r["title"])
            if r["override"] == "keep":
                include.add(r["title"])
            elif r["override"] == "drop":
                exclude.add(r["title"])
        save_overrides(include, exclude)
        st.success(f"Saved: {len(include)} include · {len(exclude)} exclude. Re-run the pipeline to apply.")
        st.rerun()


# ---------- audit ----------
with audit_tab:
    st.subheader("Corpus audit")
    st.caption("A random sample of Articles already **in** the Corpus, let in by the rules or the AI check. Label each "
               "one; the labels estimate how much of the Corpus is wrong or marginal and test any filter once. "
               "**keep**: about US–Japan fighting in WWII · **marginal**: tied to it only in passing · "
               "**wrong**: not about US–Japan fighting. Saved to `config/corpus_audit.yaml`.")
    audit = load_audit()
    if not audit:
        st.info("No sample yet. Run `python Processing/audit_sample.py`.")
    else:
        sample = pd.DataFrame([{"stratum": s, "title": t, "label": l or ""} for s, d in audit.items() for t, l in d.items()])
        sample = sample.merge(rows[["title", "wikipedia", "primary_tag", "reason", "relevance", "in_scope"]], on="title", how="left")
        done = int((sample["label"] != "").sum())
        st.progress(done / len(sample), text=f"{done} of {len(sample)} labelled")
        if done:  # share of each label, weighted by how many Corpus Articles each stratum stands for
            size = rows[rows["in_scope"]]["decided_by"].value_counts()
            est = []
            for label in AUDIT_LABELS:
                share = sum(size.get(s, 0) * (g["label"] == label).sum() / max((g["label"] != "").sum(), 1)
                            for s, g in sample.groupby("stratum")) / sum(size.get(s, 0) for s in audit)
                est.append({"label": label, "share": share, "articles": round(share * sum(size.get(s, 0) for s in audit))})
            cols = st.columns(3)
            for c, e in zip(cols, est):
                c.metric(f"{e['label']} (estimate)", f"{e['share']:.0%}", f"≈ {e['articles']:,} Articles", delta_color="off",
                         help="Projected to the rules + AI-check part of the Corpus. Rough until most of the sample is labelled.")
        sample["lead"] = sample["title"].map(lambda t: page_record(t).get("lead_text", "")[:300])
        only_open = st.toggle("Hide labelled", value=False)
        view = sample[sample["label"] == ""] if only_open else sample
        edited = st.data_editor(
            view[["label", "title", "wikipedia", "primary_tag", "stratum", "reason", "lead"]],
            column_config={
                "label": st.column_config.SelectboxColumn("Your call", options=[""] + AUDIT_LABELS, width="small"),
                "title": st.column_config.TextColumn("Article", disabled=True, width="medium"),
                "wikipedia": st.column_config.LinkColumn("Link", display_text="open ↗", width="small"),
                "primary_tag": st.column_config.TextColumn("Tag", disabled=True, width="small"),
                "stratum": st.column_config.TextColumn("Let in by", disabled=True, width="small"),
                "reason": st.column_config.TextColumn("Why it is in", disabled=True),
                "lead": st.column_config.TextColumn("Opening text", disabled=True, width="large"),
            },
            hide_index=True, width="stretch", height=560, key=f"audit-{only_open}-{done}")
        if st.button("💾 Save labels", type="primary", key="save-audit"):
            stratum_of = dict(zip(sample["title"], sample["stratum"]))
            for _, r in edited.iterrows():
                audit[stratum_of[r["title"]]][r["title"]] = r["label"] or None
            save_audit(audit)
            st.success("Saved.")
            st.rerun()


# ---------- evaluation ----------
with eval_tab:
    st.subheader("Evaluation set")
    st.caption("Measures the pipeline and never tunes it. Claude drafted a label for every Article; set **your** call "
               "for each one (the draft is only a hint). **keep**: about US–Japan fighting in WWII · **marginal**: tied "
               "to it only in passing (counts as correct) · **wrong**: not about US–Japan fighting. Sections: "
               "*precision_* Articles in the Corpus · *classification_recall* left out but tied or close to the cut-off · "
               "*discovery_recall* linked from Wikipedia list pages. Saved to `config/eval_set.yaml`; then run "
               "`python Processing/evaluate.py`.")
    sections = load_eval()
    if not sections:
        st.info("No evaluation set yet. Run `python Processing/eval_sample.py`.")
    else:
        sample = pd.DataFrame([{"section": s, "title": t, "label": l or ""} for s, d in sections.items() for t, l in d.items()])
        sample = sample.merge(eval_drafts(), on="title", how="left").merge(
            rows[["title", "primary_tag", "in_scope", "decided_by", "reason"]], on="title", how="left")
        sample["wikipedia"] = "https://en.wikipedia.org/wiki/" + sample["title"].map(lambda t: quote(t.replace(" ", "_")))
        sample["pipeline"] = sample.apply(lambda r: "not discovered" if pd.isna(r["in_scope"]) else
                                          f"{'in' if r['in_scope'] else 'out'} ({r['decided_by']})", axis=1)
        done = int((sample["label"] != "").sum())
        agree = int(((sample["label"] != "") & (sample["label"] == sample["draft"])).sum())
        c1, c2 = st.columns(2)
        c1.progress(done / len(sample), text=f"{done} of {len(sample)} verified")
        c2.metric("Agreement with Claude's drafts", f"{agree / done:.0%}" if done else "–", f"{done - agree} flipped",
                  delta_color="off", help="How far Claude's labels can be trusted when no person checks them.")
        f1, f2 = st.columns([2, 1])
        section = f1.selectbox("Section", ["all"] + list(sections), key="eval-section")
        only_open = f2.toggle("Hide verified", value=False, key="eval-open")
        view = sample if section == "all" else sample[sample["section"] == section]
        view = view[view["label"] == ""] if only_open else view
        view = view.assign(lead=view["title"].map(lambda t: page_record(t).get("lead_text", "")[:300]))
        edited = st.data_editor(
            view[["label", "draft", "why", "title", "wikipedia", "section", "pipeline", "lead"]],
            column_config={
                "label": st.column_config.SelectboxColumn("Your call", options=[""] + AUDIT_LABELS, width="small"),
                "draft": st.column_config.TextColumn("Claude", disabled=True, width="small"),
                "why": st.column_config.TextColumn("Claude's reason", disabled=True, width="medium"),
                "title": st.column_config.TextColumn("Article", disabled=True, width="medium"),
                "wikipedia": st.column_config.LinkColumn("Link", display_text="open ↗", width="small"),
                "section": st.column_config.TextColumn("Section", disabled=True, width="small"),
                "pipeline": st.column_config.TextColumn("Pipeline", disabled=True, width="small"),
                "lead": st.column_config.TextColumn("Opening text", disabled=True, width="large"),
            },
            hide_index=True, width="stretch", height=560, key=f"eval-{section}-{only_open}-{done}")
        if st.button("💾 Save labels", type="primary", key="save-eval"):
            section_of = dict(zip(sample["title"], sample["section"]))
            for _, r in edited.iterrows():
                sections[section_of[r["title"]]][r["title"]] = r["label"] or None
            save_eval(sections)
            st.success("Saved.")
            st.rerun()


# ---------- explore ----------
with explore_tab:
    st.subheader("Explore articles")
    e1, e2, e3 = st.columns([3, 2, 2])
    query = e1.text_input("Search title", key="explore-search", placeholder="e.g. Midway, Yamato, Nimitz")
    scope = e2.radio("Show", ["In the Corpus", "Left out", "All"], horizontal=True)
    tag_pick = e3.multiselect("Tag", sorted(rows["primary_tag"].unique()), key="explore-tags")
    ex = rows if scope == "All" else rows[rows["in_scope"] == (scope == "In the Corpus")]
    if query:
        ex = ex[ex["title"].str.contains(query, case=False, regex=False)]
    if tag_pick:
        ex = ex[ex["primary_tag"].isin(tag_pick)]
    st.caption(f"{len(ex):,} articles")
    st.dataframe(ex[["title", "wikipedia", "primary_tag", "in_scope", "decided_by", "reason", "found_via", "relevance"]]
                 .sort_values("title").head(500),
                 column_config={"wikipedia": st.column_config.LinkColumn("Link", display_text="open ↗"),
                                "in_scope": st.column_config.CheckboxColumn("In Corpus")},
                 hide_index=True, width="stretch", height=320)

    pick = st.selectbox("Open an article", ex["title"].sort_values().head(500), index=None, placeholder="Choose a title…")
    if pick:
        r = rows[rows["title"] == pick].iloc[0]
        p = page_record(pick)
        a, b = st.columns([3, 2])
        with a:
            st.markdown(f"### [{pick}]({r['wikipedia']})")
            st.write(p.get("lead_text", "")[:1500])
        with b:
            st.markdown(f"**In the Corpus:** {'✅ yes' if r['in_scope'] else '❌ no'}  \n"
                        f"**Decided by:** {r['decided_by']}  \n**Tag:** {r['primary_tag']} (from {r['tag_source']})  \n"
                        f"**Why:** {r['reason']}  \n**Found via:** {r['found_via']}  \n"
                        f"**AI score:** {r['relevance']}")
            if r["decided_by"] in ("rules", "e5", "review"):
                k1, k2 = st.columns(2)
                if k1.button("Force include", disabled=bool(r["in_scope"]) and r["decided_by"] == "rules"):
                    save_overrides(overrides["include"] + [pick], [t for t in overrides["exclude"] if t != pick]); st.rerun()
                if k2.button("Force exclude"):
                    save_overrides([t for t in overrides["include"] if t != pick], overrides["exclude"] + [pick]); st.rerun()
            with st.expander("Categories"):
                st.write(", ".join(c.removeprefix("Category:") for c in r["categories"]))
        with st.expander("Raw wikitext (what the consumer receives)"):
            st.code(wikitext(pick)[:20000], language="text")


# ---------- experiments ----------
with experiments_tab:
    st.subheader("AI check: current method vs kNN")
    res = load_knn_results(mtime(INTERIM / "knn_results.json"))
    if not res:
        st.info("Run `Processing/knn_classifier.ipynb` to populate this tab.")
    else:
        seeds = res["seeds"]
        st.caption(f"Test set: {res['holdout']['n']} hand-labelled unsure articles, never used for training. "
                   f"kNN seeds: {seeds['related']} related · {seeds['close']} look-alike · {seeds['far']} clearly unrelated.")
        table = pd.DataFrame([{"method": name, "K": str(m.get("k") or "–"), "accuracy": m.get("accuracy"),
                               "precision": m.get("precision"), "recall": m.get("recall"), "AUC": m.get("auc")}
                              for name, m in res["variants"].items() if not name.startswith("ensemble")])
        st.dataframe(table, hide_index=True, width="stretch", column_config={"AUC": st.column_config.NumberColumn(format="%.2f")}
            | {c: st.column_config.ProgressColumn(c, min_value=0, max_value=1, format="%.2f") for c in ("accuracy", "precision", "recall")})
        abl = pd.DataFrame([{"seeds used": k, "accuracy": v["accuracy"]} for k, v in res["ablations"].items()])
        st.markdown("**Which seeds matter** (test accuracy)")
        st.altair_chart(alt.Chart(abl).mark_bar(color="#7048e8").encode(
            x=alt.X("accuracy:Q", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("seeds used:N", sort="-x", title=None), tooltip=["seeds used", alt.Tooltip("accuracy:Q", format=".0%")]),
            width="stretch")
        st.markdown("**Gray zone: the two scores side by side**")
        sc = gray.dropna(subset=["knn_score", "relevance"])
        st.altair_chart(alt.Chart(sc).mark_circle(size=28, opacity=0.6).encode(
            x=alt.X("relevance:Q", title="current AI score"), y=alt.Y("knn_score:Q", title="kNN score"),
            color=alt.Color("decision:N", scale=alt.Scale(domain=["accept", "review", "reject", "override"],
                                                          range=["#2f9e44", "#f08c00", "#e03131", "#1c7ed6"])),
            tooltip=["title", "primary_tag", "relevance", "knn_score", "decision"]).interactive(), width="stretch")
        st.caption("Full write-up: `Processing/reports/knn_experiment.md`")
