"""Pydantic models for every output artifact, plus the invariant helpers
that downstream stages (notably `validate`) call against real data.

The models declared here are the pipeline's output contract. JSON Schemas
exported via :mod:`regula._schema_export` are derived from these models
and committed under ``schemas/``; downstream consumers pin against those.

Coordinate convention
---------------------

All bboxes use PDF userspace points (1/72 inch), origin **top-left**, y
increasing downward. Parsers normalise to this convention before any
schema-bound model is constructed. The convention is also written into
:class:`DocumentMeta` (``coordinate_convention``) and into each per-document
YAML under ``sourcing:`` — three independent records so a downstream
consumer cannot misinterpret.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChunkType(str, Enum):
    SECTION_HEADING = "section_heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    DIAGRAM = "diagram"
    APPENDIX = "appendix"
    GLOSSARY_ENTRY = "glossary_entry"
    REGULATION_QUOTE = "regulation_quote"
    CAPTION = "caption"


class ReferenceType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL_STANDARD = "external_standard"
    EXTERNAL_DOCUMENT = "external_document"
    REQUIREMENT = "requirement"
    CAPTION = "caption"
    DEFINED_TERM = "defined_term"


_CHUNK_ID_TYPE_ALT = "|".join(re.escape(t.value) for t in ChunkType)
_CHUNK_ID_RE = re.compile(
    rf"^[A-Za-z0-9._-]+-({_CHUNK_ID_TYPE_ALT})-[A-Za-z0-9._-]+$"
)


# --- sourcing primitives --------------------------------------------------


class SourceSpan(BaseModel):
    """One contiguous region of one page that produced part of a chunk."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    text_offset_start: int = Field(ge=0)
    text_offset_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> SourceSpan:
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"bbox must have positive width and height, got {self.bbox}")
        if self.text_offset_end <= self.text_offset_start:
            raise ValueError(
                "text_offset_end must be strictly greater than text_offset_start"
            )
        return self


