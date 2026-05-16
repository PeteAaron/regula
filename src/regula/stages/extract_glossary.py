"""Stage 5 — `extract_glossary`. Parse glossary section, back-fill terms.

When ``cfg.references.glossary_section`` is configured, this stage looks
up that section in the TOC, walks paragraph chunks within its
``order_index`` window, and treats each one as a candidate glossary
entry. A simple heuristic splits the first sentence's leading
capitalised phrase as the *term* and the rest as the *definition* —
sufficient for many regulatory glossaries; real-document tuning may
require document-specific extension via the ``attributes`` config block.

After building the glossary, every chunk *outside* the glossary section
gets ``defined_terms_used`` back-filled with the normalised terms that
appear (whole-word, case-insensitive) in its text.

When no glossary section is configured, the stage is a no-op — writes
an empty :class:`Glossary` and a pass-through chunks.jsonl.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from regula.config import Config
from regula.logging import bind_stage, get_logger
from regula.schemas import (
    TOC,
    Chunk,
    ChunkType,
    Glossary,
    GlossaryEntry,
    StageReport,
    TOCEntry,
)

NAME = "extract_glossary"

# Heuristic: a "term" is the leading capitalised words/phrase up to one
# of these delimiters or a sentence break.
_TERM_DELIMITER = re.compile(r"\s*[—:.;-]\s+|\s{2,}|\s+is\s+|\s+means\s+", flags=re.IGNORECASE)


def _flatten_toc(entries: list[TOCEntry]) -> list[TOCEntry]:
    out: list[TOCEntry] = []
    for e in entries:
        out.append(e)
        out.extend(_flatten_toc(e.children))
    return out


def _find_glossary_window(
    toc: TOC, glossary_section: str | None
) -> tuple[int, int] | None:
    if not glossary_section:
        return None
    target_norm = glossary_section.strip().lower()
    for entry in _flatten_toc(toc.entries):
        if entry.label.strip().lower() == target_norm:
            return entry.first_order_index, entry.last_order_index
        if target_norm in entry.label.strip().lower():
            return entry.first_order_index, entry.last_order_index
    return None


def _split_term_definition(text: str) -> tuple[str, str] | None:
    """Return ``(term, definition)`` if the text looks like a glossary entry."""
    text = text.strip()
    if not text:
        return None
    match = _TERM_DELIMITER.search(text)
    if match is None:
        return None
    term = text[: match.start()].strip()
    definition = text[match.end():].strip()
    if not term or not definition or len(term.split()) > 8:
        return None
    return term, definition


def _normalise_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def _load_chunks(path: Path) -> list[Chunk]:
    return [
        Chunk.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_chunks(path: Path, chunks: list[Chunk]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(c.model_dump_json() + "\n")


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    resolve_dir = output_dir / "intermediate" / "resolve_references"
    toc_path = output_dir / "intermediate" / "build_toc" / "toc.json"
    if not resolve_dir.exists():
        raise FileNotFoundError(
            f"extract_glossary stage requires resolve_references output at {resolve_dir}"
        )
    if not toc_path.exists():
        raise FileNotFoundError(
            f"extract_glossary stage requires build_toc output at {toc_path}"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    chunks = _load_chunks(resolve_dir / "chunks.jsonl")
    toc = TOC.model_validate_json(toc_path.read_text(encoding="utf-8"))

    glossary_section = cfg.references.glossary_section
    window = _find_glossary_window(toc, glossary_section)

    log.info(
        "extract_glossary.start",
        chunks=len(chunks),
        glossary_section=glossary_section or "(none)",
        window=str(window) if window else None,
    )

    entries: list[GlossaryEntry] = []
    in_glossary: set[str] = set()
    if window is not None:
        first_oi, last_oi = window
        for c in chunks:
            if not (first_oi <= c.order_index <= last_oi):
                continue
            # The chunker emits non-paragraph text inside the glossary
            # section as type=glossary_entry; we honour that.
            if c.type is not ChunkType.GLOSSARY_ENTRY:
                continue
            in_glossary.add(c.chunk_id)
            split = _split_term_definition(c.text)
            if split is None:
                continue
            term, definition = split
            entries.append(
                GlossaryEntry(
                    term=term,
                    normalised_term=_normalise_term(term),
                    definition=definition,
                    chunk_id=c.chunk_id,
                )
            )

    # Back-fill defined_terms_used on every chunk outside the glossary.
    term_patterns: list[tuple[str, re.Pattern[str]]] = [
        (e.normalised_term, re.compile(r"\b" + re.escape(e.normalised_term) + r"\b", flags=re.IGNORECASE))
        for e in entries
    ]
    n_chunks_with_terms = 0
    for c in chunks:
        if c.chunk_id in in_glossary or not term_patterns:
            continue
        found: list[str] = []
        for norm, pat in term_patterns:
            if pat.search(c.text):
                found.append(norm)
        if found:
            c.defined_terms_used = sorted(set(found))
            n_chunks_with_terms += 1

    _write_chunks(stage_dir / "chunks.jsonl", chunks)
    (stage_dir / "glossary.json").write_text(
        Glossary(entries=entries).model_dump_json(indent=2)
    )

    finished = datetime.now(UTC)
    log.info(
        "extract_glossary.done",
        terms=len(entries),
        chunks_with_terms=n_chunks_with_terms,
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={
            "glossary_terms": len(entries),
            "chunks_with_terms": n_chunks_with_terms,
        },
    )
