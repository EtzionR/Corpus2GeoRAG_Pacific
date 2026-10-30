# Complete core graph in one Parquet file

`graph.parquet` contains the accepted core graph **without the later geography overlay**. Geography remains `PENDING_LAST_STAGE`. R6, the existing `what_happened_at` spatial date-filter issue, remains **OPEN**. The 11 accepted core date queries do not close R6.

The complete population is 268,703 nodes and 131,034,512 unordered co-occurrence pairs. Each pair retains its raw shared-page count and both directional strengths. The raw shared-page count sum is 175,316,858. These relations mean co-occurrence on source pages; they do not prove physical participation or Event location.

## File format and reader

The file uses Zstandard compression. All nodes occur first in original index order, followed by all pairs in original order. Nullable typed columns are:

| Column | Type | Meaning |
|---|---|---|
| record_type | string | `node` or `pair` |
| node_index | int64 | Original node position; null for pairs |
| node_json | string | Complete original node object; null for pairs |
| source_index, target_index | int64 | Original pair endpoints |
| shared_page_count | int64 | Original raw pair weight |
| strength_source_target, strength_target_source | float64 | Original directional strengths |

The Parquet schema metadata contains `geoai.format`, `geoai.graph_metadata`, and `geoai.source_graph_sha256`, each encoded as JSON. The original graph metadata and every original node field, including identity, text, dates, source references and memberships, are preserved. Null columns do not add fields to original node objects.

Use PyArrow to read bounded batches; this avoids loading the complete graph into RAM:

```python
import pyarrow.parquet as pq
for batch in pq.ParquetFile("graph.parquet").iter_batches(batch_size=100000):
    # Inspect record_type; parse node_json with json.loads for node rows.
    pass
```

## Verification and restore

Python 3.11+ and PyArrow are required. PyArrow 24.0.0 was used for this delivery. No project checkout, Agent installation or network connection is required to restore:

```text
python restore.py graph.parquet manifest.json restored.json.gz
```

Keep `restore.py` and the unchanged bundled `compact_format.py` together. `LICENSE` is copied from the source repository. The restore tool requires a trusted companion manifest. Before output, it checks the full Parquet SHA256 and footer source/metadata binding. During restore, it checks every ordered node digest and typed pair digest, counts and raw sum. The final graph appears only after those checks pass. Existing outputs and interrupted `.part` outputs are never overwritten.

The restored file uses the existing Agent compact JSON format. Graph values, array order and object fields are preserved; compressed gzip bytes can differ. A full production restored JSON is intentionally not included.

`manifest.json` and `verification.json` record full Parquet readback checks. Node SHA256 hashes ordered UTF-8 sorted-key compact JSON objects, each followed by LF. Pair SHA256 hashes ordered `struct.pack('<qqqdd', source, target, count, forward, reverse)` values, the same digest used by the accepted Agent proof.

`source_agent_acceptance.json` is the original complete Agent proof. `source_date_query_acceptance.json` is the original 11-query date report. Both bind the original core JSON SHA256 `2c5d87879d7a870a23cfa025b927d0ad7344383f71453c3f74547de6bd11882f`. The accepted pair SHA256 is `6e0289425a05e9e3c510d0a17a31cdce4a1547a3eed594201e31aa27c3fd8928`. The Parquet manifest preserves that source binding and pair proof. `delivery_manifest.json` binds the small reader, proof and documentation files separately from the original running writer code hash.

SHA256 checks detect file changes relative to the trusted manifest. They do not establish who supplied that manifest.

`writer_source_at_conversion.txt`, `initial_writer_manifest.json`, and `initial_value_verification.json` preserve the original conversion source and proof. They are historical provenance, not restore entry points. The original module restore API was corrected after conversion to require a manifest and ordered value proofs. The final manifest updates only this restore contract and code provenance; Parquet bytes and complete node/pair/source proofs are unchanged. `code_sha256` identifies the historical running writer; `current_utility_code_sha256` identifies the corrected utility. The final `verification.json` binds the final manifest.
