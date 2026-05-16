"""Stage 3 — `resolve_references`. Discover cross-references on every chunk.

Two passes are combined:

* **Hyperlink pass** — every PDF hyperlink from the parse stage is mapped
  to a source chunk (by source-page+bbox containment) and, if internal,
  to a target chunk (by destination-page+y containment). Hyperlink-derived
  references are authoritative.
* **Pattern pass** — each regex from ``cfg.references.patterns`` is run
  over every chunk's text. Internal patterns resolve to a target chunk
  via paragraph-number matching; external patterns produce a normalised
  ``external_id``. The pattern pass populates references that hyperlinks
  missed.

When both passes fire on the same (source, target) pair the hyperlink-
derived reference wins. The stage also emits an inverted backlink
index — :class:`regula.schemas.ReferencesIndex` — as a sidecar artifact.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from regula.config import Config, ReferencePattern
from regula.logging import bind_stage, get_logger
from regula.schemas import (
    Chunk,
    ChunkType,
    Reference,
    ReferenceBacklink,
    ReferencesIndex,
    ReferenceType,
    SourceSpan,
    StageReport,
)

NAME = "resolve_references"


# --- IO helpers ----------------------------------------------------------


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


# --- normalisation helpers ----------------------------------------------


def _normalise_external_id(pattern: ReferencePattern, match_text: str) -> str:
    """Best-effort normalisation of an external citation."""
    cleaned = re.sub(r"\s+", " ", match_text).strip()
    cleaned = cleaned.replace(" ", "-")
    return cleaned


def _internal_target_index(chunks: list[Chunk]) -> dict[tuple[ChunkType, str], str]:
    """Map (type, identifier) → chunk_id so paragraph numbers like '2.4' can
    be resolved to the chunk_id 'ADB1-2022-paragraph-2.4'. Identifiers are
    derived from the chunk_id's trailing component."""
    index: dict[tuple[ChunkType, str], str] = {}
    for c in chunks:
        # chunk_id is "<doc_id>-<type>-<identifier>" but the slug stripped
        # punctuation. Recover the identifier from the chunk_id suffix.
        prefix = f"{c.doc_id}-{c.type.value}-"
        identifier = c.chunk_id[len(prefix):]
        index[(c.type, identifier.lower())] = c.chunk_id
    return index


# --- hyperlink pass ------------------------------------------------------


def _bbox_contains(outer: tuple[float, float, float, float], x: float, y: float) -> bool:
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def _bbox_overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _chunk_containing_point(
    chunks: list[Chunk], page: int, y: float
) -> Chunk | None:
    """Return the chunk whose source span on ``page`` contains the given y."""
    best: tuple[float, Chunk] | None = None
    for c in chunks:
        for span in c.meta.source_spans:
            if span.page != page:
                continue
            x0, y0, x1, y1 = span.bbox
            if y0 <= y <= y1:
                return c
            distance = min(abs(y - y0), abs(y - y1))
            if best is None or distance < best[0]:
                best = (distance, c)
    return best[1] if best is not None else None


def _chunk_overlapping_bbox(
    chunks: list[Chunk], page: int, bbox: tuple[float, float, float, float]
) -> Chunk | None:
    for c in chunks:
        for span in c.meta.source_spans:
            if span.page != page:
                continue
            if _bbox_overlaps(span.bbox, bbox):
                return c
    return None


def _hyperlink_references(
    chunks: list[Chunk], links: list[dict[str, Any]]
) -> dict[str, list[Reference]]:
    """Returns map from source chunk_id to list of Reference."""
    out: dict[str, list[Reference]] = {}
    for link in links:
        src_bbox = tuple(float(c) for c in link["bbox"])
        src_chunk = _chunk_overlapping_bbox(chunks, int(link["page"]), src_bbox)
        if src_chunk is None:
            continue
        if link["kind"] == "internal":
            dest_page = int(link.get("dest_page", 0))
            dest_point = link.get("dest_point") or [0.0, 0.0]
            dest_y = float(dest_point[1]) if len(dest_point) > 1 else 0.0
            target = _chunk_containing_point(chunks, dest_page, dest_y)
            if target is None or target.chunk_id == src_chunk.chunk_id:
                continue
            ref = Reference(
                target_chunk_id=target.chunk_id,
                label=src_chunk.text[
                    : min(len(src_chunk.text), 80)
                ].strip()
                or "(hyperlink)",
                type=ReferenceType.INTERNAL,
                source="hyperlink",
            )
        else:
            ref = Reference(
                target_chunk_id=None,
                label=str(link.get("uri", "")),
                type=ReferenceType.EXTERNAL_DOCUMENT,
                external_id=str(link.get("uri", "")),
                source="hyperlink",
            )
        out.setdefault(src_chunk.chunk_id, []).append(ref)
    return out


# --- pattern pass --------------------------------------------------------


