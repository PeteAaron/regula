"""Page-oriented HTML preview of a pipeline run.

Replaces the chunk-oriented document view from the v0 pipeline. The
new preview shows *every* block from *every* page with no filtering
and no classification — the user's job is to look at what was
extracted and decide what to ignore. Visual hints (font size, bold,
indentation, region tag) convey the hierarchy of the page without
forcing a classification.

Design choices:

* **Page-by-page layout.** A sidebar lists every page in the document;
  clicking jumps to that page's section. Each page is rendered as a
  card with its blocks in reading order.
* **Visual fidelity, not pixel fidelity.** Blocks are *not* absolutely
  positioned at their bboxes; they flow vertically. Font size is
  scaled proportionally to the source, indentation is preserved via
  left-margin, bold/italic are preserved. The effect is a stripped-
  down render that surfaces hierarchy without trying to imitate the
  PDF layout.
* **Advisory hints visible.** Each block carries a ``region`` and
  ``looks_like`` tag (computed by ``extract_blocks``). These show as
  small badges on hover. They're never used to filter or merge blocks
  — purely there so the user can spot patterns (running footers,
  running heads, footnotes) and define ignore rules downstream.
* **Hyperlink markers.** When a hyperlink's bbox sits inside a
  block's bbox, the block shows a small link icon — click to follow
  internal jumps or visit external URIs. The full link list is also
  available in ``links.json``.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from regula.schemas import (
    Block,
    BlockLooksLike,
    BlockRegion,
    DeferredFeatureList,
    DocumentMeta,
    Links,
    PageLink,
    Pages,
)


def render_preview(output_dir: Path) -> str:
    """Read all artifacts from ``output_dir`` and return the HTML string."""
    blocks = _load_blocks(output_dir / "blocks.jsonl")
    pages = Pages.model_validate_json(
        (output_dir / "pages.json").read_text(encoding="utf-8")
    )
    document = DocumentMeta.model_validate_json(
        (output_dir / "document.json").read_text(encoding="utf-8")
    )
    deferred = _maybe_load_deferred(output_dir / "deferred.json")
    links = _maybe_load_links(output_dir / "links.json")

    body_size = _median_font_size(blocks)

    blocks_by_page: dict[int, list[Block]] = {}
    for b in blocks:
        blocks_by_page.setdefault(b.page, []).append(b)
    for page_blocks in blocks_by_page.values():
        page_blocks.sort(key=lambda b: b.reading_order_index)

    links_by_page: dict[int, list[PageLink]] = {}
    if links:
        for link in links.links:
            links_by_page.setdefault(link.page, []).append(link)

    page_dims = {p.page_number: (p.width, p.height) for p in pages.pages}

    status_pill = (
        "<span class='ok'>passed</span>"
        if document.pipeline_passed
        else "<span class='fail'>FAILED</span>"
    )
    summary = (
        f"{document.page_count} pages · {document.block_count} blocks · "
        f"validation {status_pill}"
    )

    return _PAGE.format(
        title=html.escape(document.title),
        doc_id=html.escape(document.doc_id),
        summary=summary,
        meta=_render_doc_meta(document),
        page_nav=_render_page_nav(pages, blocks_by_page),
        pages_html=_render_pages(
            pages,
            blocks_by_page,
            links_by_page,
            page_dims,
            body_size,
            document.doc_id,
        ),
        deferred=_render_deferred(deferred),
    )


def write_preview(output_dir: Path, out_path: Path | None = None) -> Path:
    """Render and write the HTML, returning the path written."""
    target = out_path or output_dir / "preview.html"
    target.write_text(render_preview(output_dir), encoding="utf-8")
    return target


# --- doc-level header ----------------------------------------------------


def _render_doc_meta(doc: DocumentMeta) -> str:
    rows = [
        ("edition", doc.edition),
        ("jurisdiction", doc.jurisdiction),
        ("legal status", doc.legal_status),
        ("regula", doc.regula_version),
        ("source PDF", doc.source_pdf),
        ("pdf sha", doc.source_pdf_sha256[:24] + "…"),
        ("config sha", doc.config_sha256[:24] + "…"),
        ("generated at", doc.generated_at.isoformat(timespec="seconds")),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    return (
        "<details class='doc-meta-details'>"
        "<summary>more details</summary>"
        f"<table class='doc-meta'>{body}</table>"
        "</details>"
    )


# --- page navigator ------------------------------------------------------


def _render_page_nav(pages: Pages, blocks_by_page: dict[int, list[Block]]) -> str:
    items: list[str] = []
    for p in pages.pages:
        n = len(blocks_by_page.get(p.page_number, []))
        items.append(
            f"<li><a href='#page-{p.page_number}'>"
            f"<span class='pg-num'>p{p.page_number}</span>"
            f"<span class='pg-count'>{n} block{'s' if n != 1 else ''}</span>"
            f"</a></li>"
        )
    return f"<ul class='page-nav'>{''.join(items)}</ul>"


# --- page rendering ------------------------------------------------------


def _render_pages(
    pages: Pages,
    blocks_by_page: dict[int, list[Block]],
    links_by_page: dict[int, list[PageLink]],
    page_dims: dict[int, tuple[float, float]],
    body_size: float,
    doc_id: str,
) -> str:
    sections: list[str] = []
    for p in pages.pages:
        page_blocks = blocks_by_page.get(p.page_number, [])
        page_links = links_by_page.get(p.page_number, [])
        sections.append(
            _render_page(
                p.page_number,
                page_blocks,
                page_links,
                page_dims.get(p.page_number, (595.0, 842.0)),
                body_size,
                doc_id,
            )
        )
    return "\n".join(sections)


def _render_page(
    page_num: int,
    blocks: list[Block],
    links: list[PageLink],
    page_dim: tuple[float, float],
    body_size: float,
    doc_id: str,
) -> str:
    width, height = page_dim
    if not blocks:
        body = "<p class='empty'>(no blocks extracted from this page)</p>"
    else:
        body = "\n".join(
            _render_block(b, links, width, body_size, doc_id) for b in blocks
        )
    return (
        f"<section class='page' id='page-{page_num}'>"
        f"<header class='page-head'>"
        f"<h2>Page {page_num}</h2>"
        f"<span class='dims'>{width:.0f} × {height:.0f} pt</span>"
        f"<span class='count'>{len(blocks)} block{'s' if len(blocks) != 1 else ''}</span>"
        f"</header>"
        f"<div class='page-body'>{body}</div>"
        f"</section>"
    )


def _block_links(block: Block, page_links: list[PageLink]) -> list[PageLink]:
    """Return any hyperlinks whose bbox overlaps this block's bbox."""
    x0, y0, x1, y1 = block.bbox
    out: list[PageLink] = []
    for link in page_links:
        lx0, ly0, lx1, ly1 = link.bbox
        if lx0 < x1 and lx1 > x0 and ly0 < y1 and ly1 > y0:
            out.append(link)
    return out


