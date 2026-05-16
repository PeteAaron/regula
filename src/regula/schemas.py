"""Pydantic models for every output artifact, plus the invariant helpers
that downstream stages (notably ``validate``) call against real data.

The models declared here are the pipeline's output contract. JSON Schemas
exported via :mod:`regula._schema_export` are derived from these models
and committed under ``schemas/``; downstream consumers pin against those.

For a non-technical, prose walk-through of every model and field, see
``docs/schemas.md``. The docstrings and ``Field(description=...)`` strings
here are intentionally brief — they exist so the generated JSON Schemas
have inline descriptions for tools that read them programmatically. The
narrative explanation lives in the Markdown.

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
    """Kind of element a chunk represents.

    Drives validation rules (e.g. tables must carry an ``asset_path``)
    and is part of every ``chunk_id``. See ``docs/schemas.md`` §Chunk types
    for when each value is used.
    """

    SECTION_HEADING = "section_heading"  # a numbered or named heading
    PARAGRAPH = "paragraph"              # numbered body paragraph (the default)
    TABLE = "table"                      # tabular block; carries asset_path
    DIAGRAM = "diagram"                  # figure/image; carries asset_path
    APPENDIX = "appendix"                # an appendix's top-level heading
    GLOSSARY_ENTRY = "glossary_entry"    # one defined term in the glossary
    REGULATION_QUOTE = "regulation_quote"  # quoted text from the regulation itself
    CAPTION = "caption"                  # caption belonging to a table/diagram


class ReferenceType(str, Enum):
    """Kind of edge between a chunk and its target.

    See ``docs/schemas.md`` §References for guidance on which type to use.
    """

    INTERNAL = "internal"                    # points at another chunk in this doc
    EXTERNAL_STANDARD = "external_standard"  # cites a British/European standard
    EXTERNAL_DOCUMENT = "external_document"  # cites another regulatory document
    REQUIREMENT = "requirement"              # cites a Building Regulation requirement
    CAPTION = "caption"                      # caption ↔ asset edge
    DEFINED_TERM = "defined_term"            # chunk ↔ glossary edge


_CHUNK_ID_TYPE_ALT = "|".join(re.escape(t.value) for t in ChunkType)
_CHUNK_ID_RE = re.compile(
    rf"^[A-Za-z0-9._-]+-({_CHUNK_ID_TYPE_ALT})-[A-Za-z0-9._-]+$"
)


# --- sourcing primitives --------------------------------------------------


class SourceSpan(BaseModel):
    """One contiguous region of one page that produced part of a chunk.

    A chunk can have multiple spans: one paragraph wrapping across a page
    break would have two; one wrapping across two columns would have two
    on the same page. Together, the spans give exact, lossless provenance
    from a chunk's text back to the source PDF.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(
        ge=1,
        description="1-indexed page number in the source PDF.",
    )
    bbox: tuple[float, float, float, float] = Field(
        description=(
            "Bounding box (x0, y0, x1, y1) in PDF points, top-left origin. "
            "x1 > x0 and y1 > y0 are enforced."
        )
    )
    text_offset_start: int = Field(
        ge=0,
        description=(
            "Inclusive offset into chunk.text where this span's substring begins."
        ),
    )
    text_offset_end: int = Field(
        ge=0,
        description=(
            "Exclusive offset into chunk.text where this span's substring ends. "
            "Must be strictly greater than text_offset_start."
        ),
    )

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
    """Per-page geometry. Lets consumers map a bbox onto the rendered page
    even when the PDF has rotated or unusually sized pages."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1, description="1-indexed page number.")
    width: float = Field(gt=0, description="Page width in PDF points.")
    height: float = Field(gt=0, description="Page height in PDF points.")
    rotation: int = Field(
        default=0,
        description="Page rotation in degrees: 0, 90, 180, or 270.",
    )

    @field_validator("rotation")
    @classmethod
    def _rotation_quarter_turn(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError(f"rotation must be one of 0/90/180/270, got {v}")
        return v


class Pages(BaseModel):
    """The collection of pages for the source PDF — one entry per page,
    in page-number order. Written to ``intermediate/parse/pages.json``."""

    model_config = ConfigDict(extra="forbid")

    pages: list[Page] = Field(description="Pages in order, 1..N.")


# --- blocks ---------------------------------------------------------------
#
# A Block is the primary output of the wound-back pipeline (see
# AGENTS.md "wind-back" entry, 2026-05-16): one text element per page,
# unclassified, with positional and font metadata preserved. The user
# inspects blocks in the preview and decides which to keep, ignore, or
# group into higher-level structures downstream.
#
# No regex-driven heading detection, no paragraph-number matching, no
# continuation merging. Just "what the parser saw on the page".


class BlockRegion(str, Enum):
    """Where a block sits on the page — derived from y-position only.
    Advisory: a "footer"-region block isn't filtered out, it's just
    flagged so the user can spot running-footer patterns at a glance."""

    HEADER = "header"  # top ~7% of the page
    FOOTER = "footer"  # bottom ~7% of the page
    MARGIN = "margin"  # left or right edge band
    BODY = "body"      # everything else


class BlockLooksLike(str, Enum):
    """Visual-signal-only guess at what a block resembles. Strictly
    advisory: nothing downstream filters or groups by this value. The
    user can sort/filter the preview by it to spot patterns."""

    LARGE_TEXT = "large_text"      # font_size noticeably larger than body
    SMALL_TEXT = "small_text"      # font_size noticeably smaller than body
    EMPHASIS = "emphasis"          # body-sized but bold or italic
    BODY = "body"                  # body-sized normal weight
    UNKNOWN = "unknown"            # no useful signal


class Block(BaseModel):
    """One text block from one page, as the parser saw it.

    Blocks are deliberately unclassified — no "paragraph", no "heading",
    no merging across page breaks. The pipeline's job is to extract;
    classification and grouping happen downstream once the user has
    spotted what to ignore in the preview.
    """

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(
        description=(
            "Deterministic identifier of the form "
            "``<doc_id>:p<page>:b<reading_order_index>`` — e.g. "
            "``adb1-2022:p3:b7``. Stable across runs on the same PDF."
        )
    )
    doc_id: str = Field(description="The document this block belongs to.")
    page: int = Field(ge=1, description="1-indexed page number.")
    reading_order_index: int = Field(
        ge=0,
        description=(
            "Position within the page in the parser's reading order. "
            "0-indexed; unique per page."
        ),
    )
    bbox: tuple[float, float, float, float] = Field(
        description=(
            "Bounding box (x0, y0, x1, y1) in PDF points, top-left origin."
        )
    )
    text: str = Field(description="The block's textual content as the parser returned it.")
    font_size: float = Field(ge=0, description="Font size of the first span in the block, in PDF points.")
    font_name: str = Field(description="Font name of the first span — e.g. 'Helvetica-Bold'.")
    is_bold: bool = Field(description="Whether the first span has the bold flag.")
    is_italic: bool = Field(description="Whether the first span has the italic flag.")
    region: BlockRegion = Field(
        description=(
            "Advisory: where on the page this block sits. Computed from "
            "y-position relative to page height. Never used to filter "
            "anything — it's there so the user can spot header/footer "
            "patterns in the preview."
        )
    )
    looks_like: BlockLooksLike = Field(
        description=(
            "Advisory: a visual-signal-only guess at what kind of text "
            "this is. Based on font size relative to the document's "
            "char-weighted median body size, plus bold/italic flags. "
            "Strictly advisory."
        )
    )

    @model_validator(mode="after")
    def _check(self) -> Block:
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"bbox must have positive width and height, got {self.bbox}")
        expected_prefix = f"{self.doc_id}:p{self.page}:b{self.reading_order_index}"
        if self.block_id != expected_prefix:
            raise ValueError(
                f"block_id {self.block_id!r} must equal "
                f"<doc_id>:p<page>:b<reading_order_index> = {expected_prefix!r}"
            )
        return self


class PageLink(BaseModel):
    """One hyperlink from the source PDF, attached to the page it sits
    on. Internal links resolve to a destination page + point; external
    links carry a URI. Written to ``links.json``."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1, description="1-indexed page the link sits on.")
    bbox: tuple[float, float, float, float] = Field(
        description="Clickable region of the link on the source page."
    )
    kind: Literal["internal", "external"] = Field(description="Link kind.")
    uri: str | None = Field(
        default=None,
        description="External target URI. None for internal links.",
    )
    dest_page: int | None = Field(
        default=None,
        ge=1,
        description="Destination page for internal links. None for external.",
    )
    dest_point: tuple[float, float] | None = Field(
        default=None,
        description="Destination point on dest_page for internal links.",
    )


