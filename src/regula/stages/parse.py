"""Stage 1 — `parse`. Writes parser-raw structured output to disk.

Phase 2 status: stub. Writes empty placeholder artifacts so downstream
stages have files to read. Phase 4 replaces the body with real Docling +
PyMuPDF calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import Pages, StageReport

NAME = "parse"


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    log.info("parse.start", source_pdf=cfg.source_pdf)

    (stage_dir / "pages.json").write_text(Pages(pages=[]).model_dump_json(indent=2))
    (stage_dir / "docling.json").write_text(json.dumps({}, indent=2))
    (stage_dir / "links.json").write_text(json.dumps([], indent=2))
    (stage_dir / "outline.json").write_text(json.dumps([], indent=2))

    finished = datetime.now(UTC)
    log.info("parse.done", artifacts=4)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"pages": 0, "links": 0, "outline_entries": 0},
    )
