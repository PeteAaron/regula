"""Stage 2 — `chunk`. Walk the parsed tree and emit chunk records.

The walker classifies each text element from the parse stage as a section
heading (when the PDF outline points at it), a numbered paragraph (when
the configured ``paragraph_regex`` matches), or a continuation of the
preceding paragraph (when ``merge_continuations`` is enabled).

Unclassified text is currently dropped with a warning — Phase 4's
focus is the regulatory-document common case (numbered paragraphs under
a heading hierarchy). Tables and captions slot in later behind the same
walker; the parser already exposes the raw image list.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import Chunk, ChunkMeta, ChunkType, SourceSpan, StageReport

NAME = "chunk"

# How close an element's top y must be to the outline's destination y for
# the outline entry to claim that element as the heading. PDF outlines
# typically point at the top of the page that contains the heading, not
# the heading visual itself; in our synthetic fixtures the gap is ~10pt.
_OUTLINE_MATCH_TOLERANCE_Y = 60.0


def _slug(s: str) -> str:
    out = re.sub(r"\s+", "-", s.strip().lower())
    out = re.sub(r"[^A-Za-z0-9._-]", "", out)
    return out or "anon"


class _IdAllocator:
    """Issue unique ``<doc_id>-<type>-<identifier>`` chunk_ids."""

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self._used: set[str] = set()

    def allocate(self, ctype: ChunkType, identifier: str) -> str:
        base = f"{self.doc_id}-{ctype.value}-{_slug(identifier)}"
        if base not in self._used:
            self._used.add(base)
            return base
        i = 2
        while f"{base}-{i}" in self._used:
            i += 1
        full = f"{base}-{i}"
        self._used.add(full)
        return full


def _build_outline_index(
    outline: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    heading_levels: list[int],
) -> dict[int, dict[str, Any]]:
    """Match outline entries against text elements.

    For each outline entry, prefer a same-page element whose text contains
    (or is contained by) the outline title; fall back to the closest
    same-page element within ``_OUTLINE_MATCH_TOLERANCE_Y`` of the
    outline's destination y. Returns a map from element index to outline
    entry.
    """
    claimed: dict[int, dict[str, Any]] = {}
    used_idx: set[int] = set()
    for entry in outline:
        if entry["level"] not in heading_levels:
            continue
        page = entry["page"]
        dest_y = float(entry.get("dest_y", 0.0))
        title_norm = entry["title"].strip().lower()
        best: tuple[int, float, int] | None = None  # (score_tier, y_dist, idx)
        for idx, e in enumerate(elements):
            if idx in used_idx or e["page"] != page:
                continue
            text_norm = e["text"].strip().lower()
            y_dist = abs(e["bbox"][1] - dest_y)
            if title_norm in text_norm or text_norm in title_norm:
                tier = 0  # text match — preferred
            elif y_dist <= _OUTLINE_MATCH_TOLERANCE_Y:
                tier = 1
            else:
                continue
            candidate = (tier, y_dist, idx)
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            claimed[best[2]] = entry
            used_idx.add(best[2])
    return claimed


def _source_span(elem: dict[str, Any], start: int, end: int) -> SourceSpan:
    return SourceSpan(
        page=elem["page"],
        bbox=tuple(float(c) for c in elem["bbox"]),
        text_offset_start=start,
        text_offset_end=end,
    )


def _parser_identifier(tree: dict[str, Any]) -> str:
    version = tree.get("parser_version", "unknown")
    # PyMuPDF version string is long; keep just the leading version number.
    version_short = version.split()[1] if " " in version else version
    return f"{tree.get('parser', 'unknown')}@{version_short}"


def walk(tree: dict[str, Any], cfg: Config) -> list[Chunk]:
    """Walk parsed elements in reading order and produce Chunks."""
    elements: list[dict[str, Any]] = tree.get("elements", [])
    outline: list[dict[str, Any]] = tree.get("outline", [])

    outline_index = _build_outline_index(
        outline, elements, cfg.chunking.heading_levels
    )
    para_re = re.compile(cfg.chunking.paragraph_regex)

    allocator = _IdAllocator(cfg.doc_id)
    extracted_by = _parser_identifier(tree)
    log = get_logger(NAME)

    chunks: list[Chunk] = []
    stack: list[Chunk] = []
    stack_levels: list[int] = []
    order_index = 0
    last_chunk: Chunk | None = None

    glossary_section_norm = (
        cfg.references.glossary_section.strip().lower()
        if cfg.references.glossary_section
        else None
    )

    def section_paths() -> tuple[list[str], list[str]]:
        return [c.text for c in stack], [c.chunk_id for c in stack]

    def in_glossary_section() -> bool:
        if glossary_section_norm is None:
            return False
        return any(glossary_section_norm in h.text.strip().lower() for h in stack)

    for idx, elem in enumerate(elements):
        text = elem["text"]

        # Heading
        if idx in outline_index:
            entry = outline_index[idx]
            level = int(entry["level"])
            while stack_levels and stack_levels[-1] >= level:
                stack.pop()
                stack_levels.pop()
            path, path_ids = section_paths()
            parent_id = path_ids[-1] if path_ids else None
            chunk_id = allocator.allocate(
                ChunkType.SECTION_HEADING, f"l{level}-{_slug(text)[:60]}"
            )
            heading = Chunk(
                chunk_id=chunk_id,
                doc_id=cfg.doc_id,
                type=ChunkType.SECTION_HEADING,
                order_index=order_index,
                page_start=elem["page"],
                page_end=elem["page"],
                section_path=path,
                section_path_ids=path_ids,
                parent_section_id=parent_id,
                breadcrumb=" > ".join(path),
                heading_level=level,
                text=text,
                meta=ChunkMeta(
                    source_spans=[_source_span(elem, 0, len(text))],
                    extracted_by=extracted_by,
                ),
            )
            chunks.append(heading)
            stack.append(heading)
            stack_levels.append(level)
            order_index += 1
            last_chunk = heading
            continue

        # Paragraph
        m = para_re.match(text)
        if m:
            identifier = m.group(1) if m.groups() else f"p{order_index}"
            path, path_ids = section_paths()
            parent_id = path_ids[-1] if path_ids else None
            chunk_id = allocator.allocate(ChunkType.PARAGRAPH, identifier)
            para = Chunk(
                chunk_id=chunk_id,
                doc_id=cfg.doc_id,
                type=ChunkType.PARAGRAPH,
                order_index=order_index,
                page_start=elem["page"],
                page_end=elem["page"],
                section_path=path,
                section_path_ids=path_ids,
                parent_section_id=parent_id,
                breadcrumb=" > ".join(path),
                text=text,
                meta=ChunkMeta(
                    source_spans=[_source_span(elem, 0, len(text))],
                    extracted_by=extracted_by,
                ),
            )
            chunks.append(para)
            order_index += 1
            last_chunk = para
            continue

        # Continuation
        if (
            cfg.chunking.merge_continuations
            and last_chunk is not None
            and last_chunk.type is ChunkType.PARAGRAPH
        ):
            join = " "
            new_start = len(last_chunk.text) + len(join)
            new_text = last_chunk.text + join + text
            last_chunk.text = new_text
            last_chunk.meta.source_spans.append(
                _source_span(elem, new_start, len(new_text))
            )
            if elem["page"] > last_chunk.page_end:
                last_chunk.page_end = elem["page"]
            continue

        # Glossary entry — when we're inside the configured glossary
        # section and the text doesn't match a heading or paragraph
        # pattern, treat it as one glossary entry per text block.
        if in_glossary_section():
            path, path_ids = section_paths()
            parent_id = path_ids[-1] if path_ids else None
            chunk_id = allocator.allocate(
                ChunkType.GLOSSARY_ENTRY, _slug(text)[:60] or f"g{order_index}"
            )
            entry = Chunk(
                chunk_id=chunk_id,
                doc_id=cfg.doc_id,
                type=ChunkType.GLOSSARY_ENTRY,
                order_index=order_index,
                page_start=elem["page"],
                page_end=elem["page"],
                section_path=path,
                section_path_ids=path_ids,
                parent_section_id=parent_id,
                breadcrumb=" > ".join(path),
                text=text,
                meta=ChunkMeta(
                    source_spans=[_source_span(elem, 0, len(text))],
                    extracted_by=extracted_by,
                ),
            )
            chunks.append(entry)
            order_index += 1
            last_chunk = entry
            continue

        # Unclassified — log and skip.
        log.warning(
            "chunk.unclassified",
            page=elem["page"],
            preview=text[:80],
        )

    return chunks


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    parse_dir = output_dir / "intermediate" / "parse"
    if not parse_dir.exists():
        raise FileNotFoundError(
            f"chunk stage requires parse output at {parse_dir} "
            f"(run `regula stage parse` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    tree = json.loads((parse_dir / "tree.json").read_text(encoding="utf-8"))
    log.info("chunk.start", elements=len(tree.get("elements", [])))

    chunks = walk(tree, cfg)

    with (stage_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(c.model_dump_json() + "\n")

    n_heading = sum(1 for c in chunks if c.type is ChunkType.SECTION_HEADING)
    n_para = sum(1 for c in chunks if c.type is ChunkType.PARAGRAPH)
    n_continuations = sum(len(c.meta.source_spans) - 1 for c in chunks) if chunks else 0

    finished = datetime.now(UTC)
    log.info(
        "chunk.done",
        chunks=len(chunks),
        headings=n_heading,
        paragraphs=n_para,
        continuations_merged=n_continuations,
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={
            "chunks_emitted": len(chunks),
            "section_headings": n_heading,
            "paragraphs": n_para,
            "continuations_merged": n_continuations,
        },
    )
