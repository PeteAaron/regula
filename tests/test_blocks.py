"""Tests for the post-wind-back pipeline: parse → extract_blocks → validate.

The default pipeline emits unclassified blocks per page with positional
and font metadata. These tests exercise that flow end-to-end against
the synthetic fixture, plus targeted unit tests for the advisory hint
classifiers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regula.cli import app
from regula.config import infer_config
from regula.schemas import Block, BlockLooksLike, BlockRegion
from regula.stages.extract_blocks import (
    _body_font_size,
    _classify_looks_like,
    _classify_region,
)

runner = CliRunner()


# --- unit tests for the hint classifiers ---------------------------------


def test_region_classifier_header() -> None:
    # bbox top within top 7% of an 800pt page
    assert _classify_region((100, 10, 400, 30), 595, 842) == BlockRegion.HEADER


def test_region_classifier_footer() -> None:
    # bbox top in bottom 7%
    assert _classify_region((100, 800, 400, 820), 595, 842) == BlockRegion.FOOTER


def test_region_classifier_body() -> None:
    assert _classify_region((100, 300, 400, 320), 595, 842) == BlockRegion.BODY


def test_region_classifier_margin() -> None:
    # x0 in the left 5%
    assert _classify_region((10, 300, 80, 320), 595, 842) == BlockRegion.MARGIN


def test_looks_like_large_text() -> None:
    # 14pt vs 10pt body → large_text
    assert _classify_looks_like(14, 10, False, False) == BlockLooksLike.LARGE_TEXT


def test_looks_like_small_text() -> None:
    assert _classify_looks_like(7, 10, False, False) == BlockLooksLike.SMALL_TEXT


def test_looks_like_emphasis() -> None:
    assert _classify_looks_like(10, 10, True, False) == BlockLooksLike.EMPHASIS


def test_looks_like_body() -> None:
    assert _classify_looks_like(10, 10, False, False) == BlockLooksLike.BODY


def test_body_font_size_picks_dominant_by_char_weight() -> None:
    """Body text dominates by character count; the char-weighted median
    should pick body size even on outline-heavy pages."""
    elements = [
        {"text": "Heading One", "font_size": 16},
        {"text": "Heading Two", "font_size": 16},
        {"text": "x" * 500, "font_size": 10},
    ]
    assert _body_font_size(elements) == 10.0


# --- end-to-end via the CLI ---------------------------------------------


def _run(tmp_path: Path, pdf: Path) -> Path:
    out = tmp_path / "run"
    result = runner.invoke(
        app, ["ingest", str(pdf), "--out-dir", str(out), "--no-fail"]
    )
    assert result.exit_code == 0, result.stdout
    return out


def test_ingest_writes_blocks_jsonl(synthetic_pdf: Path, tmp_path: Path) -> None:
    out = _run(tmp_path, synthetic_pdf)
    blocks_path = out / "blocks.jsonl"
    assert blocks_path.exists()
    lines = [
        line for line in blocks_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) > 0
    # Every line round-trips through Block.
    for line in lines:
        Block.model_validate_json(line)


def test_block_ids_are_deterministic_per_doc_id_page_index(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    out = _run(tmp_path, synthetic_pdf)
    cfg = infer_config(synthetic_pdf)
    blocks = [
        Block.model_validate_json(line)
        for line in (out / "blocks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for b in blocks:
        assert b.block_id == f"{cfg.doc_id}:p{b.page}:b{b.reading_order_index}"
    # reading_order_index is contiguous per page starting at 0.
    by_page: dict[int, list[int]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b.reading_order_index)
    for page, indices in by_page.items():
        assert sorted(indices) == list(range(len(indices))), (
            f"page {page} indices non-contiguous: {indices}"
        )


def test_blocks_cover_every_page_with_text(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    """All pages in the synthetic PDF have text — every page must
    appear in blocks.jsonl. Title pages / contents pages should NOT
    silently drop out as they did in the v0 pipeline."""
    out = _run(tmp_path, synthetic_pdf)
    blocks = [
        Block.model_validate_json(line)
        for line in (out / "blocks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    pages = json.loads((out / "pages.json").read_text())["pages"]
    pages_in_blocks = {b.page for b in blocks}
    assert pages_in_blocks == {p["page_number"] for p in pages}


def test_preview_renders_page_oriented(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    out = _run(tmp_path, synthetic_pdf)
    html = (out / "preview.html").read_text()
    # Page sections present per page.
    pages = json.loads((out / "pages.json").read_text())["pages"]
    for p in pages:
        assert f"id='page-{p['page_number']}'" in html, f"missing page {p['page_number']}"
    # Page navigator present.
    assert "class='page-nav'" in html
    # Every block has an article element with its block_id as the id.
    blocks = [
        Block.model_validate_json(line)
        for line in (out / "blocks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for b in blocks:
        assert f"id='{b.block_id}'" in html


def test_validation_does_not_fail_run_on_empty_metrics(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    """Validation is advisory-only after the wind-back. As long as
    schema conformance passes, the run completes successfully."""
    out = _run(tmp_path, synthetic_pdf)
    doc = json.loads((out / "document.json").read_text())
    assert doc["pipeline_passed"] is True
    assert doc["block_count"] > 0
    # The old chunk_count field still exists for backwards compatibility
    # but is always 0 in the new pipeline.
    assert doc["chunk_count"] == 0


# --- interactive preview features ----------------------------------------


def test_preview_embeds_pattern_state(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    """The preview's interactive cleanup needs pattern groups embedded
    as JSON. Verify the script tag is present and has the expected
    shape — block_summaries, text_groups, position_groups, etc."""
    import re

    out = _run(tmp_path, synthetic_pdf)
    html = (out / "preview.html").read_text()
    match = re.search(
        r'<script id="regula-state"[^>]*>(.*?)</script>', html, re.S
    )
    assert match is not None, "regula-state script tag missing"
    state = json.loads(match.group(1))
    assert "doc_id" in state
    assert "block_summaries" in state
    assert "text_groups" in state
    assert "position_groups" in state
    assert "block_to_groups" in state
    # Every block should have an entry in block_summaries and block_to_groups.
    blocks_count = sum(
        1
        for line in (out / "blocks.jsonl").read_text().splitlines()
        if line.strip()
    )
    assert len(state["block_summaries"]) == blocks_count
    assert len(state["block_to_groups"]) == blocks_count


def test_preview_includes_side_panel_and_controls(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    out = _run(tmp_path, synthetic_pdf)
    html = (out / "preview.html").read_text()
    # Side panel scaffold.
    assert 'id="side-panel"' in html
    assert 'data-tab="text"' in html
    assert 'data-tab="position"' in html
    # Top-bar controls.
    assert 'id="btn-export"' in html
    assert 'id="btn-reset"' in html
    assert 'id="count-suppressed"' in html
    assert 'id="count-tagged"' in html


def test_preview_blocks_carry_data_block_id(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    """The JS uses data-block-id for event delegation. Every rendered
    block must carry the attribute."""
    out = _run(tmp_path, synthetic_pdf)
    html = (out / "preview.html").read_text()
    from regula.schemas import Block

    blocks = [
        Block.model_validate_json(line)
        for line in (out / "blocks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for b in blocks:
        assert f"data-block-id='{b.block_id}'" in html


def test_pattern_groups_detect_repeated_text() -> None:
    """Direct test of the grouping helper. Repeated text across pages
    should produce a multi-member group; unique text should not."""
    from regula.inspect import _compute_groups
    from regula.schemas import Block, BlockLooksLike, BlockRegion

    def b(page: int, idx: int, text: str, y: float = 800.0) -> Block:
        return Block(
            block_id=f"doc:p{page}:b{idx}",
            doc_id="doc",
            page=page,
            reading_order_index=idx,
            bbox=(72.0, y, 200.0, y + 12),
            text=text,
            font_size=9.0,
            font_name="Helvetica",
            is_bold=False,
            is_italic=False,
            region=BlockRegion.FOOTER,
            looks_like=BlockLooksLike.SMALL_TEXT,
        )

    blocks = [
        b(1, 0, "Online version"),
        b(2, 0, "Online version"),
        b(3, 0, "Online version"),
        b(1, 1, "Some unique heading"),
        b(2, 1, "Some other content"),
    ]
    text_groups, position_groups = _compute_groups(blocks)
    # "Online version" group has 3 members.
    online_group = [v for v in text_groups.values() if len(v) == 3]
    assert len(online_group) == 1
    assert set(online_group[0]) == {"doc:p1:b0", "doc:p2:b0", "doc:p3:b0"}
    # Unique text shouldn't appear (group size < 2).
    assert not any(
        "Some unique heading" in k or "Some other content" in k
        for k in text_groups
    )
    # Position group should also catch all 5 footer-band blocks because
    # they share y-bucket + font signature.
    pos_groups_5 = [v for v in position_groups.values() if len(v) == 5]
    assert len(pos_groups_5) == 1


def test_pattern_groups_skip_single_member() -> None:
    """Groups of one are noise — they don't represent a pattern."""
    from regula.inspect import _compute_groups
    from regula.schemas import Block, BlockLooksLike, BlockRegion

    only = Block(
        block_id="doc:p1:b0",
        doc_id="doc",
        page=1,
        reading_order_index=0,
        bbox=(72.0, 100.0, 200.0, 120.0),
        text="just one",
        font_size=11.0,
        font_name="Helvetica",
        is_bold=False,
        is_italic=False,
        region=BlockRegion.BODY,
        looks_like=BlockLooksLike.BODY,
    )
    text_groups, position_groups = _compute_groups([only])
    assert text_groups == {}
    assert position_groups == {}