def _pattern_references(
    chunks: list[Chunk],
    patterns: list[ReferencePattern],
    target_index: dict[tuple[ChunkType, str], str],
) -> dict[str, list[Reference]]:
    """Run each pattern over every chunk's text."""
    out: dict[str, list[Reference]] = {}
    for chunk in chunks:
        for pattern in patterns:
            regex = re.compile(pattern.regex)
            for m in regex.finditer(chunk.text):
                full = m.group(0)
                captured = m.group(1) if m.groups() else full
                target_id: str | None = None
                external_id: str | None = None
                if pattern.type is ReferenceType.INTERNAL:
                    # Try paragraph lookup first.
                    target_id = (
                        target_index.get((ChunkType.PARAGRAPH, captured.lower()))
                        or target_index.get(
                            (ChunkType.SECTION_HEADING, captured.lower())
                        )
                        or target_index.get((ChunkType.APPENDIX, captured.lower()))
                    )
                elif pattern.type in (
                    ReferenceType.EXTERNAL_STANDARD,
                    ReferenceType.EXTERNAL_DOCUMENT,
                    ReferenceType.REQUIREMENT,
                ):
                    external_id = _normalise_external_id(pattern, full)
                ref = Reference(
                    target_chunk_id=target_id,
                    label=full,
                    type=pattern.type,
                    external_id=external_id,
                    pattern_name=pattern.name,
                    source="pattern",
                    source_span=SourceSpan(
                        page=chunk.meta.source_spans[0].page,
                        bbox=chunk.meta.source_spans[0].bbox,
                        text_offset_start=m.start(),
                        text_offset_end=m.end(),
                    ),
                )
                out.setdefault(chunk.chunk_id, []).append(ref)
    return out


# --- merge + dedupe ------------------------------------------------------


def _dedupe(refs: list[Reference]) -> list[Reference]:
    """Drop references that duplicate (target_chunk_id|external_id, type)
    pairs already produced by a hyperlink-source reference."""
    keep: list[Reference] = []
    hyperlinked_pairs: set[tuple[str | None, str | None, ReferenceType]] = set()
    # First pass: collect all hyperlink-source pairs.
    for r in refs:
        if r.source == "hyperlink":
            hyperlinked_pairs.add((r.target_chunk_id, r.external_id, r.type))
    # Second pass: keep hyperlinks always; drop pattern duplicates.
    for r in refs:
        if r.source == "hyperlink":
            keep.append(r)
            continue
        if (r.target_chunk_id, r.external_id, r.type) in hyperlinked_pairs:
            continue
        keep.append(r)
    return keep


def _build_index(chunks: list[Chunk]) -> ReferencesIndex:
    by_target: dict[str, list[ReferenceBacklink]] = {}
    unresolved: list[ReferenceBacklink] = []
    external: dict[str, list[str]] = {}
    for c in chunks:
        for ref in c.references_out:
            backlink = ReferenceBacklink(
                source_chunk_id=c.chunk_id,
                label=ref.label,
                type=ref.type,
                pattern_name=ref.pattern_name,
            )
            if ref.type is ReferenceType.INTERNAL:
                if ref.target_chunk_id is None:
                    unresolved.append(backlink)
                else:
                    by_target.setdefault(ref.target_chunk_id, []).append(backlink)
            elif ref.external_id is not None:
                external.setdefault(ref.external_id, []).append(c.chunk_id)
    return ReferencesIndex(
        by_target=by_target,
        unresolved_internal=unresolved,
        external_citations=external,
    )


# --- stage runner --------------------------------------------------------


def run(output_dir: Path, cfg: Config) -> StageReport:
    bind_stage(NAME)
    log = get_logger(NAME)
    started = datetime.now(UTC)

    chunk_dir = output_dir / "intermediate" / "chunk"
    parse_dir = output_dir / "intermediate" / "parse"
    if not chunk_dir.exists():
        raise FileNotFoundError(
            f"resolve_references stage requires chunk output at {chunk_dir} "
            f"(run `regula stage chunk` first)"
        )
    if not parse_dir.exists():
        raise FileNotFoundError(
            f"resolve_references stage requires parse output at {parse_dir}"
        )

    stage_dir = output_dir / "intermediate" / NAME
    stage_dir.mkdir(parents=True, exist_ok=True)

    chunks = _load_chunks(chunk_dir / "chunks.jsonl")
    links = json.loads((parse_dir / "links.json").read_text(encoding="utf-8"))

    log.info(
        "resolve_references.start",
        chunks=len(chunks),
        links=len(links),
        patterns=len(cfg.references.patterns),
    )

    hyperlink_refs = _hyperlink_references(chunks, links)
    target_index = _internal_target_index(chunks)
    pattern_refs = _pattern_references(chunks, cfg.references.patterns, target_index)

    n_internal_resolved = 0
    n_internal_unresolved = 0
    n_external = 0
    for c in chunks:
        merged = hyperlink_refs.get(c.chunk_id, []) + pattern_refs.get(c.chunk_id, [])
        merged = _dedupe(merged)
        c.references_out = merged
        for ref in merged:
            if ref.type is ReferenceType.INTERNAL:
                if ref.target_chunk_id is None:
                    n_internal_unresolved += 1
                else:
                    n_internal_resolved += 1
            else:
                n_external += 1

    _write_chunks(stage_dir / "chunks.jsonl", chunks)

    index = _build_index(chunks)
    (stage_dir / "references_index.json").write_text(index.model_dump_json(indent=2))

    finished = datetime.now(UTC)
    log.info(
        "resolve_references.done",
        internal_resolved=n_internal_resolved,
        internal_unresolved=n_internal_unresolved,
        external=n_external,
    )
    return StageReport(
        stage=NAME,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        ok=True,
        counts={
            "internal_resolved": n_internal_resolved,
            "internal_unresolved": n_internal_unresolved,
            "external": n_external,
        },
    )
