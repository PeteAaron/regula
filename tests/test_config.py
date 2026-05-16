"""Config-loader tests, including the DoD config-schema check."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from regula.config import Config, config_sha256, load_config

CONFIGS_DIR = Path(__file__).parent.parent / "configs"


@pytest.mark.parametrize("path", sorted(CONFIGS_DIR.glob("*.yaml")))
def test_every_config_loads(path: Path) -> None:
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    assert cfg.doc_id


def test_load_rejects_bad_regex(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "doc_id": "X",
                "title": "x",
                "edition": "x",
                "jurisdiction": "x",
                "legal_status": "x",
                "source_pdf": "x.pdf",
                "chunking": {
                    "paragraph_regex": "[unbalanced",
                    "heading_levels": [1],
                },
            }
        )
    )
    with pytest.raises(ValidationError):
        load_config(bad)


def test_load_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    bad = tmp_path / "extra.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "doc_id": "X",
                "title": "x",
                "edition": "x",
                "jurisdiction": "x",
                "legal_status": "x",
                "source_pdf": "x.pdf",
                "chunking": {"paragraph_regex": ".*", "heading_levels": [1]},
                "made_up_field": True,
            }
        )
    )
    with pytest.raises(ValidationError):
        load_config(bad)


def test_load_rejects_bad_reference_regex(tmp_path: Path) -> None:
    bad = tmp_path / "bad_ref.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "doc_id": "X",
                "title": "x",
                "edition": "x",
                "jurisdiction": "x",
                "legal_status": "x",
                "source_pdf": "x.pdf",
                "chunking": {"paragraph_regex": ".*", "heading_levels": [1]},
                "references": {
                    "patterns": [{"name": "bad", "regex": "(unclosed", "type": "internal"}],
                },
            }
        )
    )
    with pytest.raises(ValidationError):
        load_config(bad)


def test_config_sha256_is_deterministic() -> None:
    a = load_config(CONFIGS_DIR / "adb-vol1.yaml")
    b = load_config(CONFIGS_DIR / "adb-vol1.yaml")
    assert config_sha256(a) == config_sha256(b)


def test_sourcing_defaults_applied_when_omitted() -> None:
    cfg = load_config(CONFIGS_DIR / "_fixture-small.yaml")
    assert cfg.sourcing.coordinate_origin == "top_left"
    assert cfg.sourcing.coordinate_unit == "pt"


def test_validation_thresholds_clamped(tmp_path: Path) -> None:
    bad = tmp_path / "bad_threshold.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "doc_id": "X",
                "title": "x",
                "edition": "x",
                "jurisdiction": "x",
                "legal_status": "x",
                "source_pdf": "x.pdf",
                "chunking": {"paragraph_regex": ".*", "heading_levels": [1]},
                "validation": {"min_page_coverage": 1.5},
            }
        )
    )
    with pytest.raises(ValidationError):
        load_config(bad)
