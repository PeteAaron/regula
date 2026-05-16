"""Stage 1 — `parse`. Run the configured parsers, write raw structured output.

Two parsers conceptually:
- A **primary** parser produces the structural document tree (page geometry,
  reading-order text blocks, images, outline, links). Default: PyMuPDF.
- A **link extractor** produces a separate authoritative list of hyperlinks
  + outline. In the PyMuPDF default both come from the same library, so
  this stage produces a single normalised tree at
  ``intermediate/parse/tree.json`` and projections at ``pages.json``,
  ``outline.json``, ``links.json`` for downstream stages that only need
  part of the tree.

No interpretation happens here — the chunk stage takes responsibility for
mapping the raw tree onto :class:`regula.schemas.Chunk` records.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.parsers import pymupdf_parser
from regula.schemas import Page, Pages, StageReport

NAME = "parse"

# Registry of available primary parsers. Add an entry here to plug in a
# new backend (e.g. ``"docling": docling_parser``).
_PARSERS = {
    "pymupdf": pymupdf_parser,
}


def _load_parser(name: str):
    if name not in _PARSERS:
        raise ValueError(
            f"unknown parser {name!r}; available: {sorted(_PARSERS)}. "
            f"Add a new module under regula/parsers and register it here."
        )
    return _PARSERS[name]


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    primary_name = cfg.parsers.primary
    # In offline environments only PyMuPDF works. Honour the configured
    # value if it's something we know how to load; otherwise fall through
    # to PyMuPDF so the pipeline still runs.
    if primary_name not in _PARSERS:
        log.warning(
            "parse.primary_unavailable",
            requested=primary_name,
            falling_back_to="pymupdf",
        )
        primary_name = "pymupdf"

    parser = _load_parser(primary_name)
    log.info("parse.start", parser=primary_name, source_pdf=cfg.source_pdf)

    pdf_path = Path(cfg.source_pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(
            f"source PDF not found: {pdf_path} (configured in {cfg.doc_id})"
        )

    tree = parser.parse(pdf_path)

    # Write the full normalised tree for the chunker.
    (stage_dir / "tree.json").write_text(json.dumps(tree, indent=2, ensure_ascii=False))

    # Projections for stages that don't need the full tree.
    pages_model = Pages(pages=[Page(**p) for p in tree["pages"]])
    (stage_dir / "pages.json").write_text(pages_model.model_dump_json(indent=2))
    (stage_dir / "outline.json").write_text(
        json.dumps(tree["outline"], indent=2, ensure_ascii=False)
    )
    (stage_dir / "links.json").write_text(
        json.dumps(tree["links"], indent=2, ensure_ascii=False)
    )

    finished = datetime.now(UTC)
    log.info(
        "parse.done",
        pages=len(tree["pages"]),
        elements=len(tree["elements"]),
        links=len(tree["links"]),
        images=len(tree["images"]),
        outline_entries=len(tree["outline"]),
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={
            "pages": len(tree["pages"]),
            "elements": len(tree["elements"]),
            "links": len(tree["links"]),
            "images": len(tree["images"]),
            "outline_entries": len(tree["outline"]),
        },
    )