class Page(BaseModel):
    """Page-level geometry. Written to intermediate/parse/pages.json."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int = 0

    @field_validator("rotation")
    @classmethod
    def _rotation_quarter_turn(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError(f"rotation must be one of 0/90/180/270, got {v}")
        return v


class Pages(BaseModel):
    pages: list[Page]

    model_config = ConfigDict(extra="forbid")


class ChunkMeta(BaseModel):
    """Sourcing metadata for a chunk. ``extra='allow'`` is intentional — this
    is one of the three escape hatches for document-specific debug info."""

    model_config = ConfigDict(extra="allow")

    source_spans: list[SourceSpan] = Field(min_length=1)
    extracted_by: str
    parser_confidence: float | None = Field(default=None, ge=0, le=1)


# --- references -----------------------------------------------------------


class Reference(BaseModel):
    """One outbound cross-reference edge from a chunk."""

    model_config = ConfigDict(extra="forbid")

    target_chunk_id: str | None
    label: str
    type: ReferenceType
    external_id: str | None = None
    pattern_name: str | None = None
    source: Literal["hyperlink", "pattern"] | None = None
    source_span: SourceSpan | None = None


class ReferenceBacklink(BaseModel):
    """One inbound reference, recorded in the inverted index."""

    model_config = ConfigDict(extra="forbid")

    source_chunk_id: str
    label: str
    type: ReferenceType
    pattern_name: str | None = None


class ReferencesIndex(BaseModel):
    """Inverted reference index — written to ``references_index.json``.

    Backlinks are *not* denormalised onto chunks; they live here so the
    chunk schema stays small and the source of truth for an edge is the
    chunk emitting it.
    """

    model_config = ConfigDict(extra="forbid")

    by_target: dict[str, list[ReferenceBacklink]] = Field(default_factory=dict)
    unresolved_internal: list[ReferenceBacklink] = Field(default_factory=list)
    external_citations: dict[str, list[str]] = Field(default_factory=dict)


# --- chunk ----------------------------------------------------------------


class Chunk(BaseModel):
    """A node in the document. Cross-references, section containment, and
    asset relations are edges. Together: the knowledge-graph substrate."""

    model_config = ConfigDict(extra="forbid")

    # identity
    chunk_id: str
    doc_id: str
    type: ChunkType

    # reading order (single source of truth)
    order_index: int = Field(ge=0)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    # section containment
    section_path: list[str]
    section_path_ids: list[str]
    parent_section_id: str | None
    breadcrumb: str
    heading_level: int | None = None

    # content
    text: str

    # edges
    references_out: list[Reference] = Field(default_factory=list)
    defined_terms_used: list[str] = Field(default_factory=list)

    # asset linkage
    asset_path: str | None = None
    asset_sidecar_path: str | None = None
    caption_target_id: str | None = None
    captioned_by_id: str | None = None

    # sourcing
    meta: ChunkMeta

    # document-specific (the third and last escape hatch)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id_format(cls, v: str) -> str:
        if not _CHUNK_ID_RE.match(v):
            raise ValueError(
                f"chunk_id {v!r} must match <doc_id>-<type>-<identifier> where "
                f"<type> is one of: {', '.join(t.value for t in ChunkType)}"
            )
        return v

    @model_validator(mode="after")
    def _check(self) -> Chunk:
        expected_prefix = f"{self.doc_id}-{self.type.value}-"
        if not self.chunk_id.startswith(expected_prefix):
            raise ValueError(
                f"chunk_id {self.chunk_id!r} does not match expected prefix "
                f"{expected_prefix!r} from doc_id + type"
            )
        if self.page_end < self.page_start:
            raise ValueError(
                f"page_end ({self.page_end}) < page_start ({self.page_start})"
            )
        if len(self.section_path) != len(self.section_path_ids):
            raise ValueError(
                f"section_path (len {len(self.section_path)}) and section_path_ids "
                f"(len {len(self.section_path_ids)}) must have equal length"
            )
        if self.type in (ChunkType.SECTION_HEADING, ChunkType.APPENDIX):
            if self.heading_level is None:
                raise ValueError(
                    f"heading_level is required when type is {self.type.value}"
                )
        if self.type in (ChunkType.TABLE, ChunkType.DIAGRAM):
            if self.asset_path is None:
                raise ValueError(
                    f"asset_path is required when type is {self.type.value}"
                )
        if self.type is ChunkType.CAPTION:
            if self.caption_target_id is None:
                raise ValueError("caption_target_id is required when type is caption")
        return self


# --- TOC ------------------------------------------------------------------


class TOCEntry(BaseModel):
    """One entry in the table of contents. Carries the order-index window
    of the section so range queries are O(1) lookups."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    level: int = Field(ge=1)
    heading_chunk_id: str
    first_chunk_id: str
    last_chunk_id: str
    first_order_index: int = Field(ge=0)
    last_order_index: int = Field(ge=0)
    page: int = Field(ge=1)
    children: list[TOCEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> TOCEntry:
        if self.last_order_index < self.first_order_index:
            raise ValueError(
                f"last_order_index ({self.last_order_index}) < "
                f"first_order_index ({self.first_order_index})"
            )
        return self


class TOC(BaseModel):
    entries: list[TOCEntry]

    model_config = ConfigDict(extra="forbid")


# --- glossary -------------------------------------------------------------


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    normalised_term: str
    definition: str
    chunk_id: str


class Glossary(BaseModel):
    entries: list[GlossaryEntry]

    model_config = ConfigDict(extra="forbid")


# --- run / validation -----------------------------------------------------


class StageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)
    ok: bool
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ValidationMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: float
    threshold: float | None = None
    passed: bool
    counts: dict[str, int] = Field(default_factory=dict)
    sample_failures: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: list[ValidationMetric]
    passed: bool
    generated_at: datetime


