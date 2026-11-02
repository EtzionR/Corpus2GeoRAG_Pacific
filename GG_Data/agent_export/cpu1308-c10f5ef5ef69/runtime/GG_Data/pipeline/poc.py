"""Exact existing consumer helper bodies; see accompanying Apache LICENSE."""
import gzip,hashlib,json
from pathlib import Path
from typing import Any

def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def read_json(path: Path) -> Any:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)
