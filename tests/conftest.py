"""Shared fixtures for tests.

The post-wind-back pipeline operates at block level only. The synthetic
PDF fixtures here exercise the parse + extract_blocks + validate flow
without committing a binary fixture.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small deterministic 2-page PDF used by stage-level tests."""
    path = tmp_path_factory.mktemp("pdf") / "synth.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Section 1: Introduction", fontsize=18)
    page.insert_text(
        (72, 110),
        "1.1 This document describes the safety provisions for buildings.",
        fontsize=11,
    )
    page.insert_text(
        (72, 140),
        "See paragraph 1.2 and BS EN 13501-1 for further detail.",
        fontsize=11,
    )
    page.insert_text(
        (72, 180),
        "1.2 Application of these provisions depends on building type.",
        fontsize=11,
    )
    page = doc.new_page()
    page.insert_text((72, 60), "1.3 Section heading for testing", fontsize=14)
    page.insert_text(
        (72, 110), "1.3a This paragraph contains specific guidance.", fontsize=11
    )
    doc.set_toc(
        [
            [1, "Section 1: Introduction", 1],
            [2, "Section heading for testing", 2],
        ]
    )
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def synthetic_config_text(synthetic_pdf: Path) -> str:
    """A YAML config string pointing at the synthetic PDF.

    Minimal because the post-wind-back config schema has no
    per-document knobs beyond identity + source.
    """
    return f"""
doc_id: SYNTH
title: "Synthetic test"
source_pdf: {synthetic_pdf}
parsers:
  primary: pymupdf
"""
