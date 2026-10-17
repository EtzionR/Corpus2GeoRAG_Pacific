# NER_COLAB_BERT_GLINER_FULL_v2.ipynb

This Colab notebook extracts source-linked named-entity **mentions** with BERT and GLiNER2.5, then applies a configurable v2 selection policy. The policy is a working configuration, not a final project-wide model decision.

## Input

- The notebook reads `Data/DATA.json` from `https://github.com/danielbr9382/Corpus2GeoRAG_Pacific.git` at pinned commit `abb6fd24a6446ad52e488f27a401f17a471c2efc`. The JSON maps each article title to its full text.
- Each nonempty, nonheading line becomes one eligible paragraph. The notebook preserves its article title (`Source`), paragraph number (`Index`), exact text (`Text`), and character offsets in the original article.
- With `PROCESS_ALL=True`, the verified run processed **36,335 paragraphs from 428 articles**. `SEED` and `SAMPLE_SIZE` apply only when `PROCESS_ALL=False`; `RUN_RANDOM_ID` identifies the run and does not control sampling.

## Where the labels come from

| Label | Main output source and rule |
|---|---|
| `PER` | GLiNER2.5 `person` at score ≥0.50. BERT `PER` at score ≥0.90 may supplement it if it does not overlap a selected GLiNER person mention. |
| `LOC` | Valid BERT `LOC`; no additional selection threshold. |
| `ORG` | Valid BERT `ORG`; no additional selection threshold. |
| `DATE` | Valid GLiNER2.5 `date` at score ≥0.50. |
| `MISC` | Valid BERT `MISC`; GLiNER's other labels and subtypes remain in its detailed extraction output. |
| `TIME` | GLiNER2.5 `time`, stored in the **candidates** output, not the main table. |
| `EVENT` | GLiNER2.5 `event`, stored in **candidates**. Score ≥0.90 marks high priority, not a verified event fact. |

The pinned models are `dslim/bert-base-NER` and `fastino/gliner2.5-base-v1`. GLiNER's raw decoding floor is 0.01; it is separate from the 0.50 main-output selection threshold. A final row keeps the selected model's own score, without averaging model scores.

## Output structure and names

The main CSV and Parquet contain **one row per mention occurrence** with these nine columns, in order:

`entity_group, score, word, start, end, Source, Index, Text, subtype`

`subtype` is the selected model's original label (for example, `person`, `date`, or `LOC`). `start` is zero-based within `Text`; `end` is exclusive, so `Text[start:end] == word`. Repeated occurrences at different offsets remain separate rows. Model provenance, selection reason, document offsets, validation status, and raw-output references are in the detailed JSON records.

In full mode, the base name is `{FINAL_OUTPUT_PREFIX}_full_n{paragraph_count}_r{RUN_RANDOM_ID}_{YYYYMMDD}`. The main export uses that base name directly, **without** `_selected`, for `.csv`, `.parquet`, and `.json`. The JSON is an index pointing to detailed records in `selected_parts/`. The parallel `_candidates` CSV/Parquet/JSON adds `selection_status`, `selected_model`, `semantic_validation`, and `verified_event_fact` to the same nine columns. The ZIP also contains source/run metadata, per-model checkpoints and raw outputs, `manifest.json`, and `validation_summary.json`.

In the verified run, the main filename is `georag_selected_v2_full_n36335_r482092353_20261016.csv` (also `.parquet` and `.json`); its Parquet has **309,864 rows**. The candidates export has **258,235 rows**. The ZIP is named `georag_selected_v2_full_n36335_r482092353_20261016_export_0f6b15b3e9d016e1.zip`.

## Measured runtime for the verified full run

The 16 October 2026 run used `cuda:0`. Times below are complete notebook-cell blocks, including their setup and checkpoint work; they are not predictions for another machine.

| Stage | Time |
|---|---:|
| Installations and imports | 1 min 09 s |
| Source loading and paragraph preparation | 20 s |
| BERT cell | 12 min 01 s |
| GLiNER cell | 28 min 45 s |
| Selection and export | 4 min 42 s |
| Coverage/output validation | 6 min 23 s |
| ZIP preparation and verification | 3 min 36 s |
| **All measured cell blocks** | **56 min 57 s** |

Net inference recorded inside checkpoints was 11 min 03 s for BERT and 23 min 09 s for GLiNER. The measured total excludes manifest bookkeeping, final verification, and browser download/transfer. All 36,335 paragraphs were covered; the manifest reports `completed_with_errors` because **19,318 invalid GLiNER raw mentions** were retained for inspection rather than inserted into the selected table. Selection and valid offsets do not establish semantic or factual correctness.
