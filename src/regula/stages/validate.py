"""Stage 6 — `validate`. Health checks against config thresholds.

Two layers of checks:
1. **Invariant helpers** from :mod:`regula.schemas` — reading order is
   total, section windows contain their chunks, asset linkage is
   bidirectional, source spans are in bounds. These are hard errors.
2. **Configured-threshold metrics** — page coverage, internal reference
   resolution rate, text reconstruction coverage, reading-order
   monotonicity, plus per-artifact JSON Schema validation.

The same invariant code runs in Phase 1's unit tests (against constructed
data) and here (against real output) — define once, use everywhere.
"""

from __future__ import annotations

import json
import re
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
    ReferenceType,
    StageReport,
    ValidationMetric,
    ValidationReport,
    assert_asset_linkage_bidirectional,
    assert_reading_order_valid,
    assert_section_windows_consistent,
    assert_source_spans_in_bounds,
)

NAME = "validate"

# (schema name, intermediate path, output-root path). Schema check
# prefers the output-root copy if it exists (post-finalise), otherwise
# falls back to the producing stage's intermediate.
_ARTIFACTS_TO_CHECK: list[tuple[str, str, str]] = [
    ("toc", "intermediate/build_toc/toc.json", "toc.json"),
    ("glossary", "intermediate/extract_glossary/glossary.json", "glossary.json"),
    (
        "references_index",
        "intermediate/resolve_references/references_index.json",
        "references_index.json",
    ),
    ("pages", "intermediate/parse/pages.json", "pages.json"),
    # deferred.json is written by the orchestrator after finalise, so the
    # intermediate path is the same as the root path. Schema check still
    # runs post-finalise when --validate-only re-runs against the output.
    ("deferred", "deferred.json", "deferred.json"),
]


