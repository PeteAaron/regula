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

# How much an element's text length is allowed to exceed the outline
# title's. A real heading is typically the same length as its title (or
# the title prefixed with a section number). When an outline title
# appears as a substring inside a much longer paragraph, that paragraph
# is *not* the heading. ``2x + 20 chars`` is generous enough to catch
# heading text like ``"Section 0: Approved Document B — Fire safety"``
# matched against a shorter outline title ``"Section 0"``.
_HEADING_LENGTH_RATIO = 2.0
_HEADING_LENGTH_SLACK = 20

# How much larger than body font an element must be to count as a
# heading on font alone. 0.3pt clears float jitter and PDFs that use a
# 10.5pt body + 11pt headings.
_HEADING_FONT_DELTA = 0.3


def _body_font_size(elements: list[dict[str, Any]]) -> float:
    """Char-weighted median of element font sizes. Body text dominates
    by character count, so this reliably picks the body size on
    well-typeset documents. Returns a sensible fallback when the
    parse tree has no font metadata."""
    weighted: list[float] = []
    for e in elements:
        size = round(float(e.get("font_size", 0.0)), 1)
        if size <= 0:
            continue
        weight = max(1, len(e.get("text", "")))
        weighted.extend([size] * weight)
    if not weighted:
        return 10.0
    weighted.sort()
    return weighted[len(weighted) // 2]


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
    paragraph_pattern: re.Pattern[str],
    body_font_size: float,
) -> dict[int, dict[str, Any]]:
    """Match outline entries against text elements with strict
    heuristics, designed against real PDFs (not just well-bookmarked
    synthetic ones).

    Rules — an element can claim an outline entry only when **all** of
    the following hold:

    1. **Same page** as the outline destination. Proximity-only matches
       across pages are rejected outright (the prior tier-1 fallback was
       the primary source of false positives — continuations on the
       next page would get claimed as headings).
    2. **Doesn't look like a paragraph.** If the element text matches
       the document's numbered-paragraph regex, it's a paragraph, not a
       heading — regardless of what the outline claims.
    3. **Length compatible with the title.** A real heading is roughly
       the same length as its outline title. Reject candidates more
       than ``2× title_len + 20`` chars long; that catches the
       substring-inside-a-paragraph case that ate ADB §0.5.
    4. **Text correspondence.** The element must equal the title, be a
       prefix of it, or have the title as a prefix. Plain "substring
       anywhere" is rejected because outline titles tend to be common
       phrases that appear naturally inside body text.
    5. **Visual distinction.** For non-equality matches, require the
       element to be bold *or* visibly larger than body font. (Equality
       matches are trusted on text alone since they're unambiguous.)

    When several elements satisfy these rules, the closest in y to the
    outline destination wins.
    """
    claimed: dict[int, dict[str, Any]] = {}
    used_idx: set[int] = set()
    for entry in outline:
        if entry["level"] not in heading_levels:
            continue
        page = entry["page"]
        dest_y = float(entry.get("dest_y", 0.0))
        title_norm = entry["title"].strip().lower()
        title_len = len(title_norm)
        max_len = max(int(_HEADING_LENGTH_RATIO * title_len) + _HEADING_LENGTH_SLACK, title_len)
        best: tuple[int, float, int] | None = None  # (score_tier, y_dist, idx)
        for idx, e in enumerate(elements):
            if idx in used_idx or e["page"] != page:
                continue
            # If the element text starts with a paragraph-style number
            # (``"1.3 "``), strip it before comparing — many publishers
            # render headings with a leading section number that the
            # outline title omits. The body-text-with-incidental-title-
            # substring failure mode is still caught downstream by the
            # text-correspondence and font-distinction checks.
            raw = e["text"].strip()
            para_match = paragraph_pattern.match(raw)
            stripped = raw[para_match.end():].strip() if para_match else raw
            text_norm = stripped.lower()
            if len(text_norm) > max_len:
                continue
            if text_norm == title_norm:
                tier = 0
            elif text_norm.startswith(title_norm) or title_norm.startswith(text_norm):
                tier = 1
            else:
                continue
            if tier > 0:
                visually_heading = bool(e.get("is_bold")) or (
                    float(e.get("font_size", 0.0)) > body_font_size + _HEADING_FONT_DELTA
                )
                if not visually_heading:
                    continue
            y_dist = abs(e["bbox"][1] - dest_y)
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
    """Walk parsed elements in reading order and produce Chunks.

    Kept as a thin wrapper around :func:`walk_with_stats` so tests that
    only care about the chunks can stay terse.
    """
    chunks, _ = walk_with_stats(tree, cfg)
    return chunks


def walk_with_stats(
    tree: dict[str, Any], cfg: Config
) -> tuple[list[Chunk], dict[str, int]]:
    """Same as :func:`walk` but also returns counters for deferred
    features the walker intentionally skipped (images, unclassified
    text). These flow into the stage report and from there into
    ``deferred.json``."""
    elements: list[dict[str, Any]] = tree.get("elements", [])
    outline: list[dict[str, Any]] = tree.get("outline", [])

    para_re = re.compile(cfg.chunking.paragraph_regex)
    body_font = _body_font_size(elements)
    outline_index = _build_outline_index(
        outline,
        elements,
        cfg.chunking.heading_levels,
        para_re,
        body_font,
    )

    allocator = _IdAllocator(cfg.doc_id)
    extracted_by = _parser_identifier(tree)
    log = get_logger(NAME)

    chunks: list[Chunk] = []
    stack: list[Chunk] = []
    stack_levels: list[int] = []
    order_index = 0
    last_chunk: Chunk | None = None
    deferred_counts: dict[str, int] = {}

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

        # Unclassified — log and skip. Counted so finalise can record
        # the gap in deferred.json.
        deferred_counts["unclassified_text"] = (
            deferred_counts.get("unclassified_text", 0) + 1
        )
        log.warning(
            "chunk.unclassified",
            page=elem["page"],
            preview=text[:80],
        )

    # Images detected by the parser but not yet emitted as chunks.
    n_images = len(tree.get("images", []))
    if n_images:
        deferred_counts["images_skipped"] = n_images

    return chunks, deferred_counts


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

    chunks, deferred_counts = walk_with_stats(tree, cfg)

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
        **{f"deferred_{k}": v for k, v in deferred_counts.items()},
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
            **deferred_counts,
        },
    )
