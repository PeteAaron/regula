"""Export Pydantic models to JSON Schema files committed under ``schemas/``.

The committed files are the output contract for downstream consumers — they
pin against the JSON Schemas, not the Python models. The drift test in
``tests/test_schema_export.py`` calls :func:`diff_schemas` to ensure a
model change without a corresponding schema export fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from regula.config import Config
from regula.schemas import (
    TOC,
    Chunk,
    DeferredFeatureList,
    DocumentMeta,
    Glossary,
    Pages,
    ReferencesIndex,
    StageReport,
    ValidationReport,
)

SCHEMA_MODELS: dict[str, type] = {
    "chunk": Chunk,
    "toc": TOC,
    "document": DocumentMeta,
    "glossary": Glossary,
    "validation_report": ValidationReport,
    "config": Config,
    "stage_report": StageReport,
    "pages": Pages,
    "references_index": ReferencesIndex,
    "deferred": DeferredFeatureList,
}


def _render(model: type) -> str:
    """Render a model's JSON Schema in a deterministic, diff-friendly format."""
    schema = model.model_json_schema()
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def export_schemas(out_dir: Path) -> list[Path]:
    """Write one ``<name>.schema.json`` per model into ``out_dir``. Returns
    the list of files written, sorted by name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(SCHEMA_MODELS):
        model = SCHEMA_MODELS[name]
        path = out_dir / f"{name}.schema.json"
        path.write_text(_render(model), encoding="utf-8")
        written.append(path)
    return written


def diff_schemas(out_dir: Path) -> list[str]:
    """Return the names of any schemas whose committed file differs from
    what the current Python models would produce. Empty list = no drift."""
    drift: list[str] = []
    for name, model in SCHEMA_MODELS.items():
        path = out_dir / f"{name}.schema.json"
        current = _render(model)
        if not path.exists() or path.read_text(encoding="utf-8") != current:
            drift.append(name)
    return sorted(drift)
