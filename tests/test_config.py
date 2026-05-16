"""Config-loader tests for the minimal post-wind-back schema."""

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


def test_load_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    bad = tmp_path / "extra.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "doc_id": "X",
                "title": "x",
                "source_pdf": "x.pdf",
                "made_up_field": True,
            }
        )
    )
    with pytest.raises(ValidationError):
        load_config(bad)


def test_config_sha256_is_deterministic(tmp_path: Path) -> None:
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "doc_id: X\ntitle: t\nsource_pdf: x.pdf\n"
    )
    a = load_config(cfg_path)
    b = load_config(cfg_path)
    assert config_sha256(a) == config_sha256(b)


def test_sourcing_defaults_applied_when_omitted(tmp_path: Path) -> None:
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("doc_id: X\ntitle: t\nsource_pdf: x.pdf\n")
    cfg = load_config(cfg_path)
    assert cfg.sourcing.coordinate_origin == "top_left"
    assert cfg.sourcing.coordinate_unit == "pt"


def test_optional_fields_have_defaults(tmp_path: Path) -> None:
    """edition/jurisdiction/legal_status default to 'unknown' to keep the
    inferred-from-filename flow simple."""
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("doc_id: X\ntitle: t\nsource_pdf: x.pdf\n")
    cfg = load_config(cfg_path)
    assert cfg.edition == "unknown"
    assert cfg.jurisdiction == "unknown"
    assert cfg.legal_status == "unknown"
