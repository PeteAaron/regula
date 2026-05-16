"""Canonical list of capabilities deferred to later roadmap phases.

The list is static — each entry names something the pipeline could
emit but doesn't (yet). Per-run observed counts are filled in by
:func:`assemble_deferred_features` at finalise time, drawing from
stage reports that record what they saw and skipped.

Keep this list honest: when a feature lands, remove its entry here.
When a new gap appears, add an entry rather than silently shipping
incomplete output.
"""

from __future__ import annotations

from regula.schemas import DeferredFeature, StageReport

# (name, description, target_phase) — observed_count is set per run.
_BASE: list[tuple[str, str, str]] = [
    (
        "chunking",
        "Block-to-chunk grouping (paragraphs, headings, lists, etc.) is "
        "intentionally deferred. The default pipeline emits unclassified "
        "Blocks; chunking depends on document-specific conventions that "
        "the user identifies after inspecting the blocks.",
        "Later — user-driven",
    ),
    (
        "reference_resolution",
        "Cross-references between blocks (and to external standards) are "
        "not resolved. PDF hyperlinks are extracted into links.json but "
        "not yet mapped onto specific destination blocks.",
        "Later — depends on chunking",
    ),
    (
        "table_of_contents",
        "The PDF outline is captured in outline.json but not yet expanded "
        "into a hierarchical, block-linked TOC.",
        "Later — depends on chunking",
    ),
    (
        "glossary_extraction",
        "Defined terms are not extracted. Requires both block-level "
        "structure (chunking) and per-document knowledge of which section "
        "holds the glossary.",
        "Later — depends on chunking",
    ),
    (
        "diagram_blocks",
        "Raster images are detected by the parser but not yet emitted as "
        "first-class image blocks with asset linkage.",
        "Later",
    ),
    (
        "table_blocks",
        "Tables aren't extracted as structured data. PyMuPDF doesn't "
        "expose table structure; a Docling or custom detector is needed.",
        "Later",
    ),
    (
        "docling_parser",
        "Docling is unavailable in offline/sandboxed environments because "
        "its layout and OCR models live on huggingface.co. The pluggable "
        "parser registry has a slot ready for it.",
        "When environment permits",
    ),
]


# Keys that stages put in their ``StageReport.counts`` to indicate they
# saw deferred candidates. ``{count_key: feature_name}``.
_COUNT_KEY_TO_FEATURE: dict[str, str] = {
    "images": "diagram_blocks",
}


def assemble_deferred_features(
    stage_reports: list[StageReport],
) -> list[DeferredFeature]:
    """Build the per-run deferred-feature list, folding in any observed
    counts that stages reported."""
    observed: dict[str, int] = {}
    for r in stage_reports:
        for key, value in r.counts.items():
            feature = _COUNT_KEY_TO_FEATURE.get(key)
            if feature is None:
                continue
            observed[feature] = observed.get(feature, 0) + int(value)
    return [
        DeferredFeature(
            name=name,
            description=desc,
            target_phase=phase,
            observed_count=observed.get(name),
        )
        for name, desc, phase in _BASE
    ]
