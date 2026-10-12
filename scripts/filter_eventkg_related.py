"""Save EventKG 3.2 records linked to the task-force Wikidata QIDs.

First finish ``filter_eventkg_v3_2.py`` for events.nq and entities.nq.
Then run this script. Reified relations are kept when either endpoint is an
in-scope resource. Direct triples are kept when their subject is in scope.
Each source is scanned in resumable HTTP ranges; original N-Quads are saved.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from filter_eventkg_v3_2 import BASE, CHUNK_SIZE, URL_BASE, request_with_retry, save_json

DIRECT_SOURCES = (
    "text_events.nq",
    "relations_events_base.nq",
    "relations_entities_base.nq",
    "events_first_sentences.nq",
    "events_descriptions_from_text_events.nq",
    "types.nq",
    "types_dbpedia.nq",
    "types_wikidata.nq",
    "preferred_labels.nq",
)
REIFIED_SOURCES = (
    "relations_events_literals.nq",
    "relations_events_other.nq",
    "relations_entities_temporal.nq",
    "relations_entities_other.nq",
    "event_links.nq",
    "entity_links.nq",
    "text_event_links.nq",
    "event_mentions.nq",
    "entity_mentions.nq",
)
ENDPOINT_PREDICATES = (
    b"<http://www.w3.org/1999/02/22-rdf-syntax-ns#subject>",
    b"<http://www.w3.org/1999/02/22-rdf-syntax-ns#object>",
)


def load_resource_ids() -> set[bytes]:
    ids: set[bytes] = set()
    for name in ("events", "entities"):
        source_dir = BASE / "eventkg_v3_2" / name
        if not (source_dir / "manifest.json").exists():
            raise RuntimeError(f"Finish {name}.nq before relation scanning")
        for path in sorted(source_dir.glob("part_*.qid_map.tsv")):
            for line in path.read_bytes().splitlines():
                ids.add(line.split(b"\t", 1)[1])
    return ids


def group_matches(group: list[bytes], ids: set[bytes], reified: bool) -> bool:
    if reified:
        return any(
            len(parts) > 2 and parts[1] in ENDPOINT_PREDICATES and parts[2] in ids
            for line in group
            if (parts := line.split(b" ", 3))
        )
    return bool(group) and group[0].split(b" ", 1)[0] in ids


def filter_content(content: bytes, *, final: bool, ids: set[bytes], reified: bool) -> tuple[bytes, int, int]:
    """Return complete matching groups, consumed bytes, and match count."""
    kept = bytearray()
    group: list[bytes] = []
    subject: bytes | None = None
    group_start = 0
    consumed = 0
    count = 0

    def emit() -> None:
        nonlocal count
        if group_matches(group, ids, reified):
            kept.extend(b"".join(group))
            count += 1

    for line in content.splitlines(keepends=True):
        if not line.endswith(b"\n") and not final:
            break
        current_subject = line.split(b" ", 1)[0]
        if current_subject != subject:
            if group:
                emit()
            group = []
            subject = current_subject
            group_start = consumed
        group.append(line)
        consumed += len(line)
    if final and group:
        emit()
        return bytes(kept), consumed, count
    if not reified and group:
        emit()
        return bytes(kept), group_start + sum(map(len, group)), count
    return bytes(kept), group_start, count


def scan(source: str, ids: set[bytes]) -> None:
    if source not in DIRECT_SOURCES + REIFIED_SOURCES:
        raise ValueError(source)
    reified = source in REIFIED_SOURCES
    url = URL_BASE + source
    head = request_with_retry("HEAD", url, timeout=30)
    head.raise_for_status()
    total = int(head.headers["Content-Length"])
    out = BASE / "eventkg_v3_2" / source.removesuffix(".nq")
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {
        "next_offset": 0, "part": 0, "source_bytes": total, "matched": 0
    }
    if state["source_bytes"] != total:
        raise RuntimeError(f"{source} size changed; cannot resume")
    while state["next_offset"] < total:
        start = state["next_offset"]
        end = min(total - 1, start + CHUNK_SIZE - 1)
        reply = request_with_retry("GET", url, headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}, timeout=180)
        reply.raise_for_status()
        if reply.status_code != 206 or reply.headers.get("Content-Range") != f"bytes {start}-{end}/{total}":
            raise RuntimeError(f"Unexpected range reply for {source}: {reply.status_code} {reply.headers.get('Content-Range')}")
        if len(reply.content) != end - start + 1:
            raise RuntimeError(f"Short range reply for {source}")
        kept, consumed, matched = filter_content(reply.content, final=end == total - 1, ids=ids, reified=reified)
        next_offset = start + consumed
        if next_offset <= start:
            raise RuntimeError(f"A {source} group exceeds the range size")
        part = state["part"]
        temp = out / f"part_{part:05d}.nq.gz.tmp"
        with gzip.open(temp, "wb", compresslevel=6) as output:
            output.write(kept)
        os.replace(temp, out / f"part_{part:05d}.nq.gz")
        state = {"next_offset": next_offset, "part": part + 1, "source_bytes": total, "matched": state["matched"] + matched}
        save_json(state_file, state)
        print(f"{source}: {next_offset:,}/{total:,} bytes; {state['matched']:,} matched groups", flush=True)
    save_json(out / "manifest.json", state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", choices=DIRECT_SOURCES + REIFIED_SOURCES)
    args = parser.parse_args()
    resource_ids = load_resource_ids()
    print(f"Selected EventKG resources: {len(resource_ids):,}", flush=True)
    for name in (args.source,) if args.source else DIRECT_SOURCES + REIFIED_SOURCES:
        scan(name, resource_ids)
