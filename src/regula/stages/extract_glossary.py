"""Stage 5 — `extract_glossary`. Parse glossary section, back-fill terms.

Phase 2 status: stub. Writes an empty Glossary and passes chunks through.
Phase 4 parses the named glossary section, populates Glossary.entries, and
back-fills ``Chunk.defined_terms_used``.

No-op (still writes empty glossary) when ``cfg.references.glossary_section``
is None.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import Glossary, StageReport

NAME = "extract_glossary"


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    resolve_dir = output_dir / "intermediate" / "resolve_references"
    if not resolve_dir.exists():
        raise FileNotFoundError(
            f"extract_glossary stage requires resolve_references output at {resolve_dir} "
            f"(run `regula stage resolve_references` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "extract_glossary.start",
        glossary_section=cfg.references.glossary_section or "(none configured)",
    )
    chunks_in = (resolve_dir / "chunks.jsonl").read_text()
    (stage_dir / "chunks.jsonl").write_text(chunks_in)
    (stage_dir / "glossary.json").write_text(Glossary(entries=[]).model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info("extract_glossary.done", terms=0)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"glossary_terms": 0, "chunks_with_terms": 0},
    )
