from __future__ import annotations

from datetime import datetime, timezone

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.pdf_athlon_huesges import (
    extract_athlon_huesges_report,
    supports_athlon_huesges_report,
)
from exleasingcar_archive.pdf_extractor import PDFPageText, PDFTextExtraction
from exleasingcar_archive.pdf_fields import PDFFieldState
from exleasingcar_archive.pdf_layout import (
    PDFDocumentLayout,
    PDFLayoutPhoto,
    PDFLayoutWord,
    PDFPageLayout,
)

EXTRACTED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
REPORT_SHA256 = "a" * 64
SYNTHETIC_VIN = "WXYZ1234567890123"


def _assert_raises(expected_exception, callback):
    try:
        callback()
    except expected_exception:
        return
    raise AssertionError(f"Expected {expected_exception.__name__} to be raised")


def _text_extraction() -> PDFTextExtraction:
    pages = (
        (
            "Ing. Büro Example Hüsges\n"
            "Athlon Verkaufsexpertise\n"
            "Fahrzeugdaten\n"
            "Synthetic Model\n"
            f"Fahrzeug-Ident.-Nr.: {SYNTHETIC_VIN}   Laufleistung abgelesen: 12 345 km\n"
            "Erstzulassung / Baujahr: 01.02.2022 / 2022   Besichtigungsdatum/-ort: 03.02.2023 Sample Town\n"
            "Fahrzeugart: SUV   Aufbauart: Hatchback\n"
            "KBA-Nr. (HSN/ TSN): 123//456   Kraftstoff / Emissionsklasse: Hybrid / Euro 6\n"
            "Getriebe Art: automatic   Navi.-Datenträger: SSD\n"
            "Hubraum / Leistung: 2000 ccm / 110 kW   Antriebsart: Front\n"
            "Lackfarbe: Blue   Lackart: Metallic\n"
            "Anzahl Schlüssel: 2   Polster Art: Fabric\n"
            "Serviceheft verfügbar: Ja   Farbe Innenausstattung: Black\n"
            "Letzter Service km / am: -/-\n"
            "Reifenart / Felgentyp: Allwetterreifen Aluminium\n"
            "1. Achse - links / Zust. 225/60/R18/104 7 mm\n"
            "1. Achse - rechts / Zust. 225/60/R18/104 7 mm\n"
            "2. Achse - links / Zust. 225/60/R18/104 6 mm\n"
            "2. Achse - rechts / Zust. 225/60/R18/104 6 mm"
        ),
        "Serienausstattung\n",
        (
            "Sonderausstattung\n"
            "Wir weisen darauf hin that this synthetic report has limited inspection scope.\n"
        ),
        (
            "Durchgeführte Technikprüfung\n"
            "Klimaanlage: Function confirmed at inspection time.\n"
            "Lichtanlage: Lights were in order at inspection time.\n"
            "Motorlauf: Engine test completed; no visible or audible defects.\n"
            "Hinweis: *\n"
            "Unfallschadenprüfung\n"
            "Lackschichtendickenmessung: Within tolerance.\n"
            "Paint thickness readings were within the original paint range.\n"
            "Hinweis:\n"
            "Schaden\n"
            "Wir weisen darauf hin that normal wear may also be present."
        ),
        "Bilddokumentation\n",
        "Photo documentation\n",
        "Picture captions\n",
    )
    return PDFTextExtraction(
        report_sha256=REPORT_SHA256,
        page_count=len(pages),
        pages=tuple(
            PDFPageText(page_number=index, text=value)
            for index, value in enumerate(pages, start=1)
        ),
        parser_name="pypdf",
        parser_version="synthetic",
        extracted_at=EXTRACTED_AT,
    )


def _word(text: str, x0: float, top: float, width: float = 20.0) -> PDFLayoutWord:
    return PDFLayoutWord(
        text=text,
        x0=x0,
        top=top,
        x1=x0 + width,
        bottom=top + 9.0,
    )


def _page_layout(
    page_number: int,
    *,
    words: tuple[PDFLayoutWord, ...] = (),
    photos: tuple[PDFLayoutPhoto, ...] = (),
) -> PDFPageLayout:
    return PDFPageLayout(
        page_number=page_number,
        width=612.0,
        height=792.0,
        words=words,
        photos=photos,
    )


