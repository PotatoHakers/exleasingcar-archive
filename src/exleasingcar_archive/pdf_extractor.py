"""Read-only extraction of embedded text from inspection report PDFs.

This module deliberately does not download, persist, render, or OCR PDF files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

import pypdf


DEFAULT_MAX_PDF_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 500


class PDFExtractionError(ValueError):
    """Raised when a PDF cannot be safely read by the text-layer extractor."""


@dataclass(frozen=True, slots=True)
class PDFPageText:
    """Embedded text extracted from one 1-based PDF page."""

    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class PDFTextExtraction:
    """Text-layer result with document-level provenance."""

    report_sha256: str
    page_count: int
    pages: tuple[PDFPageText, ...]
    parser_name: str
    parser_version: str
    extracted_at: datetime


def _validate_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def extract_pdf_text(
    pdf_bytes: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
) -> PDFTextExtraction:
    """Extract the embedded text layer from bounded PDF bytes.

    The caller owns acquisition and storage of the original PDF. No OCR or
    network access is attempted. Blank pages are retained in the result.
    """
    _validate_limit(max_bytes, "max_bytes")
    _validate_limit(max_pages, "max_pages")

    if not isinstance(pdf_bytes, bytes):
        raise TypeError("pdf_bytes must be bytes")
    if not pdf_bytes:
        raise PDFExtractionError("PDF input is empty")
    if len(pdf_bytes) > max_bytes:
        raise PDFExtractionError("PDF exceeds the configured byte limit")
    if b"%PDF-" not in pdf_bytes[:1024]:
        raise PDFExtractionError("Input does not have a valid PDF header")

    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted:
            raise PDFExtractionError("Encrypted PDFs are not supported")
        page_count = len(reader.pages)
        if page_count > max_pages:
            raise PDFExtractionError("PDF exceeds the configured page limit")

        pages = tuple(
            PDFPageText(
                page_number=index,
                text=page.extract_text() or "",
            )
            for index, page in enumerate(reader.pages, start=1)
        )
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError("PDF could not be read or text could not be extracted") from exc

    return PDFTextExtraction(
        report_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        page_count=page_count,
        pages=pages,
        parser_name="pypdf",
        parser_version=pypdf.__version__,
        extracted_at=datetime.now(timezone.utc),
    )
