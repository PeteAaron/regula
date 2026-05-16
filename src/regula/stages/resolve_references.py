"""Stage 3 — `resolve_references`. Hyperlink pass + regex pass.

Phase 2 status: stub. Pass-through of chunks.jsonl; writes empty
ReferencesIndex. Phase 4 replaces the body with real reference resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import ReferencesIndex, StageReport

NAME = "resolve_references"


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    chunk_dir = output_dir / "intermediate" / "chunk"
    if not chunk_dir.exists():
        raise FileNotFoundError(
            f"resolve_references stage requires chunk output at {chunk_dir} "
            f"(run `regula stage chunk` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    log.info("resolve_references.start")
    chunks_in = (chunk_dir / "chunks.jsonl").read_text()
    (stage_dir / "chunks.jsonl").write_text(chunks_in)
    (stage_dir / "references_index.json").write_text(
        ReferencesIndex().model_dump_json(indent=2)
    )

    finished = datetime.now(UTC)
    log.info("resolve_references.done", resolved=0, unresolved=0)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"resolved": 0, "unresolved_internal": 0, "external": 0},
    )
