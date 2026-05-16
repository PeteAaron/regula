"""Diagnostic HTML preview of a pipeline run.

Reads the on-disk artifacts of a completed run and produces a single,
self-contained ``preview.html`` that lays the document back out in
order_index sequence with every extracted property visible. The intent
is *diagnosis*, not presentation — so the HTML deliberately surfaces:

* the raw chunk_id, type, page range, and section path under every chunk;
* internal references rendered as clickable anchor links that jump to
  the target chunk (so reading-order and resolution can be sanity-checked
  by clicking through);
* external references styled differently from internal so the difference
  is visible at a glance, and unresolved internals styled differently
  again (red wavy underline) to flag a gap;
* defined glossary terms highlighted with a tooltip showing the
  definition — confirms that ``defined_terms_used`` back-fill worked;
* a ``show JSON`` toggle under every chunk that reveals the full
  Pydantic dump for the moments when "looks right" isn't enough;
* a sidebar TOC mirroring ``toc.json`` with the [first, last]
  order_index window of each entry;
* the deferred-features list folded into the footer so reviewers can
  see what isn't (yet) covered for this run.

No JS, no external CSS — single file, opens in any browser.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from regula.schemas import (
    TOC,
    Chunk,
    ChunkType,
    DeferredFeatureList,
    DocumentMeta,
    Glossary,
    GlossaryEntry,
    Reference,
    ReferencesIndex,
    TOCEntry,
)


def render_preview(output_dir: Path) -> str:
    """Read all artifacts from ``output_dir`` and return the HTML string."""
    chunks = _load_chunks(output_dir / "chunks.jsonl")
    toc = TOC.model_validate_json((output_dir / "toc.json").read_text(encoding="utf-8"))
    refs_index = ReferencesIndex.model_validate_json(
        (output_dir / "references_index.json").read_text(encoding="utf-8")
    )
    glossary = Glossary.model_validate_json(
        (output_dir / "glossary.json").read_text(encoding="utf-8")
    )
    document = DocumentMeta.model_validate_json(
        (output_dir / "document.json").read_text(encoding="utf-8")
    )
    deferred = DeferredFeatureList.model_validate_json(
        (output_dir / "deferred.json").read_text(encoding="utf-8")
    )

    glossary_by_term = {e.normalised_term: e for e in glossary.entries}

    status_pill = (
        "<span class='ok'>passed</span>"
        if document.pipeline_passed
        else "<span class='fail'>FAILED</span>"
    )
    summary_line = (
        f"{document.page_count} pages · {document.chunk_count} chunks · "
        f"validation {status_pill}"
    )

    return _PAGE.format(
        title=html.escape(document.title),
        doc_id=html.escape(document.doc_id),
        summary=summary_line,
        meta=_render_doc_meta(document),
        toc=_render_toc(toc.entries),
        chunks=_render_chunks(chunks, refs_index, glossary_by_term, document.doc_id),
        deferred=_render_deferred(deferred),
    )


def write_preview(output_dir: Path, out_path: Path | None = None) -> Path:
    """Render and write the HTML, returning the path written."""
    target = out_path or output_dir / "preview.html"
    target.write_text(render_preview(output_dir), encoding="utf-8")
    return target


# --- doc metadata block --------------------------------------------------


def _render_doc_meta(doc: DocumentMeta) -> str:
    """Detailed doc metadata — collapsed by default."""
    rows = [
        ("edition", html.escape(doc.edition)),
        ("jurisdiction", html.escape(doc.jurisdiction)),
        ("legal status", html.escape(doc.legal_status)),
        ("regula", html.escape(doc.regula_version)),
        (
            "parsers",
            html.escape(", ".join(f"{k}@{v}" for k, v in doc.parser_versions.items()))
            or "—",
        ),
        ("source PDF", html.escape(doc.source_pdf)),
        ("pdf sha", html.escape(doc.source_pdf_sha256[:24]) + "…"),
        ("config sha", html.escape(doc.config_sha256[:24]) + "…"),
        ("generated at", html.escape(doc.generated_at.isoformat(timespec="seconds"))),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in rows
    )
    return (
        "<details class='doc-meta-details'>"
        "<summary>more details</summary>"
        f"<table class='doc-meta'>{body}</table>"
        "</details>"
    )


# --- TOC sidebar ---------------------------------------------------------


def _render_toc(entries: list[TOCEntry]) -> str:
    if not entries:
        return "<p class='empty'>(no TOC entries)</p>"
    return _render_toc_list(entries)


def _render_toc_list(entries: list[TOCEntry]) -> str:
    items: list[str] = []
    for e in entries:
        item = (
            f"<li><a href='#{html.escape(e.heading_chunk_id)}' "
            f"title='order_index [{e.first_order_index}..{e.last_order_index}]'>"
            f"{html.escape(e.label)}</a>"
        )
        if e.children:
            item += _render_toc_list(e.children)
        item += "</li>"
        items.append(item)
    return "<ul class='toc'>" + "".join(items) + "</ul>"


# --- chunk rendering -----------------------------------------------------


def _render_chunks(
    chunks: list[Chunk],
    refs_index: ReferencesIndex,
    glossary_by_term: dict[str, GlossaryEntry],
    doc_id: str,
) -> str:
    return "\n".join(
        _render_chunk(c, refs_index, glossary_by_term, doc_id) for c in chunks
    )


def _render_chunk(
    chunk: Chunk,
    refs_index: ReferencesIndex,
    glossary_by_term: dict[str, GlossaryEntry],
    doc_id: str,
) -> str:
    text_html = _render_chunk_text(chunk, glossary_by_term)
    if chunk.type is ChunkType.SECTION_HEADING:
        level = max(2, min((chunk.heading_level or 1) + 1, 6))
        body = f"<h{level} class='text'>{text_html}</h{level}>"
    elif chunk.type is ChunkType.GLOSSARY_ENTRY:
        body = f"<div class='text glossary'>{text_html}</div>"
    else:
        body = f"<p class='text'>{text_html}</p>"

    meta_strip = _render_chunk_meta_strip(chunk, refs_index, doc_id)
    json_dump = html.escape(chunk.model_dump_json(indent=2))

    return (
        f"<article class='chunk chunk-{chunk.type.value}' "
        f"id='{html.escape(chunk.chunk_id)}'>"
        f"{body}{meta_strip}"
        f"<pre class='json'>{json_dump}</pre>"
        f"</article>"
    )


def _render_chunk_text(
    chunk: Chunk, glossary_by_term: dict[str, GlossaryEntry]
) -> str:
    """Render chunk text with inline reference links + term tooltips.

    Markers are (start, end, replacement_html). When markers overlap we
    keep the earliest-starting one and drop the rest — preserves the
    original text and avoids producing invalid HTML."""
    text = chunk.text
    markers: list[tuple[int, int, str]] = []
    markers.extend(_reference_markers(chunk.references_out, text))
    markers.extend(_term_markers(chunk.defined_terms_used, text, glossary_by_term))

    markers.sort(key=lambda m: (m[0], m[1]))

    out: list[str] = []
    cursor = 0
    for start, end, repl in markers:
        if start < cursor:
            continue
        out.append(html.escape(text[cursor:start]))
        out.append(repl)
        cursor = end
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def _reference_markers(refs: list[Reference], text: str) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    for ref in refs:
        span = ref.source_span
        if span is None:
            continue
        start, end = span.text_offset_start, span.text_offset_end
        if start is None or end is None:
            continue
        if not (0 <= start < end <= len(text)):
            continue
        label = text[start:end]
        if ref.target_chunk_id:
            repl = (
                f"<a class='ref ref-internal' href='#{html.escape(ref.target_chunk_id)}' "
                f"title='→ {html.escape(ref.target_chunk_id)}'>"
                f"{html.escape(label)}</a>"
            )
        elif ref.external_id:
            repl = (
                f"<span class='ref ref-external' "
                f"title='external: {html.escape(ref.external_id)}'>"
                f"{html.escape(label)}</span>"
            )
        else:
            repl = (
                f"<span class='ref ref-unresolved' title='unresolved'>"
                f"{html.escape(label)}</span>"
            )
        markers.append((start, end, repl))
    return markers


def _term_markers(
    defined_terms: list[str],
    text: str,
    glossary_by_term: dict[str, GlossaryEntry],
) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    for norm in defined_terms:
        entry = glossary_by_term.get(norm)
        if entry is None:
            continue
        pattern = re.compile(
            r"\b" + re.escape(entry.term) + r"\b", flags=re.IGNORECASE
        )
        for m in pattern.finditer(text):
            tooltip = html.escape(entry.definition[:200])
            repl = (
                f"<span class='term' title='{tooltip}'>"
                f"{html.escape(m.group(0))}</span>"
            )
            markers.append((m.start(), m.end(), repl))
    return markers


def _short_id(chunk_id: str, doc_id: str) -> str:
    """Strip the ``<doc_id>-`` prefix off a chunk_id for display."""
    prefix = f"{doc_id}-"
    return chunk_id[len(prefix):] if chunk_id.startswith(prefix) else chunk_id


def _render_chunk_meta_strip(
    chunk: Chunk, refs_index: ReferencesIndex, doc_id: str
) -> str:
    """Compact one-line metadata. Hidden by default; revealed on hover or
    via the "Show metadata" toggle at the top. Full chunk_id lives in the
    code tag's title attribute so hover-tooltip still gives the long form."""
    pages = (
        f"p{chunk.page_start}"
        if chunk.page_start == chunk.page_end
        else f"p{chunk.page_start}–{chunk.page_end}"
    )
    short = _short_id(chunk.chunk_id, doc_id)
    parts = [
        f"<span class='oi'>#{chunk.order_index}</span>",
        f"<code title='{html.escape(chunk.chunk_id)}'>{html.escape(short)}</code>",
        f"<span class='type-tag'>{chunk.type.value}</span>",
        pages,
    ]

    n_refs = len(chunk.references_out)
    if n_refs:
        n_internal = sum(1 for r in chunk.references_out if r.target_chunk_id)
        n_external = sum(1 for r in chunk.references_out if r.external_id)
        n_unresolved = n_refs - n_internal - n_external
        details: list[str] = []
        if n_internal:
            details.append(f"{n_internal}→")
        if n_external:
            details.append(f"{n_external}⊕")
        if n_unresolved:
            details.append(f"<span class='fail'>{n_unresolved}?</span>")
        parts.append(
            "<span class='refs' title='refs out: internal→ external⊕ unresolved?'>"
            + " ".join(details)
            + "</span>"
        )

    backlinks = refs_index.by_target.get(chunk.chunk_id, [])
    if backlinks:
        n = len(backlinks)
        links = ", ".join(
            f"<a href='#{html.escape(b.source_chunk_id)}'>"
            f"{html.escape(_short_id(b.source_chunk_id, doc_id))}</a>"
            for b in backlinks[:4]
        )
        more = f" +{n - 4}" if n > 4 else ""
        parts.append(
            f"<span class='backlinks' title='cited by {n} chunk(s)'>← {links}{more}</span>"
        )

    if chunk.defined_terms_used:
        parts.append(
            "<span class='terms' title='glossary terms used'>"
            + html.escape(", ".join(chunk.defined_terms_used))
            + "</span>"
        )

    return "<div class='meta'>" + " · ".join(parts) + "</div>"


