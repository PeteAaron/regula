"""Per-document YAML config.

Post-wind-back the config surface is minimal: identity + source PDF +
parser choice. Block extraction needs no per-document knobs — that's
the point. Higher-level concerns (chunking rules, reference patterns,
glossary location, validation thresholds) re-enter the config when
their stages are reintroduced as opt-in.

Loading is strict (``extra='forbid'`` on every model) and eager — typos
fail at load, not mid-pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ParserConfig(BaseModel):
    """Which parser implementations to use for this document."""

    model_config = ConfigDict(extra="forbid")

    primary: str = Field(
        default="pymupdf",
        description=(
            "Name of the structural parser. ``pymupdf`` is the default "
            "and always available; ``docling`` is supported but requires "
            "installing the optional dependency."
        ),
    )
    link_extractor: str = Field(
        default="pymupdf",
        description="Parser used for the PDF outline and hyperlinks.",
    )


class SourcingConfig(BaseModel):
    """Coordinate convention. Frozen but explicit so downstream tools can
    inspect the config rather than guessing."""

    model_config = ConfigDict(extra="forbid")

    coordinate_origin: Literal["top_left"] = Field(default="top_left")
    coordinate_unit: Literal["pt"] = Field(default="pt")


class Config(BaseModel):
    """Top-level per-document config."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(
        description=(
            "Short, stable identifier — e.g. 'adb1-2022'. Prefix of every "
            "block_id and the output directory name."
        )
    )
    title: str = Field(description="Human-readable title.")
    edition: str = Field(default="unknown", description="Edition or year of the document.")
    jurisdiction: str = Field(default="unknown", description="Legal jurisdiction.")
    legal_status: str = Field(default="unknown", description="Legal status.")
    source_pdf: str = Field(description="Path to the source PDF.")

    parsers: ParserConfig = Field(default_factory=ParserConfig)
    sourcing: SourcingConfig = Field(default_factory=SourcingConfig)


# --- loader / hashing -----------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Load and validate a per-document YAML config."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a YAML mapping at the top level")
    return Config.model_validate(data)


def config_sha256(cfg: Config) -> str:
    """Canonical JSON dump → sha256. Stable across runs."""
    data = cfg.model_dump(mode="json")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_pdf_sha256(pdf_path: str | Path) -> str:
    """Stream a file through sha256."""
    h = hashlib.sha256()
    with Path(pdf_path).open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# --- zero-config inference ------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DOC_ID_MAX_LEN = 40


def _slugify(name: str, max_len: int = _DOC_ID_MAX_LEN) -> str:
    """Filename stem → safe doc_id, truncated at word boundary."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        return "document"
    if len(slug) <= max_len:
        return slug
    truncated = slug[:max_len].rsplit("-", 1)[0]
    return truncated or slug[:max_len]


def infer_config(pdf_path: str | Path) -> Config:
    """Build a Config from a PDF path alone."""
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(f"PDF not found: {p}")
    stem = p.stem
    return Config(
        doc_id=_slugify(stem),
        title=stem.replace("-", " ").replace("_", " ").strip() or "Untitled",
        source_pdf=str(p),
    )
