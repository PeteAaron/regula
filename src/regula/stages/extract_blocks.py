"""Stage — ``extract_blocks``.

Converts the parser's flat element list into :class:`regula.schemas.Block`
records. One element → one block, no merging, no classification, no
filtering. Advisory ``region`` and ``looks_like`` hints are computed
from the element's bbox and font signature, but they're purely
informational — nothing downstream filters on them.

This stage is intentionally minimal. Higher-level structure
(paragraphs, headings, references, glossary) is deferred until the
user has inspected the blocks and identified what conventions actually
hold for the document at hand.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import Block, BlockLooksLike, BlockRegion, StageReport

NAME = "extract_blocks"

# Page-relative bands for region classification. Headers/footers tend
# to live in the top/bottom 7% of the page; left/right margins in the
# outer 5%.
_REGION_HEADER_FRACTION = 0.07
_REGION_FOOTER_FRACTION = 0.07
_REGION_MARGIN_FRACTION = 0.05

# How much a block's font size must differ from the body median to be
# flagged as large_text or small_text.
_LARGE_FONT_DELTA = 1.5
_SMALL_FONT_DELTA = 1.0


def _body_font_size(elements: list[dict[str, Any]]) -> float:
    """Char-weighted median of element font sizes. Body text dominates
    by character count, so the weighted median picks the body size
    reliably even on documents with many short heading-sized blocks."""
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


def _classify_region(
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> BlockRegion:
    x0, y0, _, y1 = bbox
    if y1 < page_height * _REGION_HEADER_FRACTION:
        return BlockRegion.HEADER
    if y0 > page_height * (1 - _REGION_FOOTER_FRACTION):
        return BlockRegion.FOOTER
    if x0 < page_width * _REGION_MARGIN_FRACTION:
        return BlockRegion.MARGIN
    return BlockRegion.BODY


def _classify_looks_like(
    font_size: float, body_size: float, is_bold: bool, is_italic: bool
) -> BlockLooksLike:
    if font_size >= body_size + _LARGE_FONT_DELTA:
        return BlockLooksLike.LARGE_TEXT
    if font_size <= body_size - _SMALL_FONT_DELTA and font_size > 0:
        return BlockLooksLike.SMALL_TEXT
    if is_bold or is_italic:
        return BlockLooksLike.EMPHASIS
    if font_size > 0:
        return BlockLooksLike.BODY
    return BlockLooksLike.UNKNOWN


def extract(tree: dict[str, Any], cfg: Config) -> list[Block]:
    """Pure function: parser tree → list of Blocks."""
    elements: list[dict[str, Any]] = tree.get("elements", [])
    pages: list[dict[str, Any]] = tree.get("pages", [])
    page_dims = {p["page_number"]: (float(p["width"]), float(p["height"])) for p in pages}
    body_size = _body_font_size(elements)

    blocks: list[Block] = []
    per_page_index: dict[int, int] = {}
    for elem in elements:
        page = int(elem["page"])
        idx = per_page_index.get(page, 0)
        per_page_index[page] = idx + 1
        bbox = tuple(float(c) for c in elem["bbox"])
        page_w, page_h = page_dims.get(page, (595.0, 842.0))
        font_size = round(float(elem.get("font_size", 0.0)), 2)
        blocks.append(
            Block(
                block_id=f"{cfg.doc_id}:p{page}:b{idx}",
                doc_id=cfg.doc_id,
                page=page,
                reading_order_index=idx,
                bbox=bbox,
                text=elem["text"],
                font_size=font_size,
                font_name=str(elem.get("font_name", "")),
                is_bold=bool(elem.get("is_bold", False)),
                is_italic=bool(elem.get("is_italic", False)),
                region=_classify_region(bbox, page_w, page_h),
                looks_like=_classify_looks_like(
                    font_size,
                    body_size,
                    bool(elem.get("is_bold", False)),
                    bool(elem.get("is_italic", False)),
                ),
            )
        )
    return blocks


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    parse_dir = output_dir / "intermediate" / "parse"
    if not parse_dir.exists():
        raise FileNotFoundError(
            f"extract_blocks stage requires parse output at {parse_dir} "
            f"(run `regula stage parse` first)"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    tree = json.loads((parse_dir / "tree.json").read_text(encoding="utf-8"))
    log.info("extract_blocks.start", elements=len(tree.get("elements", [])))

    blocks = extract(tree, cfg)

    with (stage_dir / "blocks.jsonl").open("w", encoding="utf-8") as f:
        for b in blocks:
            f.write(b.model_dump_json() + "\n")

    by_page: dict[int, int] = {}
    by_region: dict[str, int] = {}
    by_looks: dict[str, int] = {}
    for b in blocks:
        by_page[b.page] = by_page.get(b.page, 0) + 1
        by_region[b.region.value] = by_region.get(b.region.value, 0) + 1
        by_looks[b.looks_like.value] = by_looks.get(b.looks_like.value, 0) + 1

    finished = datetime.now(UTC)
    log.info(
        "extract_blocks.done",
        blocks=len(blocks),
        pages_with_blocks=len(by_page),
        **{f"region_{k}": v for k, v in by_region.items()},
        **{f"looks_{k}": v for k, v in by_looks.items()},
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={
            "blocks_emitted": len(blocks),
            "pages_with_blocks": len(by_page),
            **{f"region_{k}": v for k, v in by_region.items()},
            **{f"looks_{k}": v for k, v in by_looks.items()},
        },
    )
