"""Per-document YAML config: typed models, eager validation, hashing helpers.

The YAML file is the *only* place document-specific knowledge lives. If you
find yourself wanting to add an ``if doc_id == ...`` branch to a stage, push
the knob into the config schema instead.

For a prose walk-through of every config field and what it controls, see
``docs/schemas.md`` §Configuration.

Loading is strict (``extra='forbid'`` on every model except the freeform
``attributes`` block) and eager (regexes compile at load time, so typos fail
at config load rather than mid-pipeline).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from regula.schemas import ReferenceType


class ParserConfig(BaseModel):
    """Which parser implementations to use for this document. Defaults
    cover digital PDFs; a future OCR parser would slot in by changing the
    ``primary`` value."""

    model_config = ConfigDict(extra="forbid")

    primary: str = Field(
        default="docling",
        description="Name of the structural parser (produces the document tree).",
    )
    link_extractor: str = Field(
        default="pymupdf",
        description="Name of the parser that extracts PDF outline and hyperlinks.",
    )


class ChunkingConfig(BaseModel):
    """How to split the document into chunks. Controls Stage 2 (``chunk``)."""

    model_config = ConfigDict(extra="forbid")

    paragraph_regex: str = Field(
        description=(
            "Regex that recognises the start of a numbered body paragraph in "
            "this document. Capture group 1 is the paragraph number used to "
            "build chunk_ids. Compiled at config-load time — invalid regex "
            "fails fast."
        )
    )
    heading_levels: list[int] = Field(
        description=(
            "Nesting levels the parser should treat as section_heading "
            "chunks — e.g. [1, 2, 3, 4]."
        )
    )
    merge_continuations: bool = Field(
        default=True,
        description=(
            "When True, paragraphs without their own number that follow a "
            "numbered paragraph are merged into it as continuations. "
            "Disable only if the document has a stricter numbering scheme."
        ),
    )
    preserve_reading_order: bool = Field(
        default=True,
        description=(
            "When True, chunks are emitted in the parser's reading order "
            "(currently always True; the flag is here to make the contract "
            "explicit)."
        ),
    )
    treat_captions_as_chunks: bool = Field(
        default=True,
        description=(
            "When True, table/diagram captions are emitted as their own "
            "caption-type chunks with bidirectional links to their target. "
            "Disable only if you genuinely want captions folded into "
            "surrounding paragraphs (lossy)."
        ),
    )

    @field_validator("paragraph_regex")
    @classmethod
    def _compile(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid paragraph_regex {v!r}: {e}") from e
        return v


class ReferencePattern(BaseModel):
    """One regex used by Stage 3 to find cross-references in chunk text.

    Internal patterns capture a target identifier (e.g. paragraph number)
    in group 1; Stage 3 resolves it to a chunk_id. External patterns
    typically don't capture — the surface text is normalised into
    ``external_id``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "Identifier for this pattern; appears in Reference.pattern_name. "
            "Use snake_case — e.g. 'internal_paragraph', 'bs_standard'."
        )
    )
    regex: str = Field(
        description="The regex itself. Compiled at config-load time."
    )
    type: ReferenceType = Field(
        description="The kind of edge this pattern produces."
    )

    @field_validator("regex")
    @classmethod
    def _compile(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid reference regex {v!r}: {e}") from e
        return v


class ReferencesConfig(BaseModel):
    """How to find cross-references. Controls Stage 3 (``resolve_references``)."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[ReferencePattern] = Field(
        default_factory=list,
        description=(
            "Regex patterns to run over every chunk's text. PDF hyperlinks "
            "are handled separately and always take precedence."
        ),
    )
    glossary_section: str | None = Field(
        default=None,
        description=(
            "Name of the section containing the glossary — e.g. "
            "'Appendix E'. Tells Stage 5 where to find defined terms. "
            "None when the document has no glossary."
        ),
    )


class AttributesConfig(BaseModel):
    """Document-specific detection knobs. ``extra='allow'`` is intentional —
    this is the one place future flags slot in without a schema change.

    Anything added here is purely advisory: stages read it to decide
    whether to populate corresponding entries under ``Chunk.attributes``.
    """

    model_config = ConfigDict(extra="allow")

    detect_requirement_scope: bool = Field(
        default=False,
        description=(
            "When True, tag each chunk with the Building Regulation "
            "requirement (B1..B5) inferred from its section context."
        ),
    )
    detect_building_type: bool = Field(
        default=False,
        description=(
            "When True, tag chunks with applicable building types "
            "(e.g. 'dwellinghouse', 'flat') mentioned in the text."
        ),
    )


class ValidationConfig(BaseModel):
    """Thresholds for Stage 6. Each threshold is a 0..1 minimum rate the
    corresponding metric must clear for the pipeline to pass."""

    model_config = ConfigDict(extra="forbid")

    min_internal_ref_resolution: float = Field(
        default=0.95,
        ge=0,
        le=1,
        description=(
            "Minimum fraction of internal references that must resolve to a "
            "known chunk. Below this, the run fails."
        ),
    )
    min_page_coverage: float = Field(
        default=0.98,
        ge=0,
        le=1,
        description=(
            "Minimum fraction of pages that must have at least one chunk. "
            "Catches accidental whole-page misses."
        ),
    )
    min_text_reconstruction_coverage: float = Field(
        default=0.97,
        ge=0,
        le=1,
        description=(
            "Walking chunks in order and concatenating their text must "
            "cover this fraction of the source PDF's text (whitespace-"
            "normalised). The round-trip guarantee."
        ),
    )
    min_reading_order_monotonicity: float = Field(
        default=0.98,
        ge=0,
        le=1,
        description=(
            "Per page, chunks sorted by order_index should have non-"
            "decreasing y-coordinates this fraction of the time. Catches "
            "reading-order regressions."
        ),
    )
    fail_on_schema_error: bool = Field(
        default=True,
        description=(
            "When True (default), any artifact failing its JSON Schema "
            "fails the run. Disable only for debugging."
        ),
    )


class SourcingConfig(BaseModel):
    """Coordinate convention. Frozen but explicit so downstream tools can
    inspect the config rather than guessing."""

    model_config = ConfigDict(extra="forbid")

    coordinate_origin: Literal["top_left"] = Field(
        default="top_left",
        description="Where the bbox origin is. Frozen at 'top_left' for now.",
    )
    coordinate_unit: Literal["pt"] = Field(
        default="pt",
        description="Bbox unit. Frozen at 'pt' (PDF points = 1/72 inch).",
    )


class Config(BaseModel):
    """Top-level per-document config. One YAML file per document — adding a
    new document means writing a new config, not changing code."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(
        description=(
            "Short, stable identifier for the document — e.g. 'ADB1-2022'. "
            "Used as the prefix of every chunk_id and as the output "
            "directory name. Must be unique per document."
        )
    )
    title: str = Field(description="Human-readable title.")
    edition: str = Field(description="Edition or year of the document.")
    jurisdiction: str = Field(description="Legal jurisdiction — e.g. 'England'.")
    legal_status: str = Field(
        description="Legal status — e.g. 'Approved Document', 'British Standard'."
    )
    source_pdf: str = Field(
        description=(
            "Path to the source PDF, relative to the repository root. The "
            "file itself is gitignored under inputs/."
        )
    )

    parsers: ParserConfig = Field(default_factory=ParserConfig)
    chunking: ChunkingConfig
    references: ReferencesConfig = Field(default_factory=ReferencesConfig)
    attributes: AttributesConfig = Field(default_factory=AttributesConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    sourcing: SourcingConfig = Field(default_factory=SourcingConfig)


# --- loader / hashing -----------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Load and validate a per-document YAML config. Raises on any schema
    or regex error — never silently coerces."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a YAML mapping at the top level")
    return Config.model_validate(data)


