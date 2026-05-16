"""Tests for the zero-config invocation: ``regula ingest <pdf>``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from regula.cli import app
from regula.config import infer_config

runner = CliRunner()


def test_infer_config_from_pdf(synthetic_pdf: Path) -> None:
    cfg = infer_config(synthetic_pdf)
    assert cfg.doc_id  # non-empty, slugified
    assert cfg.doc_id.islower()
    assert " " not in cfg.doc_id
    assert cfg.source_pdf == str(synthetic_pdf)
    assert cfg.parsers.primary == "pymupdf"


def test_infer_config_errors_when_pdf_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        infer_config(tmp_path / "nope.pdf")


def test_infer_config_caps_doc_id_length(tmp_path: Path) -> None:
    """A long PDF filename mustn't produce a multi-line block_id
    prefix. The doc_id is capped at 40 chars, truncated at a word
    boundary."""
    long_name = (
        "Approved-Document-B-Volume-1-2022-Fire-Safety-Dwellings-Extras.pdf"
    )
    pdf = tmp_path / long_name
    pdf.write_bytes(b"%PDF-1.4\n")
    cfg = infer_config(pdf)
    assert len(cfg.doc_id) <= 40
    # Truncated at a hyphen, not mid-word.
    assert not cfg.doc_id.endswith("-")
    # First few words should survive.
    assert cfg.doc_id.startswith("approved-document-b")


def test_ingest_with_positional_pdf_writes_to_cwd_subdir(
    synthetic_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``regula ingest some.pdf`` should land artifacts in ./<doc_id>/
    relative to the invoker's working directory, not in the repo's
    ``output/``."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ingest", str(synthetic_pdf), "--no-fail"])
    assert result.exit_code == 0, result.stdout

    doc_id = infer_config(synthetic_pdf).doc_id
    run_dir = tmp_path / doc_id
    assert run_dir.exists()
    for name in [
        "blocks.jsonl",
        "pages.json",
        "document.json",
        "preview.html",  # auto-written
    ]:
        assert (run_dir / name).exists(), f"missing {name}"


def test_ingest_positional_pdf_with_explicit_out_dir(
    synthetic_pdf: Path, tmp_path: Path
) -> None:
    out = tmp_path / "anywhere"
    result = runner.invoke(
        app, ["ingest", str(synthetic_pdf), "--out-dir", str(out), "--no-fail"]
    )
    assert result.exit_code == 0, result.stdout
    assert (out / "blocks.jsonl").exists()
    assert (out / "preview.html").exists()


def test_ingest_no_preview_flag(
    synthetic_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["ingest", str(synthetic_pdf), "--no-fail", "--no-preview"]
    )
    assert result.exit_code == 0, result.stdout
    doc_id = infer_config(synthetic_pdf).doc_id
    assert not (tmp_path / doc_id / "preview.html").exists()


def test_ingest_requires_exactly_one_of_pdf_or_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code != 0
    # Both supplied — also an error.
    result = runner.invoke(app, ["ingest", "some.pdf", "--config", "x.yaml"])
    assert result.exit_code != 0


def test_inspect_with_positional_run_dir(
    synthetic_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ingest_result = runner.invoke(app, ["ingest", str(synthetic_pdf), "--no-fail"])
    assert ingest_result.exit_code == 0, ingest_result.stdout
    doc_id = infer_config(synthetic_pdf).doc_id
    run_dir = tmp_path / doc_id

    # Delete the auto-preview, then re-render via inspect.
    (run_dir / "preview.html").unlink()
    result = runner.invoke(app, ["inspect", str(run_dir)])
    assert result.exit_code == 0, result.stdout
    assert (run_dir / "preview.html").exists()


def test_inspect_defaults_to_cwd(
    synthetic_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    ingest_result = runner.invoke(
        app, ["ingest", str(synthetic_pdf), "--out-dir", str(run_dir), "--no-fail"]
    )
    assert ingest_result.exit_code == 0, ingest_result.stdout
    (run_dir / "preview.html").unlink()
    monkeypatch.chdir(run_dir)
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == 0, result.stdout
    assert (run_dir / "preview.html").exists()