def _layout() -> PDFDocumentLayout:
    page_two_words = (
        _word("Serienausstattung", 40, 100, 90),
        _word("Heated", 52, 120),
        _word("windscreen", 76, 120, 45),
        _word("Towbar", 325, 120, 40),
        _word("system-", 368, 120, 44),
        _word("details", 325, 134, 38),
        _word("continued", 366, 134, 55),
        _word("Sonderausstattung", 40, 200, 100),
        _word("Winter", 52, 220, 35),
        _word("package", 90, 220, 42),
        _word("Panoramic", 325, 220, 55),
        _word("roof", 383, 220, 28),
    )
    page_three_words = (
        _word("Head-up", 52, 100, 45),
        _word("display", 100, 100, 40),
        _word("LED", 325, 100, 18),
        _word("headlights", 346, 100, 55),
        _word("Wir", 36.6, 430, 18),
        _word("weisen", 57.1, 430, 30),
        _word("darauf", 93.1, 430, 27),
        _word("hin", 126.7, 430, 12),
    )
    page_four_words = (
        _word("Schaden", 71.5, 80, 38),
        _word("Karosserie", 71.5, 100, 48),
        _word("außen", 122, 100, 28),
        _word("Bauteil", 71.5, 120, 32),
        _word("Schaden", 186.4, 120, 38),
        _word("1.", 44.8, 140, 7),
        _word("Rear", 71.5, 140, 24),
        _word("quarter", 99, 140, 40),
        _word("Scratch", 186.4, 140, 38),
        _word("Left", 71.5, 154, 20),
        _word("2.", 44.8, 174, 7),
        _word("Rear", 71.5, 174, 24),
        _word("door", 98, 174, 25),
        _word("Scratch", 186.4, 174, 38),
        _word("Sonstiges", 71.5, 200, 44),
        _word("Bauteil", 71.5, 220, 32),
        _word("Schaden", 186.4, 220, 38),
        _word("3.", 44.8, 240, 7),
        _word("Towbar", 71.5, 240, 36),
        _word("fixed", 110, 240, 24),
        _word("Not", 186.4, 240, 18),
        _word("in", 207, 240, 10),
        _word("order", 220, 240, 30),
    )
    page_seven_photos = tuple(
        PDFLayoutPhoto(
            page_number=7,
            image_index=index,
            x0=36.6 if index % 2 else 325.5,
            top=128.3 + ((index - 1) // 2) * 157.0,
            x1=(291.6 if index % 2 else 580.5),
            bottom=271.7 + ((index - 1) // 2) * 157.0,
            caption=caption,
        )
        for index, caption in enumerate(
            (
                "Front left",
                "Front right",
                "1. Rear quarter Left Scratch",
                "2. Rear door Scratch",
                "Other",
                "3. Towbar fixed Not in order",
            ),
            start=1,
        )
    )
    pages = (
        _page_layout(1),
        _page_layout(2, words=page_two_words),
        _page_layout(3, words=page_three_words),
        _page_layout(4, words=page_four_words),
        _page_layout(5),
        _page_layout(6),
        _page_layout(7, photos=page_seven_photos),
    )
    return PDFDocumentLayout(
        report_sha256=REPORT_SHA256,
        page_count=7,
        parser_name="pdfplumber",
        parser_version="synthetic",
        pages=pages,
    )


def test_athlon_huesges_profile_extracts_vehicle_fields_and_keeps_provenance():
    extraction = _text_extraction()
    result = extract_athlon_huesges_report(extraction, _layout(), lot_id="external-lot-1")
    fields = {field.key: field for field in result.fields}

    assert supports_athlon_huesges_report(extraction)
    assert fields["vehicle.reported_title"].normalized_text == "Synthetic Model"
    assert fields["vehicle.vin"].normalized_text == SYNTHETIC_VIN
    assert fields["vehicle.odometer"].normalized_text == "12 345 km"
    assert fields["vehicle.first_registration_date"].normalized_text == "01.02.2022"
    assert fields["vehicle.build_year"].normalized_text == "2022"
    assert fields["tyres.axle_1.left.dimension"].normalized_text == "225/60/R18/104"
    assert fields["tyres.axle_1.left.tread_depth_mm"].normalized_text == "7 mm"
    assert fields["inspection.air_conditioning"].state is PDFFieldState.VALUE
    assert fields["inspection.air_conditioning"].normalized_text == (
        "Function confirmed at inspection time."
    )
    assert fields["inspection.lighting"].state is PDFFieldState.VALUE
    assert fields["inspection.engine_operation"].state is PDFFieldState.VALUE
    assert fields["inspection.paint_thickness_details"].normalized_text == (
        "Paint thickness readings were within the original paint range."
    )
    standard_items = [
        field.normalized_text for field in result.fields if field.key == "equipment.standard"
    ]
    special_items = [
        field.normalized_text for field in result.fields if field.key == "equipment.special"
    ]
    assert "Heated windscreen" in standard_items
    assert "Towbar system- details continued" in standard_items
    assert "Winter package" in special_items
    assert "Head-up display" in special_items
    assert [field.state for field in result.fields if field.key == "inspection.note"] == [
        PDFFieldState.VALUE,
        PDFFieldState.BLANK,
    ]
    assert len([field for field in result.fields if field.key == "report.scope_limitation"]) == 2
    assert fields["equipment.special"].provenance.lot_id == "external-lot-1"
    assert all(field.provenance.report_sha256 == REPORT_SHA256 for field in result.fields)
    assert all(field.provenance.parser_name == "pypdf+pdfplumber+athlon_huesges" for field in result.fields)


def test_athlon_huesges_profile_keeps_each_damage_and_maps_numbered_photos():
    result = extract_athlon_huesges_report(_text_extraction(), _layout())

    assert len(result.damages) == 3
    first, second, third = result.damages
    assert (first.item_number, first.category, first.component, first.description) == (
        1,
        "Karosserie außen",
        "Rear quarter Left",
        "Scratch",
    )
    assert first.photo is not None
    assert first.photo.page_number == 7
    assert first.photo.image_index == 3
    assert second.component == "Rear door"
    assert second.photo is not None
    assert second.photo.image_index == 4
    assert third.category == "Sonstiges"
    assert third.description == "Not in order"
    assert third.photo is not None
    assert third.photo.image_index == 6
    assert all(damage.provenance.lot_id is None for damage in result.damages)


def test_athlon_huesges_profile_rejects_wrong_layout_or_unconfirmed_input():
    extraction = _text_extraction()
    layout = _layout()

    _assert_raises(
        ValueError,
        lambda: extract_athlon_huesges_report(
            extraction,
            PDFDocumentLayout(
                report_sha256="b" * 64,
                page_count=7,
                parser_name="pdfplumber",
                parser_version="synthetic",
                pages=layout.pages,
            ),
        ),
    )

    unsupported = PDFTextExtraction(
        report_sha256=REPORT_SHA256,
        page_count=1,
        pages=(PDFPageText(1, "A different inspection report"),),
        parser_name="pypdf",
        parser_version="synthetic",
        extracted_at=EXTRACTED_AT,
    )
    _assert_raises(
        ValueError,
        lambda: extract_athlon_huesges_report(
            unsupported,
            PDFDocumentLayout(
                report_sha256=REPORT_SHA256,
                page_count=1,
                parser_name="pdfplumber",
                parser_version="synthetic",
                pages=(_page_layout(1),),
            ),
        ),
    )


def test_damage_observations_are_saved_with_page_photo_and_history(tmp_path):
    database = ArchiveDatabase(tmp_path / "damage-observations.sqlite3")
    database.initialize()
    lot_id = database.upsert_auction_lot(
        "synthetic-car-1",
        "synthetic-auction-1",
        observed_at=EXTRACTED_AT,
    )
    damages = extract_athlon_huesges_report(
        _text_extraction(), _layout(), lot_id="source-lot-reference"
    ).damages

    first_ids = database.save_pdf_damage_observations(
        damages,
        auction_lot_id=lot_id,
    )
    second_ids = database.save_pdf_damage_observations(
        damages,
        auction_lot_id=lot_id,
    )
    stored = database.get_pdf_damage_observations(
        report_sha256=REPORT_SHA256,
        auction_lot_id=lot_id,
    )

    assert len(first_ids) == len(second_ids) == 3
    assert set(first_ids).isdisjoint(second_ids)
    assert len(stored) == 6
    first = stored[0]
    assert first["source_lot_id"] == "source-lot-reference"
    assert first["auction_lot_id"] == lot_id
    assert first["page_number"] == 4
    assert first["photo_page_number"] == 7
    assert first["photo_index"] == 3
    assert first["photo_x1"] > first["photo_x0"]


def test_damage_observations_reject_unknown_lot_without_saving(tmp_path):
    database = ArchiveDatabase(tmp_path / "damage-unknown-lot.sqlite3")
    database.initialize()
    damages = extract_athlon_huesges_report(_text_extraction(), _layout()).damages

    _assert_raises(
        ValueError,
        lambda: database.save_pdf_damage_observations(damages, auction_lot_id=99999),
    )

    assert database.get_pdf_damage_observations(report_sha256=REPORT_SHA256) == []
