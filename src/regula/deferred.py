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
        "diagram_chunks",
        "Raster images are detected by the parser but not yet emitted as "
        "type=diagram chunks with bidirectional asset linkage.",
        "Phase 5",
    ),
    (
        "table_chunks",
        "Tables are not extracted. PyMuPDF doesn't expose table structure; "
        "a Docling backend (or a custom detector) is needed for type=table.",
        "Phase 5",
    ),
    (
        "caption_chunks",
        "Captions for tables/figures are not detected or bidirectionally "
        "linked to their target chunks.",
        "Phase 5",
    ),
    (
        "regulation_quote_chunks",
        "Verbatim Building Regulations quotes (and similar inset legal "
        "text) are not specially classified — they currently appear as "
        "ordinary paragraph chunks.",
        "Phase 6",
    ),
    (
        "docling_parser",
        "Docling is unavailable in offline/sandboxed environments because "
        "its layout and OCR models live on huggingface.co and modelscope.cn. "
        "The pluggable parser registry has a slot ready for it.",
        "When environment permits",
    ),
    (
        "external_id_versioning",
        "External standard references (e.g. 'BS EN 13501-1:2018') are "
        "normalised by stripping whitespace only. A proper extractor would "
        "split the standard family, part number, and year edition.",
        "Phase 5",
    ),
    (
        "real_synthetic_fixture",
        "configs/_fixture-small.yaml points at a one-page placeholder PDF. "
        "A multi-page synthetic regulatory fixture (with appendices, tables, "
        "diagrams, glossary) is planned to replace it.",
        "Phase 5",
    ),
    (
        "adb_smoke_test",
        "End-to-end smoke test against ADB Vol 1 (the real reference "
        "document) is not yet wired up because the PDF can't be committed.",
        "Phase 5",
    ),
]


# Keys that stages put in their ``StageReport.counts`` to indicate they
# saw deferred candidates. ``{count_key: feature_name}``.
_COUNT_KEY_TO_FEATURE: dict[str, str] = {
    "deferred_images_skipped": "diagram_chunks",
    "deferred_unclassified_text": "regulation_quote_chunks",
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
