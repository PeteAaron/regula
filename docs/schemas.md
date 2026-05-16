# The regula output contract — a reader's guide

This document explains, in plain English, every shape of data the pipeline produces and every knob you can turn in a per-document config. It's the companion to `src/regula/schemas.py` (where the contract is defined in code) and `schemas/*.schema.json` (where it's exported for downstream consumers).

If you only have time for one section, read [The mental model](#the-mental-model) and [Chunk](#chunk--the-heart-of-the-contract).

---

## Table of contents

1. [What this document is for](#what-this-document-is-for)
2. [The mental model](#the-mental-model)
3. [The output directory](#the-output-directory)
4. [Chunk — the heart of the contract](#chunk--the-heart-of-the-contract)
5. [Chunk types](#chunk-types)
6. [Sourcing — where every chunk came from](#sourcing--where-every-chunk-came-from)
7. [References — how chunks connect](#references--how-chunks-connect)
8. [Table of contents — the section index](#table-of-contents--the-section-index)
9. [Glossary — defined terms](#glossary--defined-terms)
10. [Pipeline run records](#pipeline-run-records)
11. [Configuration](#configuration)
12. ["When do I use X?" — a cheat sheet](#when-do-i-use-x--a-cheat-sheet)
13. [Extending the schema](#extending-the-schema)
14. [Glossary of terms used in this doc](#glossary-of-terms-used-in-this-doc)

---

## What this document is for

`regula` reads a regulatory PDF and turns it into a set of structured files. Anyone using those files — to power a search index, a knowledge graph, a UI, a chatbot, a compliance check — needs to know **what each field means, when it's filled in, and which field to use for which question**.

The contract is small, but it has surface area. Three quick examples of what this doc answers:

- "I want every paragraph in §2.4 — which field do I query?" → `parent_section_id`, *not* `section_path` (which is for display only).
- "I want to highlight in the PDF where a chunk came from" → `meta.source_spans` gives you the exact pages and bounding boxes.
- "I want to add a per-document tag like 'applies to dwellinghouses'" → put it in `attributes`, not in a new top-level field.

If you're writing code that *consumes* regula output, this doc is your reference. If you're writing a *new pipeline stage*, the same shapes apply but you also need to read `AGENTS.md` and `docs/approach.md`.

---

## The mental model

A regula run turns one PDF into a **graph**:

- **Nodes** are *chunks*. A chunk is one self-contained, addressable piece of the document: a numbered paragraph, a heading, a table, a diagram, a caption, a glossary entry. Each chunk has a stable ID, a chunk of text, the exact PDF region(s) it came from, and some metadata.
- **Edges** are *relationships between chunks*:
  - **Section containment** — every chunk knows which section heading it lives under.
  - **Cross-references** — paragraph 2.4 says "see paragraph 5.6" → an edge from 2.4 to 5.6.
  - **Asset linkage** — a caption knows which figure it's captioning; the figure knows which caption is its caption.
  - **Defined-term usage** — a paragraph that contains the word "compartmentation" has an edge to the glossary entry for that term.

Three orthogonal invariants run through everything:

1. **Reading order is a total order.** Every chunk has an `order_index` from 0 to N-1. Walk them in that order and you get the document the way a reader would read it: text, tables, captions, figures, in the right place. This is the *only* authoritative ordering — never sort by page+bbox at query time.
2. **Sourcing is exact.** Every chunk carries one or more `SourceSpan` records, each pointing at a specific page region and the exact substring of the chunk's text that region produced. So "exactly which pixels of the PDF produced this character of this chunk?" is always answerable.
3. **Edges are emitted on the source, indexed on the target.** A chunk lists its outbound references on itself (`references_out`). Backlinks (who points at *me*?) live in a separate inverted index — they're derived, not authoritative.

That's it. Everything else is just convenient pre-computation of useful queries (the TOC giving you order-index windows per section, the glossary giving you a lookup table, the validation report giving you health metrics).

---

## The output directory

A successful run produces this layout under `output/<doc_id>/`:

```
output/<doc_id>/
├── document.json            ← top-level metadata and run report
├── toc.json                 ← table of contents
├── chunks.jsonl             ← one chunk per line (the main payload)
├── glossary.json            ← defined-term lookup (empty if doc has no glossary)
├── references_index.json    ← inverted backlink index (KG queries)
├── assets/                  ← extracted figures and tables (binary + JSON)
├── intermediate/            ← per-stage debug artifacts
├── validation_report.json   ← health metrics and pass/fail verdict
└── run.log                  ← structured log of the run
```

Which to read first depends on your goal:

- **Diagnosing a run that didn't pass?** Start with `validation_report.json`.
- **Building a UI or a search index?** Start with `chunks.jsonl` and `toc.json`.
- **Building a knowledge graph?** Start with `chunks.jsonl` for nodes, `references_index.json` for backlinks, and `toc.json` for hierarchical context.
- **Checking reproducibility?** `document.json` carries `source_pdf_sha256` and `config_sha256` — two runs with matching hashes should produce identical output.

---

## Chunk — the heart of the contract

A chunk is the unit of everything. Every file in the output is either a chunk, a list of chunks, or an index over chunks.

### Identity

| Field | What it is |
|---|---|
| `chunk_id` | A globally unique string of the form `<doc_id>-<type>-<identifier>`. For example `ADB1-2022-paragraph-2.4`. Stable across runs on the same PDF + config. |
| `doc_id` | The short identifier of the document the chunk belongs to (matches `Config.doc_id`). |
| `type` | What kind of element this chunk is. See [Chunk types](#chunk-types) for the full list. |

**Why `chunk_id` has that structure:** consumers can parse a chunk_id and know which document and what kind of element it points at without loading the full chunk. Building a URL that highlights paragraph 2.4? `/doc/ADB1-2022/chunk/ADB1-2022-paragraph-2.4`. Building a Cypher query? `MATCH (c {chunk_id: $id})`. The format is part of the contract.

### Reading order

| Field | What it is |
|---|---|
| `order_index` | The chunk's position in the document's reading order, from 0 to N-1. **The only authoritative ordering.** |
| `page_start` | 1-indexed page where the chunk begins. |
| `page_end` | 1-indexed page where the chunk ends (equal to `page_start` for single-page chunks). |

**When do I use `order_index` vs page numbers?**

- For "the next paragraph" / "the previous paragraph" / "everything in this section in reading order" → always `order_index`. It handles multi-column layouts, figures interleaved with text, and page breaks correctly.
- For "which page is this on?" or "render the chunks on page 5" → use the page numbers. They're for display and printing context.

**What if I want to reproduce the document?** Walk chunks sorted by `order_index` and join their `text` with appropriate separators. That's the round-trip guarantee — the `min_text_reconstruction_coverage` metric in the validation report measures how complete that walk is.

### Section containment

| Field | What it is |
|---|---|
| `section_path` | Display-only labels of every section above this chunk, outermost first. E.g. `["B1", "Means of warning…", "2 Fire alarms"]`. |
| `section_path_ids` | The chunk_ids of the section_heading chunks corresponding to each label. Same length as `section_path`. |
| `parent_section_id` | chunk_id of the immediate parent section heading. `None` only for top-level headings. |
| `breadcrumb` | Pre-rendered `" > "`.join(section_path) — purely for display. |
| `heading_level` | Required when `type` is `section_heading` or `appendix`. Gives the nesting depth (1 = outermost). `None` otherwise. |

**The single most important rule about sections:** never label-match on `section_path` or `breadcrumb`. Those are display strings — they include human-friendly punctuation, capitalisation, and whitespace that can change between editions. Use `section_path_ids` or `parent_section_id` for any retrieval logic.

**Examples of correct queries:**

- "Is this chunk anywhere under §B1?" → `"ADB1-2022-section_heading-B1" in chunk.section_path_ids`.
- "Is this chunk in the immediately-enclosing section X?" → `chunk.parent_section_id == "<X's chunk_id>"`.
- "All chunks under §2.4 inclusive" → look up §2.4's `TOCEntry` and select chunks whose `order_index` falls in `[first_order_index, last_order_index]`. See [Table of contents](#table-of-contents--the-section-index).

### Content

| Field | What it is |
|---|---|
| `text` | The chunk's textual content, post-normalisation (whitespace collapsed, hyphens at line breaks repaired, etc.). |

### Edges (the knowledge-graph substrate)

| Field | What it is |
|---|---|
| `references_out` | A list of outbound cross-references. See [References](#references--how-chunks-connect). |
| `defined_terms_used` | A list of normalised glossary terms that appear in this chunk's text. Back-filled by Stage 5. |

These two fields, together with `parent_section_id` / `section_path_ids` and the asset-linkage fields below, are *every edge* in the graph. A knowledge graph is built by:

- Creating a node for each chunk.
- For each chunk, creating "CONTAINED_IN" edges to its `parent_section_id`.
- For each `references_out` entry, creating an edge of the appropriate type.
- For each `defined_terms_used` entry, creating a "USES_TERM" edge to the corresponding glossary entry.
- For each `caption_target_id`/`captioned_by_id`, creating a "CAPTIONS" edge.

No graph construction logic is needed beyond reading the fields. That's the point.

### Asset linkage

| Field | When it's set | What it is |
|---|---|---|
| `asset_path` | type ∈ {table, diagram} | Relative path to the extracted binary asset under `output/<doc_id>/assets/`. |
| `asset_sidecar_path` | optional | JSON sidecar describing the asset's structure (e.g. table cells), if any. |
| `caption_target_id` | type = caption | chunk_id of the table/diagram this caption belongs to. |
| `captioned_by_id` | optional, on table/diagram chunks | chunk_id of the caption chunk for this asset. |

**Captions are first-class chunks.** A figure caption is its own chunk, sitting in the document's reading order between the figure and the next paragraph. It points at the figure (`caption_target_id`); the figure points back at the caption (`captioned_by_id`). The pair is enforced bidirectional by validation.

This matters because captions often carry meaning that the figure binary alone can't (e.g. "Diagram 3.1 — fire spread between dwellings less than 1 m apart"). Keeping the caption searchable as its own chunk means a query for "fire spread between dwellings" can return the caption, which then leads you to the figure.

### Sourcing

| Field | What it is |
|---|---|
| `meta` | A `ChunkMeta` record carrying source spans and parser metadata. See [Sourcing](#sourcing--where-every-chunk-came-from). |

### The escape hatch

| Field | What it is |
|---|---|
| `attributes` | A free-form dictionary for document-specific metadata that doesn't fit the core schema. **The only escape hatch on the chunk itself.** |

When a per-document config sets `attributes.detect_requirement_scope: true`, Stage 2 tags each chunk with the Building Regulation requirement (B1–B5) inferred from its section context — but the tag goes in `chunk.attributes`, like `{"requirement": ["B1"]}`. Schema-wise, the chunk model never changes.

**Rule of thumb:** anything that is meaningful for one specific document or document family goes in `attributes`. Anything that is meaningful for *every* document goes in the top-level schema (and that means a schema change + JSON Schema re-export + drift test update).

---

## Chunk types

The `type` field is one of:

| Type | Meaning | Required fields |
|---|---|---|
| `section_heading` | A numbered or named heading. Anchors a region of the document. | `heading_level` |
| `paragraph` | A numbered body paragraph. The default. | — |
| `table` | A tabular block. The binary lives in `assets/`. | `asset_path` |
| `diagram` | A figure or image. The binary lives in `assets/`. | `asset_path` |
| `appendix` | An appendix's top-level heading. Treated as a section_heading with a flag. | `heading_level` |
| `glossary_entry` | One defined term in the glossary. | — |
| `regulation_quote` | Quoted text *from the regulation itself* (e.g. a verbatim Building Regulation block within a guidance document). | — |
| `caption` | A caption belonging to a table or diagram. | `caption_target_id` |

**Why `regulation_quote` is separate from `paragraph`:** in an Approved Document, the body text is *guidance* but the boxed regulations quoted at the start of each section are *statute*. Downstream consumers (especially anything making compliance decisions) need to distinguish them. The chunk type is the cheapest way to surface that distinction.

**Why `appendix` is separate from `section_heading`:** appendices have different numbering conventions and may have different validation rules in the config. Keeping the type distinct lets configs treat them differently without conditional code.

---

## Sourcing — where every chunk came from

Every chunk carries a `meta` field of type `ChunkMeta`:

```
ChunkMeta:
  source_spans: list[SourceSpan]   # always ≥1
  extracted_by: str                # parser identifier(s) with versions
  parser_confidence: float | None  # optional, 0..1
```

`source_spans` is the field that makes the sourcing guarantee real.

### SourceSpan

A `SourceSpan` is one contiguous region of one page that produced part of a chunk:

| Field | What it is |
|---|---|
| `page` | 1-indexed page number. |
| `bbox` | `(x0, y0, x1, y1)` in PDF points, top-left origin, y increasing downward. |
| `text_offset_start` | Inclusive offset into `chunk.text` where this span's substring begins. |
| `text_offset_end` | Exclusive offset. |

So `chunk.text[span.text_offset_start : span.text_offset_end]` is the exact substring of the chunk that this PDF region produced.

**Why this matters:**

- A paragraph that wraps across a page break has two spans — one on each page. Without this, you'd have to guess.
- A paragraph in a two-column layout has two spans on the same page — one per column. Without this, the bbox would be the union of both columns and would include the gap between them.
- A user clicking on a word in a UI can ask: "which span contains offset N?" and then highlight the corresponding bbox on the rendered page.

**Coordinate convention.** All bboxes are in **PDF userspace points (1/72 inch), origin top-left, y increasing downward**. This is recorded in three places so no consumer ever has to guess:

1. As a constant in `Page`/`SourceSpan` documentation.
2. In `DocumentMeta.coordinate_convention` (currently always `"pdf_pt_top_left"`).
3. In each YAML config under `sourcing:` (`coordinate_origin: top_left`, `coordinate_unit: pt`).

If you're rendering with PDF.js or another viewer that uses bottom-left origin, you'll need to flip y using the page height from `Page.height` (also stored in `pages.json`).

### Page

`Page` records geometry for one PDF page:

| Field | What it is |
|---|---|
| `page_number` | 1-indexed. |
| `width` | Page width in PDF points. |
| `height` | Page height in PDF points. |
| `rotation` | One of 0, 90, 180, 270. |

Written collectively as `Pages` to `intermediate/parse/pages.json`. You need it when you want to overlay a `SourceSpan.bbox` onto a rendered page and account for rotation, or when converting between coordinate systems.

---

## References — how chunks connect

Three model types work together:

- `Reference` — one outbound edge, on the source chunk.
- `ReferenceBacklink` — one inbound edge, in the inverted index.
- `ReferencesIndex` — the inverted index itself.

### Reference

A `Reference` is one outbound cross-reference from a chunk to a target:

| Field | What it is |
|---|---|
| `target_chunk_id` | The chunk_id pointed at. `None` for external references and for internal references that couldn't be resolved. |
| `label` | The surface text as it appeared in the source — e.g. `"paragraph 2.4"`, `"Diagram 3.1"`, `"BS EN 13501-1:2018"`. Useful for display. |
| `type` | The kind of edge. See below. |
| `external_id` | Normalised identifier for external references — e.g. `"BS-EN-13501-1:2018"`. `None` for internal references. |
| `pattern_name` | Which config regex pattern fired. `None` if the reference came from a PDF hyperlink. |
| `source` | How the reference was discovered: `"hyperlink"` or `"pattern"`. |
| `source_span` | The PDF location where the reference's surface text appears. Useful for highlighting citations. |

### Reference types

| Type | When to use it |
|---|---|
| `internal` | Points at another chunk in this same document. `target_chunk_id` is non-null when resolved. |
| `external_standard` | Cites a British or European standard (BS / BS EN). `external_id` is populated. |
| `external_document` | Cites another regulatory document (e.g. "Approved Document M"). `external_id` is populated. |
| `requirement` | Cites a Building Regulation requirement (B1–B5). `external_id` is populated. |
| `caption` | The caption→target asset edge. Emitted when captions are treated as separate chunks. |
| `defined_term` | A chunk uses a glossary term. Emitted by Stage 5 alongside `defined_terms_used`. |

**Why are unresolved internal references not schema errors?** Because they're a signal, not a bug in the data shape. A document may genuinely reference a paragraph that doesn't exist (errata, drafting bug), or our regex may have over-matched. Either way, downstream consumers need to see them. The validation report's `internal_ref_resolution` metric tracks the rate; the `ReferencesIndex.unresolved_internal` field lists them.

### ReferencesIndex

`ReferencesIndex` is the inverted backlink index, written to `references_index.json`:

| Field | What it is |
|---|---|
| `by_target` | Map from target chunk_id to list of `ReferenceBacklink` records. Answers "what cites this chunk?". |
| `unresolved_internal` | Internal references whose target couldn't be resolved. |
| `external_citations` | Map from normalised `external_id` to list of source chunk_ids. Answers "which paragraphs cite BS EN 13501-1?". |

**Why backlinks live in a sidecar, not on each chunk:** every edge has exactly one place where it's authoritative — the `references_out` list on the source chunk. If we denormalised backlinks onto chunks, we'd have two copies of every edge, and they could disagree. The sidecar is regenerable, derived, and clearly subordinate.

---

## Table of contents — the section index

`TOC` (`toc.json`) is a hierarchical, range-indexed table of contents. Stage 4 builds it from the PDF outline and cross-checks against `section_heading` chunks.

### TOCEntry

| Field | What it is |
|---|---|
| `id` | Stable identifier for this TOC entry, e.g. `"toc-1.2.3"`. |
| `label` | The heading's display text. |
| `level` | Nesting depth, 1 = outermost. |
| `heading_chunk_id` | The `section_heading` (or `appendix`) chunk this entry points at. |
| `first_chunk_id` | The chunk in this section with the smallest `order_index`. |
| `last_chunk_id` | The chunk with the largest `order_index`. |
| `first_order_index` | The smallest `order_index` (inclusive). |
| `last_order_index` | The largest `order_index` (inclusive). |
| `page` | 1-indexed page where the section begins. |
| `children` | Nested subsections (recursive). |

**How to query "every chunk in §2.4":**

1. Walk the TOC, finding the `TOCEntry` whose `label` or `heading_chunk_id` matches §2.4.
2. Select all chunks `c` such that `entry.first_order_index <= c.order_index <= entry.last_order_index`.

That's it. One range scan over a sorted list. No tree walks, no label matching, no section_path comparison.

**How to query "every chunk between §2.4 and §2.7 inclusive":**

1. Find both `TOCEntry` records.
2. Take the union of their windows: chunks with `c.order_index ∈ [entry_A.first_order_index, entry_B.last_order_index]`.

Same idea — range queries on a total order. This is the whole reason `order_index` is the authoritative ordering.

---

## Glossary — defined terms

Stage 5 parses the document's glossary section (named in `config.references.glossary_section`) and produces a flat lookup table.

### GlossaryEntry

| Field | What it is |
|---|---|
| `term` | The term as written in the document — display form. |
| `normalised_term` | Lowercase, whitespace-collapsed. Used for matching. |
| `definition` | The term's definition, normalised text. |
| `chunk_id` | The `glossary_entry` chunk that holds this term. |

### Glossary

Just `entries: list[GlossaryEntry]`, written to `glossary.json`. Empty when the document has no glossary.

### How `defined_terms_used` works

After building the glossary, Stage 5 walks every chunk's text looking for `normalised_term` matches (whole-word, case-insensitive) and back-fills `chunk.defined_terms_used` with the list of terms found.

So a downstream consumer can ask "which paragraphs talk about 'compartmentation'?" by:

1. Looking up the `GlossaryEntry` for "compartmentation" to find its `normalised_term`.
2. Selecting chunks where `normalised_term in chunk.defined_terms_used`.

This is the cheap version. A full KG would create explicit `USES_TERM` edges from each chunk to the glossary chunk; the `defined_terms_used` list is a pre-computed convenience.

---

## Pipeline run records

Three model types describe a run rather than its content:

- `StageReport` — one entry per executed stage.
- `ValidationMetric` and `ValidationReport` — Stage 6's health check.
- `DocumentMeta` — top-level run metadata.

### StageReport

Returned by every stage function, collected into `DocumentMeta.stage_reports`:

| Field | What it is |
|---|---|
| `stage` | Stage name. |
| `started_at` / `finished_at` | UTC timestamps. |
| `duration_seconds` | Wall clock. |
| `ok` | Did it finish without errors? |
| `counts` | Stage-specific counters (free-form), e.g. `{"chunks_emitted": 1247}`. |
| `warnings` | Non-fatal anomalies the stage flagged. |
| `errors` | Fatal errors. Non-empty implies `ok=False`. |

### ValidationMetric and ValidationReport

`ValidationReport` (written to `validation_report.json`) wraps a list of `ValidationMetric`s plus an overall pass/fail:

```
ValidationReport:
  metrics: list[ValidationMetric]
  passed: bool
  generated_at: datetime

ValidationMetric:
  name: str                # e.g. "page_coverage"
  value: float             # measured value, usually 0..1
  threshold: float | None  # configured minimum, if any
  passed: bool             # value cleared threshold
  counts: dict[str, int]   # raw numbers that produced value
  sample_failures: list    # ≤ 20 illustrative cases
```

The metrics Stage 6 emits are listed in [`docs/approach.md`](approach.md) §Stage 6. New metrics can be added without a schema change — `name` and `counts` are free-form.

`passed` at the top level is the conjunction of every threshold-bearing metric's `passed`. The CLI exits non-zero when `passed=False` unless `--no-fail` is set.

### DocumentMeta

`document.json` — read this first when reviewing a run:

| Field | What it is |
|---|---|
| `doc_id`, `title`, `edition`, `jurisdiction`, `legal_status` | Document identity. |
| `source_pdf` | Path to the source PDF (relative to repo root). |
| `source_pdf_sha256` | Hash of the PDF bytes. |
| `config_sha256` | Hash of the canonical-JSON-serialised config. |
| `git_sha` | The regula source's git SHA at run time, if available. |
| `generated_at` | UTC timestamp when the run finished. |
| `regula_version` | Package version. |
| `parser_versions` | Version of each parser used. |
| `page_count` | PDF page count. |
| `chunk_count` | Number of chunks emitted. |
| `stage_reports` | One `StageReport` per stage, in execution order. |
| `pipeline_passed` | Every stage `ok` and every threshold `passed`. |
| `coordinate_convention` | Frozen at `"pdf_pt_top_left"`. |

**Reproducibility detection.** Two runs with matching `source_pdf_sha256` and `config_sha256` should produce byte-identical output (modulo `generated_at`). If they don't, the reproducibility test fails — and that's a bug.

---

## Configuration

A `Config` is loaded from one YAML file per document. The shape mirrors `src/regula/config.py`; the example in [`configs/adb-vol1.yaml`](../configs/adb-vol1.yaml) is the canonical reference.

### Top-level identity

| Field | What it is |
|---|---|
| `doc_id` | Short stable ID for the document. Becomes the prefix of every chunk_id and the output directory name. |
| `title`, `edition`, `jurisdiction`, `legal_status` | Carried into `DocumentMeta`. |
| `source_pdf` | Path to the source PDF, relative to repo root. |

### `parsers`

| Field | Default | What it controls |
|---|---|---|
| `primary` | `"docling"` | The structural parser (produces the document tree). Swap for OCR once that exists. |
| `link_extractor` | `"pymupdf"` | The parser that extracts the PDF outline and hyperlinks. |

### `chunking`

| Field | Default | What it controls |
|---|---|---|
| `paragraph_regex` | *(required)* | Regex matching the start of a numbered paragraph. Group 1 is the paragraph number used to build chunk_ids. **Compiled at config load** — typos fail fast. |
| `heading_levels` | *(required)* | Which nesting levels become `section_heading` chunks, e.g. `[1, 2, 3, 4]`. |
| `merge_continuations` | `true` | Merge un-numbered continuation paragraphs into the preceding numbered one. |
| `preserve_reading_order` | `true` | Always true for now; here so the contract is explicit. |
| `treat_captions_as_chunks` | `true` | Emit captions as their own chunks (rather than folding them into surrounding paragraphs). Strongly recommended — folding is lossy. |

### `references`

| Field | What it controls |
|---|---|
| `patterns` | List of `ReferencePattern` regexes for finding cross-references in chunk text. PDF hyperlinks are handled separately and always take precedence over patterns. |
| `glossary_section` | Name of the section containing the glossary (e.g. `"Appendix E"`). `None` if the document has no glossary; Stage 5 then becomes a no-op. |

**Each `ReferencePattern`** has `name` (used in `Reference.pattern_name`), `regex` (compiled at load time), and `type` (one of the `ReferenceType` values). For internal references, capture group 1 is the target identifier (e.g. paragraph number). For external references, the surface match is normalised into `Reference.external_id`.

### `attributes`

Document-specific detection knobs. `extra='allow'` is intentional — this is the one place future flags can land without a schema change.

| Field | Default | What it does |
|---|---|---|
| `detect_requirement_scope` | `false` | Tag each chunk with the Building Regulation requirement (B1–B5) inferred from section context. Result goes in `chunk.attributes`. |
| `detect_building_type` | `false` | Tag chunks with applicable building types (`"dwellinghouse"`, `"flat"`, …) mentioned in the text. Result goes in `chunk.attributes`. |

### `validation`

Thresholds for Stage 6. Each is a 0..1 minimum rate.

| Field | Default | What it measures |
|---|---|---|
| `min_internal_ref_resolution` | `0.95` | Fraction of internal references that resolved. |
| `min_page_coverage` | `0.98` | Fraction of pages with at least one chunk. |
| `min_text_reconstruction_coverage` | `0.97` | Walking chunks in order and concatenating their text covers this fraction of the PDF text. The round-trip guarantee. |
| `min_reading_order_monotonicity` | `0.98` | Per page, chunks sorted by `order_index` have non-decreasing y-coordinates this fraction of the time. |
| `fail_on_schema_error` | `true` | Any artifact failing its JSON Schema fails the run. |

Lower thresholds in `_fixture-small.yaml` so the synthetic test PDF passes.

### `sourcing`

Coordinate convention. Frozen but explicit so downstream tools can inspect the config.

| Field | Default | Allowed values |
|---|---|---|
| `coordinate_origin` | `"top_left"` | `"top_left"` |
| `coordinate_unit` | `"pt"` | `"pt"` |

---

## "When do I use X?" — a cheat sheet

| I want to… | Use… |
|---|---|
| Walk the document as a reader would | Sort chunks by `order_index` and emit `chunk.text`. |
| Find every chunk under section §2.4 | Look up §2.4's `TOCEntry`, then range-scan `order_index ∈ [first_order_index, last_order_index]`. |
| Find every chunk between two sections | Range-scan across the union of two TOC windows. |
| Check whether a chunk is anywhere under §B1 | `"<§B1 chunk_id>" in chunk.section_path_ids`. |
| Check the immediate parent section | `chunk.parent_section_id`. |
| Display a breadcrumb | Use `chunk.breadcrumb` directly. |
| Find every paragraph that cites BS EN 13501-1 | `references_index.external_citations["BS-EN-13501-1:2018"]`. |
| Find what cites paragraph 2.4 | `references_index.by_target["ADB1-2022-paragraph-2.4"]`. |
| Find unresolved internal refs (for QA) | `references_index.unresolved_internal`. |
| Highlight a chunk's region in the PDF | Iterate `chunk.meta.source_spans` and draw `bbox` on `page`. Use `Page.height` to flip y if your viewer is bottom-left origin. |
| Find which substring a span produced | `chunk.text[span.text_offset_start : span.text_offset_end]`. |
| Find every paragraph that uses the term "compartmentation" | Look up the `GlossaryEntry`, then select chunks where its `normalised_term` is in `chunk.defined_terms_used`. |
| Find the caption of a figure | `figure_chunk.captioned_by_id`. |
| Find the figure a caption belongs to | `caption_chunk.caption_target_id`. |
| Tell whether a re-run is needed | Compare `DocumentMeta.source_pdf_sha256` and `config_sha256` against current PDF and config. |
| Diagnose a failing run | Read `validation_report.json` for failed metrics; `document.json.stage_reports` for stage errors. |
| Add a per-document tag like "applies to dwellinghouses" | Put it in `chunk.attributes`. |

---

## Extending the schema

There are three layers of extensibility:

1. **`chunk.attributes` (free-form dict)** — document-specific tags. No schema change. The default landing place for anything that's *meaningful for one document or document family*.
2. **`AttributesConfig` (`extra="allow"`)** — config-level knobs for new detection logic. Drop a new boolean flag into the YAML; stages can opt in to read it.
3. **Top-level schema change** — when the new concept is universal across documents (every regulatory PDF has it). This requires:
   - Adding/changing fields in `src/regula/schemas.py`.
   - Re-exporting `schemas/*.schema.json` with `uv run regula export-schemas --out schemas/`.
   - Updating `docs/schemas.md` (this file).
   - Likely updating one or more invariant helpers and tests.
   - The drift test will fail if you skip step 2 — that's intentional.

**When in doubt, start with `attributes`.** Pushing knowledge into the top-level schema commits every future document to that field. Pushing into `attributes` is reversible.

---

## Glossary of terms used in this doc

| Term | Meaning |
|---|---|
| **chunk** | One self-contained, addressable piece of a document. The node of the output graph. |
| **chunk_id** | A chunk's globally-unique identifier, structured as `<doc_id>-<type>-<identifier>`. |
| **chunk type** | What kind of element a chunk represents (paragraph, heading, table, …). |
| **order_index** | Global, monotonic, doc-wide position in reading order. The authoritative ordering. |
| **source span** | One contiguous PDF region (one page, one bbox) that produced part of a chunk's text. |
| **bbox** | Bounding box, `(x0, y0, x1, y1)`, top-left origin, PDF points. |
| **reference** | An edge from a chunk to a target — another chunk, a standard, a regulation, etc. |
| **backlink** | Inverse of a reference — "who points at me?". Lives in `references_index.json`. |
| **TOC entry** | One node in the hierarchical table of contents; carries an `order_index` window for its section. |
| **defined term** | A term in the document's glossary, looked up by `normalised_term`. |
| **attributes** | Free-form per-chunk metadata. The schema escape hatch. |
| **canonical JSON** | JSON serialisation with sorted keys and minimal whitespace, used for hashing configs. |
| **drift test** | Test that fails when committed JSON Schemas don't match what the Pydantic models would currently emit. |
| **stage** | One step of the pipeline; reads from disk, writes to disk, can be re-run independently. |
| **invariant helper** | A function in `regula.schemas` that asserts a cross-model property (used by tests and by Stage 6). |
