"""Stage orchestrator.

A :class:`Pipeline` instance owns one ``Config`` and one output directory.
It runs the seven stages in order on ``run()`` and can re-execute any
single stage on ``run_stage(name)`` provided the predecessors' outputs are
on disk. Stages communicate exclusively via files under
``<output_dir>/intermediate/<stage_name>/``.

For Phase 2 the stages themselves are stubs (empty schema-valid output);
the orchestrator, however, is the real shape that Phase 4 keeps using
unchanged.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import json
from datetime import UTC, datetime

import regula
from regula.config import Config, config_sha256, source_pdf_sha256
from regula.logging import bind_stage, clear_stage, configure_logging, get_logger
from regula.schemas import DocumentMeta, StageReport, ValidationReport
from regula.stages import (
    build_toc,
    chunk,
    extract_glossary,
    finalise,
    parse,
    resolve_references,
    validate,
)


class PipelineError(RuntimeError):
    """Raised when a stage can't run because predecessor output is missing
    or the run is otherwise misconfigured."""


# Stage execution order. Single source of truth — :func:`run` and
# :func:`run_stage` both look up by name here.
StageFn = Callable[[Path, Config], StageReport]
STAGES: list[tuple[str, StageFn]] = [
    ("parse", parse.run),
    ("chunk", chunk.run),
    ("resolve_references", resolve_references.run),
    ("build_toc", build_toc.run),
    ("extract_glossary", extract_glossary.run),
    ("validate", validate.run),
]
STAGE_NAMES: list[str] = [name for name, _ in STAGES]


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


class Pipeline:
    """Stage orchestrator. One instance per run."""

    def __init__(self, cfg: Config, output_root: Path | None = None) -> None:
        self.cfg = cfg
        self.output_dir = (output_root or Path("output") / cfg.doc_id).resolve()

    # --- public API ------------------------------------------------------

    def run(self) -> ValidationReport:
        """Run all stages end-to-end. Clobbers the output directory."""
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self._prepare()

        configure_logging(self.output_dir)
        log = get_logger("pipeline")
        log.info("pipeline.start", doc_id=self.cfg.doc_id, output_dir=str(self.output_dir))

        reports: list[StageReport] = []
        for name, fn in STAGES:
            reports.append(self._invoke(name, fn))

        validation = _load_validation(self.output_dir)

        reports.append(self._invoke("finalise", finalise.run))
        pipeline_passed = all(r.ok for r in reports) and validation.passed
        self._write_document_meta(reports, pipeline_passed)

        log.info(
            "pipeline.done",
            stages=len(reports),
            pipeline_passed=pipeline_passed,
            output_dir=str(self.output_dir),
        )
        clear_stage()
        return validation

    def run_stage(self, name: str) -> StageReport:
        """Run a single stage. Preserves anything already in the output dir."""
        if name == "finalise":
            raise PipelineError("finalise is run automatically by Pipeline.run()")
        if name not in STAGE_NAMES:
            raise PipelineError(
                f"unknown stage {name!r}; known stages: {', '.join(STAGE_NAMES)}"
            )
        self._prepare()
        configure_logging(self.output_dir)
        fn = dict(STAGES)[name]
        report = self._invoke(name, fn)
        clear_stage()
        return report

    def revalidate(self) -> ValidationReport:
        """Re-run just the validate stage against existing artifacts."""
        if not self.output_dir.exists():
            raise PipelineError(
                f"output directory {self.output_dir} doesn't exist — nothing to revalidate"
            )
        configure_logging(self.output_dir)
        self._invoke("validate", validate.run)
        clear_stage()
        return _load_validation(self.output_dir)

    # --- helpers ---------------------------------------------------------

    def _prepare(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "intermediate").mkdir(exist_ok=True)
        (self.output_dir / "assets").mkdir(exist_ok=True)

    def _invoke(self, name: str, fn: StageFn) -> StageReport:
        bind_stage(name)
        try:
            return fn(self.output_dir, self.cfg)
        except FileNotFoundError as e:
            raise PipelineError(str(e)) from e

    def _write_document_meta(
        self, stage_reports: list[StageReport], pipeline_passed: bool
    ) -> None:
        chunks_path = self.output_dir / "chunks.jsonl"
        chunk_count = 0
        if chunks_path.exists():
            chunk_count = sum(
                1
                for line in chunks_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        pages_path = self.output_dir / "pages.json"
        page_count = 0
        if pages_path.exists():
            page_count = len(
                json.loads(pages_path.read_text(encoding="utf-8")).get("pages", [])
            )

        document = DocumentMeta(
            doc_id=self.cfg.doc_id,
            title=self.cfg.title,
            edition=self.cfg.edition,
            jurisdiction=self.cfg.jurisdiction,
            legal_status=self.cfg.legal_status,
            source_pdf=self.cfg.source_pdf,
            source_pdf_sha256=_safe_pdf_sha256(self.cfg.source_pdf),
            config_sha256=config_sha256(self.cfg),
            git_sha=_git_sha(),
            generated_at=datetime.now(UTC),
            regula_version=regula.__version__,
            parser_versions={},  # Phase 4 populates with real parser versions.
            page_count=page_count,
            chunk_count=chunk_count,
            stage_reports=stage_reports,
            pipeline_passed=pipeline_passed,
        )
        (self.output_dir / "document.json").write_text(
            document.model_dump_json(indent=2)
        )


def _safe_pdf_sha256(path: str) -> str:
    """Hash the source PDF if it exists; otherwise return an explicit sentinel.

    The sentinel makes it obvious in ``document.json`` when a run was
    executed without an input PDF (Phase 2 skeleton testing).
    """
    p = Path(path)
    if not p.exists():
        return "sha256:no-source-pdf"
    return source_pdf_sha256(p)


def _load_validation(output_dir: Path) -> ValidationReport:
    report_path = output_dir / "intermediate" / "validate" / "validation_report.json"
    return ValidationReport.model_validate_json(report_path.read_text(encoding="utf-8"))
