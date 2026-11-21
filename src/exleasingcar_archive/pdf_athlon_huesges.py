"""Verified text/layout rules for the Athlon / Hüsges inspection report layout."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .pdf_damage import PDFDamageObservation, PDFDamagePhotoReference
from .pdf_extractor import PDFTextExtraction
from .pdf_fields import (
    PDFExtractedField,
    PDFFieldProvenance,
    PDFFieldRule,
    PDFFieldState,
    extract_labelled_fields,
)
from .pdf_layout import PDFDocumentLayout, PDFLayoutWord

ATHLON_HUESGES_PROFILE_VERSION = "1.0.0"


ATHLON_HUESGES_FIELD_RULES = (
    PDFFieldRule(
        "vehicle.vin",
        "Fahrzeug-Ident.-Nr.",
        r"Fahrzeug-Ident\.-Nr\.\s*:\s*(?P<value>[A-HJ-NPR-Z0-9]{17})",
    ),
    PDFFieldRule(
        "vehicle.odometer",
        "Laufleistung abgelesen",
        r"Laufleistung abgelesen:\s*(?P<value>[\d .]+\s*km)",
    ),
    PDFFieldRule(
        "vehicle.first_registration_date",
        "Erstzulassung / Baujahr",
        r"Erstzulassung\s*/\s*Baujahr:\s*(?P<value>\d{2}\.\d{2}\.\d{4})",
    ),
    PDFFieldRule(
        "vehicle.build_year",
        "Erstzulassung / Baujahr",
        r"Erstzulassung\s*/\s*Baujahr:\s*\d{2}\.\d{2}\.\d{4}\s*/\s*(?P<value>\d{4})",
    ),
    PDFFieldRule(
        "vehicle.inspection_date_and_location",
        "Besichtigungsdatum/-ort",
        r"Besichtigungsdatum/-ort:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.vehicle_type",
        "Fahrzeugart",
        r"Fahrzeugart:\s*(?P<value>.*?)\s+Aufbauart:",
    ),
    PDFFieldRule(
        "vehicle.body_type",
        "Aufbauart",
        r"Aufbauart:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.kba_number",
        "KBA-Nr. (HSN/ TSN)",
        r"KBA-Nr\.\s*\(HSN/\s*TSN\):\s*(?P<value>.*?)\s+Kraftstoff\s*/\s*Emissionsklasse:",
    ),
    PDFFieldRule(
        "vehicle.fuel_and_emissions",
        "Kraftstoff / Emissionsklasse",
        r"Kraftstoff\s*/\s*Emissionsklasse:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.transmission",
        "Getriebe Art",
        r"Getriebe Art:\s*(?P<value>.*?)\s+Navi\.-Datenträger:",
    ),
    PDFFieldRule(
        "vehicle.navigation_media",
        "Navi.-Datenträger",
        r"Navi\.-Datenträger:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.engine_displacement_and_power",
        "Hubraum / Leistung",
        r"Hubraum\s*/\s*Leistung:\s*(?P<value>.*?)\s+Antriebsart:",
    ),
    PDFFieldRule(
        "vehicle.drivetrain",
        "Antriebsart",
        r"Antriebsart:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.paint_colour",
        "Lackfarbe",
        r"Lackfarbe:\s*(?P<value>.*?)\s+Lackart:",
    ),
    PDFFieldRule(
        "vehicle.paint_type",
        "Lackart",
        r"Lackart:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.key_count",
        "Anzahl Schlüssel",
        r"Anzahl Schlüssel:\s*(?P<value>\d+)",
    ),
    PDFFieldRule(
        "vehicle.upholstery_type",
        "Polster Art",
        r"Polster Art:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.service_booklet_available",
        "Serviceheft verfügbar",
        r"Serviceheft verfügbar:\s*(?P<value>.*?)\s+Farbe Innenausstattung:",
    ),
    PDFFieldRule(
        "vehicle.interior_colour",
        "Farbe Innenausstattung",
        r"Farbe Innenausstattung:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "vehicle.last_service",
        "Letzter Service km / am",
        r"Letzter Service km\s*/\s*am:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "tyres.type_and_rim",
        "Reifenart / Felgentyp",
        r"Reifenart\s*/\s*Felgentyp:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "tyres.axle_1.left",
        "1. Achse - links / Zust.",
        r"^1\.\s*Achse\s*-\s*links\s*/\s*Zust\.\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "tyres.axle_1.right",
        "1. Achse - rechts / Zust.",
        r"^1\.\s*Achse\s*-\s*rechts\s*/\s*Zust\.\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "tyres.axle_2.left",
        "2. Achse - links / Zust.",
        r"^2\.\s*Achse\s*-\s*links\s*/\s*Zust\.\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "tyres.axle_2.right",
        "2. Achse - rechts / Zust.",
        r"^2\.\s*Achse\s*-\s*rechts\s*/\s*Zust\.\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "inspection.air_conditioning",
        "Klimaanlage",
        r"^Klimaanlage:\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "inspection.lighting",
        "Lichtanlage",
        r"^Lichtanlage:\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "inspection.engine_operation",
        "Motorlauf",
        r"^Motorlauf:\s*(?P<value>.*)$",
    ),
    PDFFieldRule(
        "inspection.paint_thickness",
        "Lackschichtendickenmessung",
        r"Lackschichtendickenmessung:\s*(?P<value>.*?)$",
    ),
    PDFFieldRule(
        "inspection.note",
        "Hinweis",
        r"^Hinweis:\s*(?P<value>.*)$",
    ),
)


@dataclass(frozen=True, slots=True)
class AthlonHuesgesReportExtraction:
    """Structured text fields and damage rows from one supported report."""

    fields: tuple[PDFExtractedField, ...]
    damages: tuple[PDFDamageObservation, ...]


def supports_athlon_huesges_report(extraction: PDFTextExtraction) -> bool:
    if not isinstance(extraction, PDFTextExtraction) or not extraction.pages:
        return False
    first_page = extraction.pages[0].text.casefold()
    return "athlon verkaufsexpertise" in first_page and "hüsges" in first_page


def _group_words_by_line(words: tuple[PDFLayoutWord, ...]) -> list[list[PDFLayoutWord]]:
    lines: list[list[PDFLayoutWord]] = []
    for word in sorted(words, key=lambda item: (item.top, item.x0)):
        if not lines or abs(word.top - lines[-1][0].top) > 3.0:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [sorted(line, key=lambda item: item.x0) for line in lines]


def _field(
    *,
    extraction: PDFTextExtraction,
    lot_id: str | None,
    key: str,
    source_label: str,
    raw_text: str,
    page_number: int,
    parser_name: str,
    parser_version: str,
) -> PDFExtractedField:
    normalized = " ".join(raw_text.split())
    state = PDFFieldState.BLANK if not normalized else PDFFieldState.VALUE
    return PDFExtractedField(
        key=key,
        raw_text=raw_text,
        normalized_text=normalized or None,
        state=state,
        provenance=PDFFieldProvenance(
            report_sha256=extraction.report_sha256,
            lot_id=lot_id,
            page_number=page_number,
            source_label=source_label,
            parser_name=parser_name,
            parser_version=parser_version,
            extracted_at=extraction.extracted_at,
        ),
    )


def _continues_equipment_item(previous: str, current: str) -> bool:
    previous = previous.rstrip()
    current = current.lstrip()
    return previous.endswith(("-", ",", "(", "/")) or current.startswith((")", ","))




def _extract_vehicle_title(
    extraction: PDFTextExtraction,
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> PDFExtractedField | None:
    first_page = extraction.pages[0]
    lines = first_page.text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() != "fahrzeugdaten":
            continue
        for candidate in lines[index + 1 :]:
            value = candidate.strip()
            if not value:
                continue
            if ":" in value or len(value) > 80:
                return None
            return _field(
                extraction=extraction,
                lot_id=lot_id,
                key="vehicle.reported_title",
                source_label="Fahrzeugdaten",
                raw_text=value,
                page_number=first_page.page_number,
                parser_name=parser_name,
                parser_version=parser_version,
            )
    return None


def _extract_structured_tyre_fields(
    extraction: PDFTextExtraction,
    fields: list[PDFExtractedField],
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> list[PDFExtractedField]:
    derived: list[PDFExtractedField] = []
    pattern = re.compile(
        r"(?P<dimension>\d{3}/\d{2}/R?\d{2}/\d{2,3})\s+"
        r"(?P<tread>\d+(?:[.,]\d+)?)\s*mm",
        re.IGNORECASE,
    )
    for field in fields:
        if not field.key.startswith("tyres.axle_") or field.normalized_text is None:
            continue
        match = pattern.fullmatch(field.normalized_text)
        if match is None:
            continue
        common = {
            "extraction": extraction,
            "lot_id": lot_id,
            "source_label": field.provenance.source_label,
            "page_number": field.provenance.page_number,
            "parser_name": parser_name,
            "parser_version": parser_version,
        }
        derived.append(
            _field(
                key=f"{field.key}.dimension",
                raw_text=match.group("dimension"),
                **common,
            )
        )
        derived.append(
            _field(
                key=f"{field.key}.tread_depth_mm",
                raw_text=f"{match.group('tread')} mm",
                **common,
            )
        )
    return derived


def _extract_equipment_fields(
    extraction: PDFTextExtraction,
    layout: PDFDocumentLayout,
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> list[PDFExtractedField]:
    section: str | None = None
    pending: dict[str, tuple[str, int] | None] = {"left": None, "right": None}
    fields: list[PDFExtractedField] = []

    def flush(column: str) -> None:
        item = pending[column]
        if item is None or section is None:
            return
        text, page_number = item
        fields.append(
            _field(
                extraction=extraction,
                lot_id=lot_id,
                key=f"equipment.{section}",
                source_label=(
                    "Serienausstattung" if section == "standard" else "Sonderausstattung"
                ),
                raw_text=text,
                page_number=page_number,
                parser_name=parser_name,
                parser_version=parser_version,
            )
        )
        pending[column] = None

    for page in layout.pages:
        for line in _group_words_by_line(page.words):
            line_top = min(word.top for word in line)
            if line_top < 80.0 or line_top > page.height - 50.0:
                continue
            line_text = " ".join(word.text for word in line).strip()
            normalized_line = line_text.casefold()

            if normalized_line == "serienausstattung":
                flush("left")
                flush("right")
                section = "standard"
                continue
            if normalized_line == "sonderausstattung":
                flush("left")
                flush("right")
                section = "special"
                continue
            if "wir weisen darauf hin" in normalized_line:
                flush("left")
                flush("right")
                section = None
                continue
            if section is None:
                continue

            midpoint = page.width / 2
            for column, words in (
                ("left", [word for word in line if word.x0 < midpoint]),
                ("right", [word for word in line if word.x0 >= midpoint]),
            ):
                current = " ".join(word.text for word in words).strip()
                if not current:
                    continue
                previous = pending[column]
                if previous is not None and _continues_equipment_item(previous[0], current):
                    pending[column] = (f"{previous[0]} {current}", previous[1])
                else:
                    flush(column)
                    pending[column] = (current, page.page_number)

    flush("left")
    flush("right")
    return fields


def _damage_photo_map(layout: PDFDocumentLayout) -> dict[int, PDFDamagePhotoReference]:
    photo_map: dict[int, PDFDamagePhotoReference] = {}
    for page in layout.pages:
        for photo in page.photos:
            if not photo.caption:
                continue
            match = re.match(r"\s*(\d+)\s*\.", photo.caption)
            if match is None:
                continue
            item_number = int(match.group(1))
            photo_map[item_number] = PDFDamagePhotoReference(
                page_number=photo.page_number,
                image_index=photo.image_index,
                caption=photo.caption,
                x0=photo.x0,
                top=photo.top,
                x1=photo.x1,
                bottom=photo.bottom,
            )
    return photo_map


def _extract_damage_observations(
    extraction: PDFTextExtraction,
    layout: PDFDocumentLayout,
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> tuple[PDFDamageObservation, ...]:
    damage_page = next(
        (
            page
            for page in layout.pages
            if any(
                text == "schaden"
                for text in (
                    " ".join(word.text for word in line).strip().casefold()
                    for line in _group_words_by_line(page.words)
                )
            )
            and any(
                text in {"karosserie außen".casefold(), "sonstiges"}
                for text in (
                    " ".join(word.text for word in line).strip().casefold()
                    for line in _group_words_by_line(page.words)
                )
            )
        ),
        None,
    )
    if damage_page is None:
        return ()

    photo_map = _damage_photo_map(layout)
    category: str | None = None
    current: dict[str, object] | None = None
    observations: list[PDFDamageObservation] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        component = " ".join(str(current["component"]).split())
        description = " ".join(str(current["description"]).split())
        if component and description and str(current["category"]):
            item_number = int(current["item_number"])
            observations.append(
                PDFDamageObservation(
                    item_number=item_number,
                    category=str(current["category"]),
                    component=component,
                    description=description,
                    provenance=PDFFieldProvenance(
                        report_sha256=extraction.report_sha256,
                        lot_id=lot_id,
                        page_number=damage_page.page_number,
                        source_label="Schaden",
                        parser_name=parser_name,
                        parser_version=parser_version,
                        extracted_at=extraction.extracted_at,
                    ),
                    photo=photo_map.get(item_number),
                )
            )
        current = None

    for line in _group_words_by_line(damage_page.words):
        line_text = " ".join(word.text for word in line).strip()
        normalized = line_text.casefold()

        if normalized in {"karosserie außen".casefold(), "sonstiges"}:
            flush()
            category = line_text
            continue
        if normalized == "bauteil schaden":
            continue

        number_word = next(
            (
                word
                for word in line
                if word.x0 < 65 and re.fullmatch(r"\d+\.", word.text)
            ),
            None,
        )
        if number_word is not None:
            flush()
            item_number = int(number_word.text[:-1])
            component_words = [
                word for word in line if word.x0 >= 65 and word.x0 < 180
            ]
            description_words = [word for word in line if word.x0 >= 180]
            current = {
                "item_number": item_number,
                "category": category or "",
                "component": " ".join(word.text for word in component_words),
                "description": " ".join(word.text for word in description_words),
            }
            continue

        if current is None:
            continue
        component_words = [
            word for word in line if word.x0 >= 65 and word.x0 < 180
        ]
        description_words = [word for word in line if word.x0 >= 180]
        if component_words:
            current["component"] = (
                f"{current['component']} " + " ".join(word.text for word in component_words)
            ).strip()
        if description_words:
            current["description"] = (
                f"{current['description']} "
                + " ".join(word.text for word in description_words)
            ).strip()

    flush()
    return tuple(observations)






def _extract_paint_thickness_details(
    extraction: PDFTextExtraction,
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> list[PDFExtractedField]:
    """Keep the explanatory text below the paint-thickness result separately."""
    fields: list[PDFExtractedField] = []
    for page in extraction.pages:
        lines = page.text.splitlines()
        for index, line in enumerate(lines):
            if not line.strip().casefold().startswith("lackschichtendickenmessung:"):
                continue
            details: list[str] = []
            for candidate in lines[index + 1 :]:
                value = candidate.strip()
                if (
                    not value
                    or value.casefold().startswith("hinweis:")
                    or value.casefold().startswith("berichtsnummer")
                ):
                    break
                details.append(value)
            if details:
                fields.append(
                    _field(
                        extraction=extraction,
                        lot_id=lot_id,
                        key="inspection.paint_thickness_details",
                        source_label="Lackschichtendickenmessung (Beschreibung)",
                        raw_text="\n".join(details),
                        page_number=page.page_number,
                        parser_name=parser_name,
                        parser_version=parser_version,
                    )
                )
    return fields


def _extract_report_limitations(
    extraction: PDFTextExtraction,
    *,
    lot_id: str | None,
    parser_name: str,
    parser_version: str,
) -> list[PDFExtractedField]:
    """Keep source disclaimers and limits as text, without interpreting them."""
    fields: list[PDFExtractedField] = []
    markers = (
        ("report.method_statement", "Die Athlon Verkaufsexpertise"),
        ("report.scope_limitation", "Wir weisen darauf hin"),
    )
    for page in extraction.pages:
        lines = page.text.splitlines()
        index = 0
        while index < len(lines):
            current = lines[index].strip()
            marker = next(
                (
                    (key, label)
                    for key, label in markers
                    if current.casefold().startswith(label.casefold())
                ),
                None,
            )
            if marker is None:
                index += 1
                continue

            block = [current]
            index += 1
            while index < len(lines):
                candidate = lines[index].strip()
                if not candidate or candidate.casefold().startswith("berichtsnummer"):
                    break
                block.append(candidate)
                index += 1

            fields.append(
                _field(
                    extraction=extraction,
                    lot_id=lot_id,
                    key=marker[0],
                    source_label=marker[1],
                    raw_text="\n".join(block),
                    page_number=page.page_number,
                    parser_name=parser_name,
                    parser_version=parser_version,
                )
            )
    return fields


def extract_athlon_huesges_report(
    extraction: PDFTextExtraction,
    layout: PDFDocumentLayout,
    *,
    lot_id: str | None = None,
) -> AthlonHuesgesReportExtraction:
    """Extract verified fields and numbered damage/photo references from this report layout.

    The parser stores a VIN only as a source observation. It never updates a
    canonical vehicle identity or infers a lot association.
    """
    if not isinstance(extraction, PDFTextExtraction):
        raise TypeError("extraction must be a PDFTextExtraction")
    if not isinstance(layout, PDFDocumentLayout):
        raise TypeError("layout must be a PDFDocumentLayout")
    if extraction.page_count != layout.page_count:
        raise ValueError("text extraction and page layout must have the same page count")
    if extraction.report_sha256 != layout.report_sha256:
        raise ValueError("text extraction and page layout must refer to the same report")
    if lot_id is not None and (not isinstance(lot_id, str) or not lot_id.strip()):
        raise ValueError("lot_id must be a non-empty string or None")
    if not supports_athlon_huesges_report(extraction):
        raise ValueError("PDF is not a supported Athlon / Hüsges report")

    normalized_lot_id = lot_id.strip() if lot_id is not None else None
    parser_name = "pypdf+pdfplumber+athlon_huesges"
    parser_version = (
        f"{extraction.parser_version}+{layout.parser_version}+"
        f"{ATHLON_HUESGES_PROFILE_VERSION}"
    )
    profile_extraction = replace(
        extraction,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    fields = list(
        extract_labelled_fields(
            profile_extraction,
            ATHLON_HUESGES_FIELD_RULES,
            lot_id=normalized_lot_id,
        )
    )
    title_field = _extract_vehicle_title(
        extraction,
        lot_id=normalized_lot_id,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    if title_field is not None:
        fields.append(title_field)
    fields.extend(
        _extract_structured_tyre_fields(
            extraction,
            fields,
            lot_id=normalized_lot_id,
            parser_name=parser_name,
            parser_version=parser_version,
        )
    )
    fields.extend(
        _extract_paint_thickness_details(
            extraction,
            lot_id=normalized_lot_id,
            parser_name=parser_name,
            parser_version=parser_version,
        )
    )
    fields.extend(
        _extract_report_limitations(
            extraction,
            lot_id=normalized_lot_id,
            parser_name=parser_name,
            parser_version=parser_version,
        )
    )
    fields.extend(
        _extract_equipment_fields(
            extraction,
            layout,
            lot_id=normalized_lot_id,
            parser_name=parser_name,
            parser_version=parser_version,
        )
    )
    fields.sort(key=lambda field: field.provenance.page_number)
    damages = _extract_damage_observations(
        extraction,
        layout,
        lot_id=normalized_lot_id,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    return AthlonHuesgesReportExtraction(
        fields=tuple(fields),
        damages=damages,
    )