class Links(BaseModel):
    """All hyperlinks extracted from the source PDF, page-ordered."""

    model_config = ConfigDict(extra="forbid")

    links: list[PageLink] = Field(default_factory=list, description="All links, in page order.")


# --- chunks (legacy v0 contract; not used by the default pipeline) -------
#
# The Chunk / TOC / Glossary / Reference models below were the v0 output
# contract. They're preserved in code so a future opt-in chunking stage
# can be reintroduced once block-level inspection has identified what
# counts as a paragraph in a given document. The default pipeline does
# not write these artifacts.



class ChunkMeta(BaseModel):
    """Sourcing metadata attached to every chunk.

    ``extra='allow'`` is intentional: parsers can attach freeform debug
    info (e.g. Docling element IDs) without forcing a schema change.
    """

    model_config = ConfigDict(extra="allow")

    source_spans: list[SourceSpan] = Field(
        min_length=1,
        description=(
            "One or more regions of the PDF that produced this chunk. "
            "Concatenating chunk.text[span.start:span.end] for each span "
            "in order yields the chunk's text."
        ),
    )
    extracted_by: str = Field(
        description=(
            "Identifier(s) of the parser(s) that produced this chunk, with "
            "versions — e.g. 'docling@2.x, pymupdf@1.24'."
        )
    )
    parser_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Optional 0..1 confidence score from the parser, if it exposes one. "
            "Used by validation; not interpreted by downstream stages."
        ),
    )


