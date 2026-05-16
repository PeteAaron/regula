"""Interactive page-oriented HTML preview of a pipeline run.

Self-contained HTML + inline JS that lets the user iteratively
classify what's in the document:

* Click any block → side panel opens with two grouping tabs:
  - **Same exact text**: every block in the document with this exact
    text (catches "Online version" repeated 27 times).
  - **Same position & font**: every block with similar y-position and
    matching font signature (catches running headers / footers / page
    numbers that vary in text but share layout).
* Each tab offers two actions: **suppress** (fades the matching
  blocks out) and **tag as…** (marks them with a colour-coded label —
  heading, paragraph, page_number, running_header, running_footer,
  toc_entry, glossary_entry, noise, other).
* All decisions persist in browser ``localStorage`` keyed by the
  document's ``doc_id`` — refresh the page, decisions remain.
* An **Export rules** button downloads the suppress/tag set as YAML
  so it can be versioned, shared, or fed back into a future cleanup
  stage.

Nothing in the pipeline reads these rules yet — the previewer is
where you build them up. A later opt-in chunking stage will consume
them.

Design notes:

* The block layout is *not* pixel-faithful to the PDF. Blocks flow
  vertically inside per-page cards. Font size is scaled
  proportionally, indentation is preserved from x0, bold/italic are
  preserved — enough visual signal to surface hierarchy without
  trying to replay the PDF layout.
* Pattern groups (exact-text and position+font) are computed
  server-side in Python and embedded as a JSON blob in the HTML.
  Clicking a block is then a constant-time lookup in JS — no
  scanning across thousands of blocks per click.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

from regula.schemas import (
    Block,
    DeferredFeatureList,
    DocumentMeta,
    Links,
    PageLink,
    Pages,
)

# y-bucket size (pt) for the position-grouping key. ~10pt buckets are
# coarse enough to absorb sub-pixel jitter on running headers/footers
# but fine enough that a header and the first line of body don't
# collide.
_Y_BUCKET_PT = 10.0

# Minimum group size to keep. A group of one block isn't a pattern.
_MIN_GROUP_SIZE = 2


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

    text_groups, position_groups = _compute_groups(blocks)
    # Map every block_id → (text_group_key | None, position_group_key | None)
    # so a click on a block is an O(1) lookup in JS.
    block_to_groups: dict[str, dict[str, str | None]] = {}
    for b in blocks:
        block_to_groups[b.block_id] = {
            "text": _text_key(b),
            "position": _position_key(b),
        }

    embedded_state = {
        "doc_id": document.doc_id,
        "block_to_groups": block_to_groups,
        "text_groups": text_groups,
        "position_groups": position_groups,
        "block_summaries": _block_summaries(blocks),
    }

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
        state_json=json.dumps(embedded_state, separators=(",", ":")),
        js=_JS,
    )


def write_preview(output_dir: Path, out_path: Path | None = None) -> Path:
    """Render and write the HTML, returning the path written."""
    target = out_path or output_dir / "preview.html"
    target.write_text(render_preview(output_dir), encoding="utf-8")
    return target


# --- pattern grouping ----------------------------------------------------


def _text_key(block: Block) -> str:
    """Exact-text grouping key. Whitespace-collapsed, case-preserved."""
    return " ".join(block.text.split())


def _position_key(block: Block) -> str:
    """Position+font grouping key. Buckets y0 to absorb sub-pixel
    jitter; combines with font signature so running headers don't
    cluster with body text that happens to share a y-band on one
    page."""
    y_bucket = int(round(block.bbox[1] / _Y_BUCKET_PT))
    return (
        f"y{y_bucket}|fs{block.font_size}|fn{block.font_name}|"
        f"b{int(block.is_bold)}|i{int(block.is_italic)}|reg{block.region.value}"
    )


def _compute_groups(
    blocks: list[Block],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (exact_text_groups, position_font_groups). Both map a
    group key to the list of block_ids that share that key. Groups
    smaller than _MIN_GROUP_SIZE are dropped — single-occurrence
    patterns aren't useful for bulk suppression."""
    text_groups: dict[str, list[str]] = defaultdict(list)
    position_groups: dict[str, list[str]] = defaultdict(list)
    for b in blocks:
        text_groups[_text_key(b)].append(b.block_id)
        position_groups[_position_key(b)].append(b.block_id)
    return (
        {k: v for k, v in text_groups.items() if len(v) >= _MIN_GROUP_SIZE},
        {k: v for k, v in position_groups.items() if len(v) >= _MIN_GROUP_SIZE},
    )


