"""Tests for the deferred-features placeholder system.

Every run writes ``deferred.json`` listing capabilities the pipeline
knows about but doesn't yet implement. The Document also mirrors it
under ``deferred_features``. Stages that detect candidate inputs they
intentionally skip (e.g. images) report a count so the entry's
``observed_count`` reflects what this specific run would have produced
if the feature were implemented.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from regula.cli import app
from regula.deferred import assemble_deferred_features
from regula.schemas import StageReport

runner = CliRunner()


@pytest.fixture
def run_output(tmp_path: Path, synthetic_config_text: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(synthetic_config_text)
    cfg = yaml.safe_load(synthetic_config_text)
    out_dir = Path("output") / cfg["doc_id"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    result = runner.invoke(app, ["ingest", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout
    return out_dir


def test_deferred_json_is_written(run_output: Path) -> None:
    assert (run_output / "deferred.json").exists()


def test_deferred_features_have_expected_names(run_output: Path) -> None:
    data = json.loads((run_output / "deferred.json").read_text())
    names = {f["name"] for f in data["features"]}
    expected = {
        "diagram_chunks",
        "table_chunks",
        "caption_chunks",
        "regulation_quote_chunks",
        "docling_parser",
        "external_id_versioning",
        "real_synthetic_fixture",
        "adb_smoke_test",
    }
    missing = expected - names
    assert not missing, f"missing deferred entries: {missing}"


def test_deferred_features_mirrored_in_document(run_output: Path) -> None:
    doc = json.loads((run_output / "document.json").read_text())
    deferred = json.loads((run_output / "deferred.json").read_text())
    assert len(doc["deferred_features"]) == len(deferred["features"])
    assert {d["name"] for d in doc["deferred_features"]} == {
        f["name"] for f in deferred["features"]
    }


def test_parser_versions_recorded_in_document(run_output: Path) -> None:
    doc = json.loads((run_output / "document.json").read_text())
    assert "pymupdf" in doc["parser_versions"]
    assert "PyMuPDF" in doc["parser_versions"]["pymupdf"] or doc["parser_versions"][
        "pymupdf"
    ]


def test_deferred_observed_counts_from_stage_reports() -> None:
    """Stages report deferred counters via StageReport.counts. The
    assembler folds them into observed_count on the matching feature."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    fake_chunk_report = StageReport(
        stage="chunk",
        started_at=now,
        finished_at=now,
        duration_seconds=0.0,
        ok=True,
        counts={
            "chunks_emitted": 5,
            "deferred_images_skipped": 7,
            "deferred_unclassified_text": 2,
        },
    )
    features = assemble_deferred_features([fake_chunk_report])
    by_name = {f.name: f for f in features}
    assert by_name["diagram_chunks"].observed_count == 7
    assert by_name["regulation_quote_chunks"].observed_count == 2
    # Features with no producing-stage signal remain None.
    assert by_name["table_chunks"].observed_count is None


def test_deferred_schema_committed() -> None:
    """The schemas/ directory must include the deferred.json schema so
    downstream consumers can pin against it."""
    schema_path = Path("schemas/deferred.schema.json")
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text())
    # required fields on the container model
    assert "features" in schema["properties"]
    assert "generated_at" in schema["properties"]
