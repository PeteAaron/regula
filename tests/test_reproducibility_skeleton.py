"""Running the pipeline twice produces byte-identical output modulo
timestamp fields stripped by :mod:`regula._diff`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regula._diff import compare_outputs
from regula.cli import app

CONFIG = "configs/_fixture-small.yaml"
OUTPUT = Path("output/FIXTURE-SMALL")

runner = CliRunner()


@pytest.fixture
def two_runs(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "a"
    b = tmp_path / "b"
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    runner.invoke(app, ["ingest", "--config", CONFIG])
    shutil.copytree(OUTPUT, a)
    shutil.rmtree(OUTPUT)
    runner.invoke(app, ["ingest", "--config", CONFIG])
    shutil.copytree(OUTPUT, b)
    return a, b


def test_two_runs_diff_to_zero(two_runs: tuple[Path, Path]) -> None:
    a, b = two_runs
    diffs = compare_outputs(a, b)
    assert diffs == [], "\n".join(f"{d.path}: {d.message}" for d in diffs)


def test_cli_diff_reports_identical(two_runs: tuple[Path, Path]) -> None:
    a, b = two_runs
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 0
    assert "identical" in result.stdout


def test_diff_detects_changed_file(two_runs: tuple[Path, Path]) -> None:
    a, b = two_runs
    pages_path = b / "pages.json"
    data = json.loads(pages_path.read_text())
    data["pages"].append(
        {"page_number": 999, "width": 100.0, "height": 100.0, "rotation": 0}
    )
    pages_path.write_text(json.dumps(data))
    diffs = compare_outputs(a, b)
    assert any(d.path == "pages.json" for d in diffs), diffs
