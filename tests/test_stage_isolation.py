"""Stages can be run individually; running out of order fails loudly."""

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
def _clean_output() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)


def test_running_chunk_without_parse_fails_loudly() -> None:
    result = runner.invoke(app, ["stage", "chunk", "--config", CONFIG])
    assert result.exit_code == 1
    # The error should mention what's missing and how to fix it.
    assert "parse" in result.stdout.lower() or "parse" in result.output.lower()


def test_running_resolve_references_without_chunk_fails_loudly() -> None:
    runner.invoke(app, ["stage", "parse", "--config", CONFIG])
    result = runner.invoke(app, ["stage", "resolve_references", "--config", CONFIG])
    assert result.exit_code == 1


def test_running_parse_then_chunk_works() -> None:
    r1 = runner.invoke(app, ["stage", "parse", "--config", CONFIG])
    assert r1.exit_code == 0
    r2 = runner.invoke(app, ["stage", "chunk", "--config", CONFIG])
    assert r2.exit_code == 0
    assert (OUTPUT / "intermediate" / "chunk" / "chunks.jsonl").exists()


def test_unknown_stage_name_rejected() -> None:
    result = runner.invoke(app, ["stage", "nonexistent_stage", "--config", CONFIG])
    assert result.exit_code == 1


def test_finalise_cannot_be_called_directly() -> None:
    result = runner.invoke(app, ["stage", "finalise", "--config", CONFIG])
    assert result.exit_code == 1
