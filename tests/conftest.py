"""Shared fixtures and factories for tests.

The factories build minimally-valid instances of contract models so that
individual tests can override only the fields under test, rather than
restating every required field.

``synthetic_pdf`` (session-scoped) generates a small, deterministic PDF
with a heading hierarchy, numbered paragraphs, and cross-references so
the chunk + build_toc + resolve_references stages have real input to
chew on without committing a binary fixture beyond the placeholder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf
import pytest

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


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small deterministic PDF used by stage-level tests.

    Structure:
      Page 1: heading 'Section 1: Introduction' (outline level 1)
              1.1 paragraph that references paragraph 1.2 and BS EN 13501-1
              1.2 paragraph
      Page 2: heading 'Section heading for testing' (outline level 2)
              1.3a paragraph
    """
    path = tmp_path_factory.mktemp("pdf") / "synth.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Section 1: Introduction", fontsize=18)
    page.insert_text((72, 110), "1.1 This document describes the safety provisions for buildings.", fontsize=11)
    page.insert_text((72, 140), "See paragraph 1.2 and BS EN 13501-1 for further detail.", fontsize=11)
    page.insert_text((72, 180), "1.2 Application of these provisions depends on building type.", fontsize=11)
    page = doc.new_page()
    page.insert_text((72, 60), "1.3 Section heading for testing", fontsize=14)
    page.insert_text((72, 110), "1.3a This paragraph contains specific guidance.", fontsize=11)
    doc.set_toc(
        [
            [1, "Section 1: Introduction", 1],
            [2, "Section heading for testing", 2],
        ]
    )
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def synthetic_config_text(synthetic_pdf: Path) -> str:
    """A YAML config string pointing at the synthetic PDF."""
    return f"""
doc_id: SYNTH
title: "Synthetic test"
edition: "v0"
jurisdiction: Test
legal_status: Fixture
source_pdf: {synthetic_pdf}

parsers:
  primary: pymupdf

chunking:
  paragraph_regex: '^(\\d+\\.\\d+[a-z]?)\\s+'
  heading_levels: [1, 2]

references:
  patterns:
    - {{ name: internal_paragraph, regex: 'paragraph(?:s)?\\s+(\\d+\\.\\d+[a-z]?)', type: internal }}
    - {{ name: bs_standard, regex: 'BS\\s?(?:EN\\s)?\\d+(?:-\\d+)?', type: external_standard }}

validation:
  min_internal_ref_resolution: 0.5
  min_page_coverage: 0.5
"""