def _load_chunks(path: Path) -> list[Chunk]:
    if not path.exists():
        return []
    return [
        Chunk.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schemas(
    output_dir: Path, schemas_dir: Path, errors: list[str]
) -> dict[str, int]:
    counts = {"checked": 0, "failed": 0}
    for schema_name, intermediate_rel, root_rel in _ARTIFACTS_TO_CHECK:
        root_path = output_dir / root_rel
        path = root_path if root_path.exists() else output_dir / intermediate_rel
        if not path.exists():
            continue
        counts["checked"] += 1
        schema = _load_json(schemas_dir / f"{schema_name}.schema.json")
        try:
            jsonschema.validate(instance=_load_json(path), schema=schema)
        except jsonschema.ValidationError as e:
            counts["failed"] += 1
            errors.append(f"{root_rel}: schema validation failed: {e.message}")
    chunks_path = output_dir / "chunks.jsonl"
    if not chunks_path.exists():
        chunks_path = output_dir / "intermediate" / "extract_glossary" / "chunks.jsonl"
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


# --- metric calculators --------------------------------------------------


def _normalise_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _page_coverage(chunks: list[Chunk], page_count: int) -> tuple[float, dict[str, int]]:
    if page_count == 0:
        return 1.0, {"covered_pages": 0, "total_pages": 0}
    covered_pages: set[int] = set()
    for c in chunks:
        for p in range(c.page_start, c.page_end + 1):
            covered_pages.add(p)
    return (
        len(covered_pages) / page_count,
        {"covered_pages": len(covered_pages), "total_pages": page_count},
    )


def _internal_ref_resolution(chunks: list[Chunk]) -> tuple[float, dict[str, int]]:
    resolved = 0
    unresolved = 0
    for c in chunks:
        for ref in c.references_out:
            if ref.type is ReferenceType.INTERNAL:
                if ref.target_chunk_id is None:
                    unresolved += 1
                else:
                    resolved += 1
    total = resolved + unresolved
    rate = 1.0 if total == 0 else resolved / total
    return rate, {"resolved": resolved, "unresolved": unresolved, "total": total}


def _text_reconstruction_coverage(
    chunks: list[Chunk], parse_tree: dict[str, Any] | None
) -> tuple[float, dict[str, int]]:
    """Concatenated chunk text vs. parse-tree source text, whitespace-
    normalised. We use the parse tree's flat element text as the
    "source", since that's what the chunker had to work with."""
    if parse_tree is None:
        return 1.0, {"chunk_chars": 0, "source_chars": 0}
    source_text = _normalise_text(
        " ".join(e["text"] for e in parse_tree.get("elements", []))
    )
    chunk_text = _normalise_text(" ".join(c.text for c in chunks))
    if not source_text:
        return 1.0, {"chunk_chars": len(chunk_text), "source_chars": 0}
    # Cheap substring-coverage: count tokens from source present in chunk text.
    source_tokens = source_text.split()
    chunk_tokens_set = set(chunk_text.split())
    if not source_tokens:
        return 1.0, {"chunk_chars": len(chunk_text), "source_chars": 0}
    matched = sum(1 for t in source_tokens if t in chunk_tokens_set)
    rate = matched / len(source_tokens)
    return rate, {
        "matched_tokens": matched,
        "source_tokens": len(source_tokens),
        "chunk_chars": len(chunk_text),
        "source_chars": len(source_text),
    }


def _reading_order_monotonicity(
    chunks: list[Chunk],
) -> tuple[float, dict[str, int]]:
    """For each page, the chunks ordered by order_index should have
    non-decreasing y0. Count the fraction of adjacent-pairs that hold."""
    by_page: dict[int, list[Chunk]] = {}
    for c in chunks:
        by_page.setdefault(c.page_start, []).append(c)
    total_pairs = 0
    monotonic_pairs = 0
    for page, page_chunks in by_page.items():
        page_chunks.sort(key=lambda c: c.order_index)
        for a, b in zip(page_chunks, page_chunks[1:], strict=False):
            total_pairs += 1
            a_y = min(s.bbox[1] for s in a.meta.source_spans if s.page == page)
            b_y = min(s.bbox[1] for s in b.meta.source_spans if s.page == page)
            if b_y >= a_y:
                monotonic_pairs += 1
    if total_pairs == 0:
        return 1.0, {"monotonic_pairs": 0, "total_pairs": 0}
    return monotonic_pairs / total_pairs, {
        "monotonic_pairs": monotonic_pairs,
        "total_pairs": total_pairs,
    }


# --- stage runner --------------------------------------------------------


def _resolve_artifact(
    output_dir: Path, name: str, intermediate_stage: str
) -> Path:
    direct = output_dir / name
    if direct.exists():
        return direct
    return output_dir / "intermediate" / intermediate_stage / name


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    metrics: list[ValidationMetric] = []
    errors: list[str] = []
    warnings: list[str] = []

    chunks_path = _resolve_artifact(output_dir, "chunks.jsonl", "extract_glossary")
    toc_path = _resolve_artifact(output_dir, "toc.json", "build_toc")
    refs_path = _resolve_artifact(
        output_dir, "references_index.json", "resolve_references"
    )
    glossary_path = _resolve_artifact(
        output_dir, "glossary.json", "extract_glossary"
    )
    pages_path = output_dir / "intermediate" / "parse" / "pages.json"
    tree_path = output_dir / "intermediate" / "parse" / "tree.json"

    chunks = _load_chunks(chunks_path)
    toc = (
        TOC.model_validate_json(toc_path.read_text())
        if toc_path.exists()
        else TOC(entries=[])
    )
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
    page_count = 0
    if pages_path.exists():
        page_count = len(
            json.loads(pages_path.read_text(encoding="utf-8")).get("pages", [])
        )
    parse_tree = (
        json.loads(tree_path.read_text(encoding="utf-8")) if tree_path.exists() else None
    )

    log.info(
        "validate.start",
        chunks=len(chunks),
        page_count=page_count,
        toc_entries=len(toc.entries),
    )

    # 1. Invariants.
    invariant_errors: list[str] = []
    try:
        assert_reading_order_valid(chunks)
    except Exception as e:
        invariant_errors.append(f"reading_order_valid: {e}")
    try:
        assert_section_windows_consistent(toc, chunks)
    except Exception as e:
        invariant_errors.append(f"section_windows_consistent: {e}")
    try:
        assert_asset_linkage_bidirectional(chunks)
    except Exception as e:
        invariant_errors.append(f"asset_linkage_bidirectional: {e}")
    for c in chunks:
        try:
            assert_source_spans_in_bounds(c)
        except Exception as e:
            invariant_errors.append(f"source_spans_in_bounds[{c.chunk_id}]: {e}")
    errors.extend(invariant_errors)
    metrics.append(
        ValidationMetric(
            name="invariants_ok",
            value=1.0 if not invariant_errors else 0.0,
            threshold=1.0,
            passed=not invariant_errors,
            counts={"violations": len(invariant_errors)},
            sample_failures=[
                {"detail": s} for s in invariant_errors[:20]
            ],
        )
    )

    # 2. Schema validation.
    schemas_dir = Path("schemas")
    if schemas_dir.exists():
        schema_errors: list[str] = []
        counts = _validate_schemas(output_dir, schemas_dir, schema_errors)
        errors.extend(schema_errors)
        metrics.append(
            ValidationMetric(
                name="schemas_valid",
                value=1.0 if counts["failed"] == 0 else 0.0,
                threshold=1.0 if cfg.validation.fail_on_schema_error else None,
                passed=counts["failed"] == 0,
                counts=counts,
                sample_failures=[
                    {"detail": s} for s in schema_errors[:20]
                ],
            )
        )
    else:
        warnings.append("schemas/ directory not found; skipping JSON Schema validation")

    # 3. Page coverage.
    rate, c = _page_coverage(chunks, page_count)
    metrics.append(
        ValidationMetric(
            name="page_coverage",
            value=rate,
            threshold=cfg.validation.min_page_coverage,
            passed=rate >= cfg.validation.min_page_coverage,
            counts=c,
        )
    )

    # 4. Internal reference resolution.
    rate, c = _internal_ref_resolution(chunks)
    metrics.append(
        ValidationMetric(
            name="internal_ref_resolution",
            value=rate,
            threshold=cfg.validation.min_internal_ref_resolution,
            passed=rate >= cfg.validation.min_internal_ref_resolution,
            counts={
                **c,
                "in_index_unresolved": len(references_index.unresolved_internal),
            },
            sample_failures=[
                {
                    "source_chunk_id": b.source_chunk_id,
                    "label": b.label,
                }
                for b in references_index.unresolved_internal[:20]
            ],
        )
    )

    # 5. Text reconstruction coverage.
    rate, c = _text_reconstruction_coverage(chunks, parse_tree)
    metrics.append(
        ValidationMetric(
            name="text_reconstruction_coverage",
            value=rate,
            threshold=cfg.validation.min_text_reconstruction_coverage,
            passed=rate >= cfg.validation.min_text_reconstruction_coverage,
            counts=c,
        )
    )

    # 6. Reading-order monotonicity.
    rate, c = _reading_order_monotonicity(chunks)
    metrics.append(
        ValidationMetric(
            name="reading_order_monotonicity",
            value=rate,
            threshold=cfg.validation.min_reading_order_monotonicity,
            passed=rate >= cfg.validation.min_reading_order_monotonicity,
            counts=c,
        )
    )

    passed = all(m.passed for m in metrics) and not errors

    report = ValidationReport(
        metrics=metrics, passed=passed, generated_at=datetime.now(UTC)
    )
    (stage_dir / "validation_report.json").write_text(report.model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info(
        "validate.done",
        passed=passed,
        metrics=len(metrics),
        invariant_violations=len(invariant_errors),
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
