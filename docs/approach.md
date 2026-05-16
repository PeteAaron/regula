# Approach: Document Ingestion Pipeline (v1)

## Goal

Build a **reusable PDF → structured-chunks ingestion pipeline** for regulatory and standards documents. The deliverable is the pipeline — a configurable, testable, observable process — not the structured output of any single document.

The pipeline will be exercised end-to-end against *Approved Document B, Volume 1 (2022)* as the first test case. It should also be designed so that ingesting a second, structurally different regulatory PDF (e.g. ADB Vol 2, a British Standard preview, or a sector RRO guide) requires **configuration changes only, not code changes** in the core stages.

We are not building a retrieval system, a graph, or an LLM integration in this work. We are building the upstream machinery that turns documents into clean, structured input for whatever consumer comes next.

## What "the pipeline" means

A deterministic, idempotent, stage-based process with the following properties:

- **Stage-isolated.** Each stage reads from disk, writes to disk, and can be re-run independently. No in-memory orchestration that has to complete in one shot.
- **Configurable per document.** A YAML config file specifies how to interpret a given document (doc_id, paragraph numbering pattern, reference patterns, validation thresholds). The same code handles ADB, BS 9991, and BS 9999 with three different configs.
- **Observable.** Every stage emits intermediate artifacts and a structured log. A validation report at the end summarises pipeline health.
- **Reproducible.** Same input PDF + same config → byte-identical output (modulo timestamps in a `meta` block). This is the test of whether the process is actually deterministic.
- **Composable.** Each stage is callable as a library function. The CLI is a thin wrapper. Other tools can import and call any stage.

## Pipeline contract

### Input contract

A document is ingestible if:

- It is a digital PDF with selectable text (not a pure scan — OCR is out of scope for v1).
- It is in English.
- It has a paragraph-numbering convention that can be expressed as a regex (e.g. `^\d+\.\d+[a-z]?` for ADB).
- A per-document config file exists describing it (see "Per-document config" below).

### Output contract

For every successfully ingested document, the pipeline produces a fixed set of artifacts in the output directory:

```
output/<doc_id>/
  document.json          # top-level metadata
  toc.json               # table of contents → chunk references
  chunks.jsonl           # one chunk per line
  glossary.json          # defined-term lookup (if present)
  assets/                # extracted figures and tables (binary + JSON)
  intermediate/          # per-stage debug artifacts
  validation_report.json # health checks and metrics
  run.log                # structured log of the run
```

The **schemas for these files are the pipeline's output contract** and must be enforced by JSON Schema validation in the final stage. The schemas are defined in `src/<pkg>/schemas.py` (Pydantic) and exported to `schemas/*.schema.json` for downstream consumers.

Minimum chunk schema (the contract — content correctness is not in scope here, but shape is):

```json
{
  "chunk_id": "<doc_id>-<type>-<identifier>",
  "doc_id": "...",
  "type": "section_heading | paragraph | table | diagram | appendix | glossary_entry | regulation_quote",
  "section_path": ["..."],
  "breadcrumb": "...",
  "text": "...",
  "page_start": 0,
  "page_end": 0,
  "references_out": [
    {"target_chunk_id": "...|null", "label": "...", "type": "internal|external_standard|external_document|requirement"}
  ],
  "meta": { "source_page_bbox": [...], "extracted_by": "..." }
}
```

Document-specific fields (e.g. `requirement: ["B1"]`, `applies_to: ["dwellinghouse"]`) live under a freeform `attributes` block so the schema doesn't need to change per document.

### Per-document config

A YAML file per document drives the pipeline. Example for ADB Vol 1:

```yaml
doc_id: ADB1-2022
title: "Approved Document B, Volume 1: Fire Safety — Dwellings"
edition: "2022 edition incorporating 2022 amendments"
jurisdiction: England
legal_status: Approved Document
source_pdf: inputs/ADB-Vol1-2022.pdf

parsers:
  primary: docling
  link_extractor: pymupdf

chunking:
  paragraph_regex: '^(\d+\.\d+[a-z]?)\s+'
  heading_levels: [1, 2, 3, 4]
  merge_continuations: true

references:
  patterns:
    - { name: internal_paragraph, regex: 'paragraph(?:s)?\s+(\d+\.\d+[a-z]?)', type: internal }
    - { name: appendix, regex: 'Appendix\s+([A-Z])', type: internal }
    - { name: diagram, regex: 'Diagram\s+(\d+\.\d+)', type: internal }
    - { name: table, regex: 'Table\s+(\d+\.\d+)', type: internal }
    - { name: bs_standard, regex: 'BS\s?(?:EN\s)?\d+(?:-\d+)?(?::\d{4})?', type: external_standard }
    - { name: approved_doc, regex: 'Approved Document\s+[A-Z]\d?', type: external_document }
    - { name: requirement, regex: 'Requirement\s+(B[1-5])', type: requirement }

  glossary_section: "Appendix E"

attributes:
  detect_requirement_scope: true   # tag each chunk with B1..B5 based on section
  detect_building_type: true       # tag with dwellinghouse / flat where mentioned

validation:
  min_internal_ref_resolution: 0.95
  min_page_coverage: 0.98
  fail_on_schema_error: true
```

