"""Tests for the diagnostic HTML previewer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from regula.cli import app
from regula.inspect import render_preview

runner = CliRunner()


@pytest.fixture
def synth_output(tmp_path: Path, synthetic_config_text: str) -> Path:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(synthetic_config_text)
    doc_id = yaml.safe_load(synthetic_config_text)["doc_id"]
    out_dir = Path("output") / doc_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir


@pytest.fixture
def gloss_output(tmp_path: Path, synthetic_glossary_config_text: str) -> Path:
    cfg_path = tmp_path / "g.yaml"
    cfg_path.write_text(synthetic_glossary_config_text)
    doc_id = yaml.safe_load(synthetic_glossary_config_text)["doc_id"]
    out_dir = Path("output") / doc_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir


def test_inspect_cli_writes_preview(
    tmp_path: Path, synthetic_config_text: str, synth_output: Path
) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(synthetic_config_text)
    result = runner.invoke(app, ["inspect", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert (synth_output / "preview.html").exists()


def test_preview_contains_chunk_anchors(synth_output: Path) -> None:
    html = render_preview(synth_output)
    # Every chunk should have a HTML id matching its chunk_id so refs can
    # link to it.
    chunks = [
        json.loads(line)
        for line in (synth_output / "chunks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for c in chunks:
        assert f"id='{c['chunk_id']}'" in html


def test_preview_internal_ref_is_anchor_link(synth_output: Path) -> None:
    html = render_preview(synth_output)
    # paragraph 1.1 references paragraph 1.2 — should render as href anchor.
    assert "href='#SYNTH-paragraph-1.2'" in html
    assert "ref-internal" in html


def test_preview_external_ref_marked(synth_output: Path) -> None:
    html = render_preview(synth_output)
    # BS EN reference should be styled as external.
    assert "ref-external" in html
    assert "BS-EN" in html  # external_id appears in title attribute


def test_preview_renders_toc(synth_output: Path) -> None:
    html = render_preview(synth_output)
    assert "Contents" in html
    assert "class='toc'" in html or 'class="toc"' in html


def test_preview_includes_deferred_features(synth_output: Path) -> None:
    html = render_preview(synth_output)
    assert "Deferred capabilities" in html
    assert "diagram_chunks" in html


def test_preview_highlights_glossary_terms(gloss_output: Path) -> None:
    html = render_preview(gloss_output)
    assert "class='term'" in html
    # Tooltip carries the definition.
    assert "fire spread" in html


def test_preview_shows_backlinks_in_meta_strip(synth_output: Path) -> None:
    html = render_preview(synth_output)
    # paragraph 1.2 should have a backlink to 1.1.
    assert "backlinks:" in html


def test_preview_chunks_in_order_index_sequence(synth_output: Path) -> None:
    html = render_preview(synth_output)
    chunks = sorted(
        (
            json.loads(line)
            for line in (synth_output / "chunks.jsonl").read_text().splitlines()
            if line.strip()
        ),
        key=lambda c: c["order_index"],
    )
    # Each chunk_id appears later in the HTML than the previous one.
    last_pos = -1
    for c in chunks:
        pos = html.find(f"id='{c['chunk_id']}'")
        assert pos > last_pos, f"chunk {c['chunk_id']} out of order"
        last_pos = pos


def test_inspect_errors_when_run_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "missing.yaml"
    cfg.write_text(
        """
doc_id: NOPE-DOES-NOT-EXIST
title: x
edition: x
jurisdiction: x
legal_status: x
source_pdf: /tmp/none.pdf
parsers: {primary: pymupdf}
chunking: {paragraph_regex: '^x$', heading_levels: [1]}
references: {patterns: []}
validation: {}
"""
    )
    result = runner.invoke(app, ["inspect", "--config", str(cfg)])
    assert result.exit_code != 0