# --- deferred features footer -------------------------------------------


def _render_deferred(deferred: DeferredFeatureList) -> str:
    if not deferred.features:
        return (
            "<details class='deferred-details'>"
            "<summary>Deferred capabilities (0)</summary>"
            "<p class='empty'>(none — pipeline is complete)</p>"
            "</details>"
        )
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


def _load_chunks(path: Path) -> list[Chunk]:
    return [
        Chunk.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- HTML template (inline CSS, no JS) ----------------------------------


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
  --c-heading: #1f3a8a;
  --c-glossary: #2a8a3e;
  --c-target: #fff5cc;
  --c-target-border: #e6b800;
  --c-ref-internal: #1755a8;
  --c-ref-external: #2a8a3e;
  --c-fail: #b91c1c;
  --c-ok: #15803d;
  --c-term-bg: #fff8c5;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  margin: 0; color: var(--c-text); background: var(--c-bg);
  line-height: 1.65; font-size: 16px;
}}
.ok {{ color: var(--c-ok); font-weight: 600; }}
.fail {{ color: var(--c-fail); font-weight: 600; }}

/* --- header --- */
header.page-head {{
  padding: 18px 32px 12px; border-bottom: 1px solid var(--c-border);
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
}}
header.page-head h1 {{ margin: 0; font-size: 1.15em; font-weight: 600; }}
header.page-head .doc-id {{
  color: var(--c-muted); font-family: ui-monospace, monospace; font-size: 0.88em;
}}
header.page-head .summary {{ color: var(--c-muted); font-size: 0.9em; margin-left: auto; }}
.doc-meta-details {{ width: 100%; margin-top: 6px; }}
.doc-meta-details summary {{
  cursor: pointer; color: var(--c-muted); font-size: 0.82em; user-select: none;
  display: inline-block; padding: 2px 0;
}}
.doc-meta-details summary:hover {{ color: var(--c-text); }}
table.doc-meta {{ font-size: 0.82em; margin: 8px 0 0; border-collapse: collapse; }}
table.doc-meta th {{
  text-align: left; padding: 1px 14px 1px 0; font-weight: normal;
  color: var(--c-muted); vertical-align: top;
}}
table.doc-meta td {{ padding: 1px 0; font-family: ui-monospace, monospace; }}