class DocumentMeta(BaseModel):
    """Top-level run metadata written to ``document.json``."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    edition: str
    jurisdiction: str
    legal_status: str
    source_pdf: str
    source_pdf_sha256: str
    config_sha256: str
    git_sha: str | None = None
    generated_at: datetime
    regula_version: str
    parser_versions: dict[str, str] = Field(default_factory=dict)
    page_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    stage_reports: list[StageReport] = Field(default_factory=list)
    pipeline_passed: bool
    coordinate_convention: Literal["pdf_pt_top_left"] = "pdf_pt_top_left"


# Rebuild forward refs after all classes are defined.
TOCEntry.model_rebuild()


# --- invariant helpers ----------------------------------------------------
#
# These functions enforce cross-model invariants. They're used by tests in
# Phase 1 (constructed examples) and by Stage 6 ``validate`` in later
# phases (real output). Define once, use everywhere.


class InvariantError(ValueError):
    """Raised when a cross-model invariant is violated."""


def assert_reading_order_valid(chunks: list[Chunk]) -> None:
    """``order_index`` must be unique and cover 0..N-1 exactly."""
    indices = [c.order_index for c in chunks]
    if len(set(indices)) != len(indices):
        seen: dict[int, str] = {}
        for c in chunks:
            if c.order_index in seen:
                raise InvariantError(
                    f"duplicate order_index {c.order_index}: "
                    f"{seen[c.order_index]!r} and {c.chunk_id!r}"
                )
            seen[c.order_index] = c.chunk_id
    expected = set(range(len(chunks)))
    if set(indices) != expected:
        missing = expected - set(indices)
        extra = set(indices) - expected
        raise InvariantError(
            f"order_index must cover 0..{len(chunks) - 1} exactly. "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def _flatten_toc(toc: TOC) -> dict[str, TOCEntry]:
    flat: dict[str, TOCEntry] = {}

    def walk(entry: TOCEntry) -> None:
        flat[entry.heading_chunk_id] = entry
        for child in entry.children:
            walk(child)

    for entry in toc.entries:
        walk(entry)
    return flat


def assert_section_windows_consistent(toc: TOC, chunks: list[Chunk]) -> None:
    """Every chunk with a parent section must fall inside that section's
    [first_order_index, last_order_index] window."""
    flat = _flatten_toc(toc)
    by_id = {c.chunk_id: c for c in chunks}
    for entry in flat.values():
        if entry.heading_chunk_id not in by_id:
            raise InvariantError(
                f"TOC entry {entry.id!r} references unknown "
                f"heading chunk_id {entry.heading_chunk_id!r}"
            )
    for c in chunks:
        if c.parent_section_id is None:
            continue
        if c.parent_section_id not in flat:
            raise InvariantError(
                f"chunk {c.chunk_id!r} has parent_section_id "
                f"{c.parent_section_id!r} not present in TOC"
            )
        entry = flat[c.parent_section_id]
        if not (entry.first_order_index <= c.order_index <= entry.last_order_index):
            raise InvariantError(
                f"chunk {c.chunk_id!r} (order_index={c.order_index}) is outside "
                f"its parent section {c.parent_section_id!r} window "
                f"[{entry.first_order_index}, {entry.last_order_index}]"
            )


def assert_asset_linkage_bidirectional(chunks: list[Chunk]) -> None:
    """Caption chunks point at a table/diagram, which must point back."""
    by_id = {c.chunk_id: c for c in chunks}
    for c in chunks:
        if c.type is ChunkType.CAPTION:
            target_id = c.caption_target_id
            if target_id is None:
                raise InvariantError(f"caption {c.chunk_id!r} has no caption_target_id")
            target = by_id.get(target_id)
            if target is None:
                raise InvariantError(
                    f"caption {c.chunk_id!r} targets unknown chunk {target_id!r}"
                )
            if target.type not in (ChunkType.TABLE, ChunkType.DIAGRAM):
                raise InvariantError(
                    f"caption {c.chunk_id!r} targets {target_id!r} "
                    f"whose type is {target.type.value}, not table/diagram"
                )
            if target.captioned_by_id != c.chunk_id:
                raise InvariantError(
                    f"caption {c.chunk_id!r} → {target_id!r} is not bidirectional: "
                    f"target.captioned_by_id={target.captioned_by_id!r}"
                )


def assert_source_spans_in_bounds(chunk: Chunk) -> None:
    """Every source span must index a non-empty slice of ``chunk.text``."""
    n = len(chunk.text)
    for i, span in enumerate(chunk.meta.source_spans):
        if span.text_offset_end > n:
            raise InvariantError(
                f"chunk {chunk.chunk_id!r} span {i}: text_offset_end "
                f"{span.text_offset_end} > len(text) {n}"
            )
        if chunk.text[span.text_offset_start : span.text_offset_end] == "":
            raise InvariantError(
                f"chunk {chunk.chunk_id!r} span {i} addresses empty substring"
            )
