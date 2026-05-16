"""Shared fixtures and factories for tests.

The factories build minimally-valid instances of contract models so that
individual tests can override only the fields under test, rather than
restating every required field.
"""

from __future__ import annotations

from typing import Any

from regula.schemas import (
    Chunk,
    ChunkMeta,
    ChunkType,
    SourceSpan,
    TOC,
    TOCEntry,
)


def make_source_span(**overrides: Any) -> SourceSpan:
    defaults: dict[str, Any] = dict(
        page=1,
        bbox=(0.0, 0.0, 100.0, 20.0),
        text_offset_start=0,
        text_offset_end=12,
    )
    defaults.update(overrides)
    return SourceSpan(**defaults)


def make_chunk(**overrides: Any) -> Chunk:
    """Build a minimal valid paragraph chunk under doc_id=ADB1-2022."""
    text = overrides.pop("text", "Hello world.")
    span = make_source_span(text_offset_end=len(text))
    defaults: dict[str, Any] = dict(
        chunk_id="ADB1-2022-paragraph-1.1",
        doc_id="ADB1-2022",
        type=ChunkType.PARAGRAPH,
        order_index=0,
        page_start=1,
        page_end=1,
        section_path=["Section A"],
        section_path_ids=["ADB1-2022-section_heading-A"],
        parent_section_id="ADB1-2022-section_heading-A",
        breadcrumb="Section A",
        text=text,
        meta=ChunkMeta(source_spans=[span], extracted_by="docling@2.x"),
    )
    defaults.update(overrides)
    return Chunk(**defaults)


def make_toc_entry(**overrides: Any) -> TOCEntry:
    defaults: dict[str, Any] = dict(
        id="toc-A",
        label="Section A",
        level=1,
        heading_chunk_id="ADB1-2022-section_heading-A",
        first_chunk_id="ADB1-2022-section_heading-A",
        last_chunk_id="ADB1-2022-paragraph-1.1",
        first_order_index=0,
        last_order_index=1,
        page=1,
    )
    defaults.update(overrides)
    return TOCEntry(**defaults)


def make_toc(*entries: TOCEntry) -> TOC:
    return TOC(entries=list(entries))
