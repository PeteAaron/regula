"""Stage — `finalise`. Copy chosen intermediate artifacts to the output root.

The orchestrator handles ``document.json`` separately (after finalise has
finished) so its own ``StageReport`` can be included in
``DocumentMeta.stage_reports``.

Post-wind-back the artifact set is intentionally minimal: blocks + pages +
links + validation_report. Higher-level outputs (chunks, references, TOC,
glossary) are deferred until the user has identified document conventions
manually.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import Links, PageLink, StageReport

NAME = "finalise"

_COPIES: list[tuple[str, str]] = [
    ("intermediate/extract_blocks/blocks.jsonl", "blocks.jsonl"),
    ("intermediate/parse/pages.json", "pages.json"),
    ("intermediate/parse/outline.json", "outline.json"),
    ("intermediate/validate/validation_report.json", "validation_report.json"),
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

    # Convert the raw parser link records into the typed Links schema.
    raw_links_path = output_dir / "intermediate" / "parse" / "links.json"
    if raw_links_path.exists():
        raw = json.loads(raw_links_path.read_text(encoding="utf-8"))
        links_model = Links(links=[PageLink(**r) for r in raw])
        (output_dir / "links.json").write_text(links_model.model_dump_json(indent=2))
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
