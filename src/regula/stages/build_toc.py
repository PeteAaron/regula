"""Stage 4 — `build_toc`. Derive the TOC from section_heading chunks.

The TOC's job is to make section retrieval a single range query: every
``TOCEntry`` carries the ``order_index`` window that defines its section.
Phase 4 derives the TOC from the heading chunks emitted by Stage 2 (since
those have already been cross-checked against the PDF outline by the
chunker). Each heading's window runs from its own ``order_index`` up to
(but not including) the next heading at the same or higher level.

Nesting comes from each chunk's ``heading_level``: a level-2 heading
inside a level-1 window becomes a child of that level-1 entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import TOC, Chunk, ChunkType, StageReport, TOCEntry

NAME = "build_toc"


def _load_chunks(path: Path) -> list[Chunk]:
    return [
        Chunk.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _compute_windows(
    chunks: list[Chunk],
) -> list[dict[str, Any]]:
    """For each heading chunk in order, compute the [first, last] window
    that defines its section."""
    headings = [
        c
        for c in chunks
        if c.type in (ChunkType.SECTION_HEADING, ChunkType.APPENDIX)
    ]
    by_order = {c.order_index: c for c in chunks}
    max_order = max(by_order) if by_order else -1
    out: list[dict[str, Any]] = []
    for i, h in enumerate(headings):
        # Find next heading at the same or higher level (i.e. smaller number).
        next_idx: int | None = None
        for j in range(i + 1, len(headings)):
            if (headings[j].heading_level or 1) <= (h.heading_level or 1):
                next_idx = j
                break
        last_oi = (
            headings[next_idx].order_index - 1 if next_idx is not None else max_order
        )
        last_chunk = by_order[last_oi]
        out.append(
            {
                "heading": h,
                "first_oi": h.order_index,
                "last_oi": last_oi,
                "first_chunk_id": h.chunk_id,
                "last_chunk_id": last_chunk.chunk_id,
            }
        )
    return out


def build_toc(chunks: list[Chunk]) -> TOC:
    """Return a TOC tree derived from heading chunks."""
    windowed = _compute_windows(chunks)
    if not windowed:
        return TOC(entries=[])

    # Compute parent_of using a stack of (level, index).
    parent_of: list[int] = [-1] * len(windowed)
    stack: list[tuple[int, int]] = []
    for i, w in enumerate(windowed):
        level = int(w["heading"].heading_level or 1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            parent_of[i] = stack[-1][1]
        stack.append((level, i))

    # Build entries in reverse so each child is ready before its parent.
    children_of: list[list[TOCEntry]] = [[] for _ in windowed]
    entries: list[TOCEntry] = [None] * len(windowed)  # type: ignore[assignment]
    for i in reversed(range(len(windowed))):
        w = windowed[i]
        entry = TOCEntry(
            id=f"toc-{i + 1}",
            label=w["heading"].text,
            level=int(w["heading"].heading_level or 1),
            heading_chunk_id=w["heading"].chunk_id,
            first_chunk_id=w["first_chunk_id"],
            last_chunk_id=w["last_chunk_id"],
            first_order_index=w["first_oi"],
            last_order_index=w["last_oi"],
            page=w["heading"].page_start,
            children=children_of[i],
        )
        entries[i] = entry
        if parent_of[i] >= 0:
            children_of[parent_of[i]].insert(0, entry)

    top = [entries[i] for i in range(len(entries)) if parent_of[i] == -1]
    return TOC(entries=top)


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
    # Prefer the latest chunks.jsonl (after resolve_references runs) but
    # fall back to chunk's output for single-stage execution.
    candidates = [
        output_dir / "intermediate" / "resolve_references" / "chunks.jsonl",
        output_dir / "intermediate" / "chunk" / "chunks.jsonl",
    ]
    chunks_path = next((p for p in candidates if p.exists()), None)
    if chunks_path is None:
        raise FileNotFoundError(
            f"build_toc stage requires chunk output (run `regula stage chunk` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    chunks = _load_chunks(chunks_path)
    log.info("build_toc.start", chunks=len(chunks))

    toc = build_toc(chunks)
    (stage_dir / "toc.json").write_text(toc.model_dump_json(indent=2))

    finished = datetime.now(UTC)
    n_top = len(toc.entries)
    n_all = _count_entries(toc.entries)
    log.info("build_toc.done", top_entries=n_top, total_entries=n_all)
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={"top_level_entries": n_top, "total_entries": n_all},
    )


def _count_entries(entries: list[TOCEntry]) -> int:
    total = 0
    for e in entries:
        total += 1 + _count_entries(e.children)
    return total
