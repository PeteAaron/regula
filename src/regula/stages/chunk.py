"""Stage 2 — `chunk`. Walks the parsed document tree and emits chunks.

Phase 2 status: stub. Writes an empty chunks.jsonl. Phase 4 walks the
Docling output and produces real Chunk records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import StageReport

NAME = "chunk"


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    parse_dir = output_dir / "intermediate" / "parse"
    if not parse_dir.exists():
        raise FileNotFoundError(
            f"chunk stage requires parse output at {parse_dir} (run `regula stage parse` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    log.info("chunk.start")
    # Empty chunks for the skeleton; Phase 4 replaces this with the real walk.
    (stage_dir / "chunks.jsonl").write_text("")

    finished = datetime.now(UTC)
    log.info("chunk.done", chunks=0)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"chunks_emitted": 0, "continuations_merged": 0},
    )
