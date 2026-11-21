from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from pypdf import PdfWriter

from exleasingcar_archive.pdf_extractor import PDFExtractionError
from exleasingcar_archive.pdf_layout import extract_pdf_layout


def _blank_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extract_pdf_layout_keeps_page_count_hash_and_parser_provenance():
    pdf_bytes = _blank_pdf(3)

    layout = extract_pdf_layout(pdf_bytes)

    assert layout.report_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert layout.page_count == 3
    assert len(layout.pages) == 3
    assert [page.page_number for page in layout.pages] == [1, 2, 3]
    assert all(page.photos == () for page in layout.pages)
    assert layout.parser_name == "pdfplumber"
    assert layout.parser_version


def test_extract_pdf_layout_applies_limits_and_input_validation():
    pdf_bytes = _blank_pdf(2)

    with pytest.raises(PDFExtractionError, match="page limit"):
        extract_pdf_layout(pdf_bytes, max_pages=1)
    with pytest.raises(PDFExtractionError, match="byte limit"):
        extract_pdf_layout(pdf_bytes, max_bytes=len(pdf_bytes) - 1)
    with pytest.raises(TypeError, match="bytes"):
        extract_pdf_layout(bytearray(pdf_bytes))
    with pytest.raises(PDFExtractionError, match="header"):
        extract_pdf_layout(b"not a pdf")
