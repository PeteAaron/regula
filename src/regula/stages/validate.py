"""Stage — `validate`. Informational health metrics for a block run.

Post-wind-back, validate is advisory only. It produces counts and
descriptive stats — blocks per page, font-size distribution, internal-
vs-external link breakdown — so the user can spot anomalies in the
preview, but it never fails the run.

The hard cross-model invariants from the v0 chunk pipeline (reading
order monotonicity, section window containment, etc.) no longer apply
because chunking is deferred. The remaining check is per-artifact JSON
Schema validation — that one *does* fail the run, since a malformed
artifact means something is wrong with the extractor itself.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from regula._schema_export import SCHEMA_MODELS
from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import (
    Block,
    Links,
    Pages,
    StageReport,
    ValidationMetric,
    ValidationReport,
)

NAME = "validate"

_ARTIFACTS_TO_CHECK: list[tuple[str, str, str]] = [
    ("pages", "intermediate/parse/pages.json", "pages.json"),
    ("deferred", "deferred.json", "deferred.json"),
]


def _load_blocks(output_dir: Path) -> list[Block]:
    path = output_dir / "intermediate" / "extract_blocks" / "blocks.jsonl"
    if not path.exists():
        return []
    blocks: list[Block] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            blocks.append(Block.model_validate_json(line))
    return blocks


def _load_pages(output_dir: Path) -> Pages | None:
    path = output_dir / "intermediate" / "parse" / "pages.json"
    if not path.exists():
        return None
    return Pages.model_validate_json(path.read_text(encoding="utf-8"))


def _load_links(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "intermediate" / "parse" / "links.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _page_coverage_metric(blocks: list[Block], pages: Pages | None) -> ValidationMetric:
    total = len(pages.pages) if pages else 0
    if total == 0:
        return ValidationMetric(name="page_coverage", value=0.0, passed=True)
    covered = len({b.page for b in blocks})
    return ValidationMetric(
        name="page_coverage",
        value=covered / total,
        passed=True,
        counts={"pages_with_blocks": covered, "total_pages": total},
    )


def _blocks_per_page_metric(blocks: list[Block], pages: Pages | None) -> ValidationMetric:
    if not blocks or not pages:
        return ValidationMetric(name="blocks_per_page_mean", value=0.0, passed=True)
    per_page: dict[int, int] = {}
    for b in blocks:
        per_page[b.page] = per_page.get(b.page, 0) + 1
    counts_list = list(per_page.values()) or [0]
    mean = statistics.mean(counts_list)
    return ValidationMetric(
        name="blocks_per_page_mean",
        value=mean,
        passed=True,
        counts={
            "blocks_total": len(blocks),
            "min_per_page": min(counts_list),
            "max_per_page": max(counts_list),
        },
    )


def _region_breakdown(blocks: list[Block]) -> ValidationMetric:
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.region.value] = counts.get(b.region.value, 0) + 1
    return ValidationMetric(
        name="blocks_by_region",
        value=float(len(blocks)),
        passed=True,
        counts=counts,
    )


def _looks_like_breakdown(blocks: list[Block]) -> ValidationMetric:
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.looks_like.value] = counts.get(b.looks_like.value, 0) + 1
    return ValidationMetric(
        name="blocks_by_looks_like",
        value=float(len(blocks)),
        passed=True,
        counts=counts,
    )


def _link_breakdown(links_raw: list[dict[str, Any]]) -> ValidationMetric:
    counts: dict[str, int] = {"internal": 0, "external": 0}
    for r in links_raw:
        kind = r.get("kind")
        if kind in counts:
            counts[kind] += 1
    return ValidationMetric(
        name="links_by_kind",
        value=float(len(links_raw)),
        passed=True,
        counts=counts,
    )


def _schema_check(output_dir: Path) -> tuple[ValidationMetric, list[str]]:
    """Validate produced JSON artifacts against their committed JSON Schemas.

    This is the one check that can still fail the run — a malformed
    artifact means the producer itself is broken.
    """
    repo_root = Path(__file__).parent.parent.parent.parent
    schema_dir = repo_root / "schemas"
    failures: list[dict[str, Any]] = []
    errors: list[str] = []

    schema_map = {name: model for name, model in SCHEMA_MODELS.items()}

    for schema_key, intermediate_rel, root_rel in _ARTIFACTS_TO_CHECK:
        # Prefer the output-root copy if it exists.
        candidates = [output_dir / root_rel, output_dir / intermediate_rel]
        target = next((p for p in candidates if p.exists()), None)
        if target is None:
            continue
        schema_path = schema_dir / f"{schema_key}.schema.json"
        if not schema_path.exists():
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(instance=data, schema=schema)
        except (jsonschema.ValidationError, json.JSONDecodeError) as e:
            failures.append({"artifact": str(target.name), "error": str(e)[:200]})
            errors.append(f"{target.name}: {e}")

    # Blocks (jsonl) — validate each line.
    blocks_path = output_dir / "blocks.jsonl"
    if not blocks_path.exists():
        blocks_path = output_dir / "intermediate" / "extract_blocks" / "blocks.jsonl"
    if blocks_path.exists():
        for i, line in enumerate(blocks_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                Block.model_validate_json(line)
            except Exception as e:
                failures.append({"artifact": "blocks.jsonl", "line": i + 1, "error": str(e)[:200]})
                errors.append(f"blocks.jsonl:{i + 1}: {e}")
                if len(failures) > 5:
                    break

    metric = ValidationMetric(
        name="schema_conformance",
        value=1.0 if not failures else 0.0,
        threshold=1.0,
        passed=not failures,
        counts={"failures": len(failures)},
        sample_failures=failures[:20],
    )
    return metric, errors


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    blocks = _load_blocks(output_dir)
    pages = _load_pages(output_dir)
    links = _load_links(output_dir)
    log.info("validate.start", blocks=len(blocks), pages=len(pages.pages) if pages else 0)

    metrics: list[ValidationMetric] = [
        _page_coverage_metric(blocks, pages),
        _blocks_per_page_metric(blocks, pages),
        _region_breakdown(blocks),
        _looks_like_breakdown(blocks),
        _link_breakdown(links),
    ]
    schema_metric, schema_errors = _schema_check(output_dir)
    metrics.append(schema_metric)

    report = ValidationReport(
        metrics=metrics,
        passed=schema_metric.passed,  # only schema fails the run
        generated_at=datetime.now(UTC),
    )
    (stage_dir / "validation_report.json").write_text(report.model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info(
        "validate.done",
        passed=report.passed,
        metrics=len(metrics),
        schema_failures=len(schema_errors),
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"metrics_produced": len(metrics), "schema_failures": len(schema_errors)},
        errors=schema_errors,
    )
