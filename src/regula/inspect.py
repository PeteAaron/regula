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

    return _PAGE.format(
        title=html.escape(document.title),
        doc_id=html.escape(document.doc_id),
        meta=_render_doc_meta(document),
        toc=_render_toc(toc.entries),
        chunks=_render_chunks(chunks, refs_index, glossary_by_term),
        deferred=_render_deferred(deferred),
    )


def write_preview(output_dir: Path, out_path: Path | None = None) -> Path:
    """Render and write the HTML, returning the path written."""
    target = out_path or output_dir / "preview.html"
    target.write_text(render_preview(output_dir), encoding="utf-8")
    return target


# --- doc metadata block --------------------------------------------------


def _render_doc_meta(doc: DocumentMeta) -> str:
    rows = [
        ("doc_id", doc.doc_id),
        ("title", doc.title),
        ("edition", doc.edition),
        ("jurisdiction", doc.jurisdiction),
        ("legal status", doc.legal_status),
        ("pages", str(doc.page_count)),
        ("chunks", str(doc.chunk_count)),
        (
            "pipeline",
            "<span class='ok'>passed</span>"
            if doc.pipeline_passed
            else "<span class='fail'>FAILED</span>",
        ),
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
    return f"<table class='doc-meta'>{body}</table>"


# --- TOC sidebar ---------------------------------------------------------


def _render_toc(entries: list[TOCEntry]) -> str:
    if not entries:
        return "<p class='empty'>(no TOC entries)</p>"
    return _render_toc_list(entries)


def _render_toc_list(entries: list[TOCEntry]) -> str:
    items: list[str] = []
    for e in entries:
        item = (
            f"<li><a href='#{html.escape(e.heading_chunk_id)}'>"
            f"{html.escape(e.label)}</a>"
            f"<span class='oi'>oi=[{e.first_order_index}..{e.last_order_index}]</span>"
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
) -> str:
    return "\n".join(
        _render_chunk(c, refs_index, glossary_by_term) for c in chunks
    )


def _render_chunk(
    chunk: Chunk,
    refs_index: ReferencesIndex,
    glossary_by_term: dict[str, GlossaryEntry],
) -> str:
    text_html = _render_chunk_text(chunk, glossary_by_term)
    if chunk.type is ChunkType.SECTION_HEADING:
        level = max(2, min((chunk.heading_level or 1) + 1, 6))
        body = f"<h{level} class='text'>{text_html}</h{level}>"
    elif chunk.type is ChunkType.GLOSSARY_ENTRY:
        body = f"<div class='text glossary'>{text_html}</div>"
    else:
        body = f"<p class='text'>{text_html}</p>"

    meta_strip = _render_chunk_meta_strip(chunk, refs_index)
    json_dump = html.escape(chunk.model_dump_json(indent=2))

    return (
        f"<article class='chunk chunk-{chunk.type.value}' "
        f"id='{html.escape(chunk.chunk_id)}'>"
        f"{body}{meta_strip}"
        f"<details class='json'><summary>show JSON</summary>"
        f"<pre>{json_dump}</pre></details>"
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


def _render_chunk_meta_strip(chunk: Chunk, refs_index: ReferencesIndex) -> str:
    pages = (
        f"p{chunk.page_start}"
        if chunk.page_start == chunk.page_end
        else f"p{chunk.page_start}–{chunk.page_end}"
    )
    parts = [
        f"#{chunk.order_index}",
        f"<code>{html.escape(chunk.chunk_id)}</code>",
        f"<span class='type-tag'>{chunk.type.value}</span>",
        pages,
    ]
    if chunk.section_path:
        parts.append(
            "<span class='sec'>"
            + html.escape(" › ".join(chunk.section_path))
            + "</span>"
        )

    n_refs = len(chunk.references_out)
    if n_refs:
        n_internal = sum(1 for r in chunk.references_out if r.target_chunk_id)
        n_external = sum(1 for r in chunk.references_out if r.external_id)
        n_unresolved = n_refs - n_internal - n_external
        ref_summary = f"refs out: {n_refs}"
        details: list[str] = []
        if n_internal:
            details.append(f"{n_internal} internal")
        if n_external:
            details.append(f"{n_external} external")
        if n_unresolved:
            details.append(f"<span class='fail'>{n_unresolved} unresolved</span>")
        if details:
            ref_summary += " (" + ", ".join(details) + ")"
        parts.append(ref_summary)

    backlinks = refs_index.by_target.get(chunk.chunk_id, [])
    if backlinks:
        links = ", ".join(
            f"<a href='#{html.escape(b.source_chunk_id)}'>"
            f"{html.escape(b.source_chunk_id.rsplit('-', 1)[-1])}</a>"
            for b in backlinks[:6]
        )
        more = f" +{len(backlinks) - 6}" if len(backlinks) > 6 else ""
        parts.append(f"backlinks: {links}{more}")

    if chunk.defined_terms_used:
        parts.append("terms: " + ", ".join(map(html.escape, chunk.defined_terms_used)))

    if chunk.meta.source_spans:
        span_count = len(chunk.meta.source_spans)
        first_bbox = chunk.meta.source_spans[0].bbox
        bbox_str = ", ".join(f"{c:.0f}" for c in first_bbox)
        parts.append(f"spans: {span_count} ({bbox_str}…)")

    return "<div class='meta'>" + " · ".join(parts) + "</div>"


# --- deferred features footer -------------------------------------------


def _render_deferred(deferred: DeferredFeatureList) -> str:
    if not deferred.features:
        return "<p class='empty'>(none — pipeline is complete)</p>"
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
    return f"<ul class='deferred-list'>{''.join(items)}</ul>"


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
  --c-text: #222;
  --c-muted: #6a6a6a;
  --c-border: #d0d0d0;
  --c-bg: #ffffff;
  --c-card: #fafafa;
  --c-heading: #1f3a8a;
  --c-paragraph-border: #888;
  --c-glossary: #2a8a3e;
  --c-glossary-bg: #eef7ef;
  --c-target: #fff5cc;
  --c-target-border: #e6b800;
  --c-ref-internal: #1755a8;
  --c-ref-external: #2a8a3e;
  --c-fail: #b91c1c;
  --c-ok: #15803d;
  --c-term-bg: #fff2a8;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  margin: 0; color: var(--c-text); background: var(--c-bg); line-height: 1.55; }}
header {{ padding: 20px 32px; border-bottom: 1px solid var(--c-border); background: #f7f7f7; }}
header h1 {{ margin: 0 0 8px 0; font-size: 1.4em; }}
header .doc-id {{ color: var(--c-muted); font-family: ui-monospace, monospace; }}
table.doc-meta {{ font-size: 0.85em; margin-top: 12px; border-collapse: collapse; }}
table.doc-meta th {{ text-align: left; padding: 1px 12px 1px 0; font-weight: normal; color: var(--c-muted); vertical-align: top; }}
table.doc-meta td {{ padding: 1px 0; font-family: ui-monospace, monospace; }}
.ok {{ color: var(--c-ok); font-weight: 600; }}
.fail {{ color: var(--c-fail); font-weight: 600; }}
.layout {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 32px; padding: 24px 32px; max-width: 1400px; margin: 0 auto; }}
nav.toc-nav {{ position: sticky; top: 16px; align-self: start; max-height: calc(100vh - 32px); overflow-y: auto; font-size: 0.88em; }}
nav.toc-nav h3 {{ margin: 0 0 8px 0; font-size: 0.95em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-muted); }}
ul.toc {{ list-style: none; padding-left: 12px; margin: 0; }}
ul.toc > li {{ margin: 6px 0; }}
ul.toc a {{ color: var(--c-text); text-decoration: none; }}
ul.toc a:hover {{ text-decoration: underline; }}
ul.toc .oi {{ display: block; color: var(--c-muted); font-family: ui-monospace, monospace; font-size: 0.85em; }}
main {{ min-width: 0; }}
.chunk {{ margin-bottom: 14px; padding: 12px 16px; border-left: 3px solid var(--c-border);
  background: var(--c-card); border-radius: 0 4px 4px 0; }}
.chunk:target {{ background: var(--c-target); border-left-color: var(--c-target-border); }}
.chunk-section_heading {{ border-left-color: var(--c-heading); background: #eef2fb; }}
.chunk-paragraph {{ border-left-color: var(--c-paragraph-border); }}
.chunk-glossary_entry {{ border-left-color: var(--c-glossary); background: var(--c-glossary-bg); }}
.chunk .text {{ margin: 0 0 8px 0; }}
.chunk h2.text, .chunk h3.text, .chunk h4.text, .chunk h5.text, .chunk h6.text {{ margin: 0 0 8px 0; }}
.meta {{ font-size: 0.78em; color: var(--c-muted); font-family: ui-monospace, monospace;
  display: flex; flex-wrap: wrap; gap: 0 0.5em; }}
.meta::before {{ content: ""; }}
.meta > * {{ }}
.meta code {{ background: #ececec; padding: 1px 5px; border-radius: 3px; font-size: 0.95em; }}
.meta .sec {{ font-style: italic; }}
.meta .type-tag {{ background: #ddd; padding: 0 6px; border-radius: 10px; color: #333; }}
.meta a {{ color: var(--c-ref-internal); }}
details.json {{ margin-top: 8px; font-size: 0.82em; }}
details.json summary {{ cursor: pointer; color: var(--c-muted); }}
details.json pre {{ background: #f0f0f0; padding: 10px; border-radius: 3px;
  overflow-x: auto; font-size: 0.9em; }}
.ref {{ text-decoration: underline; text-decoration-thickness: 1px; }}
.ref-internal {{ color: var(--c-ref-internal); }}
.ref-external {{ color: var(--c-ref-external); }}
.ref-unresolved {{ color: var(--c-fail); text-decoration-style: wavy; }}
.term {{ background: var(--c-term-bg); padding: 0 2px; border-radius: 2px; cursor: help;
  border-bottom: 1px dotted #d4a017; }}
section.deferred-section {{ margin-top: 48px; padding: 20px 24px; background: #f5f5f5;
  border-radius: 4px; font-size: 0.92em; }}
section.deferred-section h3 {{ margin: 0 0 12px 0; color: var(--c-muted); text-transform: uppercase;
  font-size: 0.9em; letter-spacing: 0.04em; }}
ul.deferred-list {{ padding-left: 20px; margin: 0; }}
ul.deferred-list li {{ margin: 8px 0; }}
ul.deferred-list .phase {{ display: inline-block; margin-left: 8px; padding: 1px 8px;
  background: #ddd; border-radius: 10px; font-size: 0.85em; color: #555; }}
ul.deferred-list .observed {{ display: inline-block; margin-left: 8px;
  background: #fff2a8; padding: 1px 6px; border-radius: 3px; font-family: ui-monospace, monospace;
  font-size: 0.85em; }}
.empty {{ color: var(--c-muted); font-style: italic; }}
</style>
</head>
<body>
<header>
<h1>regula preview · {title}</h1>
<div class="doc-id">{doc_id}</div>
{meta}
</header>
<div class="layout">
<nav class="toc-nav">
<h3>Contents</h3>
{toc}
</nav>
<main>
{chunks}
<section class="deferred-section">
<h3>Deferred capabilities</h3>
{deferred}
</section>
</main>
</div>
</body>
</html>
"""
