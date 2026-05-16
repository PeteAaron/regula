"""Unit tests for ``regula.stages.chunk`` — the walker that turns a
parse tree into chunks.

These tests target specific failure modes observed when running on
real-world PDFs (Approved Document B in particular) where the
permissive outline matching used to claim body paragraphs and
page-break continuations as section headings.

Each test constructs a minimal parse-tree fixture directly and runs
``walk_with_stats``, sidestepping the parse stage entirely. That keeps
the failure modes legible and the tests fast.
"""

from __future__ import annotations

from typing import Any

import yaml

from regula.config import Config
from regula.schemas import ChunkType
from regula.stages.chunk import _body_font_size, walk_with_stats


def _cfg(**overrides: Any) -> Config:
    base = {
        "doc_id": "TEST",
        "title": "T",
        "edition": "v0",
        "jurisdiction": "x",
        "legal_status": "x",
        "source_pdf": "/tmp/dummy.pdf",
        "parsers": {"primary": "pymupdf"},
        "chunking": {
            "paragraph_regex": r"^(\d+\.\d+[a-z]?)\s+",
            "heading_levels": [1, 2, 3, 4],
            "merge_continuations": True,
        },
        "references": {"patterns": []},
        "validation": {
            "min_internal_ref_resolution": 0.0,
            "min_page_coverage": 0.0,
            "min_text_reconstruction_coverage": 0.0,
            "min_reading_order_monotonicity": 0.0,
            "fail_on_schema_error": False,
        },
    }
    base.update(overrides)
    return Config.model_validate(yaml.safe_load(yaml.safe_dump(base)))


def _element(
    page: int,
    bbox: tuple[float, float, float, float],
    text: str,
    *,
    font_size: float = 10.0,
    is_bold: bool = False,
) -> dict[str, Any]:
    return {
        "page": page,
        "block_index": 0,
        "bbox": list(bbox),
        "text": text,
        "font_size": font_size,
        "font_name": "Helvetica",
        "is_bold": is_bold,
        "is_italic": False,
    }


def _tree(
    elements: list[dict[str, Any]], outline: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "parser": "pymupdf",
        "parser_version": "PyMuPDF 1.0",
        "pages": [{"page_number": 1, "width": 595, "height": 842, "rotation": 0}],
        "elements": elements,
        "outline": outline,
        "links": [],
        "images": [],
    }