def config_sha256(cfg: Config) -> str:
    """Canonical JSON dump → sha256. Stable across Python sessions because
    ``json.dumps`` with ``sort_keys=True`` is deterministic on dict order
    and Pydantic v2's ``model_dump(mode='json')`` is total."""
    data = cfg.model_dump(mode="json")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_pdf_sha256(pdf_path: str | Path) -> str:
    """Stream a file through sha256. Stable across runs for a given byte
    sequence — used by :class:`regula.schemas.DocumentMeta` to detect when
    a source PDF has changed and a re-run is needed."""
    h = hashlib.sha256()
    with Path(pdf_path).open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# --- zero-config inference ------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Filename stem → safe doc_id (lowercase, alphanumerics + hyphens)."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "document"


def infer_config(pdf_path: str | Path) -> Config:
    """Build a Config from a PDF path alone. Used by ``regula <pdf>`` to
    let users run the pipeline with no YAML.

    Defaults are intentionally permissive: validation thresholds are low
    so a first run almost always completes, the paragraph regex matches
    common ``1.1`` / ``2.4a`` style numbering, and ``pymupdf`` is the
    primary parser (docling is deferred). When inference is wrong the
    user can dump the inferred config to YAML, tweak it, and re-run with
    ``--config``.
    """
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(f"PDF not found: {p}")
    stem = p.stem
    return Config(
        doc_id=_slugify(stem),
        title=stem.replace("-", " ").replace("_", " ").strip() or "Untitled",
        edition="unknown",
        jurisdiction="unknown",
        legal_status="unknown",
        source_pdf=str(p),
        parsers=ParserConfig(primary="pymupdf", link_extractor="pymupdf"),
        chunking=ChunkingConfig(
            paragraph_regex=r"^(\d+\.\d+[a-z]?)\s+",
            heading_levels=[1, 2, 3, 4],
        ),
        references=ReferencesConfig(
            patterns=[
                ReferencePattern(
                    name="internal_paragraph",
                    regex=r"paragraph(?:s)?\s+(\d+\.\d+[a-z]?)",
                    type="internal",
                ),
                ReferencePattern(
                    name="bs_standard",
                    regex=r"BS\s?(?:EN\s)?\d+(?:-\d+)?(?::\d{4})?",
                    type="external_standard",
                ),
            ],
        ),
        validation=ValidationConfig(
            min_internal_ref_resolution=0.0,
            min_page_coverage=0.5,
            min_text_reconstruction_coverage=0.5,
            min_reading_order_monotonicity=0.8,
            fail_on_schema_error=True,
        ),
    )
