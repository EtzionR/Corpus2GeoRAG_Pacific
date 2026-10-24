"""Build one verified six-table package from the completed fixed-10K POC."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


TABLE_SOURCES = {
    "mentions": ("mentions_pages", "mentions.json.gz"),
    "wiki_pages": ("entities", "wiki_pages.json.gz"),
    "entities": ("model_geography", "entities.json.gz"),
    "events": ("model_geography", "events.json.gz"),
    "relations": ("model_geography", "relations.json.gz"),
    "geography": ("model_geography", "geography.json.gz"),
}


def require_unique(rows: list[dict], key, label: str) -> None:
    values = [key(row) for row in rows]
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def run(base: Path, output: Path) -> dict:
    """Copy the verified tables without changing any source artifact."""
    if output.exists():
        raise ValueError("Use a new package output directory")

    manifests = {
        "mentions_pages": read_json(base / "mentions_pages/manifest.json"),
        "entities": read_json(base / "entities/manifest.json"),
        "model_geography": read_json(base / "model_geography/manifest.json"),
    }
    verification = read_json(base / "model_geography/verification.json")
    completion_path = base / "recovery_20261024_workflow_v2/completion_report.json"
    completion = read_json(completion_path)
    if completion["status"] != "PASSED_FIXED_10K_MODEL_GRAPH_AND_SPATIAL_INTEGRATION":
        raise ValueError("The fixed-10K completion gate did not pass")
    if verification["status"] != "VERIFIED_MODEL_WITH_RANKED_GEOGRAPHY":
        raise ValueError("The Geography model verification gate did not pass")
    if (
        digest(base / "model_geography/manifest.json")
        != verification["manifest_sha256"]
    ):
        raise ValueError("The Geography model manifest changed")

    rows, sources = {}, {}
    for table, (stage, name) in TABLE_SOURCES.items():
        source = base / stage / name
        expected = manifests[stage]["outputs"].get(name)
        if expected is None or digest(source) != expected:
            raise ValueError(f"Source checksum differs: {table}")
        rows[table] = read_json(source)
        sources[name] = source

    mention_sha = manifests["mentions_pages"]["outputs"]["mentions.json.gz"]
    if mention_sha not in manifests["entities"].get("inputs", {}).values():
        raise ValueError("Entities lineage does not pin Mentions")
    if (
        digest(base / "entities/manifest.json")
        not in manifests["model_geography"].get("inputs", {}).values()
    ):
        raise ValueError("Final model lineage does not pin Entities")

    require_unique(rows["mentions"], lambda row: row["mention_id"], "Mention ID")
    require_unique(
        rows["mentions"], lambda row: row["input_row_number"], "Mention input row"
    )
    require_unique(
        rows["wiki_pages"],
        lambda row: (row["wiki_site"], str(row["wiki_page_id"])),
        "Wiki Page key",
    )
    require_unique(
        rows["entities"], lambda row: (row["entity_id"], row["type"]), "Entity key"
    )
    require_unique(rows["events"], lambda row: row["event_id"], "Event ID")
    require_unique(
        rows["relations"], lambda row: row["observation_id"], "Relation observation"
    )
    require_unique(rows["geography"], lambda row: row["place_id"], "Geography place")

    entity_keys = {(row["entity_id"], row["type"]) for row in rows["entities"]}
    entity_ids = {row[0] for row in entity_keys}
    page_keys = {
        (row["wiki_site"], str(row["wiki_page_id"])) for row in rows["wiki_pages"]
    }
    if any(
        row.get("entity_id") is not None
        and (row["entity_id"], row["entity_type"]) not in entity_keys
        for row in rows["mentions"]
    ):
        raise ValueError("Mention Entity foreign key differs")
    if any(
        row.get("wiki_site") is not None
        and row.get("wiki_page_id") is not None
        and (row["wiki_site"], str(row["wiki_page_id"])) not in page_keys
        for row in rows["mentions"]
    ):
        raise ValueError("Mention Wiki Page foreign key differs")
    if any(row["event_id"] not in entity_ids for row in rows["events"]):
        raise ValueError("Event Entity foreign key differs")
    if any(
        row["subject_entity_id"] not in entity_ids
        or row["object_entity_id"] not in entity_ids
        for row in rows["relations"]
    ):
        raise ValueError("Relation Entity foreign key differs")
    if any((row["place_id"], "LOC") not in entity_keys for row in rows["geography"]):
        raise ValueError("Geography Entity foreign key differs")

    geometry = base / "model_geography/geometries.parquet"
    expected_geometry = manifests["model_geography"]["outputs"].get(
        "geometries.parquet"
    )
    if expected_geometry is None or digest(geometry) != expected_geometry:
        raise ValueError("Geometry checksum differs")
    if any(
        row["geometry_ref"]
        != {
            "file": "geometries.parquet",
            "place_id": row["place_id"],
            "sha256": expected_geometry,
        }
        for row in rows["geography"]
    ):
        raise ValueError("Geography pointer differs")
    sources[geometry.name] = geometry

    for relative, expected in manifests["model_geography"]["outputs"].items():
        if not relative.startswith("mapping/"):
            continue
        source = base / "model_geography" / relative
        if digest(source) != expected:
            raise ValueError(f"Mapping checksum differs: {relative}")
        sources[relative] = source

    supporting = {
        "supporting/page_evidence.json.gz": (
            "mentions_pages",
            "page_evidence.json.gz",
        ),
        "supporting/entity_full_records.json.gz": (
            "entities",
            "full_records.json.gz",
        ),
        "supporting/excluded_relations.json.gz": (
            "model_geography",
            "excluded_relations.json.gz",
        ),
        "supporting/relation_selection.json.gz": (
            "model_geography",
            "relation_selection.json.gz",
        ),
        "supporting/temporal_decisions.json.gz": (
            "model_geography",
            "temporal_decisions.json.gz",
        ),
    }
    for relative, (stage, name) in supporting.items():
        source = base / stage / name
        expected = manifests[stage]["outputs"].get(name)
        if expected is None or digest(source) != expected:
            raise ValueError(f"Supporting checksum differs: {name}")
        sources[relative] = source

    output.mkdir(parents=True)
    for relative, source in sources.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    lineage = {
        "mentions_pages_manifest.json": base / "mentions_pages/manifest.json",
        "entities_manifest.json": base / "entities/manifest.json",
        "model_manifest.json": base / "model_geography/manifest.json",
        "model_verification.json": base / "model_geography/verification.json",
        "completion_report.json": completion_path,
    }
    (output / "lineage").mkdir()
    for name, source in lineage.items():
        shutil.copy2(source, output / "lineage" / name)

    counts = {name: len(value) for name, value in rows.items()}
    if counts != {
        "mentions": manifests["mentions_pages"]["sample_count"],
        "wiki_pages": manifests["mentions_pages"]["wiki_page_count"],
        "entities": completion["model_entity_rows"],
        "events": completion["model_event_date_joins"],
        "relations": completion["graph_assertions"],
        "geography": completion["geometry_rows"],
    }:
        raise ValueError(f"Completed table counts differ: {counts}")
    if counts["mentions"] != completion["original_mentions"]:
        raise ValueError("Completion and Mentions counts differ")

    artifacts = {
        path.relative_to(output).as_posix(): digest(path)
        for path in output.rglob("*")
        if path.is_file()
    }
    report = {
        "status": "VERIFIED_FIXED_10K_SIX_TABLE_PACKAGE",
        "scope": "fixed-10K POC; not the full 309,864-mention population",
        "business_tables": counts,
        "geography_targets": completion["geography_targets"],
        "unresolved_geography": completion["unresolved_geography"],
        "mentions_without_wiki_page": sum(
            row.get("wiki_site") is None or row.get("wiki_page_id") is None
            for row in rows["mentions"]
        ),
        "normalized_date_mentions_without_entity": sum(
            row.get("entity_id") is None for row in rows["mentions"]
        ),
        "all_nonempty_foreign_keys_valid": True,
        "identity_assignments_changed": False,
        "network_requests": 0,
        "agent_composite_spatial_date_filter": "OPEN_OUTSIDE_DATA_MODEL",
        "artifacts": artifacts,
    }
    manifest_path = output / "package_manifest.json"
    manifest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["package_manifest_sha256"] = digest(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.base, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
