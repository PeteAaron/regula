"""Stage 6 — `validate`. Health checks against config thresholds.

Runs the cross-model invariant helpers from :mod:`regula.schemas` and
per-artifact JSON Schema validation against the committed
``schemas/*.schema.json`` files. The same helpers that Phase 1 unit-tested
on constructed data run here on real output — define once, use everywhere.

Phase 2 status: works against the skeleton's empty artifacts. Every
invariant holds trivially on empty data; the report's ``metrics`` list is
populated with zero-valued, threshold-cleared entries so downstream
consumers see the metric schema even before real data shows up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from regula._schema_export import SCHEMA_MODELS
from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import (
    TOC,
    Chunk,
    Glossary,
    ReferencesIndex,
    StageReport,
    ValidationMetric,
    ValidationReport,
    assert_asset_linkage_bidirectional,
    assert_reading_order_valid,
    assert_section_windows_consistent,
    assert_source_spans_in_bounds,
)

NAME = "validate"

# Map of (artifact relative path) -> SCHEMA_MODELS key. Used by JSON Schema
# validation. Only artifacts the pipeline actually emits at the output root
# (plus the intermediate pages.json) are checked here.
_ARTIFACTS_TO_CHECK: dict[str, str] = {
    "toc.json": "toc",
    "glossary.json": "glossary",
    "references_index.json": "references_index",
    "intermediate/parse/pages.json": "pages",
}


def _load_chunks(path: Path) -> list[Chunk]:
    if not path.exists():
        return []
    chunks: list[Chunk] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(Chunk.model_validate_json(line))
    return chunks


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schemas(
    output_dir: Path, schemas_dir: Path, errors: list[str], warnings: list[str]
) -> dict[str, int]:
    counts = {"checked": 0, "failed": 0}
    for rel, schema_name in _ARTIFACTS_TO_CHECK.items():
        path = output_dir / rel
        if not path.exists():
            continue
        counts["checked"] += 1
        schema = _load_json(schemas_dir / f"{schema_name}.schema.json")
        data = _load_json(path)
        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:
            counts["failed"] += 1
            errors.append(f"{rel}: schema validation failed: {e.message}")
    # chunks.jsonl is special — validate each line.
    chunks_path = output_dir / "chunks.jsonl"
    if chunks_path.exists():
        chunk_schema = _load_json(schemas_dir / "chunk.schema.json")
        for i, line in enumerate(chunks_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            counts["checked"] += 1
            try:
                jsonschema.validate(instance=json.loads(line), schema=chunk_schema)
            except jsonschema.ValidationError as e:
                counts["failed"] += 1
                errors.append(f"chunks.jsonl line {i + 1}: {e.message}")
    return counts


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    metrics: list[ValidationMetric] = []
    errors: list[str] = []
    warnings: list[str] = []

    log.info("validate.start")

    # 1. Locate assembled outputs (may be in intermediate if finalise hasn't run).
    chunks_path = output_dir / "chunks.jsonl"
    if not chunks_path.exists():
        chunks_path = output_dir / "intermediate" / "extract_glossary" / "chunks.jsonl"
    toc_path = output_dir / "toc.json"
    if not toc_path.exists():
        toc_path = output_dir / "intermediate" / "build_toc" / "toc.json"
    refs_path = output_dir / "references_index.json"
    if not refs_path.exists():
        refs_path = output_dir / "intermediate" / "resolve_references" / "references_index.json"
    glossary_path = output_dir / "glossary.json"
    if not glossary_path.exists():
        glossary_path = output_dir / "intermediate" / "extract_glossary" / "glossary.json"

    chunks = _load_chunks(chunks_path)
    toc = TOC.model_validate_json(toc_path.read_text()) if toc_path.exists() else TOC(entries=[])
    references_index = (
        ReferencesIndex.model_validate_json(refs_path.read_text())
        if refs_path.exists()
        else ReferencesIndex()
    )
    _ = (
        Glossary.model_validate_json(glossary_path.read_text())
        if glossary_path.exists()
        else Glossary(entries=[])
    )

    # 2. Cross-model invariants. Each raises InvariantError on violation.
    try:
        assert_reading_order_valid(chunks)
    except Exception as e:
        errors.append(f"reading_order_valid: {e}")
    try:
        assert_section_windows_consistent(toc, chunks)
    except Exception as e:
        errors.append(f"section_windows_consistent: {e}")
    try:
        assert_asset_linkage_bidirectional(chunks)
    except Exception as e:
        errors.append(f"asset_linkage_bidirectional: {e}")
    for c in chunks:
        try:
            assert_source_spans_in_bounds(c)
        except Exception as e:
            errors.append(f"source_spans_in_bounds[{c.chunk_id}]: {e}")

    metrics.append(
        ValidationMetric(
            name="invariants_ok",
            value=1.0 if not errors else 0.0,
            threshold=1.0,
            passed=not errors,
            counts={"violations": len(errors)},
        )
    )

    # 3. Per-artifact JSON Schema validation.
    schemas_dir = Path("schemas")
    schema_counts = {"checked": 0, "failed": 0}
    if schemas_dir.exists():
        schema_counts = _validate_schemas(output_dir, schemas_dir, errors, warnings)
        metrics.append(
            ValidationMetric(
                name="schemas_valid",
                value=(1.0 if schema_counts["failed"] == 0 else 0.0),
                threshold=1.0 if cfg.validation.fail_on_schema_error else None,
                passed=schema_counts["failed"] == 0,
                counts=schema_counts,
            )
        )
    else:
        warnings.append("schemas/ directory not found; skipping JSON Schema validation")

    # 4. Trivial coverage metrics (placeholder rates; Phase 4 fills these in
    #    with real measurements). They report ``value=1.0`` against ``total=0``
    #    so the skeleton passes cleanly.
    metrics.append(
        ValidationMetric(
            name="page_coverage",
            value=1.0,
            threshold=cfg.validation.min_page_coverage,
            passed=True,
            counts={"covered_pages": 0, "total_pages": 0},
        )
    )
    metrics.append(
        ValidationMetric(
            name="internal_ref_resolution",
            value=1.0,
            threshold=cfg.validation.min_internal_ref_resolution,
            passed=True,
            counts={
                "resolved": 0,
                "unresolved": len(references_index.unresolved_internal),
                "total_internal": 0,
            },
        )
    )
    metrics.append(
        ValidationMetric(
            name="text_reconstruction_coverage",
            value=1.0,
            threshold=cfg.validation.min_text_reconstruction_coverage,
            passed=True,
            counts={"chunk_chars": sum(len(c.text) for c in chunks)},
        )
    )
    metrics.append(
        ValidationMetric(
            name="reading_order_monotonicity",
            value=1.0,
            threshold=cfg.validation.min_reading_order_monotonicity,
            passed=True,
            counts={"chunks": len(chunks)},
        )
    )

    passed = all(m.passed for m in metrics) and not errors

    report = ValidationReport(
        metrics=metrics,
        passed=passed,
        generated_at=datetime.now(UTC),
    )
    (stage_dir / "validation_report.json").write_text(report.model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info(
        "validate.done",
        passed=passed,
        metrics=len(metrics),
        invariant_violations=len(errors),
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=passed,
        counts={"metrics": len(metrics), "chunks_examined": len(chunks)},
        warnings=warnings,
        errors=errors,
    )
