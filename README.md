# regula

> A configurable, deterministic ingestion pipeline that turns regulatory and standards PDFs into structured, cross-linked chunks.

**Status:** early / v1 in development. The pipeline below is the target. See [`docs/approach.md`](docs/approach.md) for the full spec.

## What this is

`regula` takes a regulatory document — an Approved Document, a British Standard, a sector RRO guide — and produces a clean, machine-readable representation of it. The output is structured, hierarchical, and cross-linked: every numbered paragraph is its own chunk, every internal cross-reference is resolved to a chunk ID, every external citation is normalised, and every chunk carries enough breadcrumbs to be displayed, retrieved, or linked back to the source PDF.

The deliverable is the **pipeline itself**, not the structured output of any single document. Adding a new document means writing a new YAML config, not changing the code.

## What this is not

- Not a retrieval system. There are no embeddings, no vector store, no MCP server in this repo.
- Not a knowledge graph. The output is structured chunks with cross-reference edges; building a graph on top is a downstream concern.
- Not an LLM. No model calls anywhere in the core pipeline. Optional LLM-assisted extraction may be added later as a separate, opt-in stage.
- Not an OCR tool. Scanned PDFs are out of scope for v1. Documents must have selectable text.

## Quick start

```bash
# install from GitHub (one-time)
uv tool install git+https://github.com/peteaaron/regula.git
# …or: pipx install git+https://github.com/peteaaron/regula.git
# …or, in an existing project: uv add git+https://github.com/peteaaron/regula.git
```

The fastest path — drop a PDF anywhere on disk and run:

```bash
cd ~/Documents
regula ingest my-document.pdf
# → ./my-document/chunks.jsonl, document.json, toc.json, preview.html, …
open my-document/preview.html
```

The pipeline infers a sensible default config from the filename (lenient
validation thresholds, common paragraph numbering, basic external-reference
patterns) and writes the artifacts to `./<doc-slug>/` next to where you ran
it. Open the `preview.html` to see what was extracted; tighten the rules
with a YAML config when you need more control.

For full control, write a YAML config and pass `--config`:

```bash
# clone repo for the YAML-driven workflow
git clone https://github.com/peteaaron/regula && cd regula && uv sync

regula ingest --config configs/adb-vol1.yaml             # full pipeline
regula inspect --config configs/adb-vol1.yaml            # render preview.html
regula stage resolve_references --config configs/adb-vol1.yaml  # re-run one stage
regula validate --config configs/adb-vol1.yaml           # re-check thresholds
regula diff output/ADB1-2022 output-prev/ADB1-2022       # prove determinism
```

## How it works

The pipeline runs as a sequence of isolated stages. Each stage reads from disk, writes to disk, and can be re-run independently:

1. **`parse`** — extract document structure (Docling) and internal hyperlinks (PyMuPDF).
2. **`chunk`** — emit chunk records by walking the document tree. Default unit is the numbered paragraph.
3. **`resolve_references`** — resolve internal cross-references (hyperlink-based + regex-based) and normalise external citations.
4. **`build_toc`** — derive the table of contents from the PDF outline and link entries to chunks.
5. **`extract_glossary`** — parse the glossary section (if present) and back-fill defined-term usage on every chunk.
6. **`validate`** — run health checks against configurable thresholds (page coverage, reference resolution rate, schema conformance).
7. **`finalise`** — assemble the output artifacts and write run metadata.

Document-specific behaviour — paragraph numbering, reference patterns, glossary location, validation thresholds — lives entirely in a per-document YAML config. The core code is document-agnostic.

## Configuration

Every document gets a config file under `configs/`. See [`configs/adb-vol1.yaml`](configs/adb-vol1.yaml) for the reference example. The schema is enforced via Pydantic; invalid configs fail at load time, not mid-run.

## Output

For every successfully ingested document, the pipeline produces:

```
output/<doc_id>/
  document.json          # top-level metadata and run info
  toc.json               # table of contents → chunk references
  chunks.jsonl           # one chunk per line
  glossary.json          # defined-term lookup (if applicable)
  assets/                # extracted figures, tables, images
  intermediate/          # per-stage debug artifacts (preserved)
  validation_report.json # health metrics and pass/fail
  run.log                # structured log of the run
```

Schemas for each artifact are exported to `schemas/*.schema.json` and form the output contract for downstream consumers.

## Repo structure

```
regula/
  src/regula/
    cli.py
    pipeline.py             # stage orchestration
    config.py               # config loading & validation
    schemas.py              # Pydantic models
    parsers/                # pluggable parser implementations
    stages/                 # one module per pipeline stage
    logging.py
  configs/                  # one YAML per document
  schemas/                  # exported JSON Schemas (output contract)
  tests/
    fixtures/               # synthetic test documents
    ...
  docs/
    approach.md             # the design spec
  inputs/                   # source PDFs (gitignored)
  output/                   # generated (gitignored)
```

## Design principles

- **Process, not artifact.** The pipeline is the product. Per-document output proves it works.
- **Configurable, not branched.** Document-specific knowledge lives in YAML, never in `if doc_id == ...` code.
- **Deterministic and reproducible.** Same PDF + same config → byte-identical output. Tested explicitly.
- **Stage-isolated.** Every stage reads and writes disk artifacts. No mid-run monolith.
- **Observable.** Intermediate artifacts are preserved. Logs are structured. Failures are loud.
- **Deterministic before clever.** No LLM-assisted extraction until deterministic extraction is genuinely exhausted.

## Roadmap

In scope for v1:
- Pipeline + ADB Vol 1 (2022) ingestion as the reference case.
- A second config (synthetic fixture or real document) to prove the pipeline is generic.

Out of scope for v1, but planned:
- Embeddings and retrieval layer (separate repo).
- MCP server exposing structured retrieval.
- Cross-document entity resolution (glossary harmonisation across regulations).
- Version diffing for document amendments.
- OCR for scanned documents.

## Working with agents in this repo

See [`AGENTS.md`](AGENTS.md) for the canonical agent brief — repo conventions, where to put document-specific logic, common pitfalls, and how to validate changes. `CLAUDE.md` defers to the same file.

## License

TBD.