# --- references -----------------------------------------------------------


class Reference(BaseModel):
    """One outbound cross-reference from a chunk to a target.

    Produced by Stage 3 (``resolve_references``). Internal references that
    couldn't be resolved keep ``target_chunk_id=None`` and surface in the
    validation report as resolution-rate misses, not schema errors.
    """

    model_config = ConfigDict(extra="forbid")

    target_chunk_id: str | None = Field(
        description=(
            "chunk_id this reference points at. None when the target is "
            "external (a BS standard, another Approved Document) or when "
            "an internal reference couldn't be resolved."
        )
    )
    label: str = Field(
        description=(
            "Surface text as it appeared in the source — e.g. 'paragraph 2.4', "
            "'Diagram 3.1', 'BS EN 13501-1:2018'."
        )
    )
    type: ReferenceType = Field(description="The kind of edge this reference represents.")
    external_id: str | None = Field(
        default=None,
        description=(
            "Normalised identifier for external references — e.g. "
            "'BS-EN-13501-1:2018'. None for internal references."
        ),
    )
    pattern_name: str | None = Field(
        default=None,
        description=(
            "Name of the config regex pattern that fired (None if the "
            "reference came from a PDF hyperlink). Useful for debugging "
            "pattern coverage."
        ),
    )
    source: Literal["hyperlink", "pattern"] | None = Field(
        default=None,
        description=(
            "How the reference was discovered. Hyperlink-derived references "
            "are preferred over pattern-derived ones during deduplication."
        ),
    )
    source_span: SourceSpan | None = Field(
        default=None,
        description=(
            "Where in the source PDF the reference's surface text appears. "
            "Useful for highlighting the citation in a UI."
        ),
    )


class ReferenceBacklink(BaseModel):
    """One inbound reference, recorded in the inverted index. Backlinks are
    *derived* from ``Chunk.references_out`` — never edit them by hand."""

    model_config = ConfigDict(extra="forbid")

    source_chunk_id: str = Field(description="chunk_id of the chunk that emitted this reference.")
    label: str = Field(description="The surface text of the original reference.")
    type: ReferenceType = Field(description="The kind of edge.")
    pattern_name: str | None = Field(
        default=None,
        description="Name of the config pattern that fired, if pattern-derived.",
    )


class ReferencesIndex(BaseModel):
    """Inverted reference index written to ``references_index.json``.

    Phase 3 (``resolve_references``) emits this after assembling
    ``Chunk.references_out`` on every chunk. Backlinks are *not* attached
    to chunks themselves; they live here so the source of truth for each
    edge stays on the emitting chunk.
    """

    model_config = ConfigDict(extra="forbid")

    by_target: dict[str, list[ReferenceBacklink]] = Field(
        default_factory=dict,
        description=(
            "Map from target chunk_id to the list of inbound references. "
            "Answers 'what cites paragraph 2.4?'"
        ),
    )
    unresolved_internal: list[ReferenceBacklink] = Field(
        default_factory=list,
        description=(
            "Internal references whose target couldn't be resolved. Each "
            "one is a validation-rate miss; the list is bounded by the "
            "sample size in the validation report, not here."
        ),
    )
    external_citations: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map from normalised external_id (e.g. 'BS-EN-13501-1:2018') to "
            "the chunk_ids that cite it. Answers 'which paragraphs cite "
            "this standard?'"
        ),
    )


