# regula

> A deterministic pipeline that extracts every text block from a regulatory PDF, preserving page, position, and font signals — so a human can see *what's there* before deciding how to chunk it.

**Status:** wound back from the v0 chunk-oriented pipeline (2026-05-16). Block extraction works end-to-end; chunking, references, glossary, and TOC are deferred until block-level review identifies document-specific conventions worth automating.

## What this is

`regula` reads a PDF and writes one record per text block per page, with no classification, no merging, and no filtering. Each block carries its bbox, font signals, an advisory `region` (header/footer/margin/body, from y-position) and `looks_like` hint (large_text/body/emphasis/small_text, from font signals) — but nothing downstream filters or merges based on them. They're hints for the human reviewer.

The HTML preview surfaces every block from every page so noise patterns (running headers, footers, online-version watermarks) are visible at a glance. Those become ignore-rules later; the pipeline never guesses on the user's behalf.

## What this is not

- Not a chunker. It does not group blocks into paragraphs, headings, or sections. Those decisions depend on conventions that differ between documents — they live downstream of block inspection.
- Not a reference resolver. PDF hyperlinks are captured in `links.json` with their source and destination bboxes, but they're not yet mapped onto specific blocks.
- Not an LLM. No model calls anywhere.
- Not an OCR tool. Documents must have selectable text.

## Quick start

```bash
# install from GitHub (one-time)
uv tool install git+https://github.com/peteaaron/regula.git
# …or: pipx install git+https://github.com/peteaaron/regula.git
```

Drop a PDF anywhere and run:

```bash
cd ~/Documents
regula ingest my-document.pdf
# → ./my-document/blocks.jsonl, pages.json, links.json, preview.html, …
open my-document/preview.html
```

A `./<doc-slug>/` directory appears next to the PDF, holding:

```
my-document/
  blocks.jsonl            # one Block per line (text + bbox + font + advisory hints)
  pages.json              # per-page geometry (width, height, rotation)
  links.json              # every hyperlink, with source bbox and destination
  outline.json            # PDF outline as the parser reports it
  document.json           # run metadata (doc_id, sha256s, regula version, …)
  validation_report.json  # advisory health metrics; never fails the run
  deferred.json           # what the pipeline could do but doesn't yet
  preview.html            # page-by-page diagnostic view (open in any browser)
  intermediate/           # per-stage debug artifacts (preserved)
  run.log                 # structured log of the run
```

For a YAML-driven workflow with a stable `doc_id`:

```bash
git clone https://github.com/peteaaron/regula && cd regula && uv sync
regula ingest --config configs/adb-vol1.yaml
regula inspect --config configs/adb-vol1.yaml   # re-render preview.html
regula diff output/ADB1-2022 output-prev/ADB1-2022   # prove determinism
```

## Pipeline

Three stages, all reading from and writing to disk:

1. **`parse`** — PyMuPDF reads the PDF, producing a flat element list (one per text block as the parser sees it), pages, hyperlinks, raster images, and the PDF outline. Written to `intermediate/parse/`.
2. **`extract_blocks`** — converts parser elements into typed `Block` records. Computes char-weighted median body font size, then assigns each block an advisory `region` (from y-position) and `looks_like` (from font + bold/italic). No filtering. Written to `intermediate/extract_blocks/blocks.jsonl`.
3. **`validate`** — informational metrics only (blocks-per-page, region breakdown, link kinds, font distribution). Plus JSON Schema conformance — that one *can* fail the run, because a malformed artifact means the extractor itself is broken.

`finalise` then copies artifacts to the output root. There's no `chunk`, `resolve_references`, `build_toc`, or `extract_glossary` stage in the default pipeline.

## Output contract

Every artifact validates against a committed JSON Schema under `schemas/`. The key ones:

- `block.schema.json` — fields on each `Block` record
- `pages.schema.json` — page geometry
- `links.schema.json` — hyperlinks
- `document.schema.json` — run metadata
- `validation_report.schema.json` — advisory metrics

Downstream consumers should pin against the JSON Schemas, not the Python models.

## Why so minimal?

The earlier (v0) pipeline tried to be helpful: it pattern-matched numbered paragraphs, claimed PDF outline entries as section headings, merged page-break continuations, and resolved cross-references — all driven by regexes in the per-document YAML config. On well-behaved synthetic fixtures it worked. On real documents (Approved Document B Vol 1) the conventions didn't hold tightly enough: paragraphs got stolen as headings via outline substring matches, continuations were misclassified on page breaks, and the title page / contents page silently dropped out because nothing matched a "paragraph" or "heading" pattern.

The lesson: don't classify until you've seen what's there. The wound-back pipeline emits everything and lets the human declare conventions, not the other way round.

The deleted machinery still lives in `schemas.py` (the `Chunk`, `TOC`, `Glossary`, `Reference` models) for the day a chunking stage is reintroduced as an opt-in step after the user has identified what counts as a paragraph in this document.

## Design principles

- **Extract, don't classify.** The pipeline's job is to faithfully surface what's on the page. Classification is downstream.
- **Position + font are evidence, not verdicts.** Advisory hints are computed; nothing filters on them.
- **Deterministic and reproducible.** Same PDF + same config → byte-identical output (excluding timestamped artifacts the diff tool strips).
- **Stage-isolated.** Every stage reads and writes disk artifacts. No mid-run state.
- **Observable.** Intermediate artifacts are preserved. Logs are structured.

## Repo structure

```
regula/
  src/regula/
    cli.py
    pipeline.py             # stage orchestration
    config.py               # minimal: doc_id, title, source_pdf, parsers
    schemas.py              # Block + Links + Pages + DocumentMeta + legacy models
    inspect.py              # page-oriented HTML preview
    parsers/                # pluggable parser implementations (pymupdf today)
    stages/                 # parse, extract_blocks, validate, finalise
    deferred.py             # canonical list of capabilities not yet built
  configs/                  # one YAML per document
  schemas/                  # exported JSON Schemas (output contract)
  tests/
  docs/
  inputs/                   # source PDFs (gitignored)
  output/                   # generated (gitignored)
```

## Roadmap

Block extraction is in. The shape of the next step depends on which patterns the previews reveal — see `deferred.json` for the working list. Likely candidates, in rough order:

1. Manual ignore rules in the YAML config (e.g. "drop any block whose text matches `^Online version$`").
2. An opt-in `chunk` stage that consumes user-defined paragraph and heading rules.
3. Link resolution from hyperlinks to specific destination blocks.
4. Table-of-contents construction from the PDF outline + located blocks.

## License

TBD.