def _block_summaries(blocks: list[Block]) -> dict[str, dict[str, object]]:
    """One-line summaries used by the side panel when listing members of
    a group. Keeping these on the Python side avoids embedding the full
    block payload twice."""
    return {
        b.block_id: {
            "text": b.text[:200],
            "page": b.page,
            "font_size": b.font_size,
            "region": b.region.value,
        }
        for b in blocks
    }


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
            f"<span class='pg-count'>{n}</span>"
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
    css_size = max(11.0, min(block.font_size, 22.0))
    style_bits = [f"font-size:{css_size:.1f}px"]
    if block.is_bold:
        style_bits.append("font-weight:600")
    if block.is_italic:
        style_bits.append("font-style:italic")
    if page_width > 0:
        indent_fraction = block.bbox[0] / page_width
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
        link_marker = "<span class='block-links' data-no-select>" + " ".join(labels) + "</span>"

    return (
        f"<article class='block {region_class} {looks_class}' "
        f"data-block-id='{html.escape(block.block_id)}' "
        f"id='{html.escape(block.block_id)}'>"
        f"<div class='block-text' style='{style}'>{html.escape(block.text)}{link_marker}</div>"
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
  --c-selected: #fff2a8;
  --c-highlight: #ffd966;

  /* Tag colours — left-border markers on tagged blocks. */
  --tag-heading: #1f3a8a;
  --tag-paragraph: #15803d;
  --tag-page_number: #888;
  --tag-running_header: #c08a1a;
  --tag-running_footer: #1f4e8a;
  --tag-toc_entry: #6a1f8a;
  --tag-glossary_entry: #1f8a8a;
  --tag-noise: #b91c1c;
  --tag-other: #8a1f6a;
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
  padding: 12px 20px 10px; border-bottom: 1px solid var(--c-border);
  display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  position: sticky; top: 0; z-index: 10;
  background: rgba(255,255,255,0.97); backdrop-filter: blur(4px);
}}
header.page-head-bar h1 {{ margin: 0; font-size: 1.05em; font-weight: 600; }}
header.page-head-bar .doc-id {{
  color: var(--c-muted); font-family: ui-monospace, monospace; font-size: 0.85em;
}}
header.page-head-bar .summary {{ color: var(--c-muted); font-size: 0.88em; }}
.doc-meta-details summary {{
  cursor: pointer; color: var(--c-muted); font-size: 0.8em; user-select: none;
}}
table.doc-meta {{ font-size: 0.82em; margin-top: 8px; border-collapse: collapse; }}
table.doc-meta th {{
  text-align: left; padding: 1px 14px 1px 0; font-weight: normal;
  color: var(--c-muted); vertical-align: top;
}}
table.doc-meta td {{ padding: 1px 0; font-family: ui-monospace, monospace; }}

