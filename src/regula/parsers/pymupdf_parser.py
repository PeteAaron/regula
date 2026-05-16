"""PyMuPDF-based document parser.

Produces a normalised tree from a PDF: page geometry, flat reading-order
list of text elements with per-element font metadata, raster images with
their bounding boxes, the PDF outline, and every internal/external
hyperlink. Coordinate convention matches the contract — PDF userspace
points, top-left origin (which is also PyMuPDF's default).

This is the *primary* parser for the digital-PDF case. The brief specifies
Docling as the default, but Docling needs to download ML models on first
use and isn't available in offline/sandboxed environments. PyMuPDF gives
us enough structure to drive the chunker for well-formatted regulatory
PDFs. A Docling parser can slot in here later behind the same interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

NAME = "pymupdf"
VERSION = pymupdf.__doc__.splitlines()[0] if pymupdf.__doc__ else "unknown"

# PyMuPDF span flag bits — see pymupdf docs.
_FLAG_BOLD = 1 << 4
_FLAG_ITALIC = 1 << 1


def _is_bold(flags: int) -> bool:
    return bool(flags & _FLAG_BOLD)


def _is_italic(flags: int) -> bool:
    return bool(flags & _FLAG_ITALIC)


def _block_text(block: dict[str, Any]) -> str:
    """Join all line/span text in a block, preserving inline whitespace."""
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = "".join(span.get("text", "") for span in line.get("spans", []))
        parts.append(line_text)
    return " ".join(p.strip() for p in parts if p.strip())


def _block_font_signature(block: dict[str, Any]) -> tuple[float, str, int]:
    """Return ``(font_size, font_name, flags)`` of the block's first span.
    Good enough for heading detection on regularly-typeset documents."""
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            return (
                round(float(span.get("size", 0.0)), 2),
                str(span.get("font", "")),
                int(span.get("flags", 0)),
            )
    return (0.0, "", 0)


def _link_record(link: dict[str, Any], page_number: int) -> dict[str, Any] | None:
    kind = link.get("kind")
    if kind == pymupdf.LINK_GOTO:
        record: dict[str, Any] = {
            "page": page_number,
            "bbox": list(link["from"]),
            "kind": "internal",
            "dest_page": int(link.get("page", -1)) + 1,
        }
        to = link.get("to")
        if to is not None:
            record["dest_point"] = [float(to[0]), float(to[1])]
        return record
    if kind == pymupdf.LINK_URI:
        return {
            "page": page_number,
            "bbox": list(link["from"]),
            "kind": "external",
            "uri": str(link.get("uri", "")),
        }
    return None


def parse(pdf_path: str | Path) -> dict[str, Any]:
    """Return the normalised parse tree for ``pdf_path``.

    The shape of the returned dict is part of the contract between
    :mod:`regula.stages.parse` and :mod:`regula.stages.chunk`. Stages
    other than these two should not consume this format directly.
    """
    path = Path(pdf_path)
    doc = pymupdf.open(path)

    pages: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []

    for page_idx, page in enumerate(doc, start=1):
        pages.append(
            {
                "page_number": page_idx,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "rotation": int(page.rotation),
            }
        )

        # Text blocks → flat elements list in reading order.
        page_dict = page.get_text("dict", sort=True)
        for block_idx, block in enumerate(page_dict.get("blocks", [])):
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            text = _block_text(block)
            if not text:
                continue
            size, font, flags = _block_font_signature(block)
            elements.append(
                {
                    "page": page_idx,
                    "block_index": block_idx,
                    "bbox": [float(c) for c in block["bbox"]],
                    "text": text,
                    "font_size": size,
                    "font_name": font,
                    "is_bold": _is_bold(flags),
                    "is_italic": _is_italic(flags),
                }
            )

        # Hyperlinks.
        for link in page.get_links():
            record = _link_record(link, page_idx)
            if record is not None:
                links.append(record)

        # Raster images on the page.
        for img_info in page.get_image_info(xrefs=True):
            bbox = img_info.get("bbox")
            if not bbox:
                continue
            images.append(
                {
                    "page": page_idx,
                    "xref": int(img_info.get("xref", 0)),
                    "bbox": [float(c) for c in bbox],
                    "width": int(img_info.get("width", 0)),
                    "height": int(img_info.get("height", 0)),
                }
            )

    # PDF outline. ``simple=False`` includes the destination details.
    outline = doc.get_toc(simple=False)
    outline_records: list[dict[str, Any]] = []
    for entry in outline:
        # entry shape: [level, title, page, dest_info]
        record: dict[str, Any] = {
            "level": int(entry[0]),
            "title": str(entry[1]),
            "page": int(entry[2]),
        }
        if len(entry) > 3 and isinstance(entry[3], dict):
            dest = entry[3]
            if "to" in dest and hasattr(dest["to"], "y"):
                record["dest_y"] = float(dest["to"].y)
        outline_records.append(record)

    doc.close()

    return {
        "parser": NAME,
        "parser_version": VERSION,
        "pages": pages,
        "elements": elements,
        "links": links,
        "images": images,
        "outline": outline_records,
    }


def extract_image(pdf_path: str | Path, xref: int) -> bytes:
    """Return the raw bytes of one image by xref. Used by the chunk stage
    to write extracted assets under ``assets/``."""
    doc = pymupdf.open(pdf_path)
    try:
        return doc.extract_image(xref)["image"]
    finally:
        doc.close()
