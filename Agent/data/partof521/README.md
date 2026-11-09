# Self-fixed graph: partof521

`graph.selffix.json` is the graph the agent should load (`GRAPH_PATH=data/partof521/graph.selffix.json`). It is stored with **Git LFS** (140 MB).

- **Get it:** after cloning, run `git lfs pull`. Without Git LFS, download it from `https://media.githubusercontent.com/media/EtzionR/Corpus2GeoRAG_Pacific/main/Agent/data/partof521/graph.selffix.json`.
- **Check it:** `shasum -a 256 -c SHA256SUMS` in this folder. If the agent reports a "Git LFS pointer", the file hasn't been downloaded yet.
- **Built from:** the graph team's partof521 delivery (team PR #46, 2026-11-08), `graph.json`, SHA-256 `337cd76503c182aefeec255a2934ccc3feb5a17b5698fea7d87d92111c620631`.
- **Built with:** `graph_enrichment` 0.3.0 (Wikidata enrichment, then self-fix steps 1, 3, 5 and 6, then birth and death dates, then page titles), on 2026-11-09. Rebuild it with the command in `graph_enrichment/README.md`.
- **What it adds to the delivery:**
  - 647 entities (233 from the corpus's own links, 414 from battle infoboxes)
  - about 5,100 relations, including each battle's sides, commanders and units
  - 609 corrected types
  - birth and death dates for 978 people
  - titles for the 209 source pages the delivery names only by page id (`metadata.page_titles`)
  - Nothing the team delivered was removed.
- **Licence:** facts and text from Wikipedia are CC BY-SA 4.0; facts from Wikidata are CC0. Every added record and relation carries its source (`provenance`, page, revision, field), so keep that attribution when you reuse the data.