Adding a new document = writing a new config. Adding a fundamentally new *structure* (e.g. a numbered-clause BS instead of numbered-paragraph ADB) might require a new parser profile, but should not require touching the core stages.

## Stages

Each stage has the signature `stage(input_dir, output_dir, config) -> StageReport`. Stages write to `output/<doc_id>/intermediate/<stage_name>/` so they can be inspected and re-run.

### Stage 1 — `parse`

Run the configured parsers, write raw structured output. No interpretation yet.

- Default `primary: docling` produces a hierarchical document tree → `intermediate/parse/docling.json`.
- Default `link_extractor: pymupdf` produces the PDF outline and every internal hyperlink with destination coordinates → `intermediate/parse/links.json`, `intermediate/parse/outline.json`.
- Page-level text + bounding boxes → `intermediate/parse/pages.json`.

Parsers are pluggable. A parser is any class exposing `parse(pdf_path) -> dict`. The default set covers digital PDFs with text; a future OCR parser would slot in here.

### Stage 2 — `chunk`

Walk the parsed document tree and emit chunk records. Driven by the config's `chunking` block.

- Identify chunk type per element (heading / paragraph / table / figure / appendix item / glossary entry).
- Assign chunk IDs using `<doc_id>-<type>-<identifier>` format.
- Maintain a running `section_path` stack.
- Handle paragraph continuations per config (`merge_continuations`).

Output: `intermediate/chunk/chunks.jsonl` (without references resolved yet).

### Stage 3 — `resolve_references`

Two passes, combined:

- **Hyperlink pass (deterministic).** For every link from `intermediate/parse/links.json`, map `(dest_page, dest_y)` to the chunk whose bbox contains that point. Emit a `references_out` entry of type `internal`.
- **Pattern pass.** Run each regex from the config's `references.patterns` over each chunk's text. Resolve internal targets to chunk IDs; leave external references with `target_chunk_id: null` and a normalised `external_id`.

Deduplicate when both passes fire on the same source/target pair, preferring the hyperlink pass.

Output: `intermediate/resolve_references/chunks.jsonl` (with `references_out` populated).

### Stage 4 — `build_toc`

Build TOC from the PDF outline (authoritative) and cross-check against Stage 2's heading chunks. Each TOC entry points to a `chunk_id`.

Output: `toc.json`.

### Stage 5 — `extract_glossary`

If `references.glossary_section` is set in the config, parse the named section into a flat term → definition lookup, mirror to `glossary.json`, and back-fill `defined_terms_used` on every chunk (whole-word match, case-insensitive).

This stage is a no-op if the document has no glossary.

### Stage 6 — `validate`

Run health checks against the assembled output. Each check produces a metric and a pass/fail verdict against the config's thresholds.

- **Schema validation** — every artifact passes its JSON Schema.
- **Page coverage** — ≥ `min_page_coverage` of pages are covered by at least one chunk.
- **Reference resolution rate** — ≥ `min_internal_ref_resolution` of `type: internal` references resolve to a known chunk.
- **TOC integrity** — every TOC entry resolves to an existing `chunk_id`.
- **Round-trip word count** — assembled chunks vs source PDF text within tolerance.
- **ID uniqueness** — no duplicate `chunk_id`s.

Output: `validation_report.json`. Pipeline exits non-zero if any threshold fails (unless `--no-fail` is passed).

### Stage 7 — `finalise`

Copy/assemble the final artifacts into `output/<doc_id>/` from the intermediate stages, write `document.json` with run metadata (git SHA, config hash, timestamps, parser versions), and emit `run.log`.

## CLI and library interfaces

```bash
# Run the whole pipeline
adb-chunker ingest --config configs/adb-vol1.yaml

# Run a single stage (reads from prior stage's intermediate output)
adb-chunker stage chunk --config configs/adb-vol1.yaml
adb-chunker stage resolve_references --config configs/adb-vol1.yaml

# Re-validate without re-running anything
adb-chunker validate --config configs/adb-vol1.yaml

# Inspect a chunk
adb-chunker preview --config configs/adb-vol1.yaml --chunk-id ADB1-2022-§2.4

# Diff two runs (proves reproducibility)
adb-chunker diff output/ADB1-2022 output-prev/ADB1-2022
```

Library API mirrors the CLI:

```python
from adb_chunker import Pipeline, load_config

cfg = load_config("configs/adb-vol1.yaml")
report = Pipeline(cfg).run()
# Or per-stage
Pipeline(cfg).run_stage("resolve_references")
```

