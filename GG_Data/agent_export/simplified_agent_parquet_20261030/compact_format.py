"""Versioned, line-framed JSON arrays for bounded-memory graph interchange.

The whole file is valid JSON. This format requires one array item per line;
that framing lets the Agent use the standard library without loading edges.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from pathlib import Path

FORMAT = "simplified-cooccurrence-v1"
HEADER = '{"format":"' + FORMAT + '",\n'
COLUMNS = [
    "source_index",
    "target_index",
    "shared_page_count",
    "strength_source_target",
    "strength_target_source",
]
LOG = logging.getLogger(__name__)


def opener(path, mode):
    return (
        gzip.open(path, mode, encoding="utf-8")
        if str(path).endswith(".gz")
        else open(path, mode, encoding="utf-8")
    )


def sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_graph(path, metadata, nodes, pairs):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(path.name + ".part")
    # Select compression using the final suffix, not the temporary suffix.
    stream = (
        gzip.open(temporary, "xt", encoding="utf-8", compresslevel=1)
        if path.suffix == ".gz"
        else temporary.open("x", encoding="utf-8")
    )
    metadata = dict(metadata, edge_columns=COLUMNS)
    with stream:
        stream.write(HEADER)
        stream.write(
            '"metadata":'
            + json.dumps(
                metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            + ',\n"nodes":[\n'
        )
        for rows, count, closing in [
            (nodes, metadata["node_count"], '],\n"edges":[\n'),
            (pairs, metadata["pair_count"], "]}\n"),
        ]:
            written = 0
            for row in rows:
                written += 1
                if written > count:
                    raise ValueError("Graph row count exceeded metadata")
                stream.write(
                    json.dumps(
                        row, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    )
                )
                stream.write(",\n" if written < count else "\n")
                if written % 10000000 == 0:
                    LOG.info("Graph rows written %s/%s", written, count)
            if written != count:
                raise ValueError("Graph row count differs from metadata")
            stream.write(closing)
    temporary.replace(path)


def records(path):
    """Yield metadata, nodes and pairs, and validate framing through EOF."""
    with opener(path, "rt") as stream:
        if stream.readline() != HEADER:
            raise ValueError("Unsupported compact graph format")
        line = stream.readline()
        if not line.startswith('"metadata":') or not line.endswith(",\n"):
            raise ValueError("Invalid compact graph metadata framing")
        metadata = json.loads(line[len('"metadata":') : -2])
        if metadata.get("edge_columns") != COLUMNS or any(
            type(metadata.get(k)) is not int or metadata[k] < 0
            for k in ["node_count", "pair_count"]
        ):
            raise ValueError("Invalid compact graph counts or columns")
        yield "metadata", metadata
        if stream.readline() != '"nodes":[\n':
            raise ValueError("Invalid node array framing")
        for kind, count, closing in [
            ("node", metadata["node_count"], "],\n"),
            ("pair", metadata["pair_count"], "]}\n"),
        ]:
            for i in range(count):
                line = stream.readline()
                suffix = ",\n" if i + 1 < count else "\n"
                if not line.endswith(suffix):
                    raise ValueError("Invalid graph array framing")
                yield kind, json.loads(line[: -len(suffix)])
            if stream.readline() != closing:
                raise ValueError("Invalid graph array end")
            if kind == "node" and stream.readline() != '"edges":[\n':
                raise ValueError("Invalid edge array framing")
        if stream.read(1):
            raise ValueError("Unexpected graph trailing content")
