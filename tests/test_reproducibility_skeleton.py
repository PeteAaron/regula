"""Running the pipeline twice produces byte-identical output modulo
timestamp fields stripped by :mod:`regula._diff`."""

from __future__ import annotations

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
    # Mutate one chunk-of-data field in b/toc.json — not a stripped key.
    toc_path = b / "toc.json"
    toc_path.write_text(toc_path.read_text().replace('"entries": []', '"entries": []  '))
    # That's whitespace only; still parses to the same dict, so diff should
    # report identical. Now introduce a real difference:
    import json

    data = json.loads(toc_path.read_text())
    data["entries"] = [
        {
            "id": "sentinel",
            "label": "Sentinel",
            "level": 1,
            "heading_chunk_id": "X-section_heading-S",
            "first_chunk_id": "X-section_heading-S",
            "last_chunk_id": "X-section_heading-S",
            "first_order_index": 0,
            "last_order_index": 0,
            "page": 1,
            "children": [],
        }
    ]
    toc_path.write_text(json.dumps(data))
    diffs = compare_outputs(a, b)
    assert any(d.path == "toc.json" for d in diffs), diffs
