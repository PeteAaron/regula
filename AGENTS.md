# AGENTS.md

Canonical brief for any agent (Claude Code, Cursor, Aider, Continue, …) working in this repo. `CLAUDE.md` and any other tool-specific entry points defer here — keep guidance in this file so it stays consistent across tools.

## What this repo is

`regula` is a **deterministic, configurable PDF → structured-chunks ingestion pipeline** for regulatory and standards documents (Approved Documents, British Standards, sector RRO guides). The deliverable is the pipeline itself; per-document output is the proof that the pipeline works.

The full design spec lives in [`docs/approach.md`](docs/approach.md). Read it before making non-trivial changes. This file covers what an agent needs day-to-day; the spec covers *why*.

## Repo structure

```
regula/
  pyproject.toml
  AGENTS.md / CLAUDE.md / README.md
  docs/
    approach.md            # the design spec — the source of truth
  src/regula/
    __init__.py
    cli.py                 # Typer entry point — thin wrapper over the library
    pipeline.py            # stage orchestration
    config.py              # config loading & Pydantic validation
    schemas.py             # Pydantic models for all output artifacts
    logging.py             # structlog setup
    parsers/               # pluggable parser implementations
      __init__.py
    stages/                # one module per pipeline stage
      __init__.py
  configs/                 # one YAML per document; document-specific knowledge lives here
  schemas/                 # exported JSON Schemas (the output contract)
  tests/
    fixtures/              # synthetic test PDFs + golden outputs
  inputs/                  # source PDFs (gitignored)
  output/                  # generated artifacts (gitignored)
```

## Stages (current target)

1. `parse` — Docling for structure, PyMuPDF for links/outline/geometry.
2. `chunk` — emit chunk records from the parsed tree.
3. `resolve_references` — hyperlink pass + regex pass; deduplicate.
4. `build_toc` — TOC from PDF outline, cross-checked against headings.
5. `extract_glossary` — defined-term lookup, back-fill `defined_terms_used`.
6. `validate` — health checks against config thresholds.
7. `finalise` — assemble final artifacts, write `document.json` with run metadata.

Each stage has the signature `stage(input_dir, output_dir, config) -> StageReport`, reads from disk, writes to disk, and can be re-run independently.

## The output contract — where to read about it

Three documents describe the contract at different levels of detail:

- **[`docs/schemas.md`](docs/schemas.md)** — non-technical, prose walk-through of every model, every field, and "when do I use X?". **Start here** if you're consuming output for the first time, or extending the schema, or trying to remember what a field means.
- **`src/regula/schemas.py`** — the contract in code (Pydantic v2 models with field-level `description=` strings).
- **`schemas/*.schema.json`** — the exported JSON Schemas, the contract downstream consumers pin against. Descriptions on every field flow through from the Pydantic models.

## Output directory

For every successfully ingested document the pipeline produces:

```
output/<doc_id>/
  document.json
  toc.json
  chunks.jsonl
  glossary.json
  assets/
  intermediate/
  validation_report.json
  run.log
```

Schemas live in `src/regula/schemas.py` (Pydantic) and are exported to `schemas/*.schema.json`. The exported JSON Schemas are the contract downstream consumers pin against — treat changes to them as breaking.

## Conventions

- **Document-specific knowledge lives in YAML, never in code.** If you find yourself writing `if doc_id == "ADB1-2022"`, push the branch into the config schema instead.
- **Stage isolation.** A stage may only read artifacts produced by its immediate predecessor (or the original input PDF + config). No stage may peek at a later stage's output. Re-running stages out of order must fail loudly, never silently produce stale results.
- **Determinism.** Same PDF + same config → byte-identical output, modulo a `meta.timestamps` block. The reproducibility test enforces this.
- **Loud failures.** Prefer raising on unexpected document structure over silently coercing. A loud failure on a malformed paragraph is easier to fix than a chunk silently lost.
- **No LLM calls in the core pipeline.** Deferred until deterministic extraction is genuinely exhausted, and only ever as an optional, separate stage that augments rather than replaces deterministic output.
- **CLI is a thin wrapper.** Every CLI command corresponds to a callable library function. Other tools should be able to `import regula` and call any stage.

## Testing

- **Unit tests per stage** against disk fixtures.
- **Golden-file tests** using `tests/fixtures/small.pdf` (synthetic, 3–5 pages, hand-built) and committed expected output under `tests/fixtures/small_expected/`.
- **Reproducibility test** runs the pipeline twice on the fixture and asserts byte-identical output (excluding timestamps).
- **Real-document smoke test** runs against the ADB PDF and asserts the validation thresholds in `configs/adb-vol1.yaml` pass. Local-only if the PDF isn't licensable.
- **Config schema test** — every YAML in `configs/` loads and validates without error.

