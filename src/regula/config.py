"""Per-document YAML config: typed models, eager validation, hashing helpers.

The YAML file is the *only* place document-specific knowledge lives. If you
find yourself wanting to add an ``if doc_id == ...`` branch to a stage, push
the knob into the config schema instead.

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
    model_config = ConfigDict(extra="forbid")

    primary: str = "docling"
    link_extractor: str = "pymupdf"


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_regex: str
    heading_levels: list[int]
    merge_continuations: bool = True
    preserve_reading_order: bool = True
    treat_captions_as_chunks: bool = True

    @field_validator("paragraph_regex")
    @classmethod
    def _compile(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid paragraph_regex {v!r}: {e}") from e
        return v


class ReferencePattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    regex: str
    type: ReferenceType

    @field_validator("regex")
    @classmethod
    def _compile(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid reference regex {v!r}: {e}") from e
        return v


class ReferencesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patterns: list[ReferencePattern] = Field(default_factory=list)
    glossary_section: str | None = None


class AttributesConfig(BaseModel):
    """Document-specific detection knobs. ``extra='allow'`` is intentional —
    this is the one place future flags slot in without a schema change."""

    model_config = ConfigDict(extra="allow")

    detect_requirement_scope: bool = False
    detect_building_type: bool = False


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_internal_ref_resolution: float = Field(default=0.95, ge=0, le=1)
    min_page_coverage: float = Field(default=0.98, ge=0, le=1)
    min_text_reconstruction_coverage: float = Field(default=0.97, ge=0, le=1)
    min_reading_order_monotonicity: float = Field(default=0.98, ge=0, le=1)
    fail_on_schema_error: bool = True


class SourcingConfig(BaseModel):
    """Coordinate convention. Frozen but explicit so downstream tools can
    inspect the config rather than guessing."""

    model_config = ConfigDict(extra="forbid")

    coordinate_origin: Literal["top_left"] = "top_left"
    coordinate_unit: Literal["pt"] = "pt"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    edition: str
    jurisdiction: str
    legal_status: str
    source_pdf: str

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
