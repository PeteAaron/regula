"""Walking-skeleton acceptance: `regula ingest` produces the full output
directory and every artifact validates against its committed JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from regula._schema_export import SCHEMA_MODELS
from regula.cli import app

CONFIG = "configs/_fixture-small.yaml"
OUTPUT = Path("output/FIXTURE-SMALL")

runner = CliRunner()


def _load_schema(name: str) -> dict:
    return json.loads(Path(f"schemas/{name}.schema.json").read_text(encoding="utf-8"))


def _validate_file(path: Path, schema_name: str) -> None:
    schema = _load_schema(schema_name)
    if path.suffix == ".jsonl":
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            jsonschema.validate(instance=json.loads(line), schema=schema)
    else:
        jsonschema.validate(instance=json.loads(path.read_text(encoding="utf-8")), schema=schema)


@pytest.fixture(scope="module", autouse=True)
def _run_ingest(tmp_path_factory) -> None:  # noqa: PT004
    if OUTPUT.exists():
        import shutil

        shutil.rmtree(OUTPUT)
    result = runner.invoke(app, ["ingest", "--config", CONFIG])
    assert result.exit_code == 0, result.stdout


def test_output_directory_shape() -> None:
    expected = {
        "document.json",
        "toc.json",
        "chunks.jsonl",
        "glossary.json",
        "references_index.json",
        "validation_report.json",
        "pages.json",
        "run.log",
        "assets",
        "intermediate",
    }
    actual = {p.name for p in OUTPUT.iterdir()}
    missing = expected - actual
    assert not missing, f"missing from output: {missing}"


def test_intermediate_directories_exist() -> None:
    for stage in ("parse", "chunk", "resolve_references", "build_toc", "extract_glossary", "validate"):
        assert (OUTPUT / "intermediate" / stage).is_dir(), f"missing intermediate/{stage}"


@pytest.mark.parametrize(
    "path,schema",
    [
        ("document.json", "document"),
        ("toc.json", "toc"),
        ("glossary.json", "glossary"),
        ("references_index.json", "references_index"),
        ("validation_report.json", "validation_report"),
        ("pages.json", "pages"),
        ("chunks.jsonl", "chunk"),
    ],
)
def test_artifact_matches_schema(path: str, schema: str) -> None:
    assert schema in SCHEMA_MODELS
    _validate_file(OUTPUT / path, schema)


def test_document_records_full_pipeline() -> None:
    doc = json.loads((OUTPUT / "document.json").read_text())
    assert doc["pipeline_passed"] is True
    assert len(doc["stage_reports"]) == 7  # parse..validate + finalise
    assert [r["stage"] for r in doc["stage_reports"]] == [
        "parse", "chunk", "resolve_references", "build_toc",
        "extract_glossary", "validate", "finalise",
    ]
    # Placeholder PDF has 1 page with one line of text that doesn't match
    # paragraph_regex, so we get 1 page and 0 chunks.
    assert doc["page_count"] == 1
    assert doc["chunk_count"] == 0
    assert doc["source_pdf_sha256"].startswith("sha256:") or len(doc["source_pdf_sha256"]) == 64


def test_validation_report_passes() -> None:
    report = json.loads((OUTPUT / "validation_report.json").read_text())
    assert report["passed"] is True
    assert all(m["passed"] for m in report["metrics"]), report
    metric_names = {m["name"] for m in report["metrics"]}
    assert {
        "invariants_ok",
        "schemas_valid",
        "page_coverage",
        "internal_ref_resolution",
        "text_reconstruction_coverage",
        "reading_order_monotonicity",
    } <= metric_names


def test_run_log_is_json_lines() -> None:
    log_path = OUTPUT / "run.log"
    assert log_path.exists()
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "run.log is empty"
    # Every line should parse as JSON.
    for line in lines:
        json.loads(line)