Run `pytest` to execute the suite. Stage-isolated tests should not require the real ADB PDF.

## Output contract invariants

The schema in `src/regula/schemas.py` is the contract. A small number of cross-model invariants must hold for every successful run; they are enforced by helper functions in the same module and re-checked by Stage 6 (`validate`) on real output. Treat any new query pattern in downstream code as a candidate for being expressed against these invariants rather than re-derived.

- **Coordinate convention is frozen.** All bboxes are PDF userspace points (1/72 inch), origin **top-left**, y increasing downward. The convention is recorded in three independent places — `DocumentMeta.coordinate_convention`, the YAML `sourcing:` block, and module-level documentation in `schemas.py`. Never re-derive bbox orientation from heuristics.
- **`order_index` is the only authoritative reading order.** Walking chunks sorted by `order_index` reproduces the document. Never sort by `page_start + bbox.y` at query time — multi-column layouts and figure interleaving break that. Stage `chunk` is where `order_index` is assigned; nothing downstream is allowed to change it.
- **Section containment uses IDs, not labels.** Use `Chunk.parent_section_id` or `Chunk.section_path_ids` for "is X inside section Y" queries. Use `TOCEntry.first_order_index`..`last_order_index` for range queries ("all chunks in §2.4", "between §2.4 and §2.7"). Never label-match on `section_path` / `breadcrumb` — those are display strings.
- **Cross-references are emitted on the source chunk.** `Chunk.references_out` is the source of truth for every edge. The inverted backlink index lives in `references_index.json` (a Stage 3 sidecar); backlinks are not denormalised onto chunks.
- **Source spans must be exact.** Every `SourceSpan` must address a non-empty slice of `chunk.text` (`text_offset_start..text_offset_end`) and a positive-area page region. This is what makes "exactly which region of the PDF produced this character" recoverable.
- **Asset linkage is bidirectional.** A `caption` chunk's `caption_target_id` must point at a `table`/`diagram` chunk whose `captioned_by_id` points back. Tables and diagrams must carry an `asset_path`.

The invariant helpers — `assert_reading_order_valid`, `assert_section_windows_consistent`, `assert_asset_linkage_bidirectional`, `assert_source_spans_in_bounds` — are public functions on `regula.schemas`. Use them whenever building or consuming chunks. Stage 6 calls the same helpers; defining the rules once means consumer code and validator code can't drift apart.

## JSON Schema export

Pydantic models in `src/regula/schemas.py` are the source of truth. Committed JSON Schemas under `schemas/*.schema.json` are the public contract for downstream consumers. They are kept in sync via:

```
uv run regula export-schemas --out schemas/
```

`tests/test_schema_export.py` runs a drift check that fails CI if a model change is not accompanied by a re-export. Treat schema diffs as breaking changes and call them out in the PR description.

## Common pitfalls

- Adding document-specific logic to a stage instead of the config. (See first convention.)
- Sorting chunks by page+bbox instead of `order_index`. (See "Output contract invariants".)
- Label-matching on `section_path` for retrieval. Use `section_path_ids` / `parent_section_id` / TOC windows.
- Modifying a Pydantic model in `schemas.py` without re-exporting `schemas/*.schema.json`. The drift test will catch this, but the PR review should catch it first.
- Reading from `intermediate/` directories outside the immediate predecessor stage.
- Mutating shared state across stages — stages communicate via disk artifacts only.
- Silently swallowing malformed input. Raise.
- Forgetting to hash the config and source PDF into `document.json` — downstream consumers rely on these hashes to detect when reprocessing is needed.
- Over-trusting the regex pass for references; the hyperlink pass is authoritative when both fire on the same pair.
- Treating the chunk schema as flexible. Document-specific fields go under the freeform `attributes` block; the top-level schema does not change per document.

## Definition of done (v1)

The spec's DoD is the source of truth ([`docs/approach.md`](docs/approach.md)), summarised:

1. `regula ingest --config configs/adb-vol1.yaml` runs end-to-end without crashes and produces the full output contract.
2. Validation thresholds on ADB Vol 1 are met (≥95% internal ref resolution, ≥98% page coverage, schema pass).
3. The reproducibility test passes.
4. The golden-file test on the synthetic fixture passes.
5. A second config runs through the pipeline with **no code changes**.
6. All output files validate against their JSON Schemas.

Pipeline correctness is what v1 proves. Judgement calls about whether the ADB chunks are "good" are a downstream concern.