def _render_block(
    block: Block,
    page_links: list[PageLink],
    page_width: float,
    body_size: float,
    doc_id: str,
) -> str:
    # Font sizing — scale proportionally to actual font, clamped so a
    # 24pt heading doesn't dominate visually.
    css_size = max(11.0, min(block.font_size, 22.0))
    style_bits = [f"font-size:{css_size:.1f}px"]
    if block.is_bold:
        style_bits.append("font-weight:600")
    if block.is_italic:
        style_bits.append("font-style:italic")
    # Indentation — preserve x0 offset as a left-margin, capped.
    if page_width > 0:
        indent_fraction = block.bbox[0] / page_width
        # Subtract the typical body-text x0 (~12% of page width) so most
        # body lines have zero indent and indented lines show up clearly.
        indent_px = max(0.0, (indent_fraction - 0.12) * 240)
        if indent_px > 2:
            style_bits.append(f"margin-left:{indent_px:.0f}px")

    style = ";".join(style_bits)

    region_class = f"region-{block.region.value}"
    looks_class = f"looks-{block.looks_like.value}"

    block_links = _block_links(block, page_links)
    link_marker = ""
    if block_links:
        labels = []
        for link in block_links:
            if link.kind == "internal" and link.dest_page is not None:
                labels.append(
                    f"<a href='#page-{link.dest_page}' "
                    f"title='internal link → p{link.dest_page}'>↗p{link.dest_page}</a>"
                )
            elif link.uri:
                short = link.uri.split("/")[2] if "://" in link.uri else link.uri
                labels.append(
                    f"<a href='{html.escape(link.uri, quote=True)}' "
                    f"target='_blank' rel='noopener' "
                    f"title='external: {html.escape(link.uri)}'>"
                    f"↗{html.escape(short[:40])}</a>"
                )
        link_marker = "<span class='block-links'>" + " ".join(labels) + "</span>"

    region_badge = f"<span class='badge badge-region'>{block.region.value}</span>"
    looks_badge = f"<span class='badge badge-looks'>{block.looks_like.value}</span>"

    meta = (
        f"<div class='meta'>"
        f"<code>{html.escape(block.block_id)}</code>"
        f"<span class='font-info'>{block.font_size}pt {html.escape(block.font_name)}</span>"
        f"<span class='bbox'>bbox: ({block.bbox[0]:.0f}, {block.bbox[1]:.0f}, "
        f"{block.bbox[2]:.0f}, {block.bbox[3]:.0f})</span>"
        f"{region_badge}{looks_badge}"
        f"</div>"
    )

    return (
        f"<article class='block {region_class} {looks_class}' "
        f"id='{html.escape(block.block_id)}'>"
        f"<div class='block-text' style='{style}'>{html.escape(block.text)}{link_marker}</div>"
        f"{meta}"
        f"</article>"
    )


