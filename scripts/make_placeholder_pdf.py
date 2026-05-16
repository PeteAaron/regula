"""One-off script that generated tests/fixtures/small.pdf.

The PDF itself is committed; this script exists so the fixture is
reproducible. Phase 3 will replace the placeholder with a proper synthetic
regulatory-style fixture (3–5 pages, numbered paragraphs, a diagram, a
table, a tiny glossary). Until then, this single-page PDF gives
``source_pdf_sha256`` something deterministic to hash.

Run from the repository root:

    uv run python scripts/make_placeholder_pdf.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


def main() -> None:
    out = Path("tests/fixtures/small.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "regula placeholder fixture v0", fontsize=14)
    doc.save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
