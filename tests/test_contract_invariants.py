"""Cross-model invariants — the helpers that Stage 6 ``validate`` will also call.

Phase 1 exercises them against in-memory examples (no PDFs); later phases
exercise them against real output.
"""

from __future__ import annotations

import pytest

from regula.schemas import (
    ChunkMeta,
    ChunkType,
    InvariantError,
    SourceSpan,
    assert_asset_linkage_bidirectional,
    assert_reading_order_valid,
    assert_section_windows_consistent,
    assert_source_spans_in_bounds,
)
from tests.conftest import make_chunk, make_source_span, make_toc, make_toc_entry


# --- reading order --------------------------------------------------------


def test_reading_order_valid_when_consecutive() -> None:
    chunks = [
        make_chunk(chunk_id="ADB1-2022-paragraph-1.1", order_index=0),
        make_chunk(chunk_id="ADB1-2022-paragraph-1.2", order_index=1),
        make_chunk(chunk_id="ADB1-2022-paragraph-1.3", order_index=2),
    ]
    assert_reading_order_valid(chunks)


def test_reading_order_rejects_duplicates() -> None:
    chunks = [
        make_chunk(chunk_id="ADB1-2022-paragraph-1.1", order_index=0),
        make_chunk(chunk_id="ADB1-2022-paragraph-1.2", order_index=0),
    ]
    with pytest.raises(InvariantError, match="duplicate order_index"):
        assert_reading_order_valid(chunks)


def test_reading_order_rejects_gaps() -> None:
    chunks = [
        make_chunk(chunk_id="ADB1-2022-paragraph-1.1", order_index=0),
        make_chunk(chunk_id="ADB1-2022-paragraph-1.2", order_index=2),
    ]
    with pytest.raises(InvariantError, match="cover 0"):
        assert_reading_order_valid(chunks)


# --- section windows ------------------------------------------------------


def test_section_windows_consistent() -> None:
    heading = make_chunk(
        chunk_id="ADB1-2022-section_heading-A",
        type=ChunkType.SECTION_HEADING,
        heading_level=1,
        parent_section_id=None,
        order_index=0,
    )
    para = make_chunk(
        chunk_id="ADB1-2022-paragraph-1.1",
        order_index=1,
        parent_section_id="ADB1-2022-section_heading-A",
    )
    toc = make_toc(
        make_toc_entry(
            heading_chunk_id="ADB1-2022-section_heading-A",
            first_chunk_id="ADB1-2022-section_heading-A",
            last_chunk_id="ADB1-2022-paragraph-1.1",
            first_order_index=0,
            last_order_index=1,
        )
    )
    assert_section_windows_consistent(toc, [heading, para])


def test_section_window_violation_raises() -> None:
    heading = make_chunk(
        chunk_id="ADB1-2022-section_heading-A",
        type=ChunkType.SECTION_HEADING,
        heading_level=1,
        parent_section_id=None,
        order_index=0,
    )
    para = make_chunk(
        chunk_id="ADB1-2022-paragraph-1.1",
        order_index=5,  # outside the section window
        parent_section_id="ADB1-2022-section_heading-A",
    )
    toc = make_toc(
        make_toc_entry(
            heading_chunk_id="ADB1-2022-section_heading-A",
            first_chunk_id="ADB1-2022-section_heading-A",
            last_chunk_id="ADB1-2022-section_heading-A",
            first_order_index=0,
            last_order_index=0,
        )
    )
    with pytest.raises(InvariantError, match="outside its parent section"):
        assert_section_windows_consistent(toc, [heading, para])


def test_section_windows_unknown_parent_raises() -> None:
    para = make_chunk(
        chunk_id="ADB1-2022-paragraph-1.1",
        order_index=0,
        parent_section_id="ADB1-2022-section_heading-MISSING",
    )
    toc = make_toc()
    with pytest.raises(InvariantError, match="not present in TOC"):
        assert_section_windows_consistent(toc, [para])


# --- asset linkage --------------------------------------------------------


def test_asset_linkage_bidirectional() -> None:
    diagram = make_chunk(
        chunk_id="ADB1-2022-diagram-3.1",
        type=ChunkType.DIAGRAM,
        asset_path="assets/diagram-3.1.png",
        captioned_by_id="ADB1-2022-caption-3.1",
        order_index=0,
    )
    caption = make_chunk(
        chunk_id="ADB1-2022-caption-3.1",
        type=ChunkType.CAPTION,
        caption_target_id="ADB1-2022-diagram-3.1",
        order_index=1,
    )
    assert_asset_linkage_bidirectional([diagram, caption])


def test_asset_linkage_one_way_raises() -> None:
    diagram = make_chunk(
        chunk_id="ADB1-2022-diagram-3.1",
        type=ChunkType.DIAGRAM,
        asset_path="assets/diagram-3.1.png",
        captioned_by_id=None,  # missing back-pointer
        order_index=0,
    )
    caption = make_chunk(
        chunk_id="ADB1-2022-caption-3.1",
        type=ChunkType.CAPTION,
        caption_target_id="ADB1-2022-diagram-3.1",
        order_index=1,
    )
    with pytest.raises(InvariantError, match="not bidirectional"):
        assert_asset_linkage_bidirectional([diagram, caption])


def test_caption_targets_non_asset_raises() -> None:
    para = make_chunk(chunk_id="ADB1-2022-paragraph-1.1", order_index=0)
    caption = make_chunk(
        chunk_id="ADB1-2022-caption-1.1",
        type=ChunkType.CAPTION,
        caption_target_id="ADB1-2022-paragraph-1.1",
        order_index=1,
    )
    with pytest.raises(InvariantError, match="not table/diagram"):
        assert_asset_linkage_bidirectional([para, caption])


# --- source spans in bounds ----------------------------------------------


def test_source_spans_in_bounds() -> None:
    text = "Hello world."
    span = make_source_span(text_offset_end=len(text))
    chunk = make_chunk(text=text, meta=ChunkMeta(source_spans=[span], extracted_by="x"))
    assert_source_spans_in_bounds(chunk)


def test_source_span_out_of_bounds_raises() -> None:
    span = SourceSpan(
        page=1,
        bbox=(0.0, 0.0, 10.0, 10.0),
        text_offset_start=0,
        text_offset_end=999,
    )
    chunk = make_chunk(
        text="short", meta=ChunkMeta(source_spans=[span], extracted_by="x")
    )
    with pytest.raises(InvariantError, match="text_offset_end"):
        assert_source_spans_in_bounds(chunk)
