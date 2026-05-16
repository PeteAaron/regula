"""Stage 4 — `build_toc`. TOC from PDF outline, cross-checked against headings.

Phase 2 status: stub. Writes an empty TOC. Phase 4 derives a real TOC from
the PDF outline (parse) and section_heading chunks (resolve_references).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import TOC, StageReport

NAME = "build_toc"


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    parse_dir = output_dir / "intermediate" / "parse"
    if not parse_dir.exists():
        raise FileNotFoundError(
            f"build_toc stage requires parse output at {parse_dir} "
            f"(run `regula stage parse` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    log.info("build_toc.start")
    (stage_dir / "toc.json").write_text(TOC(entries=[]).model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info("build_toc.done", entries=0)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"toc_entries": 0},
    )
