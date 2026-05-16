"""Schema export + drift detection.

The drift test enforces the rule that any model change must be accompanied
by re-exporting ``schemas/*.schema.json``. Without this, downstream
consumers pinning against the JSON Schemas would silently fall out of sync
with the Pydantic models.
"""

from __future__ import annotations

import json
from pathlib import Path

from regula._schema_export import SCHEMA_MODELS, diff_schemas, export_schemas

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


def test_export_writes_one_file_per_model(tmp_path: Path) -> None:
    written = export_schemas(tmp_path)
    assert len(written) == len(SCHEMA_MODELS)
    names_written = {p.stem.replace(".schema", "") for p in written}
    assert names_written == set(SCHEMA_MODELS)


def test_exported_schemas_are_valid_json(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    for name in SCHEMA_MODELS:
        path = tmp_path / f"{name}.schema.json"
        data = json.loads(path.read_text())
        assert data["type"] == "object"


def test_no_drift_against_committed_schemas() -> None:
    drift = diff_schemas(SCHEMAS_DIR)
    assert drift == [], (
        f"schemas/ is out of date for: {drift}. "
        f"Run `uv run regula export-schemas --out schemas/` and commit."
    )


def test_export_is_idempotent(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    first = {p.name: p.read_text() for p in tmp_path.iterdir()}
    export_schemas(tmp_path)
    second = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert first == second
