"""Focused tests for durable PDF field observation storage."""

from datetime import datetime, timezone

import pytest

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.pdf_extractor import PDFPageText, PDFTextExtraction
from exleasingcar_archive.pdf_fields import PDFFieldRule, PDFFieldState, extract_labelled_fields

EXTRACTED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
REPORT_SHA256 = "a" * 64
RULES = (
    PDFFieldRule(
        key="mileage",
        label="Mileage",
        pattern=r"^Mileage:\\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        key="oil_leak",
        label="Oil leak",
        pattern=r"^Oil leak:\\s*(?P<value>.*)$",
        state_tokens=(("No", PDFFieldState.EXPLICIT_NO),),
    ),
    PDFFieldRule(
        key="interior_condition",
        label="Interior condition",
        pattern=r"^Interior condition:\\s*(?P<value>.*)$",
    ),
)


def _fields(
    *page_texts: str,
    lot_id: str | None = None,
    report_sha256: str = REPORT_SHA256,
):
    extraction = PDFTextExtraction(
        report_sha256=report_sha256,
        page_count=len(page_texts),
        pages=tuple(
            PDFPageText(page_number=index, text=text)
            for index, text in enumerate(page_texts, start=1)
        ),
        parser_name="pypdf",
        parser_version="test-version",
        extracted_at=EXTRACTED_AT,
    )
    return extract_labelled_fields(extraction, RULES, lot_id=lot_id)


def test_pdf_field_observations_keep_provenance_states_and_repeated_history(tmp_path):
    database = ArchiveDatabase(tmp_path / "pdf-observations.sqlite3")
    database.initialize()
    auction_lot_id = database.upsert_auction_lot(
        "synthetic-car-1", "synthetic-auction-1", observed_at=EXTRACTED_AT
    )
    fields = _fields(
        "Mileage: 42 500   km\\nOil leak: No\\nInterior condition:   ",
        "Mileage: 43 000 km",
        lot_id="external-lot-reference-1",
    )

    first_ids = database.save_pdf_field_observations(
        fields, auction_lot_id=auction_lot_id
    )
    second_ids = database.save_pdf_field_observations(
        fields, auction_lot_id=auction_lot_id
    )

    observations = database.get_pdf_field_observations(
        report_sha256=REPORT_SHA256, auction_lot_id=auction_lot_id
    )
    by_key_and_page = {
        (row["field_key"], row["page_number"]): row for row in observations
    }

    assert len(first_ids) == len(second_ids) == len(fields)
    assert set(first_ids).isdisjoint(second_ids)
    assert len(observations) == len(fields) * 2
    assert by_key_and_page[("mileage", 1)]["raw_text"] == "42 500   km"
    assert by_key_and_page[("mileage", 1)]["normalized_text"] == "42 500 km"
    assert by_key_and_page[("mileage", 2)]["normalized_text"] == "43 000 km"
    assert by_key_and_page[("oil_leak", 1)]["state"] == PDFFieldState.EXPLICIT_NO.value
    assert by_key_and_page[("interior_condition", 1)]["state"] == PDFFieldState.BLANK.value
    assert by_key_and_page[("interior_condition", 1)]["normalized_text"] is None
    assert by_key_and_page[("mileage", 1)]["source_lot_id"] == "external-lot-reference-1"
    assert by_key_and_page[("mileage", 1)]["auction_lot_id"] == auction_lot_id
    assert by_key_and_page[("mileage", 1)]["extracted_at"].endswith("Z")

    assert len(
        database.get_pdf_field_observations(report_sha256=REPORT_SHA256)
    ) == len(fields) * 2
    assert len(
        database.get_pdf_field_observations(auction_lot_id=auction_lot_id)
    ) == len(fields) * 2


def test_unresolved_source_lot_identifier_is_kept_without_a_database_link(tmp_path):
    database = ArchiveDatabase(tmp_path / "unresolved-lot.sqlite3")
    database.initialize()
    fields = _fields(
        "Mileage: 12 000 km",
        lot_id="lot-id-that-is-not-in-the-database",
    )

    database.save_pdf_field_observations(fields)
    observations = database.get_pdf_field_observations(report_sha256=REPORT_SHA256)

    assert len(observations) == 1
    assert observations[0]["source_lot_id"] == "lot-id-that-is-not-in-the-database"
    assert observations[0]["auction_lot_id"] is None
    assert database.get_pdf_field_observations(auction_lot_id=12345) == []


def test_unknown_database_lot_is_rejected_without_saving_any_rows(tmp_path):
    database = ArchiveDatabase(tmp_path / "unknown-database-lot.sqlite3")
    database.initialize()
    fields = _fields("Mileage: 12 000 km", lot_id="external-lot-1")

    with pytest.raises(ValueError, match="does not refer to an existing lot"):
        database.save_pdf_field_observations(fields, auction_lot_id=99999)

    assert database.get_pdf_field_observations(report_sha256=REPORT_SHA256) == []


def test_pdf_observation_batches_and_lookup_filters_are_validated(tmp_path):
    database = ArchiveDatabase(tmp_path / "pdf-observation-validation.sqlite3")
    database.initialize()

    with pytest.raises(TypeError, match="tuple of PDFExtractedField"):
        database.save_pdf_field_observations([])
    assert database.save_pdf_field_observations(()) == ()

    with pytest.raises(ValueError, match="provide report_sha256"):
        database.get_pdf_field_observations()
    with pytest.raises(ValueError, match="64 hexadecimal"):
        database.get_pdf_field_observations(report_sha256="not-a-hash")
    with pytest.raises(TypeError, match="integer or None"):
        database.get_pdf_field_observations(auction_lot_id=True)


def test_one_batch_cannot_mix_different_report_hashes(tmp_path):
    database = ArchiveDatabase(tmp_path / "mixed-report-batch.sqlite3")
    database.initialize()
    first = _fields("Mileage: 12 000 km")[0]
    other_fields = _fields("Mileage: 13 000 km", report_sha256="b" * 64)

    with pytest.raises(ValueError, match="same report"):
        database.save_pdf_field_observations((first, *other_fields))

    assert database.get_pdf_field_observations(report_sha256=REPORT_SHA256) == []
    assert database.get_pdf_field_observations(report_sha256="b" * 64) == []