# --- chunk ----------------------------------------------------------------


class Chunk(BaseModel):
    """The core node of the output graph.

    A chunk is one self-contained, addressable piece of a document — a
    numbered paragraph, a heading, a table, a diagram, a caption, a
    glossary entry. Everything else in the contract is either an edge
    between chunks (``references_out``, ``parent_section_id``,
    ``caption_target_id``), provenance on the chunk (``meta``), or an
    index over chunks (``TOC``, ``ReferencesIndex``).

    See ``docs/schemas.md`` §Chunk for a field-by-field plain-English guide.
    """

    model_config = ConfigDict(extra="forbid")

    # identity
    chunk_id: str = Field(
        description=(
            "Globally unique identifier in the form '<doc_id>-<type>-<identifier>'. "
            "Stable across pipeline runs on the same PDF + config."
        )
    )
    doc_id: str = Field(
        description="The document this chunk belongs to (matches Config.doc_id)."
    )
    type: ChunkType = Field(description="What kind of element this chunk represents.")

    # reading order
    order_index: int = Field(
        ge=0,
        description=(
            "The chunk's position in the document's reading order. Unique, "
            "0..N-1 across the document. THE single authoritative ordering — "
            "never sort by page+bbox at query time."
        ),
    )
    page_start: int = Field(
        ge=1,
        description="1-indexed page number where this chunk begins.",
    )
    page_end: int = Field(
        ge=1,
        description=(
            "1-indexed page number where this chunk ends (inclusive; equal "
            "to page_start for a single-page chunk)."
        ),
    )

    # section containment
    section_path: list[str] = Field(
        description=(
            "Human-readable labels of every section heading above this chunk, "
            "outermost first — e.g. ['B1', 'Means of warning…', '2 Fire alarms']. "
            "For display only. Never label-match against this for retrieval."
        )
    )
    section_path_ids: list[str] = Field(
        description=(
            "chunk_ids of the section-heading chunks corresponding to each "
            "label in section_path. Same length as section_path. Use these "
            "for 'is X under section Y' queries."
        )
    )
    parent_section_id: str | None = Field(
        description=(
            "chunk_id of the immediately-enclosing section heading. None "
            "only for top-level headings."
        )
    )
    breadcrumb: str = Field(
        description=(
            "Pre-rendered ' > '-joined section_path. Display-only convenience."
        )
    )
    heading_level: int | None = Field(
        default=None,
        description=(
            "Required when type is section_heading or appendix; gives the "
            "nesting depth (1 is outermost). None for non-heading chunks."
        ),
    )

    # content
    text: str = Field(description="The chunk's textual content, post-normalisation.")

    # edges
    references_out: list[Reference] = Field(
        default_factory=list,
        description=(
            "Outbound cross-references discovered by Stage 3. Empty for "
            "chunks that don't cite anything. THE source of truth for "
            "every edge from this chunk."
        ),
    )
    defined_terms_used: list[str] = Field(
        default_factory=list,
        description=(
            "Normalised glossary terms that appear (whole-word, "
            "case-insensitive) in this chunk's text. Back-filled by Stage 5."
        ),
    )

    # asset linkage
    asset_path: str | None = Field(
        default=None,
        description=(
            "Path to the extracted binary asset, relative to "
            "output/<doc_id>/ — e.g. 'assets/diagram-3.1.png'. Required "
            "for tables and diagrams."
        ),
    )
    asset_sidecar_path: str | None = Field(
        default=None,
        description=(
            "Path to a JSON sidecar describing the asset (e.g. table cells), "
            "if any. None when the binary alone is sufficient."
        ),
    )
    caption_target_id: str | None = Field(
        default=None,
        description=(
            "When this chunk is a caption: the chunk_id of the table/diagram "
            "it captions. Required for type=caption."
        ),
    )
    captioned_by_id: str | None = Field(
        default=None,
        description=(
            "When this chunk is captioned: the chunk_id of its caption. "
            "Bidirectional with caption_target_id."
        ),
    )

    # sourcing
    meta: ChunkMeta = Field(
        description=(
            "Provenance: which PDF region(s) produced this chunk, by which "
            "parser. Always populated."
        )
    )

    # escape hatch
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Document-specific tags that don't fit the core schema — e.g. "
            "{'requirement': ['B1'], 'applies_to': ['dwellinghouse']}. The "
            "only schema escape hatch on the chunk itself."
        ),
    )

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
    """One entry in the table of contents.

    Carries pre-computed ``first_order_index..last_order_index`` bounds so
    "every chunk in this section" and "every chunk between section A and
    section B" are cheap range queries — never tree walks plus label
    matching.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Stable identifier for the TOC entry — e.g. 'toc-1.2.3'."
    )
    label: str = Field(description="The heading's display text.")
    level: int = Field(
        ge=1,
        description="Nesting depth, 1 outermost.",
    )
    heading_chunk_id: str = Field(
        description="The section_heading or appendix chunk this entry points at."
    )
    first_chunk_id: str = Field(
        description="chunk_id with the smallest order_index inside this section."
    )
    last_chunk_id: str = Field(
        description="chunk_id with the largest order_index inside this section."
    )
    first_order_index: int = Field(
        ge=0,
        description="Smallest order_index inside this section (inclusive).",
    )
    last_order_index: int = Field(
        ge=0,
        description="Largest order_index inside this section (inclusive).",
    )
    page: int = Field(ge=1, description="1-indexed page where this section begins.")
    children: list[TOCEntry] = Field(
        default_factory=list,
        description="Nested TOC entries (subsections).",
    )

    @model_validator(mode="after")
    def _check(self) -> TOCEntry:
        if self.last_order_index < self.first_order_index:
            raise ValueError(
                f"last_order_index ({self.last_order_index}) < "
                f"first_order_index ({self.first_order_index})"
            )
        return self


class TOC(BaseModel):
    """Hierarchical table of contents derived from the PDF outline and
    cross-checked against section_heading chunks. Written to ``toc.json``."""

    model_config = ConfigDict(extra="forbid")

    entries: list[TOCEntry] = Field(description="Top-level TOC entries.")


# --- glossary -------------------------------------------------------------


class GlossaryEntry(BaseModel):
    """One defined term and its definition, extracted by Stage 5."""

    model_config = ConfigDict(extra="forbid")

    term: str = Field(description="The term as written in the document (display form).")
    normalised_term: str = Field(
        description=(
            "Lowercase, whitespace-collapsed version of term used for matching "
            "against chunk text. This is what appears in Chunk.defined_terms_used."
        )
    )
    definition: str = Field(description="The term's definition, normalised text.")
    chunk_id: str = Field(
        description=(
            "chunk_id of the glossary_entry chunk that holds this term. Lets "
            "the lookup table round-trip back to the source."
        )
    )


class Glossary(BaseModel):
    """All defined terms in the document. Written to ``glossary.json``.
    Empty when the document has no glossary section."""

    model_config = ConfigDict(extra="forbid")

    entries: list[GlossaryEntry] = Field(description="Defined-term lookup table.")


# --- run / validation -----------------------------------------------------


class StageReport(BaseModel):
    """Summary of one stage's execution — written into the run log and into
    ``DocumentMeta.stage_reports``. Returned by every stage function."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(description="Stage name, e.g. 'chunk' or 'resolve_references'.")
    started_at: datetime = Field(description="UTC start timestamp.")
    finished_at: datetime = Field(description="UTC finish timestamp.")
    duration_seconds: float = Field(ge=0, description="Wall-clock duration.")
    ok: bool = Field(description="Whether the stage completed without errors.")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Stage-specific counters — e.g. {'chunks_emitted': 1247, "
            "'continuations_merged': 18}. Free-form."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal anomalies the stage flagged for human review.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Fatal errors. Non-empty implies ok=False.",
    )