# --- deferred footer -----------------------------------------------------


def _render_deferred(deferred: DeferredFeatureList | None) -> str:
    if deferred is None or not deferred.features:
        return ""
    items = []
    for f in deferred.features:
        observed = (
            f" <span class='observed'>observed: {f.observed_count}</span>"
            if f.observed_count is not None
            else ""
        )
        items.append(
            f"<li><strong>{html.escape(f.name)}</strong>"
            f"<span class='phase'>{html.escape(f.target_phase)}</span>"
            f"{observed}<br>{html.escape(f.description)}</li>"
        )
    return (
        "<details class='deferred-details'>"
        f"<summary>Deferred capabilities ({len(deferred.features)})</summary>"
        f"<ul class='deferred-list'>{''.join(items)}</ul>"
        "</details>"
    )


# --- IO ------------------------------------------------------------------


def _load_blocks(path: Path) -> list[Block]:
    if not path.exists():
        return []
    return [
        Block.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _maybe_load_deferred(path: Path) -> DeferredFeatureList | None:
    if not path.exists():
        return None
    return DeferredFeatureList.model_validate_json(path.read_text(encoding="utf-8"))


def _maybe_load_links(path: Path) -> Links | None:
    if not path.exists():
        return None
    return Links.model_validate_json(path.read_text(encoding="utf-8"))


def _median_font_size(blocks: list[Block]) -> float:
    if not blocks:
        return 10.0
    weighted: list[float] = []
    for b in blocks:
        weighted.extend([b.font_size] * max(1, len(b.text)))
    weighted.sort()
    return weighted[len(weighted) // 2]


# --- HTML page template --------------------------------------------------


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>regula · {title}</title>
<style>
:root {{
  --c-text: #1f2328;
  --c-muted: #6a737d;
  --c-faint: #afafaf;
  --c-border: #e0e0e0;
  --c-bg: #ffffff;
  --c-page-card: #fbfbfb;
  --c-block-border: #e8e8e8;
  --c-header-bg: #fdf6e3;
  --c-footer-bg: #f0f4ff;
  --c-margin-bg: #f6f4ff;
  --c-large: #2050a0;
  --c-emphasis: #7b3f00;
  --c-fail: #b91c1c;
  --c-ok: #15803d;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  margin: 0; color: var(--c-text); background: var(--c-bg);
  line-height: 1.5; font-size: 15px;
}}
.ok {{ color: var(--c-ok); font-weight: 600; }}
.fail {{ color: var(--c-fail); font-weight: 600; }}

/* --- header --- */
header.page-head-bar {{
  padding: 14px 28px 10px; border-bottom: 1px solid var(--c-border);
  display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  position: sticky; top: 0; z-index: 10;
  background: rgba(255,255,255,0.97); backdrop-filter: blur(4px);
}}
header.page-head-bar h1 {{ margin: 0; font-size: 1.05em; font-weight: 600; }}
header.page-head-bar .doc-id {{
  color: var(--c-muted); font-family: ui-monospace, monospace; font-size: 0.85em;
}}
header.page-head-bar .summary {{ color: var(--c-muted); font-size: 0.88em; margin-left: auto; }}
.doc-meta-details summary {{
  cursor: pointer; color: var(--c-muted); font-size: 0.8em; user-select: none;
}}
table.doc-meta {{ font-size: 0.82em; margin-top: 8px; border-collapse: collapse; }}
table.doc-meta th {{
  text-align: left; padding: 1px 14px 1px 0; font-weight: normal;
  color: var(--c-muted); vertical-align: top;
}}
table.doc-meta td {{ padding: 1px 0; font-family: ui-monospace, monospace; }}

/* --- layout --- */
.layout {{
  display: grid; grid-template-columns: 160px minmax(0, 1fr);
  gap: 36px; padding: 24px 28px 80px; max-width: 1400px; margin: 0 auto;
}}
nav.page-nav-wrap {{
  position: sticky; top: 70px; align-self: start;
  max-height: calc(100vh - 90px); overflow-y: auto; font-size: 0.85em;
}}
nav.page-nav-wrap h3 {{
  margin: 0 0 10px 0; font-size: 0.75em; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--c-muted);
}}
ul.page-nav {{ list-style: none; padding: 0; margin: 0; }}
ul.page-nav li {{ margin: 2px 0; }}
ul.page-nav a {{
  display: flex; justify-content: space-between; gap: 10px;
  padding: 4px 8px; border-radius: 4px;
  color: var(--c-text); text-decoration: none;
}}
ul.page-nav a:hover {{ background: #f0f0f0; }}
ul.page-nav .pg-num {{ font-family: ui-monospace, monospace; }}
ul.page-nav .pg-count {{ color: var(--c-muted); font-size: 0.85em; }}
main {{ min-width: 0; }}

/* --- page card --- */
section.page {{
  margin-bottom: 36px; padding: 0;
  background: var(--c-page-card);
  border: 1px solid var(--c-border);
  border-radius: 6px;
  overflow: hidden;
}}
section.page > header.page-head {{
  padding: 10px 16px; border-bottom: 1px solid var(--c-border);
  background: #fff;
  display: flex; gap: 16px; align-items: baseline;
  font-size: 0.9em; color: var(--c-muted);
}}
section.page > header.page-head h2 {{
  margin: 0; font-size: 1em; color: var(--c-text);
}}
section.page > header.page-head .count {{ margin-left: auto; }}
.page-body {{ padding: 16px 20px; }}

/* --- block --- */
.block {{
  position: relative;
  margin: 0;
  padding: 6px 8px 6px 10px;
  border-left: 2px solid transparent;
  border-radius: 3px;
}}
.block:hover {{ background: rgba(0,0,0,0.025); border-left-color: var(--c-block-border); }}
.block .block-text {{
  white-space: pre-wrap;
  line-height: 1.4;
}}
.block .block-links {{
  margin-left: 8px;
}}
.block .block-links a {{
  font-size: 0.78em;
  font-family: ui-monospace, monospace;
  color: var(--c-large);
  text-decoration: none;
  margin-right: 6px;
}}
.block .block-links a:hover {{ text-decoration: underline; }}

/* Region tints — subtle backgrounds so the user can spot header/footer/margin clusters. */
.region-header .block-text {{ color: #6a4a00; }}
.region-footer .block-text {{ color: #2a3060; }}
.region-margin .block-text {{ color: #5a2a80; }}
.region-header {{ background: rgba(253, 246, 227, 0.4); }}
.region-footer {{ background: rgba(240, 244, 255, 0.4); }}
.region-margin {{ background: rgba(246, 244, 255, 0.4); }}

/* Looks-like accents on the left border. */
.looks-large_text {{ border-left-color: var(--c-large); }}
.looks-emphasis {{ border-left-color: var(--c-emphasis); }}
.looks-small_text {{ border-left-color: #aaa; }}

/* --- meta strip (hidden by default; revealed on hover or via toggle) --- */
.meta {{
  display: none;
  font-size: 0.72em; color: var(--c-muted); font-family: ui-monospace, monospace;
  margin-top: 4px;
  flex-wrap: wrap; gap: 2px 10px; align-items: baseline;
}}
.block:hover > .meta {{ display: flex; }}
.meta code {{
  background: #ececec; padding: 0 5px; border-radius: 3px;
  color: #444;
}}
.meta .badge {{
  background: #ddd; padding: 0 6px; border-radius: 10px;
  font-family: -apple-system, sans-serif; color: #555;
}}
.meta .badge-region {{ background: #eee; }}
.meta .badge-looks {{ background: #e6efff; color: var(--c-large); }}
.region-header .meta .badge-region {{ background: var(--c-header-bg); color: #6a4a00; }}
.region-footer .meta .badge-region {{ background: var(--c-footer-bg); color: #2a3060; }}
.region-margin .meta .badge-region {{ background: var(--c-margin-bg); color: #5a2a80; }}

body:has(#tog-meta:checked) .meta {{ display: flex; }}

/* --- toggle bar --- */
.view-toggles {{
  display: flex; gap: 16px; align-items: center;
  padding: 0;
  font-size: 0.85em; color: var(--c-muted);
}}
.view-toggles label {{ cursor: pointer; user-select: none; }}
.view-toggles input {{ vertical-align: -1px; margin-right: 4px; }}

/* --- deferred capabilities --- */
.deferred-details {{
  margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--c-border);
}}
.deferred-details summary {{
  cursor: pointer; color: var(--c-muted); font-size: 0.85em; user-select: none;
  text-transform: uppercase; letter-spacing: 0.04em;
}}
ul.deferred-list {{
  padding-left: 0; list-style: none; margin: 12px 0 0; font-size: 0.88em;
}}
ul.deferred-list li {{ margin: 10px 0; color: var(--c-muted); }}
ul.deferred-list strong {{ color: var(--c-text); }}
ul.deferred-list .phase {{
  display: inline-block; margin-left: 8px; padding: 0 7px; background: #eee;
  border-radius: 10px; font-size: 0.88em; color: #666;
}}
ul.deferred-list .observed {{
  display: inline-block; margin-left: 6px; padding: 0 6px;
  background: #fff2a8; border-radius: 3px;
  font-family: ui-monospace, monospace; font-size: 0.85em; color: #6b4500;
}}
.empty {{ color: var(--c-muted); font-style: italic; }}
</style>
</head>
<body>
<header class="page-head-bar">
<h1>{title}</h1>
<span class="doc-id">{doc_id}</span>
<span class="summary">{summary}</span>
<div class="view-toggles">
<label><input type="checkbox" id="tog-meta"> Show metadata</label>
</div>
{meta}
</header>
<div class="layout">
<nav class="page-nav-wrap">
<h3>Pages</h3>
{page_nav}
</nav>
<main>
{pages_html}
{deferred}
</main>
</div>
</body>
</html>
"""
