"""Save the current WWII task force QID population and its Wikidata entities.

Run: python scripts/fetch_wwii_task_force.py
The output is local source data under Data/external/wwii_task_force/.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "Data" / "external" / "wwii_task_force"
USER_AGENT = "Corpus2GeoRAG-Pacific-data/0.1 (https://github.com/EtzionR/Corpus2GeoRAG_Pacific)"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
CATEGORY = "Category:World_War_II_task_force_articles"
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
worker_state = local()


def api(url: str, params: dict[str, Any], client: requests.Session = session) -> dict[str, Any]:
    params = {"format": "json", "formatversion": 2, "maxlag": 15 if url == WIKIDATA_API else 5, **params}
    for attempt in range(6):
        try:
            response = client.get(url, params=params, timeout=90)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data
        except (requests.RequestException, RuntimeError) as exc:
            if attempt == 5:
                raise
            delay = min(60, 2**attempt)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")
                if retry_after and retry_after.isdecimal():
                    delay = max(delay, int(retry_after))
            print(f"API retry {attempt + 1}/5 in {delay}s: {exc}", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def collect_category() -> list[dict[str, Any]]:
    final = OUT / "category_talk_pages.jsonl"
    if final.exists():
        return read_jsonl(final)
    partial = OUT / "category_talk_pages.jsonl.part"
    state_file = OUT / "category_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    cursor = state.get("cmcontinue")
    count = state.get("count", 0)
    with partial.open("a", encoding="utf-8") as stream:
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": CATEGORY,
                "cmnamespace": 1,
                "cmlimit": 500,
            }
            if cursor:
                params["cmcontinue"] = cursor
            data = api(WIKIPEDIA_API, params)
            members = data["query"]["categorymembers"]
            for member in members:
                stream.write(json.dumps(member, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            count += len(members)
            cursor = data.get("continue", {}).get("cmcontinue")
            write_json(state_file, {"cmcontinue": cursor, "count": count})
            if count % 5000 < 500 or not cursor:
                print(f"Task force talk pages: {count}", flush=True)
            if not cursor:
                break
    os.replace(partial, final)
    state_file.unlink(missing_ok=True)
    return read_jsonl(final)


def map_qids(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = OUT / "article_qids.jsonl"
    rows = read_jsonl(path)
    if len(rows) > len(members):
        raise RuntimeError("QID map has more rows than category pages")
    with path.open("a", encoding="utf-8") as stream:
        for start in range(len(rows), len(members), 50):
            batch = members[start : start + 50]
            titles = [entry["title"].removeprefix("Talk:") for entry in batch]
            data = api(
                WIKIPEDIA_API,
                {
                    "action": "query",
                    "prop": "pageprops",
                    "ppprop": "wikibase_item",
                    "redirects": 1,
                    "titles": "|".join(titles),
                },
            )
            aliases = {x["from"]: x["to"] for group in ("normalized", "redirects") for x in data.get("query", {}).get(group, [])}
            pages = {page["title"]: page for page in data["query"]["pages"]}
            for member, title in zip(batch, titles):
                resolved = title
                for _ in range(4):
                    if resolved not in aliases:
                        break
                    resolved = aliases[resolved]
                page = pages.get(resolved, {})
                row = {
                    "talk_pageid": member["pageid"],
                    "talk_title": member["title"],
                    "article_title": title,
                    "resolved_title": resolved,
                    "article_pageid": page.get("pageid"),
                    "qid": page.get("pageprops", {}).get("wikibase_item"),
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows.append(row)
            stream.flush()
            if len(rows) % 5000 < 50 or len(rows) == len(members):
                print(f"Article QIDs mapped: {len(rows)}/{len(members)}", flush=True)
    return rows


def download_entities(rows: list[dict[str, Any]]) -> None:
    qids = sorted({row["qid"] for row in rows if row["qid"]})
    index = OUT / "wikidata_qids.txt"
    index.write_text("\n".join(qids) + "\n", encoding="ascii")
    archive = OUT / "wikidata_entities.jsonl.gz"
    batch_dir = OUT / "wikidata_batches"
    batch_dir.mkdir(exist_ok=True)
    batches = [qids[start : start + 50] for start in range(0, len(qids), 50)]

    def fetch_batch(number: int, batch: list[str]) -> None:
        if not hasattr(worker_state, "client"):
            worker_state.client = requests.Session()
            worker_state.client.headers.update({"User-Agent": USER_AGENT})
        path = batch_dir / f"batch_{number:05d}.jsonl.gz"
        if path.exists():
            return
        data = api(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(batch)}, worker_state.client)
        temp = path.with_suffix(".gz.tmp")
        with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as stream:
            entities = data.get("entities", {})
            for qid in batch:
                stream.write(json.dumps({"requested_qid": qid, "entity": entities.get(qid)}, ensure_ascii=False) + "\n")
        os.replace(temp, path)

    todo = [(number, batch) for number, batch in enumerate(batches) if not (batch_dir / f"batch_{number:05d}.jsonl.gz").exists()]
    completed = len(batches) - len(todo)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_batch, number, batch) for number, batch in todo]
        for future in as_completed(futures):
            future.result()
            completed += 1
            if completed % 50 == 0 or completed == len(batches):
                print(f"Wikidata batches: {completed}/{len(batches)}", flush=True)
    temp_archive = archive.with_suffix(".gz.tmp")
    with temp_archive.open("wb") as target:
        for number in range(len(batches)):
            with (batch_dir / f"batch_{number:05d}.jsonl.gz").open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    os.replace(temp_archive, archive)
    write_json(
        OUT / "manifest.json",
        {
            "source_category": f"https://en.wikipedia.org/wiki/{CATEGORY}",
            "talk_pages": len(rows),
            "mapped_articles": sum(bool(row["qid"]) for row in rows),
            "unique_qids": len(qids),
            "wikidata_entities_requested": len(qids),
            "files": [p.name for p in (OUT / "category_talk_pages.jsonl", OUT / "article_qids.jsonl", index, archive)],
        },
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    members = collect_category()
    rows = map_qids(members)
    download_entities(rows)


if __name__ == "__main__":
    main()
