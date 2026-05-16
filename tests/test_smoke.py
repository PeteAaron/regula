"""Phase 0 smoke test — package imports and the CLI is wired up."""

from __future__ import annotations

from typer.testing import CliRunner

import regula
from regula.cli import app

runner = CliRunner()


def test_package_imports() -> None:
    assert regula.__version__


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "regula" in result.stdout.lower()


def test_cli_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    for cmd in ("ingest", "stage", "inspect", "diff"):
        assert cmd in result.stdout
