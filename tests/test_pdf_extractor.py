from __future__ import annotations

import hashlib
from datetime import timezone
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from exleasingcar_archive.pdf_extractor import (
    PDFExtractionError,
    extract_pdf_text,
)


def _synthetic_pdf(page_texts: list[str | None], *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text is None:
            continue

        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font}
                )
            }
        )
        page[NameObject("/Resources")] = resources

        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(
            f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)

    if password is not None:
        writer.encrypt(password)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extracts_text_page_by_page_and_preserves_empty_pages():
    pdf_bytes = _synthetic_pdf(
        ["Vehicle identity and mileage", None, "Damage item 1"]
    )

    result = extract_pdf_text(pdf_bytes)

    assert result.page_count == 3
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert result.pages[0].text == "Vehicle identity and mileage"
    assert result.pages[1].text == ""
    assert result.pages[2].text == "Damage item 1"
    assert result.report_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.parser_name == "pypdf"
    assert result.parser_version
    assert result.extracted_at.tzinfo == timezone.utc


def test_rejects_non_pdf_input():
    with pytest.raises(PDFExtractionError, match="PDF header"):
        extract_pdf_text(b"<html>not a PDF</html>")


def test_rejects_empty_input():
    with pytest.raises(PDFExtractionError, match="empty"):
        extract_pdf_text(b"")


def test_rejects_input_over_byte_limit():
    pdf_bytes = _synthetic_pdf(["small"])
    with pytest.raises(PDFExtractionError, match="byte limit"):
        extract_pdf_text(pdf_bytes, max_bytes=len(pdf_bytes) - 1)


def test_rejects_documents_over_page_limit():
    pdf_bytes = _synthetic_pdf(["one", "two"])
    with pytest.raises(PDFExtractionError, match="page limit"):
        extract_pdf_text(pdf_bytes, max_pages=1)


def test_rejects_encrypted_pdf():
    pdf_bytes = _synthetic_pdf(["private"], password="secret")
    with pytest.raises(PDFExtractionError, match="Encrypted"):
        extract_pdf_text(pdf_bytes)


def test_rejects_corrupt_pdf_with_pdf_header():
    with pytest.raises(PDFExtractionError, match="could not be read"):
        extract_pdf_text(b"%PDF-1.7\\nnot-a-valid-pdf")


@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_limits_must_be_positive_integers(value):
    with pytest.raises((TypeError, ValueError)):
        extract_pdf_text(b"%PDF-1.4", max_pages=value)


def test_pdf_input_must_be_bytes():
    with pytest.raises(TypeError, match="must be bytes"):
        extract_pdf_text(bytearray(b"%PDF-1.4"))
