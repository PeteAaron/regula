"""End-to-end test for the extract_glossary stage."""

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
def glossary_output(
    tmp_path: Path, synthetic_glossary_config_text: str
) -> Path:
    config_path = tmp_path / "gloss.yaml"
    config_path.write_text(synthetic_glossary_config_text)
    cfg = yaml.safe_load(synthetic_glossary_config_text)
    out_dir = Path("output") / cfg["doc_id"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir


def test_glossary_terms_extracted(glossary_output: Path) -> None:
    gloss = json.loads((glossary_output / "glossary.json").read_text())
    terms = {e["normalised_term"] for e in gloss["entries"]}
    assert "compartmentation" in terms
    assert "dwellinghouse" in terms


def test_glossary_entry_carries_definition(glossary_output: Path) -> None:
    gloss = json.loads((glossary_output / "glossary.json").read_text())
    comp = next(e for e in gloss["entries"] if e["normalised_term"] == "compartmentation")
    assert "fire spread" in comp["definition"].lower()


def test_defined_terms_backfilled_on_outside_chunks(glossary_output: Path) -> None:
    chunks = [
        json.loads(line)
        for line in (glossary_output / "chunks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    p11 = next(c for c in chunks if c["chunk_id"].endswith("paragraph-1.1"))
    assert "compartmentation" in p11["defined_terms_used"], p11["defined_terms_used"]


def test_glossary_chunks_dont_self_reference(glossary_output: Path) -> None:
    chunks = [
        json.loads(line)
        for line in (glossary_output / "chunks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    gloss = json.loads((glossary_output / "glossary.json").read_text())
    glossary_chunk_ids = {e["chunk_id"] for e in gloss["entries"]}
    for c in chunks:
        if c["chunk_id"] in glossary_chunk_ids:
            assert c["defined_terms_used"] == []


def test_glossary_no_op_when_section_absent(synth_output_no_glossary: Path) -> None:
    gloss = json.loads((synth_output_no_glossary / "glossary.json").read_text())
    assert gloss["entries"] == []


@pytest.fixture
def synth_output_no_glossary(
    tmp_path: Path, synthetic_config_text: str
) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(synthetic_config_text)
    cfg = yaml.safe_load(synthetic_config_text)
    out_dir = Path("output") / cfg["doc_id"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir
