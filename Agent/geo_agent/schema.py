"""Typed contract of the agent's reply: `AskResult`, the dict `GeoAgent.ask()` returns.

The agent itself builds plain dicts; this model documents and checks them. The UI
can validate a reply with `AskResult.model_validate(result)` and use attributes
instead of string keys. Every model allows extra keys (`extra="allow"`), so keys
added later (e.g. `source_refs`, `bbox`) don't break an older UI.

`SCHEMA_VERSION` is in every reply. It changes in the minor part when keys are
added, and in the major part when a key is renamed, removed or changes type.

The JSON Schema is committed at `geo_agent/schemas/ask_result.schema.json` for
non-Python consumers; regenerate it with `uv run python -m geo_agent.schema`
(a test fails while it is out of date).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from geo_agent.trace import FAILURE_STAGES

SCHEMA_VERSION = "1.4"  # 1.1: bbox, center; 1.2: source_refs, feature geometry_source, confidence n_providers; 1.3: ungrounded_names; 1.4: match nickname/hull_number/alias_part, resolved evidence and model_reading
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "ask_result.schema.json"


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ConfidenceBasis(_Open):
    max_edge_count: float = Field(description="Highest mention count of a relation supporting the answer")
    n_source_pages: int = Field(description="Distinct verified source pages")
    n_providers: int | None = Field(None, description="Distinct source providers (Wikidata, EventKG, Wikipedia, ...) behind the evidence")
    uncertain_match_used: bool = Field(description="A weak or ambiguous name match was used")
    text_based_approximation: bool | None = Field(None, description="Present (true) when a concept was found only through text mentions")


class Confidence(_Open):
    level: Literal["high", "medium", "low"] = Field(description="Support label; not a probability")
    basis: ConfidenceBasis


class Geometry(_Open):
    type: Literal["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]
    coordinates: list[Any] = Field(description="GeoJSON coordinates, [longitude, latitude] order")


class FeatureProperties(_Open):
    id: str = Field(description="Graph id; matches entity_ids")
    name: str = Field(description="Display name")
    type: str = Field(description="Node type, `location` today")
    role: Literal["answer", "context"] = Field(description="`answer` when the feature (or the entity it is drawn for) answers the question")
    related_to: str | None = Field(None, description="Present when the place is drawn on behalf of a non-spatial entity (event, org, person)")
    geometry_source: str | None = Field(None, description="'direct' when the entity's own geometry is drawn, 'related' when it is drawn at a linked place")
    via: str | None = Field(None, description="Set questions: the path that connects the place")
    strength: float | None = Field(None, description="Set questions: how well supported that path is (higher is stronger)")


class Feature(_Open):
    type: Literal["Feature"]
    geometry: Geometry
    properties: FeatureProperties


class FeatureCollection(_Open):
    type: Literal["FeatureCollection"]
    features: list[Feature]


class Center(_Open):
    lat: float
    lon: float


class ToolCall(_Open):
    name: str
    args: Any = Field(description="Tool arguments, usually an object")


class DroppedId(_Open):
    id: str
    reason: Literal["unknown", "not_in_tool_output"]


class ContextMismatch(_Open):
    question_year: int
    start_date: str
    end_date: str


class ResolvedEntity(_Open):
    id: str
    name: str
    typed: str = Field(description="The text the agent searched")
    matched_label: str | None = None
    match: Literal["name", "alias", "nickname", "hull_number", "alias_part", "fuzzy"] | None = None
    score: float | None = None
    quality: Literal["clear", "uncertain"]
    model_rewritten_query: bool
    model_reading: bool = Field(False, description="The model's own reading: the graph doesn't tie the user's words to "
                                "this entity (e.g. a nickname the graph doesn't hold)")
    evidence: str | None = Field(None, description="Where the graph holds the matched nickname: the alias or the text")
    context_mismatch: ContextMismatch | None = None


class SourceRef(_Open):
    provider: str | None = Field(None, description="Source system, e.g. 'Wikidata', 'EventKG', 'Wikipedia'")
    record_id: str | None = Field(None, description="Id of the source assertion (never a Wikipedia page title)")
    url: str | None = Field(None, description="Link to the source record or page")
    page: str | None = Field(None, description="Wikipedia page title, when the source is a Wikipedia page")


class ResultSetInfo(_Open):
    handle: str
    label: str
    total: int = Field(description="Members in the set; show this count")
    on_map: int
    map_truncated: bool


class DroppedSet(_Open):
    handle: str
    reason: Literal["unknown", "not_in_tool_output"]


class ErrorInfo(_Open):
    type: str = Field(description="Exception class name")
    message: str
    stage: Literal[FAILURE_STAGES] = Field(  # type: ignore[valid-type]
        description="model_call, tool_call, step_limit, post_processing, unknown; from AgentRunner: "
                    "timeout (stopped after the time limit), cancelled, busy (another question of the "
                    "same thread_id was still running; this one did not run)")


class AskResult(_Open):
    """One reply of the agent. Every key is present in every reply, including errors."""

    schema_version: str = Field(description="Version of this contract, e.g. '1.0'")
    answer: str = Field(description="Chat text; may contain Markdown. Untrusted: render sanitized, never as raw HTML. Empty on error.")
    answer_ids: list[str] = Field(description="Entities that answer the question; highlight these")
    context_ids: list[str] = Field(description="Other entities the answer mentions; show more quietly")
    entity_ids: list[str] = Field(description="answer_ids followed by context_ids")
    sources: list[str] = Field(description="Wikipedia page titles the answer relies on (never record ids)")
    source_refs: list[SourceRef] = Field(description="Structured provenance of the answer's evidence (at most 25); [] when none")
    geojson: FeatureCollection
    bbox: list[float] | None = Field(
        min_length=4, max_length=4,
        description="[min_lon, min_lat, max_lon, max_lat] around the map features; null without features. "
                    "min_lon > max_lon means the box crosses the 180° meridian (for Leaflet fit_bounds, add 360 to max_lon).")
    center: Center | None = Field(description="Middle of bbox, for the initial map view; null without features")
    tool_calls: list[ToolCall]
    run_id: str = Field(description="Trace id: `uv run geo-agent --trace <run_id>`")
    warnings: list[str] = Field(description="Warning codes; open set, see README")
    dropped_ids: list[DroppedId]
    unverified_sources: list[str]
    unverified_numbers: list[float]
    ungrounded_names: list[str] = Field(description="Graph entities the answer names although no tool returned them "
                                        "(the model added them from its own knowledge); warning ungrounded_names")
    resolved_entities: list[ResolvedEntity]
    interpretation: list[str] = Field(description="How names were read when not literal; show to the user")
    confidence: Confidence
    result_sets: list[ResultSetInfo]
    dropped_sets: list[DroppedSet]
    error: ErrorInfo | None = Field(description="null on success")


def json_schema() -> dict[str, Any]:
    schema = AskResult.model_json_schema()
    schema["$id"] = f"geo_agent/ask_result/{SCHEMA_VERSION}"
    return schema


def schema_text() -> str:
    return json.dumps(json_schema(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    """Write the JSON Schema file; with --check, exit 1 if it is out of date instead."""
    current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else None
    if "--check" in sys.argv[1:]:
        ok = current == schema_text()
        print(f"{SCHEMA_PATH}: {'up to date' if ok else 'OUT OF DATE (run: uv run python -m geo_agent.schema)'}")
        return 0 if ok else 1
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(schema_text(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
