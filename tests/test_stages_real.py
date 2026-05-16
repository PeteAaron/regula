"""End-to-end stage tests against the synthetic PDF.

Phase 4 makes the previously-stub stages do real work. These tests
exercise the full pipeline against a generated 2-page synthetic PDF
and assert: chunks are produced with correct types and ordering, the
TOC matches the heading hierarchy, references resolve, the reference
index has correct backlinks, and validation passes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from regula.cli import app

runner = CliRunner()


@pytest.fixture
def synth_output(
    tmp_path: Path, synthetic_config_text: str
) -> Path:
    """Run the pipeline once against the synthetic PDF; return output dir."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(synthetic_config_text)
    cfg = yaml.safe_load(synthetic_config_text)
    out_dir = Path("output") / cfg["doc_id"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir


def _load_chunks(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_chunks_emitted_in_reading_order(synth_output: Path) -> None:
    chunks = _load_chunks(synth_output / "chunks.jsonl")
    assert len(chunks) == 5
    types = [c["type"] for c in chunks]
    assert types == [
        "section_heading", "paragraph", "paragraph",
        "section_heading", "paragraph",
    ]
    # order_index covers 0..4 exactly
    assert sorted(c["order_index"] for c in chunks) == [0, 1, 2, 3, 4]


def test_paragraph_continuation_merged(synth_output: Path) -> None:
    chunks = _load_chunks(synth_output / "chunks.jsonl")
    # paragraph 1.1 absorbed the "See paragraph 1.2 and BS EN 13501-1..." line.
    p11 = next(c for c in chunks if c["chunk_id"].endswith("paragraph-1.1"))
    assert "See paragraph 1.2" in p11["text"]
    assert "BS EN 13501-1" in p11["text"]
    assert len(p11["meta"]["source_spans"]) == 2  # primary + continuation


def test_section_path_ids_match_heading_chunks(synth_output: Path) -> None:
    chunks = _load_chunks(synth_output / "chunks.jsonl")
    p13a = next(c for c in chunks if c["chunk_id"].endswith("paragraph-1.3a"))
    # 1.3a is under both the level-1 and level-2 headings.
    assert len(p13a["section_path_ids"]) == 2
    assert p13a["parent_section_id"].endswith("section-heading-for-testing"[:60])


def test_toc_reflects_hierarchy(synth_output: Path) -> None:
    toc = json.loads((synth_output / "toc.json").read_text())
    assert len(toc["entries"]) == 1
    top = toc["entries"][0]
    assert top["level"] == 1
    assert top["first_order_index"] == 0
    assert top["last_order_index"] == 4  # covers the whole doc
    assert len(top["children"]) == 1
    child = top["children"][0]
    assert child["level"] == 2
    assert child["first_order_index"] == 3
    assert child["last_order_index"] == 4


def test_internal_reference_resolved(synth_output: Path) -> None:
    chunks = _load_chunks(synth_output / "chunks.jsonl")
    p11 = next(c for c in chunks if c["chunk_id"].endswith("paragraph-1.1"))
    internal_refs = [r for r in p11["references_out"] if r["type"] == "internal"]
    assert internal_refs, p11["references_out"]
    assert any(r["target_chunk_id"] and r["target_chunk_id"].endswith("paragraph-1.2") for r in internal_refs)


def test_external_reference_normalised(synth_output: Path) -> None:
    chunks = _load_chunks(synth_output / "chunks.jsonl")
    p11 = next(c for c in chunks if c["chunk_id"].endswith("paragraph-1.1"))
    externals = [r for r in p11["references_out"] if r["type"] == "external_standard"]
    assert externals
    assert externals[0]["external_id"].startswith("BS-EN")


def test_references_index_inverted_backlinks(synth_output: Path) -> None:
    idx = json.loads((synth_output / "references_index.json").read_text())
    p12_id = next(
        k for k in idx["by_target"] if k.endswith("paragraph-1.2")
    )
    backlinks = idx["by_target"][p12_id]
    assert any(b["source_chunk_id"].endswith("paragraph-1.1") for b in backlinks)


def test_references_index_external_citations(synth_output: Path) -> None:
    idx = json.loads((synth_output / "references_index.json").read_text())
    # Some BS-EN... key appears, citing paragraph 1.1
    bs_keys = [k for k in idx["external_citations"] if k.startswith("BS-EN")]
    assert bs_keys, idx["external_citations"]
    assert any(
        s.endswith("paragraph-1.1") for s in idx["external_citations"][bs_keys[0]]
    )


def test_validation_passes_on_synthetic(synth_output: Path) -> None:
    report = json.loads((synth_output / "validation_report.json").read_text())
    assert report["passed"] is True, [m for m in report["metrics"] if not m["passed"]]


def test_section_window_invariant_holds(synth_output: Path) -> None:
    from regula.schemas import TOC, assert_section_windows_consistent
    from regula.schemas import Chunk

    chunks = [
        Chunk.model_validate_json(line)
        for line in (synth_output / "chunks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    toc = TOC.model_validate_json((synth_output / "toc.json").read_text())
    assert_section_windows_consistent(toc, chunks)


def test_document_records_real_counts(synth_output: Path) -> None:
    doc = json.loads((synth_output / "document.json").read_text())
    assert doc["page_count"] == 2
    assert doc["chunk_count"] == 5
    assert doc["pipeline_passed"] is True
