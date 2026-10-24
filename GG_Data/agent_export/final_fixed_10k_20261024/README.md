# Fixed-10K data-model and Agent delivery

This directory contains the completed fixed-10,000-mention POC delivery. It is
not the full 309,864-mention population and is not a production release.

## Files

- `fixed_10k_agent_graph_20261024.json`: exact verified Agent input JSON,
  copied without reserialization. SHA-256:
  `70be8618fbb7a5333af15564032f02c3977cc97fd02aa4271a64a0fe47316bc4`.
- `fixed_10k_data_model_20261024.zip`: consolidated six-table data model,
  geometry, mapping catalogs, supporting evidence and lineage reports.
  SHA-256:
  `9e980b9698f1eabac2a710ceb397281d67b40f31887db1874997461ab94fc580`.

The Agent JSON contains 7,710 nodes, 32,899 edge views and 16,466 base
assertions. It retains the approved metadata status `DRAFT_AGENT_MECHANISM_QA`.
All endpoints, relation catalog entries, reverse views and Agent model joins
passed verification.

The ZIP contains 10,000 Mentions, 691 Wiki Pages, 8,002 Entities, 2,717 Events,
16,466 Relations and 1,246 Geography rows. Its internal
`package_manifest.json` pins every artifact checksum and records 26 unresolved
Geography targets, 29 Mentions without a Wiki Page and 952 normalized DATE
Mentions without an Entity, all as explicit expected cases. All non-empty
foreign keys passed verification.

The open `what_happened_at` composite spatial date-filter issue is not a graph
or data-model defect. It remains open and is recorded in the package manifest.

The ZIP is stored through Git LFS because it exceeds GitHub's normal file-size
limit. Run `git lfs pull` after cloning if LFS content was not downloaded.