class ValidationMetric(BaseModel):
    """One health check result with its threshold and verdict."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Metric name — e.g. 'page_coverage'.")
    value: float = Field(description="The measured value (typically a 0..1 rate).")
    threshold: float | None = Field(
        default=None,
        description="The configured threshold, if any. None for informational metrics.",
    )
    passed: bool = Field(description="Whether the value cleared the threshold.")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Raw counts that produced the value — e.g. {'covered': 198, 'total': 200}.",
    )
    sample_failures: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=20,
        description="Up to 20 illustrative failure cases for human inspection.",
    )


class ValidationReport(BaseModel):
    """Health report from Stage 6, written to ``validation_report.json``.

    ``passed`` is the conjunction of every metric's ``passed`` field; the
    pipeline exits non-zero when ``passed`` is False unless ``--no-fail``
    is set.
    """

    model_config = ConfigDict(extra="forbid")

    metrics: list[ValidationMetric] = Field(description="Every health metric the run produced.")
    passed: bool = Field(description="Did every threshold-bearing metric pass?")
    generated_at: datetime = Field(description="UTC timestamp when validation ran.")


class DeferredFeature(BaseModel):
    """A capability the pipeline knows about but does not yet implement.

    Written to ``deferred.json`` on every run so that downstream consumers
    (and humans reviewing output) can tell the difference between
    "0 tables were emitted because there are none" and
    "0 tables were emitted because the table extractor isn't built yet".

    ``observed_count`` is populated when a stage *saw* but intentionally
    *skipped* something (e.g. images detected by the parser but not yet
    emitted as diagram chunks). A non-zero value is a useful signal that
    implementing the feature would materially change this document's
    output. ``None`` means the run produced no signal either way.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Stable identifier — e.g. 'diagram_chunks'.")
    description: str = Field(
        description="One-sentence summary of what's not (yet) implemented."
    )
    target_phase: str = Field(
        description="Roadmap phase that will land this — e.g. 'Phase 5'."
    )
    observed_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "If a stage detected candidate inputs but skipped them, the "
            "count goes here. ``None`` if the run had nothing to report."
        ),
    )