## Recommended stack

```
python >= 3.11
docling             # structural parsing
pymupdf             # links, outline, page geometry
pydantic            # schemas and config models
jsonschema          # output contract enforcement
pyyaml              # config files
typer + rich        # CLI + logging
structlog           # structured logging
pytest              # tests
```

No vector store, no embeddings, no LLM calls. Pure deterministic extraction.

## Project layout

```
doc-ingest/
  pyproject.toml
  configs/
    adb-vol1.yaml
    _fixture-small.yaml      # synthetic test config
  schemas/
    chunk.schema.json
    toc.schema.json
    document.schema.json
    validation_report.schema.json
  src/doc_ingest/
    __init__.py
    cli.py
    pipeline.py              # stage orchestration
    config.py                # config loading & validation
    schemas.py               # Pydantic models
    parsers/
      __init__.py
      docling_parser.py
      pymupdf_links.py
    stages/
      parse.py
      chunk.py
      resolve_references.py
      build_toc.py
      extract_glossary.py
      validate.py
      finalise.py
    logging.py
  tests/
    fixtures/
      small.pdf              # 3–5 page synthetic regulatory-style doc
      small_expected/        # golden output for the fixture
    test_pipeline_end_to_end.py
    test_chunk_stage.py
    test_resolve_references.py
    test_reproducibility.py  # run twice, diff outputs
  inputs/                    # PDFs (gitignored)
  output/                    # generated (gitignored)
```

## Testing strategy

The pipeline being the deliverable means tests are first-class:

- **Unit tests per stage** against in-memory inputs. Each stage is a pure-ish function over disk artifacts, easy to fixture.
- **Golden-file tests** using a small synthetic regulatory PDF (3–5 pages, hand-built, committed to `tests/fixtures/small.pdf`). The pipeline must produce a known-good output for this fixture.
- **Reproducibility test** that runs the pipeline twice on the fixture and asserts byte-identical output (excluding the `meta.timestamps` block).
- **Real-document smoke test** that runs the pipeline against the ADB PDF and asserts the validation thresholds in the config pass. Run in CI if the PDF is licensable; otherwise as a local-only test.
- **Config schema test** — every YAML in `configs/` loads and validates without error.

## Observability

- Every stage emits a structured log line for each chunk processed and each decision made (e.g. "merged continuation 2.4 → 2.4a", "unresolved reference 'paragraph 7.99' in chunk ADB1-2022-§5.12").
- `validation_report.json` includes per-metric pass/fail, raw counts, and a sample of failures (first 20 unresolved references, etc.) so issues are actionable.
- `intermediate/` survives the run so any stage can be inspected post-hoc without re-running anything.

## Definition of done (v1)

The pipeline is done when **all of the following are true**:

1. `adb-chunker ingest --config configs/adb-vol1.yaml` runs end-to-end with zero crashes and produces the full output contract.
2. The validation report on ADB Vol 1 meets the configured thresholds (≥95% internal ref resolution, ≥98% page coverage, schema pass).
3. The reproducibility test passes — running the pipeline twice produces identical output.
4. The golden-file test on the synthetic fixture passes.
5. A second config (`configs/_fixture-small.yaml` or a real second document if available) runs through the pipeline with **no code changes**, only config changes. This is the test of whether the process is actually generic.
6. All output files validate against their JSON Schemas.

Note what is **not** in the definition of done: judgment calls about whether the ADB chunks are "good." Content quality is a downstream concern. Pipeline correctness is what we're proving.

## Out of scope (v1)

- OCR / scanned PDFs.
- Multi-document orchestration (ingest one doc at a time).
- Versioning, amendment diffing, supersession tracking.
- Embeddings, retrieval, MCP server, graph DB.
- Entity resolution across documents.
- LLM-assisted extraction. Defer until deterministic extraction is exhausted; when it is added, it must be a separate, optional stage that augments rather than replaces deterministic output.
- Any UI beyond the CLI. The static HTML viewer mentioned earlier is no longer part of v1 — it's a downstream consumer of the contract.

## Notes for the implementer

- Treat the YAML config as the **only** place document-specific knowledge lives. If you find yourself adding an `if doc_id == "ADB1-2022"` branch in the code, that's a bug — push the logic into the config schema.
- Keep stage boundaries clean. A stage may not read from any artifact older than its immediate predecessor's output. Re-running stages out of order should either work or fail loudly, never silently produce stale results.
- Hash the config and the source PDF into `document.json` so downstream consumers can detect when something needs reprocessing.
- The `references_out` field is the most error-prone part of the pipeline; over-test it. Build a small synthetic doc with every reference pattern represented and assert exact resolution.
- Prefer raising on unexpected document structure over silently coercing. A loud failure on a malformed paragraph is easier to fix than a chunk silently lost.
