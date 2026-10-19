"""Copy EventKG 3.2 event/entity records whose Wikidata ID is in the WWII list.

Run after fetch_wwii_task_force.py maps article QIDs:
    python scripts/filter_eventkg_v3_2.py events.nq
    python scripts/filter_eventkg_v3_2.py entities.nq

Each source is read in resumable 64 MiB HTTP ranges. The output keeps the
original N-Quads and source graph for provenance. It does not fetch relations.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import TextIO

import requests


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "Data" / "external" / "wwii_task_force"
URL_BASE = "https://eventkg.l3s.uni-hannover.de/data/v3_2/output/"
CHUNK_SIZE = 64 * 1024 * 1024
QID_RE = re.compile(rb"<http://www\.wikidata\.org/entity/(Q[0-9]+)>")
session = requests.Session()
session.headers["User-Agent"] = "Corpus2GeoRAG-Pacific-data/0.1 (https://github.com/EtzionR/Corpus2GeoRAG_Pacific)"


def request_with_retry(method: str, url: str, **kwargs: object) -> requests.Response:
    for attempt in range(8):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            response.close()
            error: Exception = RuntimeError(f"HTTP {response.status_code}")
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = exc
        if attempt == 7:
            raise RuntimeError(f"EventKG {method} failed after 8 attempts: {error}") from error
        delay = min(60, 2 ** attempt)
        print(f"EventKG {method} retry {attempt + 1}/7 in {delay}s: {error}", flush=True)
        time.sleep(delay)
    raise AssertionError("unreachable")


def save_json(path: Path, value: dict[str, int]) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def download(source: str) -> None:
    if source not in ("events.nq", "entities.nq"):
        raise ValueError("Only events.nq and entities.nq are supported")
    qids = {line.strip().encode("ascii") for line in (BASE / "wikidata_qids.txt").read_text(encoding="ascii").splitlines()}
    out = BASE / "eventkg_v3_2" / source.removesuffix(".nq")
    out.mkdir(parents=True, exist_ok=True)
    url = URL_BASE + source
    head = request_with_retry("HEAD", url, timeout=30)
    head.raise_for_status()
    total = int(head.headers["Content-Length"])
    state_file = out / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"next_offset": 0, "part": 0, "source_bytes": total, "matched": 0}
    if state["source_bytes"] != total:
        raise RuntimeError("EventKG source size changed; cannot resume")
    while state["next_offset"] < total:
        start = state["next_offset"]
        end = min(total - 1, start + CHUNK_SIZE - 1)
        response = request_with_retry("GET", url, headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}, timeout=180)
        response.raise_for_status()
        expected_range = f"bytes {start}-{end}/{total}"
        if response.status_code != 206 or response.headers.get("Content-Range") != expected_range:
            raise RuntimeError(f"Unexpected range reply: {response.status_code} {response.headers.get('Content-Range')}")
        content = response.content
        if len(content) != end - start + 1:
            raise RuntimeError("Short EventKG range reply")
        part = state["part"]
        data_temp = out / f"part_{part:05d}.nq.gz.tmp"
        map_temp = out / f"part_{part:05d}.qid_map.tsv.tmp"
        matched = 0
        consumed = 0
        group_start = 0
        group_lines: list[bytes] = []
        subject: bytes | None = None

        def emit(data_file: gzip.GzipFile, map_file: TextIO, resource_subject: bytes) -> None:
            nonlocal matched
            qid_matches = {match for line in group_lines if b"owl#sameAs>" in line for match in QID_RE.findall(line)} & qids
            if not qid_matches:
                return
            for line in group_lines:
                data_file.write(line)
            for qid in sorted(qid_matches):
                map_file.write(qid.decode("ascii") + "\t" + resource_subject.decode("ascii") + "\n")
            matched += 1

        with gzip.open(data_temp, "wb", compresslevel=6) as data_file, map_temp.open("w", encoding="ascii") as map_file:
            for line in content.splitlines(keepends=True):
                if not line.endswith(b"\n") and end != total - 1:
                    break
                current_subject = line.split(b" ", 1)[0]
                if current_subject != subject:
                    if group_lines:
                        emit(data_file, map_file, subject)
                    group_lines = []
                    subject = current_subject
                    group_start = consumed
                group_lines.append(line)
                consumed += len(line)
            if end == total - 1:
                if group_lines:
                    emit(data_file, map_file, subject)
                next_offset = total
            else:
                next_offset = start + group_start
                if next_offset <= start:
                    raise RuntimeError("A single EventKG resource exceeds the range size")
        os.replace(data_temp, out / f"part_{part:05d}.nq.gz")
        os.replace(map_temp, out / f"part_{part:05d}.qid_map.tsv")
        state = {"next_offset": next_offset, "part": part + 1, "source_bytes": total, "matched": state["matched"] + matched}
        save_json(state_file, state)
        print(f"{source}: {next_offset:,}/{total:,} bytes; {state['matched']:,} matched records", flush=True)
    save_json(out / "manifest.json", state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("events.nq", "entities.nq"))
    download(parser.parse_args().source)
