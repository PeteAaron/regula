"""CLI surface-level tests for `preview`, `validate`, and `diff`."""

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


def test_preview_chunk_not_found() -> None:
    result = runner.invoke(app, ["preview", "--config", CONFIG, "--chunk-id", "NOPE"])
    assert result.exit_code == 1
    # Error text goes to stderr; CliRunner captures it on .output for non-mix runs.
    combined = result.stdout + result.output
    assert "not found" in combined.lower()


def test_preview_no_output_dir(tmp_path: Path) -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    result = runner.invoke(app, ["preview", "--config", CONFIG, "--chunk-id", "X"])
    assert result.exit_code == 1


def test_revalidate_existing_run() -> None:
    result = runner.invoke(app, ["validate", "--config", CONFIG])
    assert result.exit_code == 0
    assert "passed=True" in result.stdout


def test_revalidate_no_output_dir() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    result = runner.invoke(app, ["validate", "--config", CONFIG])
    assert result.exit_code == 1


def test_diff_identical(tmp_path: Path) -> None:
    a = tmp_path / "a"
    shutil.copytree(OUTPUT, a)
    result = runner.invoke(app, ["diff", str(OUTPUT), str(a)])
    assert result.exit_code == 0
    assert "identical" in result.stdout


def test_diff_different(tmp_path: Path) -> None:
    b = tmp_path / "b"
    shutil.copytree(OUTPUT, b)
    (b / "toc.json").write_text('{"entries": [{"id":"x","label":"X","level":1,'
                                '"heading_chunk_id":"FIXTURE-SMALL-section_heading-X",'
                                '"first_chunk_id":"FIXTURE-SMALL-section_heading-X",'
                                '"last_chunk_id":"FIXTURE-SMALL-section_heading-X",'
                                '"first_order_index":0,"last_order_index":0,'
                                '"page":1,"children":[]}]}')
    result = runner.invoke(app, ["diff", str(OUTPUT), str(b)])
    assert result.exit_code == 1
    assert "toc.json" in result.stdout
