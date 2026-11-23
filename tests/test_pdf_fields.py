from __future__ import annotations

from datetime import datetime, timezone

import pytest

from exleasingcar_archive.pdf_extractor import PDFPageText, PDFTextExtraction
from exleasingcar_archive.pdf_fields import (
    PDFExtractedField,
    PDFFieldRule,
    PDFFieldState,
    extract_labelled_fields,
)

EXTRACTED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _extraction(*pages: str) -> PDFTextExtraction:
    return PDFTextExtraction(
        report_sha256="a" * 64,
        page_count=len(pages),
        pages=tuple(
            PDFPageText(page_number=index, text=text)
            for index, text in enumerate(pages, start=1)
        ),
        parser_name="pypdf",
        parser_version="6.1.0",
        extracted_at=EXTRACTED_AT,
    )


def test_extracts_labelled_values_with_exact_page_and_report_provenance():
    extraction = _extraction(
        "Vehicle details\nMileage:  42 500 km\n",
        "Inspection\nMileage: 43 000 km\n",
    )
    rules = (
        PDFFieldRule(
            key="mileage",
            label="Mileage",
            pattern=r"^Mileage:\s*(?P<value>.*)$",
        ),
    )

    fields = extract_labelled_fields(extraction, rules, lot_id="lot-123")

    assert len(fields) == 2
    assert all(isinstance(field, PDFExtractedField) for field in fields)
    assert [field.raw_text for field in fields] == ["42 500 km", "43 000 km"]
    assert [field.normalized_text for field in fields] == ["42 500 km", "43 000 km"]
    assert [field.provenance.page_number for field in fields] == [1, 2]
    assert all(field.provenance.report_sha256 == "a" * 64 for field in fields)
    assert all(field.provenance.lot_id == "lot-123" for field in fields)
    assert all(field.provenance.source_label == "Mileage" for field in fields)
    assert all(field.provenance.parser_name == "pypdf" for field in fields)
    assert all(field.provenance.parser_version == "6.1.0" for field in fields)
    assert all(field.provenance.extracted_at == EXTRACTED_AT for field in fields)


def test_distinct_explicit_states_are_preserved_without_boolean_coercion():
    extraction = _extraction(
        "Oil leak: No\n"
        "Air conditioning: N/A\n"
        "Road test: Not tested\n"
        "Interior condition:   \n"
        "Tyre condition: Good\n"
    )
    rules = (
        PDFFieldRule(
            key="oil_leak",
            label="Oil leak",
            pattern=r"^Oil leak:\s*(?P<value>.*)$",
            state_tokens=(("No", PDFFieldState.EXPLICIT_NO),),
        ),
        PDFFieldRule(
            key="air_conditioning",
            label="Air conditioning",
            pattern=r"^Air conditioning:\s*(?P<value>.*)$",
            state_tokens=(("N/A", PDFFieldState.NOT_APPLICABLE),),
        ),
        PDFFieldRule(
            key="road_test",
            label="Road test",
            pattern=r"^Road test:\s*(?P<value>.*)$",
            state_tokens=(("Not tested", PDFFieldState.NOT_TESTED),),
        ),
        PDFFieldRule(
            key="interior_condition",
            label="Interior condition",
            pattern=r"^Interior condition:\s*(?P<value>.*)$",
        ),
        PDFFieldRule(
            key="tyre_condition",
            label="Tyre condition",
            pattern=r"^Tyre condition:\s*(?P<value>.*)$",
        ),
    )

    fields = extract_labelled_fields(extraction, rules)
    by_key = {field.key: field for field in fields}

    assert by_key["oil_leak"].state is PDFFieldState.EXPLICIT_NO
    assert by_key["air_conditioning"].state is PDFFieldState.NOT_APPLICABLE
    assert by_key["road_test"].state is PDFFieldState.NOT_TESTED
    assert by_key["interior_condition"].state is PDFFieldState.BLANK
    assert by_key["interior_condition"].normalized_text is None
    assert by_key["tyre_condition"].state is PDFFieldState.VALUE
    assert by_key["tyre_condition"].normalized_text == "Good"


def test_unconfigured_status_words_remain_literal_values():
    extraction = _extraction("Inspection result: No\n")
    rules = (
        PDFFieldRule(
            key="inspection_result",
            label="Inspection result",
            pattern=r"^Inspection result:\s*(?P<value>.*)$",
        ),
    )

    fields = extract_labelled_fields(extraction, rules)

    assert fields[0].state is PDFFieldState.VALUE
    assert fields[0].normalized_text == "No"


def test_missing_labels_are_not_invented_as_empty_observations():
    extraction = _extraction("Vehicle details\nMileage: 12 000 km\n")
    rules = (
        PDFFieldRule(
            key="vin",
            label="VIN",
            pattern=r"^VIN:\s*(?P<value>.*)$",
        ),
    )

    assert extract_labelled_fields(extraction, rules) == ()


def test_lot_association_is_optional_and_never_inferred():
    extraction = _extraction("Mileage: 12 000 km")
    rules = (
        PDFFieldRule(
            key="mileage",
            label="Mileage",
            pattern=r"^Mileage:\s*(?P<value>.*)$",
        ),
    )

    fields = extract_labelled_fields(extraction, rules)

    assert fields[0].provenance.lot_id is None
    with pytest.raises(ValueError, match="lot_id"):
        extract_labelled_fields(extraction, rules, lot_id="  ")


def test_rules_require_named_value_capture_and_valid_state_tokens():
    with pytest.raises(ValueError, match="named 'value'"):
        PDFFieldRule("mileage", "Mileage", r"^Mileage:\s*(.*)$")
    with pytest.raises(ValueError, match="unique"):
        PDFFieldRule(
            "status",
            "Status",
            r"^Status:\s*(?P<value>.*)$",
            state_tokens=(
                ("No", PDFFieldState.EXPLICIT_NO),
                (" no ", PDFFieldState.UNKNOWN),
            ),
        )


def test_input_types_are_validated():
    extraction = _extraction("Mileage: 12 000 km")

    with pytest.raises(TypeError, match="PDFTextExtraction"):
        extract_labelled_fields("not an extraction", ())
    with pytest.raises(TypeError, match="tuple of PDFFieldRule"):
        extract_labelled_fields(extraction, [])