/* --- view-mode toggle bar --- */
.view-toggles {{
  position: sticky; top: 0; z-index: 10;
  padding: 8px 32px; border-bottom: 1px solid var(--c-border);
  background: rgba(255,255,255,0.95); backdrop-filter: blur(4px);
  font-size: 0.85em; color: var(--c-muted); display: flex; gap: 16px;
}}
.view-toggles label {{ cursor: pointer; user-select: none; }}
.view-toggles input {{ vertical-align: -1px; margin-right: 4px; }}
.view-toggles .legend {{ margin-left: auto; font-size: 0.92em; color: var(--c-faint); }}
.view-toggles .legend .ref-internal,
.view-toggles .legend .ref-external,
.view-toggles .legend .term {{ font-size: 0.95em; }}

/* --- layout --- */
.layout {{
  display: grid; grid-template-columns: 240px minmax(0, 760px);
  gap: 48px; padding: 28px 32px 80px; max-width: 1200px; margin: 0 auto;
}}
nav.toc-nav {{
  position: sticky; top: 56px; align-self: start;
  max-height: calc(100vh - 72px); overflow-y: auto; font-size: 0.9em;
}}
nav.toc-nav h3 {{
  margin: 0 0 10px 0; font-size: 0.78em; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--c-muted);
}}
ul.toc {{ list-style: none; padding-left: 12px; margin: 0; }}
ul.toc ul.toc {{ padding-left: 14px; margin-top: 4px; }}
ul.toc > li {{ margin: 5px 0; }}
ul.toc a {{ color: var(--c-text); text-decoration: none; }}
ul.toc a:hover {{ color: var(--c-ref-internal); text-decoration: underline; }}

