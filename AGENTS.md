# AGENTS.md

Canonical brief for any agent (Claude Code, Cursor, Aider, Continue, …) working in this repo. `CLAUDE.md` and any other tool-specific entry points defer here — keep guidance in this file so it stays consistent across tools.

## What this repo is

`regula` is a **deterministic PDF → text-block extraction pipeline** for regulatory and standards documents. The pipeline produces one record per text block per page with positional and font metadata. **It does not classify, merge, or group blocks** — those decisions depend on document-specific conventions that the human reviewer identifies after inspecting the output.

> **Wind-back, 2026-05-16.** The earlier (v0) pipeline tried to do too much — regex-driven paragraph detection, outline-based heading claims, page-break continuation merging, reference resolution, glossary extraction. On real documents (Approved Document B Vol 1) the conventions didn't hold tightly enough; paragraphs were stolen as headings via outline substring matches, continuations were misclassified on page breaks, and the title page / contents page silently dropped out. The pipeline was wound back to "extract every block, classify nothing" so the human can see what's actually there before declaring how to chunk it. The `Chunk` / `TOC` / `Glossary` / `Reference` models are preserved in `schemas.py` for the day a manual-rules-driven chunking stage is reintroduced as an opt-in step.

The full design spec lives in [`docs/approach.md`](docs/approach.md) but predates the wind-back — read it for historical context, not as the current target.

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

## Stages (current)

1. `parse` — PyMuPDF reads the PDF: pages, flat element list with font signals, hyperlinks, raster images, outline. Written to `intermediate/parse/`.
2. `extract_blocks` — converts parser elements into typed `Block` records. Computes a char-weighted median body font size, then assigns each block an advisory `region` (from y-position) and `looks_like` (from font + bold/italic). No filtering, no merging, no classification beyond the hints.
3. `validate` — advisory metrics (blocks per page, region breakdown, link kinds) + per-artifact JSON Schema conformance. Only the schema check can fail the run.
4. `finalise` — copies artifacts to the output root.

Each stage has the signature `run(output_dir, config) -> StageReport`, reads from disk, writes to disk, and can be re-run independently.

The deleted v0 stages (`chunk`, `resolve_references`, `build_toc`, `extract_glossary`) are gone. Don't reintroduce them without explicit direction — the user wants to define document-specific rules manually after reviewing blocks.

## The output contract — where to read about it

Three documents describe the contract at different levels of detail:

- **[`docs/schemas.md`](docs/schemas.md)** — non-technical, prose walk-through of every model, every field, and "when do I use X?". **Start here** if you're consuming output for the first time, or extending the schema, or trying to remember what a field means.
- **`src/regula/schemas.py`** — the contract in code (Pydantic v2 models with field-level `description=` strings).
- **`schemas/*.schema.json`** — the exported JSON Schemas, the contract downstream consumers pin against. Descriptions on every field flow through from the Pydantic models.

## Output directory

```
output/<doc_id>/
  blocks.jsonl            # one Block per line
  pages.json              # per-page geometry
  links.json              # PDF hyperlinks
  outline.json            # PDF outline as parser reports it
  document.json           # run metadata
  validation_report.json  # advisory health metrics
  deferred.json           # capabilities not yet built
  preview.html            # diagnostic page-oriented preview
  assets/
  intermediate/
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

- **Coordinate convention is frozen.** All bboxes are PDF userspace points (1/72 inch), origin **top-left**, y increasing downward. Recorded in `DocumentMeta.coordinate_convention`, the YAML `sourcing:` block, and module-level documentation in `schemas.py`.
- **`Block.block_id` is deterministic and unique.** Format: `<doc_id>:p<page>:b<reading_order_index>`. The Pydantic validator enforces this — same PDF + same `doc_id` → same `block_id`s every time.
- **`reading_order_index` is per-page, 0..N-1, contiguous.** It's what the parser returned (PyMuPDF's `sort=True` reading order). The walker doesn't reorder.
- **Advisory hints are never load-bearing.** `region` and `looks_like` are computed from position and font alone. They surface in the preview as badges; nothing in the pipeline filters or groups on them. If downstream code wants to drop "footer" blocks, the human writes that rule explicitly — the extractor never does it.

The legacy `Chunk` / `TOC` / `Glossary` / `Reference` invariant helpers (`assert_reading_order_valid`, `assert_section_windows_consistent`, etc.) still exist in `schemas.py` but are unused — they'll come back when chunking does.

## JSON Schema export

Pydantic models in `src/regula/schemas.py` are the source of truth. Committed JSON Schemas under `schemas/*.schema.json` are the public contract for downstream consumers. They are kept in sync via:

```
uv run regula export-schemas --out schemas/
```

`tests/test_schema_export.py` runs a drift check that fails CI if a model change is not accompanied by a re-export. Treat schema diffs as breaking changes and call them out in the PR description.

## Common pitfalls

- **Reintroducing classification heuristics.** The wind-back was deliberate — don't add regex-driven paragraph detection, outline-based heading claims, or merging rules without explicit user direction. Advisory hints (`region`, `looks_like`) are the limit; nothing acts on them in-pipeline.
- **Filtering blocks at extraction time.** Even "obviously noise" blocks (running headers, page numbers) must appear in `blocks.jsonl`. Ignore-rules are a downstream concern.
- Modifying a Pydantic model in `schemas.py` without re-exporting `schemas/*.schema.json`. The drift test catches this; the PR review should catch it first.
- Reading from `intermediate/` directories outside the immediate predecessor stage.
- Mutating shared state across stages — stages communicate via disk artifacts only.
- Silently swallowing malformed input. Raise.
- Forgetting to hash the config and source PDF into `document.json` — downstream consumers rely on these hashes to detect when reprocessing is needed.
