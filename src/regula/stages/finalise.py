"""Stage 7 — `finalise`. Copy chosen intermediate artifacts to the output root.

The orchestrator handles ``document.json`` separately (after finalise has
finished) so its own ``StageReport`` can be included in
``DocumentMeta.stage_reports``.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import StageReport

NAME = "finalise"

# (relative source under output_dir, relative destination under output_dir).
_COPIES: list[tuple[str, str]] = [
    ("intermediate/extract_glossary/chunks.jsonl", "chunks.jsonl"),
    ("intermediate/build_toc/toc.json", "toc.json"),
    ("intermediate/extract_glossary/glossary.json", "glossary.json"),
    ("intermediate/resolve_references/references_index.json", "references_index.json"),
    ("intermediate/validate/validation_report.json", "validation_report.json"),
    ("intermediate/parse/pages.json", "pages.json"),
]


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    log.info("finalise.start")

    copied = 0
    for src_rel, dst_rel in _COPIES:
        src = output_dir / src_rel
        if not src.exists():
            continue
        shutil.copy2(src, output_dir / dst_rel)
        copied += 1

    finished = datetime.now(UTC)
    log.info("finalise.done", copied=copied)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"artifacts_copied": copied},
    )
