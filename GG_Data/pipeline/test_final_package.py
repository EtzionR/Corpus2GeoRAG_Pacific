"""Focused checks for the fixed-10K six-table package builder."""

import gzip
import json

import pytest

from GG_Data.pipeline.final_package import digest, read_json, run


def write_gzip(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(rows, stream)


def fixture(tmp_path):
    base = tmp_path / "run"
    mention = {
        "mention_id": "m1",
        "input_row_number": 0,
        "entity_id": "Q1",
        "entity_type": "LOC",
        "wiki_site": "enwiki",
        "wiki_page_id": 1,
    }
    page = {"wiki_site": "enwiki", "wiki_page_id": 1}
    entity = {"entity_id": "Q1", "type": "LOC"}
    event_entity = {"entity_id": "Q2", "type": "event"}
    event = {"event_id": "Q2"}
    relation = {
        "observation_id": "r1",
        "subject_entity_id": "Q1",
        "object_entity_id": "Q2",
    }
    stage_rows = {
        "mentions_pages": {
            "mentions.json.gz": [mention],
            "page_evidence.json.gz": [],
        },
        "entities": {
            "wiki_pages.json.gz": [page],
            "full_records.json.gz": [],
        },
        "model_geography": {
            "entities.json.gz": [entity, event_entity],
            "events.json.gz": [event],
            "relations.json.gz": [relation],
            "geography.json.gz": [],
            "excluded_relations.json.gz": [],
            "relation_selection.json.gz": [],
            "temporal_decisions.json.gz": [],
        },
    }
    for stage, files in stage_rows.items():
        for name, rows in files.items():
            write_gzip(base / stage / name, rows)
    geometry = base / "model_geography/geometries.parquet"
    geometry.write_bytes(b"fixture")
    mapping = base / "model_geography/mapping/catalog.csv"
    mapping.parent.mkdir(parents=True)
    mapping.write_text("id\nQ1\n", encoding="utf-8")

    entity_manifest = base / "entities/manifest.json"
    for stage, files in stage_rows.items():
        outputs = {name: digest(base / stage / name) for name in files}
        if stage == "model_geography":
            outputs["geometries.parquet"] = digest(geometry)
            outputs["mapping/catalog.csv"] = digest(mapping)
        inputs = {}
        if stage == "entities":
            inputs["mentions.json.gz"] = digest(
                base / "mentions_pages/mentions.json.gz"
            )
        if stage == "model_geography":
            inputs["entities/manifest.json"] = digest(entity_manifest)
        manifest = {"inputs": inputs, "outputs": outputs}
        if stage == "mentions_pages":
            manifest.update(sample_count=1, wiki_page_count=1)
        (base / stage / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    model_manifest = base / "model_geography/manifest.json"
    (base / "model_geography/verification.json").write_text(
        json.dumps(
            {
                "status": "VERIFIED_MODEL_WITH_RANKED_GEOGRAPHY",
                "manifest_sha256": digest(model_manifest),
            }
        ),
        encoding="utf-8",
    )
    completion = base / "recovery_20261024_workflow_v2/completion_report.json"
    completion.parent.mkdir(parents=True)
    completion.write_text(
        json.dumps(
            {
                "status": "PASSED_FIXED_10K_MODEL_GRAPH_AND_SPATIAL_INTEGRATION",
                "original_mentions": 1,
                "model_entity_rows": 2,
                "model_event_date_joins": 1,
                "graph_assertions": 1,
                "geometry_rows": 0,
                "geography_targets": 0,
                "unresolved_geography": 0,
            }
        ),
        encoding="utf-8",
    )
    return base


def test_package_copies_verified_six_table_release(tmp_path):
    base = fixture(tmp_path)
    source_hash = digest(base / "mentions_pages/mentions.json.gz")
    output = tmp_path / "package"
    report = run(base, output)
    assert report["status"] == "VERIFIED_FIXED_10K_SIX_TABLE_PACKAGE"
    assert report["business_tables"] == {
        "mentions": 1,
        "wiki_pages": 1,
        "entities": 2,
        "events": 1,
        "relations": 1,
        "geography": 0,
    }
    assert digest(output / "mentions.json.gz") == source_hash
    assert digest(base / "mentions_pages/mentions.json.gz") == source_hash
    assert read_json(output / "package_manifest.json")["network_requests"] == 0


def test_package_rejects_invalid_mention_foreign_key(tmp_path):
    base = fixture(tmp_path)
    mentions = base / "mentions_pages/mentions.json.gz"
    write_gzip(
        mentions,
        [
            {
                "mention_id": "m1",
                "input_row_number": 0,
                "entity_id": "Q404",
                "entity_type": "LOC",
                "wiki_site": "enwiki",
                "wiki_page_id": 1,
            }
        ],
    )
    manifest = read_json(base / "mentions_pages/manifest.json")
    manifest["outputs"]["mentions.json.gz"] = digest(mentions)
    (base / "mentions_pages/manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    entities_manifest = base / "entities/manifest.json"
    manifest = read_json(entities_manifest)
    manifest["inputs"]["mentions.json.gz"] = digest(mentions)
    entities_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    model_manifest = base / "model_geography/manifest.json"
    manifest = read_json(model_manifest)
    manifest["inputs"]["entities/manifest.json"] = digest(entities_manifest)
    model_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    verification = base / "model_geography/verification.json"
    value = read_json(verification)
    value["manifest_sha256"] = digest(model_manifest)
    verification.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="Mention Entity foreign key differs"):
        run(base, tmp_path / "invalid")
