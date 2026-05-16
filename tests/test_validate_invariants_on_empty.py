"""Closes the Phase 1 ↔ Phase 2 loop: the same invariant helpers that
were tested in Phase 1 with constructed data now run inside the validate
stage on the skeleton's real (empty) output."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regula.cli import app

CONFIG = "configs/_fixture-small.yaml"
OUTPUT = Path("output/FIXTURE-SMALL")

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_run() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    runner.invoke(app, ["ingest", "--config", CONFIG])


def test_invariants_passed_metric_is_present_and_passed() -> None:
    import json

    report = json.loads((OUTPUT / "validation_report.json").read_text())
    metric = next(m for m in report["metrics"] if m["name"] == "invariants_ok")
    assert metric["passed"] is True
    assert metric["counts"]["violations"] == 0


def test_schemas_valid_metric_passes_on_skeleton() -> None:
    import json

    report = json.loads((OUTPUT / "validation_report.json").read_text())
    metric = next(m for m in report["metrics"] if m["name"] == "schemas_valid")
    assert metric["passed"] is True
    assert metric["counts"]["failed"] == 0


def test_invariant_helpers_callable_on_real_output() -> None:
    # Directly call them on the on-disk artifacts, the way Stage 6 does.
    from regula.schemas import (
        TOC,
        Chunk,
        ReferencesIndex,
        assert_asset_linkage_bidirectional,
        assert_reading_order_valid,
        assert_section_windows_consistent,
        assert_source_spans_in_bounds,
    )

    chunks_path = OUTPUT / "chunks.jsonl"
    toc_path = OUTPUT / "toc.json"
    refs_path = OUTPUT / "references_index.json"

    assert chunks_path.exists() and toc_path.exists() and refs_path.exists()

    chunks = [
        Chunk.model_validate_json(line)
        for line in chunks_path.read_text().splitlines()
        if line.strip()
    ]
    toc = TOC.model_validate_json(toc_path.read_text())

    assert_reading_order_valid(chunks)
    assert_section_windows_consistent(toc, chunks)
    assert_asset_linkage_bidirectional(chunks)
    for c in chunks:
        assert_source_spans_in_bounds(c)
    _ = ReferencesIndex.model_validate_json(refs_path.read_text())
