"""Approved graph interfaces; see docs/ENTITY_MODEL_TO_AGENT_GRAPH_S2T.md.

These helpers do not change the existing Agent query engine. Its integration
must be tested before the resulting graph can be accepted by that engine.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
from shapely.geometry.base import BaseGeometry

from .poc import digest


def graph_node_id(entity_id: str, entity_type: str) -> str:
    """S2T section 4: separate typed QID nodes; preserve per-mention SYN IDs."""
    if re.fullmatch(r"Q[1-9][0-9]*", entity_id):
        if not entity_type or "::" in entity_type:
            raise ValueError(f"Invalid stored entity type: {entity_type!r}")
        return f"{entity_id}::{entity_type}"
    if entity_id.startswith("SYN_"):
        return entity_id
    raise ValueError(f"Unexpected model entity ID: {entity_id!r}")


def endpoint_index(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[str, ...]]:
    """Resolve QID endpoints to separate typed nodes without expanding edges.

    Node `wikidata_id` retains its original QID. Every edge remains the same
    assertion object; no endpoint type winner or Cartesian product is created.
    """
    by_qid: dict[str, list[str]] = defaultdict(list)
    seen = set()
    for node in nodes:
        if node["id"] in seen:
            raise ValueError(f"Duplicate graph node: {node['id']}")
        seen.add(node["id"])
        qid = node.get("wikidata_id")
        if qid is not None:
            if not re.fullmatch(r"Q[1-9][0-9]*", qid) or not node["id"].startswith(
                qid + "::"
            ):
                raise ValueError(f"Invalid typed graph identity: {node['id']}")
            by_qid[qid].append(node["id"])
        elif not node["id"].startswith("SYN_"):
            raise ValueError(f"Missing original Wikidata ID: {node['id']}")
    for edge in edges:
        for field in ("source", "target"):
            if edge[field] not in by_qid:
                raise ValueError(f"Unknown QID edge endpoint: {edge[field]}")
    return {qid: tuple(ids) for qid, ids in by_qid.items()}


def load_geometry(pointer: dict | None, artifact_root: Path) -> BaseGeometry | None:
    """Load Geography's model-level file/place/checksum pointer as Shapely.

    User correction, 21 October: this reference is not a graph JSON field.
    No accepted object -> null. A broken pointer is an error, never a fallback
    geometry. Preserve stored CRS; this loader does not transform coordinates.
    The Agent must check that CRS against its spatial-function requirements.
    """
    if pointer is None:
        return None
    return load_geometries([pointer], artifact_root)[pointer["place_id"]]


def load_geometries(
    pointers: list[dict], artifact_root: Path, *, required_crs: str | None = None
) -> dict[str, BaseGeometry]:
    """Load model references once per file without changing stored geometries.

    Graph S2T section 4: Agent spatial functions require longitude/latitude
    degrees. A caller can require that CRS; incompatible objects fail instead
    of being transformed. All single-object reference checks remain in force.
    """
    from pyproj import CRS

    root = artifact_root.resolve()
    files = {}
    result = {}
    for pointer in pointers:
        if not isinstance(pointer, dict) or set(pointer) != {
            "file",
            "place_id",
            "sha256",
        }:
            raise ValueError("Geometry pointer requires file, place_id and sha256")
        if not all(isinstance(v, str) and v for v in pointer.values()):
            raise ValueError("Geometry pointer fields must be nonempty strings")
        if not re.fullmatch(r"Q[1-9][0-9]*|SYN_[A-Za-z0-9_]+", pointer["place_id"]):
            raise ValueError("Geometry place_id must be a QID or existing SYN ID")
        if not re.fullmatch(r"[0-9a-f]{64}", pointer["sha256"]):
            raise ValueError("Geometry checksum must be a SHA-256 hex digest")
        if pointer["place_id"] in result:
            raise ValueError(f"Duplicate geometry reference: {pointer['place_id']}")
        path = (root / pointer["file"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Geometry pointer leaves the artifact directory")
        if path not in files:
            sha = digest(path)
            if sha != pointer["sha256"]:
                raise ValueError(f"Geometry artifact checksum differs: {path}")
            frame = gpd.read_parquet(path)
            if frame.crs is None:
                raise ValueError(f"Geometry artifact has no CRS metadata: {path}")
            if required_crs and not frame.crs.equals(
                CRS(required_crs), ignore_axis_order=True
            ):
                raise ValueError(
                    f"Geometry CRS is incompatible with {required_crs}: {path}"
                )
            files[path] = (sha, frame)
        sha, frame = files[path]
        if sha != pointer["sha256"]:
            raise ValueError(f"Geometry artifact checksum differs: {path}")
        selected = frame.loc[frame["place_id"] == pointer["place_id"]]
        if len(selected) != 1:
            raise ValueError(
                f"Expected one geometry for {pointer['place_id']}; found {len(selected)}"
            )
        geometry = selected.geometry.iloc[0]
        if (
            not isinstance(geometry, BaseGeometry)
            or geometry.is_empty
            or not geometry.is_valid
        ):
            raise ValueError(f"Invalid/empty stored geometry: {pointer['place_id']}")
        result[pointer["place_id"]] = geometry
    return result
