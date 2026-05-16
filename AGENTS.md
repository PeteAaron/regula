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

## Output contract

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

## Common pitfalls

- Adding document-specific logic to a stage instead of the config. (See first convention.)
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
