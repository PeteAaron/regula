"""Per-model schema tests — every validator the contract relies on."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from regula._schema_export import SCHEMA_MODELS
from regula.schemas import (
    Chunk,
    ChunkMeta,
    ChunkType,
    Reference,
    ReferenceType,
    SourceSpan,
    TOCEntry,
)
from tests.conftest import make_chunk, make_source_span


def test_chunk_round_trip() -> None:
    c = make_chunk()
    dumped = c.model_dump(mode="json")
    restored = Chunk.model_validate(dumped)
    assert restored == c


def test_chunk_id_format_accepts_known_type() -> None:
    make_chunk(chunk_id="ADB1-2022-paragraph-2.4")


def test_chunk_id_format_rejects_bogus() -> None:
    with pytest.raises(ValidationError):
        make_chunk(chunk_id="bogus")


def test_chunk_id_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        make_chunk(chunk_id="ADB1-2022-not_a_type-2.4")


def test_chunk_id_must_match_doc_id_prefix() -> None:
    with pytest.raises(ValidationError):
        make_chunk(chunk_id="OTHER-paragraph-1.1", doc_id="ADB1-2022")


def test_page_end_lt_page_start_raises() -> None:
    with pytest.raises(ValidationError):
        make_chunk(page_start=5, page_end=3)


def test_section_path_lengths_must_match() -> None:
    with pytest.raises(ValidationError):
        make_chunk(
            section_path=["A", "B"],
            section_path_ids=["ADB1-2022-section_heading-A"],
        )


def test_section_heading_requires_heading_level() -> None:
    with pytest.raises(ValidationError):
        make_chunk(
            chunk_id="ADB1-2022-section_heading-A",
            type=ChunkType.SECTION_HEADING,
            heading_level=None,
        )


def test_table_requires_asset_path() -> None:
    with pytest.raises(ValidationError):
        make_chunk(
            chunk_id="ADB1-2022-table-2.1",
            type=ChunkType.TABLE,
            asset_path=None,
        )


def test_diagram_requires_asset_path() -> None:
    with pytest.raises(ValidationError):
        make_chunk(
            chunk_id="ADB1-2022-diagram-3.1",
            type=ChunkType.DIAGRAM,
            asset_path=None,
        )


def test_caption_requires_caption_target_id() -> None:
    with pytest.raises(ValidationError):
        make_chunk(
            chunk_id="ADB1-2022-caption-3.1",
            type=ChunkType.CAPTION,
            caption_target_id=None,
        )


def test_source_span_rejects_zero_height() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(
            page=1,
            bbox=(0.0, 0.0, 10.0, 0.0),
            text_offset_start=0,
            text_offset_end=5,
        )


def test_source_span_rejects_inverted_x() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(
            page=1,
            bbox=(10.0, 0.0, 5.0, 20.0),
            text_offset_start=0,
            text_offset_end=5,
        )


def test_source_span_rejects_inverted_text_offsets() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(
            page=1,
            bbox=(0.0, 0.0, 10.0, 10.0),
            text_offset_start=10,
            text_offset_end=5,
        )


def test_chunk_meta_requires_at_least_one_span() -> None:
    with pytest.raises(ValidationError):
        ChunkMeta(source_spans=[], extracted_by="docling@2.x")


def test_unresolved_internal_reference_is_valid() -> None:
    r = Reference(target_chunk_id=None, label="paragraph 7.99", type=ReferenceType.INTERNAL)
    assert r.target_chunk_id is None


def test_external_reference_without_target_is_valid() -> None:
    r = Reference(
        target_chunk_id=None,
        label="BS EN 13501-1:2018",
        type=ReferenceType.EXTERNAL_STANDARD,
        external_id="BS-EN-13501-1:2018",
    )
    assert r.external_id == "BS-EN-13501-1:2018"


def test_toc_entry_rejects_inverted_window() -> None:
    with pytest.raises(ValidationError):
        TOCEntry(
            id="toc-A",
            label="A",
            level=1,
            heading_chunk_id="ADB1-2022-section_heading-A",
            first_chunk_id="x",
            last_chunk_id="y",
            first_order_index=10,
            last_order_index=5,
            page=1,
        )


@pytest.mark.parametrize("name,model", sorted(SCHEMA_MODELS.items()))
def test_every_model_emits_json_schema(name: str, model: type) -> None:
    schema = model.model_json_schema()
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert "properties" in schema


def test_source_span_offsets_inside_text() -> None:
    span = make_source_span(text_offset_end=5)
    chunk = make_chunk(text="Hello world", meta=ChunkMeta(source_spans=[span], extracted_by="x"))
    assert chunk.text[span.text_offset_start : span.text_offset_end] == "Hello"
