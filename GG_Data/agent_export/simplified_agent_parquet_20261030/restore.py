"""Restore compact graph values from the verified portable Parquet transport.

Usage: python restore.py graph.parquet manifest.json restored.json.gz
Requires Python 3.11+ and PyArrow. No project checkout or network is used.
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

import pyarrow.parquet as pq
from compact_format import FORMAT, write_graph


def restore(parquet, manifest, output):
    parquet, manifest, output = map(Path, (parquet, manifest, output))
    if output.exists() or output.with_name(output.name + ".part").exists():
        raise FileExistsError(output)
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    with parquet.open("rb") as stream:
        actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if (
        expected["status"] != "VERIFIED_PARQUET_VALUES"
        or expected["outputs"][parquet.name] != actual_sha
    ):
        raise ValueError("Parquet differs from its verified manifest")
    file = pq.ParquetFile(parquet)
    footer = file.schema_arrow.metadata
    metadata = json.loads(footer[b"geoai.graph_metadata"])
    if (
        json.loads(footer[b"geoai.format"]) != FORMAT
        or json.loads(footer[b"geoai.source_graph_sha256"])
        != expected["source_graph_sha256"]
        or metadata != expected["graph_metadata"]
    ):
        raise ValueError("Original graph metadata/source binding differs")

    def all_rows():
        for batch in file.iter_batches(batch_size=10000, use_threads=False):
            yield from batch.to_pylist()

    rows = all_rows()
    node_hash, pair_hash = hashlib.sha256(), hashlib.sha256()

    def nodes():
        for index in range(metadata["node_count"]):
            row = next(rows)
            if row["record_type"] != "node" or row["node_index"] != index:
                raise ValueError("Invalid node order")
            node = json.loads(row["node_json"])
            node_hash.update(
                (
                    json.dumps(
                        node,
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            yield node
        if node_hash.hexdigest() != expected["node_rows_sha256"]:
            raise ValueError("Complete ordered node digest differs")

    def pairs():
        count = raw_sum = 0
        for row in rows:
            if row["record_type"] != "pair":
                raise ValueError("Invalid pair order")
            values = [
                row[k]
                for k in [
                    "source_index",
                    "target_index",
                    "shared_page_count",
                    "strength_source_target",
                    "strength_target_source",
                ]
            ]
            pair_hash.update(struct.pack("<qqqdd", *values))
            count += 1
            raw_sum += values[2]
            yield values
        if (
            count != expected["pair_count"]
            or raw_sum != expected["raw_shared_page_count_sum"]
            or pair_hash.hexdigest() != expected["pair_rows_sha256"]
        ):
            raise ValueError("Complete ordered pair proof differs")
        if metadata["node_count"] != expected["node_count"]:
            raise ValueError("Node population count differs")

    write_graph(output, metadata, nodes(), pairs())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["parquet", "manifest", "output"]:
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    restore(args.parquet, args.manifest, args.output)