/* --- chunks --- */
main {{ min-width: 0; }}
.chunk {{
  position: relative; margin: 0; padding: 4px 0 4px 16px;
  border-left: 2px solid transparent;
  transition: border-color 0.15s ease;
}}
.chunk:hover {{ border-left-color: var(--c-border); }}
.chunk:target {{
  border-left-color: var(--c-target-border);
  background: linear-gradient(to right, var(--c-target), transparent 80%);
}}
.chunk-section_heading {{ margin-top: 28px; }}
.chunk-section_heading:first-child {{ margin-top: 0; }}
.chunk-section_heading:hover {{ border-left-color: var(--c-heading); }}
.chunk-glossary_entry:hover {{ border-left-color: var(--c-glossary); }}
.chunk .text {{ margin: 0; }}
.chunk h2.text {{ font-size: 1.25em; font-weight: 600; margin: 12px 0 4px; color: #111; }}
.chunk h3.text {{ font-size: 1.08em; font-weight: 600; margin: 10px 0 2px; color: #222; }}
.chunk h4.text {{ font-size: 1em; font-weight: 600; margin: 8px 0 2px; }}
.chunk h5.text, .chunk h6.text {{ font-size: 0.95em; font-weight: 600; margin: 6px 0 2px; }}
.chunk .glossary {{ }}
.chunk p.text {{ margin: 6px 0; }}

/* --- meta strip (hidden by default; shown on hover or via toggle) --- */
.meta {{
  display: none;
  font-size: 0.74em; color: var(--c-muted); font-family: ui-monospace, monospace;
  margin-top: 4px; padding-top: 2px;
  flex-wrap: wrap; gap: 2px 8px; align-items: baseline;
}}
.chunk:hover > .meta {{ display: flex; }}
.meta .oi {{ color: var(--c-faint); }}
.meta code {{
  background: #f0f0f0; padding: 0 5px; border-radius: 3px; font-size: 0.95em;
  color: #444;
}}
.meta .type-tag {{
  background: #eaeaea; padding: 0 6px; border-radius: 10px; color: #555;
  font-family: -apple-system, sans-serif; font-size: 0.95em;
}}
.meta a {{ color: var(--c-ref-internal); text-decoration: none; }}
.meta a:hover {{ text-decoration: underline; }}
.meta .refs {{ }}
.meta .backlinks {{ }}
.meta .terms {{ font-style: italic; }}

/* --- JSON block (hidden unless toggle on) --- */
.chunk .json {{
  display: none;
  margin: 6px 0 0; padding: 8px 10px; background: #fafafa;
  border: 1px solid var(--c-border); border-radius: 3px;
  font-size: 0.78em; line-height: 1.4; overflow-x: auto; color: #333;
}}

/* --- global toggles --- */
body:has(#tog-meta:checked) .meta {{ display: flex; }}
body:has(#tog-json:checked) .chunk .json {{ display: block; }}

/* --- inline refs and terms --- */
.ref {{ text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
.ref-internal {{ color: var(--c-ref-internal); }}
.ref-external {{ color: var(--c-ref-external); }}
.ref-unresolved {{ color: var(--c-fail); text-decoration-style: wavy; }}
.term {{
  background: var(--c-term-bg); padding: 0 2px; border-radius: 2px; cursor: help;
  border-bottom: 1px dotted #b89400;
}}

/* --- deferred capabilities --- */
.deferred-details {{ margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--c-border); }}
.deferred-details summary {{
  cursor: pointer; color: var(--c-muted); font-size: 0.85em; user-select: none;
  text-transform: uppercase; letter-spacing: 0.04em;
}}
.deferred-details summary:hover {{ color: var(--c-text); }}
ul.deferred-list {{
  padding-left: 0; list-style: none; margin: 12px 0 0; font-size: 0.88em;
}}
ul.deferred-list li {{ margin: 10px 0; padding-left: 0; color: var(--c-muted); }}
ul.deferred-list strong {{ color: var(--c-text); font-weight: 600; }}
ul.deferred-list .phase {{
  display: inline-block; margin-left: 8px; padding: 0 7px; background: #eee;
  border-radius: 10px; font-size: 0.88em; color: #666;
}}
ul.deferred-list .observed {{
  display: inline-block; margin-left: 6px; padding: 0 6px;
  background: var(--c-term-bg); border-radius: 3px;
  font-family: ui-monospace, monospace; font-size: 0.88em; color: #6b4500;
}}
.empty {{ color: var(--c-muted); font-style: italic; }}
</style>
</head>
<body>
<header class="page-head">
<h1>{title}</h1>
<span class="doc-id">{doc_id}</span>
<span class="summary">{summary}</span>
{meta}
</header>
<div class="view-toggles">
<label><input type="checkbox" id="tog-meta"> Show metadata</label>
<label><input type="checkbox" id="tog-json"> Show JSON</label>
<span class="legend">hover any chunk for its metadata · <span class="ref-internal">internal ref</span> · <span class="ref-external">external ref</span> · <span class="term">defined term</span></span>
</div>
<div class="layout">
<nav class="toc-nav">
<h3>Contents</h3>
{toc}
</nav>
<main>
{chunks}
{deferred}
</main>
</div>
</body>
</html>
"""