def test_paragraph_not_claimed_as_heading_even_if_outline_title_appears_in_text() -> None:
    """The walker used to substring-match outline titles against
    elements, which would steal a paragraph whose body happened to
    contain the title text. After the fix an element matching the
    paragraph regex can never be classified as a heading."""
    elements = [
        _element(1, (72, 50, 400, 64), "Real Heading", font_size=14, is_bold=True),
        _element(
            1,
            (72, 80, 500, 95),
            "0.5 The provisions Management of premises must be reviewed.",
        ),
    ]
    outline = [
        {"level": 1, "title": "Real Heading", "page": 1, "dest_y": 50.0},
        {"level": 3, "title": "Management of premises", "page": 1, "dest_y": 80.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert [c.type for c in chunks] == [ChunkType.SECTION_HEADING, ChunkType.PARAGRAPH]
    assert chunks[1].chunk_id.endswith("paragraph-0.5")


def test_proximity_only_match_is_rejected() -> None:
    """An outline entry whose destination y is near an element but
    whose title doesn't match the element text must not claim it.
    This is what used to convert page-break continuations into
    fake headings."""
    elements = [
        _element(
            1,
            (72, 50, 500, 65),
            "including blocks of flats, while Volume 2 covers all other types",
        ),
    ]
    outline = [
        {"level": 3, "title": "Summary", "page": 1, "dest_y": 55.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert chunks == []  # continuation text on its own with no preceding paragraph


def test_long_body_text_not_claimed_as_heading_via_substring() -> None:
    """Outline title appearing as a substring inside a much longer
    paragraph-style element must be rejected on length grounds."""
    elements = [
        _element(
            1,
            (72, 50, 500, 95),
            "Each approved document covers the requirements of the Building "
            "Regulations 2010 relating to one particular aspect of building.",
            font_size=10,
        ),
    ]
    outline = [
        # The title is a phrase that legitimately appears inside the
        # element's body text.
        {"level": 3, "title": "covers the requirements", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert chunks == []


def test_body_sized_non_bold_text_rejected_for_prefix_match() -> None:
    """For non-equality text matches we require visual distinction
    (bold or larger font). Body-sized non-bold elements are rejected
    even if their text starts with the outline title."""
    elements = [
        _element(
            1,
            (72, 50, 500, 65),
            "Summary of the introductory notes below",
            font_size=10,
            is_bold=False,
        ),
    ]
    outline = [
        {"level": 3, "title": "Summary", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert chunks == []


def test_bold_heading_with_prefix_match_is_claimed() -> None:
    """When the element is visually distinct (bold) and its text is a
    plausible heading length, the outline claim should succeed even
    without exact equality."""
    elements = [
        _element(
            1,
            (72, 50, 300, 65),
            "Summary of provisions",
            font_size=10,
            is_bold=True,
        ),
        _element(1, (72, 80, 500, 95), "0.1 First paragraph follows."),
    ]
    outline = [
        {"level": 2, "title": "Summary", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert chunks[0].type is ChunkType.SECTION_HEADING
    assert chunks[0].heading_level == 2
    assert chunks[1].type is ChunkType.PARAGRAPH


def test_equality_match_trusted_without_font_check() -> None:
    """Exact text match between element and outline title is unambiguous
    and should claim the element regardless of font (some PDFs typeset
    headings at body size)."""
    elements = [
        _element(1, (72, 50, 200, 65), "Introduction", font_size=10, is_bold=False),
        _element(1, (72, 80, 500, 95), "0.1 First paragraph."),
    ]
    outline = [
        {"level": 1, "title": "Introduction", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert chunks[0].type is ChunkType.SECTION_HEADING
    assert chunks[1].type is ChunkType.PARAGRAPH


def test_continuation_across_page_break_merges_when_no_bogus_heading_intervenes() -> None:
    """Paragraph 0.1 ends on page 1; page 2 starts mid-sentence with no
    paragraph number. With the bogus-heading bug fixed the continuation
    merges into 0.1 instead of becoming a fake heading."""
    elements = [
        _element(1, (72, 50, 500, 65), "0.1 First sentence of paragraph 0.1."),
        _element(
            2, (72, 50, 500, 65), "continues the same paragraph on page two."
        ),
        _element(2, (72, 80, 500, 95), "0.2 Second paragraph starts here."),
    ]
    outline = [
        # Outline destination near the top of page 2 — used to grab the
        # continuation as a heading via the tier-1 fallback.
        {"level": 3, "title": "Summary", "page": 2, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert [c.type for c in chunks] == [ChunkType.PARAGRAPH, ChunkType.PARAGRAPH]
    assert "continues the same paragraph" in chunks[0].text
    assert chunks[0].page_end == 2
    assert chunks[1].chunk_id.endswith("paragraph-0.2")


def test_body_font_size_picks_dominant_size_by_char_weight() -> None:
    """Heading detection relies on knowing the body font size. Char-
    weighted median should resist outline-heavy documents where many
    short heading-sized elements exist."""
    elements = [
        _element(1, (0, 0, 100, 20), "Big Heading One", font_size=16),
        _element(1, (0, 30, 100, 50), "Big Heading Two", font_size=16),
        _element(
            1,
            (0, 60, 600, 80),
            "x" * 500,
            font_size=10,
        ),
    ]
    assert _body_font_size(elements) == 10.0


def test_numbered_heading_matches_outline_after_stripping_prefix() -> None:
    """Publishers often render headings with a leading section number
    (``"1.3 Sub-section title"``) that the outline title omits
    (``"Sub-section title"``). Stripping the paragraph-style prefix
    before comparing lets these real numbered headings match without
    weakening the protections against body-text substring matches."""
    elements = [
        _element(
            1,
            (72, 50, 400, 65),
            "1.3 Sub-section title",
            font_size=14,
            is_bold=True,
        ),
    ]
    outline = [
        {"level": 2, "title": "Sub-section title", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(_tree(elements, outline), _cfg())
    assert len(chunks) == 1
    assert chunks[0].type is ChunkType.SECTION_HEADING
    # The heading text retains the leading number — the slug-stripping
    # only affects outline matching, not the emitted chunk text.
    assert chunks[0].text == "1.3 Sub-section title"


def test_heading_levels_filter_respected() -> None:
    """Outline entries at levels not listed in ``heading_levels`` must
    be ignored, even if their text matches an element exactly."""
    elements = [
        _element(1, (72, 50, 200, 65), "Sub-sub heading", font_size=10, is_bold=True),
        _element(1, (72, 80, 500, 95), "0.1 Body paragraph."),
    ]
    outline = [
        {"level": 4, "title": "Sub-sub heading", "page": 1, "dest_y": 50.0},
    ]
    chunks, _ = walk_with_stats(
        _tree(elements, outline),
        _cfg(
            chunking={
                "paragraph_regex": r"^(\d+\.\d+[a-z]?)\s+",
                "heading_levels": [1, 2],
                "merge_continuations": True,
            }
        ),
    )
    # The heading element wasn't claimed (level filtered), so it becomes
    # a continuation absorbed nowhere → unclassified. The paragraph still
    # exists.
    para_chunks = [c for c in chunks if c.type is ChunkType.PARAGRAPH]
    assert len(para_chunks) == 1
    assert all(c.type is not ChunkType.SECTION_HEADING for c in chunks)