.rule-counters {{
  margin-left: auto; display: flex; gap: 14px; align-items: center;
  font-size: 0.85em; color: var(--c-muted);
}}
.rule-counters .num {{ font-family: ui-monospace, monospace; color: var(--c-text); }}
.rule-counters button {{
  border: 1px solid var(--c-border); background: #fff;
  padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 0.95em;
}}
.rule-counters button:hover {{ background: #f5f5f5; }}
.rule-counters button.danger {{ color: var(--c-fail); }}

/* --- layout --- */
.layout {{
  display: grid; grid-template-columns: 140px minmax(0, 1fr);
  gap: 28px; padding: 20px 20px 80px; max-width: 1280px; margin: 0 auto;
}}
nav.page-nav-wrap {{
  position: sticky; top: 60px; align-self: start;
  max-height: calc(100vh - 80px); overflow-y: auto; font-size: 0.85em;
}}
nav.page-nav-wrap h3 {{
  margin: 0 0 10px 0; font-size: 0.75em; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--c-muted);
}}
ul.page-nav {{ list-style: none; padding: 0; margin: 0; }}
ul.page-nav li {{ margin: 1px 0; }}
ul.page-nav a {{
  display: flex; justify-content: space-between; gap: 8px;
  padding: 3px 8px; border-radius: 4px;
  color: var(--c-text); text-decoration: none;
}}
ul.page-nav a:hover {{ background: #f0f0f0; }}
ul.page-nav .pg-num {{ font-family: ui-monospace, monospace; }}
ul.page-nav .pg-count {{ color: var(--c-muted); font-size: 0.85em; }}
main {{ min-width: 0; }}

/* --- page card --- */
section.page {{
  margin-bottom: 30px; padding: 0;
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
.page-body {{ padding: 14px 18px; }}

/* --- block --- */
.block {{
  position: relative;
  margin: 0;
  padding: 6px 8px 6px 12px;
  border-left: 3px solid transparent;
  border-radius: 3px;
  cursor: pointer;
}}
.block:hover {{ background: rgba(0,0,0,0.025); }}
.block.selected {{
  background: var(--c-selected);
  border-left-color: var(--c-highlight);
}}
.block.highlight-group {{
  background: rgba(255, 217, 102, 0.18);
}}
.block.suppressed .block-text {{
  text-decoration: line-through;
  opacity: 0.4;
}}
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

/* Tag colours — left border. Tag class added by JS. */
.block.tag-heading {{ border-left-color: var(--tag-heading); }}
.block.tag-paragraph {{ border-left-color: var(--tag-paragraph); }}
.block.tag-page_number {{ border-left-color: var(--tag-page_number); }}
.block.tag-running_header {{ border-left-color: var(--tag-running_header); }}
.block.tag-running_footer {{ border-left-color: var(--tag-running_footer); }}
.block.tag-toc_entry {{ border-left-color: var(--tag-toc_entry); }}
.block.tag-glossary_entry {{ border-left-color: var(--tag-glossary_entry); }}
.block.tag-noise {{ border-left-color: var(--tag-noise); }}
.block.tag-other {{ border-left-color: var(--tag-other); }}

/* Region tints when no explicit tag has overridden. */
.region-header:not([class*="tag-"]) {{ background: rgba(253, 246, 227, 0.35); }}
.region-footer:not([class*="tag-"]) {{ background: rgba(240, 244, 255, 0.35); }}
.region-margin:not([class*="tag-"]) {{ background: rgba(246, 244, 255, 0.35); }}

/* --- side panel --- */
.panel {{
  position: fixed; top: 0; right: 0; height: 100vh; width: 380px;
  background: #fff;
  border-left: 1px solid var(--c-border);
  box-shadow: -4px 0 16px rgba(0,0,0,0.06);
  z-index: 100;
  display: none;
  flex-direction: column;
  overflow: hidden;
}}
.panel.open {{ display: flex; }}
.panel-head {{
  padding: 12px 16px; border-bottom: 1px solid var(--c-border);
  display: flex; align-items: baseline; gap: 10px;
}}
.panel-head h2 {{ margin: 0; font-size: 1em; }}
.panel-head .close {{
  margin-left: auto; cursor: pointer; color: var(--c-muted);
  background: none; border: none; font-size: 1.4em; line-height: 1;
}}
.panel-block-meta {{
  padding: 10px 16px; border-bottom: 1px solid var(--c-border);
  font-size: 0.82em; color: var(--c-muted);
  font-family: ui-monospace, monospace;
}}
.panel-block-text {{
  padding: 12px 16px; border-bottom: 1px solid var(--c-border);
  max-height: 140px; overflow-y: auto;
  white-space: pre-wrap; line-height: 1.45;
}}
.panel-tabs {{
  display: flex; border-bottom: 1px solid var(--c-border); background: #fafafa;
}}
.panel-tabs button {{
  flex: 1; padding: 10px 8px; background: none; border: none; cursor: pointer;
  font-size: 0.88em; color: var(--c-muted);
  border-bottom: 2px solid transparent;
}}
.panel-tabs button.active {{
  color: var(--c-text); border-bottom-color: var(--c-highlight);
  background: #fff;
}}
.panel-tab-body {{
  flex: 1; overflow-y: auto; padding: 14px 16px;
}}
.panel-summary {{
  font-size: 0.9em; color: var(--c-muted); margin-bottom: 12px;
}}
.panel-summary .count {{ color: var(--c-text); font-weight: 600; }}
.panel-actions {{
  display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px;
}}
.panel-actions button {{
  width: 100%; padding: 8px 12px; border-radius: 4px; cursor: pointer;
  border: 1px solid var(--c-border); background: #fff;
  font-size: 0.92em; text-align: left;
}}
.panel-actions button:hover {{ background: #f5f5f5; }}
.panel-actions .suppress {{ color: var(--c-fail); border-color: #f3c0c0; }}
.panel-actions .suppress:hover {{ background: #fdf0f0; }}
.panel-actions .unsuppress {{ background: #fdf0f0; }}
.panel-tag-row {{
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px;
  margin-bottom: 16px;
}}
.panel-tag-row button {{
  padding: 6px 8px; border: 1px solid var(--c-border);
  background: #fff; border-radius: 4px; cursor: pointer;
  font-size: 0.82em; text-align: left;
  border-left-width: 4px;
}}
.panel-tag-row button:hover {{ background: #f5f5f5; }}
.panel-tag-row .clear {{ grid-column: span 3; border-left-color: var(--c-border); color: var(--c-muted); }}
.panel-tag-heading {{ border-left-color: var(--tag-heading); }}
.panel-tag-paragraph {{ border-left-color: var(--tag-paragraph); }}
.panel-tag-page_number {{ border-left-color: var(--tag-page_number); }}
.panel-tag-running_header {{ border-left-color: var(--tag-running_header); }}
.panel-tag-running_footer {{ border-left-color: var(--tag-running_footer); }}
.panel-tag-toc_entry {{ border-left-color: var(--tag-toc_entry); }}
.panel-tag-glossary_entry {{ border-left-color: var(--tag-glossary_entry); }}
.panel-tag-noise {{ border-left-color: var(--tag-noise); }}
.panel-tag-other {{ border-left-color: var(--tag-other); }}
.panel-members {{
  list-style: none; padding: 0; margin: 0; font-size: 0.85em;
}}
.panel-members li {{
  padding: 6px 8px; border-bottom: 1px solid #f0f0f0;
  display: flex; gap: 8px; align-items: baseline;
}}
.panel-members a {{ color: var(--c-large); text-decoration: none; font-family: ui-monospace, monospace; }}
.panel-members a:hover {{ text-decoration: underline; }}
.panel-members .pg {{ color: var(--c-muted); font-family: ui-monospace, monospace; }}
.panel-members .text {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

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
.empty {{ color: var(--c-muted); font-style: italic; }}

.panel-empty {{ color: var(--c-muted); font-style: italic; }}
</style>
</head>
<body>
<header class="page-head-bar">
<h1>{title}</h1>
<span class="doc-id">{doc_id}</span>
<span class="summary">{summary}</span>
<div class="rule-counters">
  <span><span class="num" id="count-suppressed">0</span> suppressed</span>
  <span><span class="num" id="count-tagged">0</span> tagged</span>
  <button id="btn-export">Export rules</button>
  <button id="btn-reset" class="danger">Reset</button>
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

<aside class="panel" id="side-panel">
  <div class="panel-head">
    <h2>Block details</h2>
    <button class="close" id="panel-close">×</button>
  </div>
  <div class="panel-block-meta" id="panel-meta"></div>
  <div class="panel-block-text" id="panel-text"></div>
  <div class="panel-tabs">
    <button class="active" data-tab="text">Same text</button>
    <button data-tab="position">Same position &amp; font</button>
  </div>
  <div class="panel-tab-body" id="panel-tab-body"></div>
</aside>

<script id="regula-state" type="application/json">{state_json}</script>
<script>
{js}
</script>
</body>
</html>
"""


# --- inline JavaScript ---------------------------------------------------
#
# Kept outside the .format() call so the JS can use curly braces freely.

_JS = r"""
(function() {
  const state = JSON.parse(document.getElementById('regula-state').textContent);
  const STORAGE_KEY = 'regula:' + state.doc_id;
  const TAGS = [
    ['heading', 'Heading'],
    ['paragraph', 'Paragraph'],
    ['page_number', 'Page number'],
    ['running_header', 'Running header'],
    ['running_footer', 'Running footer'],
    ['toc_entry', 'TOC entry'],
    ['glossary_entry', 'Glossary entry'],
    ['noise', 'Noise'],
    ['other', 'Other'],
  ];

  // Persistent state — { suppressed: {block_id: true}, tags: {block_id: tag} }
  let saved = { suppressed: {}, tags: {} };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) saved = Object.assign({ suppressed: {}, tags: {} }, JSON.parse(raw));
  } catch (e) {}

  const $blocks = document.querySelectorAll('.block');
  const blockById = {};
  $blocks.forEach(el => { blockById[el.dataset.blockId] = el; });

  function save() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
    updateCounters();
  }

  function applyState() {
    $blocks.forEach(el => {
      const id = el.dataset.blockId;
      el.classList.toggle('suppressed', !!saved.suppressed[id]);
      // remove any existing tag class
      [...el.classList].filter(c => c.startsWith('tag-')).forEach(c => el.classList.remove(c));
      if (saved.tags[id]) el.classList.add('tag-' + saved.tags[id]);
    });
    updateCounters();
  }

  function updateCounters() {
    document.getElementById('count-suppressed').textContent =
      Object.keys(saved.suppressed).length;
    document.getElementById('count-tagged').textContent =
      Object.keys(saved.tags).length;
  }

  // --- side panel ---------------------------------------------------------

  const panel = document.getElementById('side-panel');
  const panelMeta = document.getElementById('panel-meta');
  const panelText = document.getElementById('panel-text');
  const panelBody = document.getElementById('panel-tab-body');
  const panelTabs = panel.querySelectorAll('.panel-tabs button');
  let activeBlockId = null;
  let activeTab = 'text';

  function clearHighlights() {
    document.querySelectorAll('.block.highlight-group').forEach(el =>
      el.classList.remove('highlight-group'));
    document.querySelectorAll('.block.selected').forEach(el =>
      el.classList.remove('selected'));
  }

  function openPanel(blockId) {
    activeBlockId = blockId;
    const summary = state.block_summaries[blockId];
    if (!summary) { closePanel(); return; }
    clearHighlights();
    const el = blockById[blockId];
    if (el) {
      el.classList.add('selected');
    }
    panelMeta.innerHTML =
      '<code>' + blockId + '</code>' +
      '<br>page ' + summary.page + ' · ' + summary.font_size + 'pt · ' + summary.region;
    panelText.textContent = summary.text;
    renderTab();
    panel.classList.add('open');
  }

  function closePanel() {
    panel.classList.remove('open');
    activeBlockId = null;
    clearHighlights();
  }

  function renderTab() {
    if (!activeBlockId) return;
    const groups = state.block_to_groups[activeBlockId] || {};
    const groupKey = activeTab === 'text' ? groups.text : groups.position;
    const members = (activeTab === 'text'
      ? state.text_groups[groupKey]
      : state.position_groups[groupKey]) || [activeBlockId];
    // highlight all members
    document.querySelectorAll('.block.highlight-group').forEach(el =>
      el.classList.remove('highlight-group'));
    members.forEach(id => {
      const el = blockById[id];
      if (el && id !== activeBlockId) el.classList.add('highlight-group');
    });

    const allSuppressed = members.every(id => saved.suppressed[id]);
    const commonTag = (function() {
      const t = saved.tags[members[0]];
      if (!t) return null;
      return members.every(id => saved.tags[id] === t) ? t : null;
    })();

    let html = '';
    html += '<div class="panel-summary">';
    if (activeTab === 'text') {
      html += '<span class="count">' + members.length + '</span> ' +
              'block' + (members.length === 1 ? '' : 's') +
              ' with this exact text';
    } else {
      html += '<span class="count">' + members.length + '</span> ' +
              'block' + (members.length === 1 ? '' : 's') +
              ' with similar position &amp; font';
    }
    html += '</div>';

    html += '<div class="panel-actions">';
    if (members.length <= 1 && activeTab === 'text') {
      html += '<div class="panel-empty">Only this block — no pattern to suppress in bulk. ' +
              'Use the actions below to suppress/tag just this one.</div>';
    }
    if (allSuppressed) {
      html += '<button class="unsuppress" data-action="unsuppress">' +
              '↺ Restore ' + members.length + ' block' +
              (members.length === 1 ? '' : 's') + '</button>';
    } else {
      html += '<button class="suppress" data-action="suppress">' +
              '✕ Suppress all ' + members.length + ' block' +
              (members.length === 1 ? '' : 's') + '</button>';
    }
    html += '</div>';

    html += '<div class="panel-tag-row">';
    for (const [tag, label] of TAGS) {
      const active = commonTag === tag ? ' style="background:#fff2a8"' : '';
      html += '<button class="panel-tag-' + tag + '" data-action="tag" data-tag="' + tag + '"' +
              active + '>' + label + '</button>';
    }
    html += '<button class="clear" data-action="untag">Clear tag</button>';
    html += '</div>';

    if (members.length > 1) {
      html += '<ul class="panel-members">';
      for (const id of members.slice(0, 50)) {
        const s = state.block_summaries[id];
        if (!s) continue;
        const txt = (s.text || '').replace(/[<>&]/g, c =>
          ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
        html += '<li><a href="#' + id + '" data-jump="' + id + '">p' + s.page + '</a>' +
                '<span class="text">' + txt + '</span></li>';
      }
      if (members.length > 50) {
        html += '<li class="panel-empty">… ' + (members.length - 50) + ' more</li>';
      }
      html += '</ul>';
    }

    panelBody.innerHTML = html;

    // Wire action buttons.
    panelBody.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const act = btn.dataset.action;
        if (act === 'suppress') {
          members.forEach(id => saved.suppressed[id] = true);
        } else if (act === 'unsuppress') {
          members.forEach(id => delete saved.suppressed[id]);
        } else if (act === 'tag') {
          const tag = btn.dataset.tag;
          members.forEach(id => saved.tags[id] = tag);
        } else if (act === 'untag') {
          members.forEach(id => delete saved.tags[id]);
        }
        save();
        applyState();
        renderTab();
      });
    });

    panelBody.querySelectorAll('a[data-jump]').forEach(link => {
      link.addEventListener('click', e => {
        const id = link.dataset.jump;
        const el = blockById[id];
        if (el) {
          e.preventDefault();
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          openPanel(id);
        }
      });
    });
  }

  // --- event wiring -------------------------------------------------------

  document.addEventListener('click', e => {
    // Don't intercept clicks on links inside blocks.
    if (e.target.closest('a')) return;
    if (e.target.closest('[data-no-select]')) return;
    const block = e.target.closest('.block');
    if (block) {
      openPanel(block.dataset.blockId);
      return;
    }
    if (!e.target.closest('.panel') && !e.target.closest('.rule-counters')) {
      // click outside everything — leave panel as-is, just clear selection
    }
  });

  panelTabs.forEach(btn => {
    btn.addEventListener('click', () => {
      panelTabs.forEach(b => b.classList.toggle('active', b === btn));
      activeTab = btn.dataset.tab;
      renderTab();
    });
  });

  document.getElementById('panel-close').addEventListener('click', closePanel);

  document.getElementById('btn-export').addEventListener('click', () => {
    const yaml = exportYaml();
    const blob = new Blob([yaml], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = state.doc_id + '.rules.yaml';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  document.getElementById('btn-reset').addEventListener('click', () => {
    if (!confirm('Clear all suppress/tag decisions for this document?')) return;
    saved = { suppressed: {}, tags: {} };
    localStorage.removeItem(STORAGE_KEY);
    applyState();
    if (activeBlockId) renderTab();
  });

  function exportYaml() {
    const lines = ['doc_id: ' + state.doc_id];
    const suppressed = Object.keys(saved.suppressed).sort();
    lines.push('suppress:');
    if (suppressed.length === 0) {
      lines.push('  block_ids: []');
    } else {
      lines.push('  block_ids:');
      suppressed.forEach(id => lines.push('    - ' + id));
    }
    const byTag = {};
    Object.entries(saved.tags).forEach(([id, tag]) => {
      (byTag[tag] = byTag[tag] || []).push(id);
    });
    lines.push('tags:');
    const tagKeys = Object.keys(byTag).sort();
    if (tagKeys.length === 0) {
      lines.push('  {}');
    } else {
      tagKeys.forEach(tag => {
        lines.push('  ' + tag + ':');
        byTag[tag].sort().forEach(id => lines.push('    - ' + id));
      });
    }
    return lines.join('\n') + '\n';
  }

  applyState();
})();
"""