class DeferredFeatureList(BaseModel):
    """Sidecar container written to ``deferred.json``."""

    model_config = ConfigDict(extra="forbid")

    features: list[DeferredFeature] = Field(
        description="One entry per deferred capability for this run."
    )
    generated_at: datetime = Field(description="UTC timestamp.")


class DocumentMeta(BaseModel):
    """Top-level run metadata written to ``document.json``.

    Read this file first when reviewing a pipeline run — it carries the
    document identity, the source-PDF and config hashes (which together
    determine reproducibility), pipeline pass/fail, and per-stage reports.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(description="The document's stable identifier.")
    title: str = Field(description="Human-readable title.")
    edition: str = Field(description="Which edition/year of the document this is.")
    jurisdiction: str = Field(description="Legal jurisdiction — e.g. 'England'.")
    legal_status: str = Field(description="Legal status — e.g. 'Approved Document'.")
    source_pdf: str = Field(description="Path to the source PDF (relative to repo root).")
    source_pdf_sha256: str = Field(
        description=(
            "SHA-256 of the source PDF bytes. Combined with config_sha256, "
            "determines whether a re-run is needed."
        )
    )
    config_sha256: str = Field(
        description="SHA-256 of the canonical-JSON-serialised config used for this run."
    )
    git_sha: str | None = Field(
        default=None,
        description="Git SHA of the regula source at run time, if available.",
    )
    generated_at: datetime = Field(description="UTC timestamp when the run finished.")
    regula_version: str = Field(description="regula package version (e.g. '0.0.1').")
    parser_versions: dict[str, str] = Field(
        default_factory=dict,
        description="Version of each parser used — e.g. {'docling': '2.0.1', 'pymupdf': '1.24.5'}.",
    )
    page_count: int = Field(ge=0, description="Number of pages in the source PDF.")
    block_count: int = Field(
        default=0,
        ge=0,
        description="Number of text blocks the pipeline emitted.",
    )
    chunk_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of chunks the pipeline emitted. Legacy v0 field — "
            "always 0 in the post-wind-back default pipeline (chunking "
            "is deferred); preserved so downstream tools that pin against "
            "the v0 schema still load."
        ),
    )
    stage_reports: list[StageReport] = Field(
        default_factory=list,
        description="One StageReport per executed stage, in execution order.",
    )
    pipeline_passed: bool = Field(
        description="Did every stage complete and every validation threshold pass?"
    )
    coordinate_convention: Literal["pdf_pt_top_left"] = Field(
        default="pdf_pt_top_left",
        description=(
            "Convention for all bboxes in this run's output. Frozen at "
            "'pdf_pt_top_left' (PDF points, top-left origin) for now."
        ),
    )
    deferred_features: list[DeferredFeature] = Field(
        default_factory=list,
        description=(
            "Capabilities not yet implemented. Mirrors ``deferred.json`` "
            "so reviewers see at a glance what isn't (yet) covered."
        ),
    )


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
