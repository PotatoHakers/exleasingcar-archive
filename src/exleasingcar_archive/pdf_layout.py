"""Read-only PDF page layout extraction for report photos and coordinate-aware parsing."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from io import BytesIO

import pdfplumber

from .pdf_extractor import DEFAULT_MAX_PDF_BYTES, DEFAULT_MAX_PDF_PAGES, PDFExtractionError


@dataclass(frozen=True, slots=True)
class PDFLayoutWord:
    """One extracted word with coordinates in PDF points from the top-left."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True, slots=True)
class PDFLayoutPhoto:
    """A photograph embedded on a PDF page, with its nearby caption if present."""

    page_number: int
    image_index: int
    x0: float
    top: float
    x1: float
    bottom: float
    caption: str | None


@dataclass(frozen=True, slots=True)
class PDFPageLayout:
    """Words and photographs found on one page."""

    page_number: int
    width: float
    height: float
    words: tuple[PDFLayoutWord, ...]
    photos: tuple[PDFLayoutPhoto, ...]


@dataclass(frozen=True, slots=True)
class PDFDocumentLayout:
    """Coordinate-aware view of a PDF, separate from its extracted text."""

    report_sha256: str
    page_count: int
    parser_name: str
    parser_version: str
    pages: tuple[PDFPageLayout, ...]


def _validate_pdf_bytes(pdf_bytes: bytes, max_bytes: int) -> None:
    if not isinstance(pdf_bytes, bytes):
        raise TypeError("pdf_bytes must be bytes")
    if not pdf_bytes:
        raise PDFExtractionError("PDF input is empty")
    if len(pdf_bytes) > max_bytes:
        raise PDFExtractionError("PDF exceeds the configured byte limit")
    if b"%PDF-" not in pdf_bytes[:1024]:
        raise PDFExtractionError("Input does not have a valid PDF header")


def _group_words_by_line(words: tuple[PDFLayoutWord, ...]) -> list[list[PDFLayoutWord]]:
    lines: list[list[PDFLayoutWord]] = []
    for word in sorted(words, key=lambda item: (item.top, item.x0)):
        if not lines or abs(word.top - lines[-1][0].top) > 3.0:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [sorted(line, key=lambda item: item.x0) for line in lines]


def _caption_for_photo(
    words: tuple[PDFLayoutWord, ...],
    *,
    x0: float,
    bottom: float,
    x1: float,
) -> str | None:
    caption_words = [
        word
        for word in words
        if bottom - 1.0 <= word.top <= bottom + 28.0
        and word.x0 >= x0 - 3.0
        and word.x1 <= x1 + 3.0
    ]
    lines = _group_words_by_line(tuple(caption_words))
    caption = "\n".join(" ".join(word.text for word in line) for line in lines)
    return caption.strip() or None


def extract_pdf_layout(
    pdf_bytes: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
) -> PDFDocumentLayout:
    """Extract bounded page words and photo rectangles without OCR or network access.

    Decorative logos, divider images, and small camera icons are excluded from
    the photo list. The remaining large landscape images are kept as page-local
    references; their bytes are not copied or persisted.
    """
    for value, name in ((max_bytes, "max_bytes"), (max_pages, "max_pages")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    _validate_pdf_bytes(pdf_bytes, max_bytes)

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as document:
            page_count = len(document.pages)
            if page_count > max_pages:
                raise PDFExtractionError("PDF exceeds the configured page limit")

            pages: list[PDFPageLayout] = []
            for page_number, page in enumerate(document.pages, start=1):
                words = tuple(
                    PDFLayoutWord(
                        text=str(word["text"]),
                        x0=float(word["x0"]),
                        top=float(word["top"]),
                        x1=float(word["x1"]),
                        bottom=float(word["bottom"]),
                    )
                    for word in page.extract_words()
                    if str(word.get("text", "")).strip()
                )

                candidates: list[tuple[float, float, float, float]] = []
                for image in page.images:
                    x0 = float(image["x0"])
                    top = float(image["top"])
                    x1 = float(image["x1"])
                    bottom = float(image["bottom"])
                    width = x1 - x0
                    height = bottom - top
                    if not all(math.isfinite(value) for value in (x0, top, x1, bottom)):
                        continue
                    if width < 100.0 or height < 60.0:
                        continue
                    aspect_ratio = width / height
                    if not 1.3 <= aspect_ratio <= 2.2:
                        continue
                    candidates.append((x0, top, x1, bottom))

                candidates.sort(key=lambda box: (box[1], box[0]))
                photos = tuple(
                    PDFLayoutPhoto(
                        page_number=page_number,
                        image_index=index,
                        x0=box[0],
                        top=box[1],
                        x1=box[2],
                        bottom=box[3],
                        caption=_caption_for_photo(
                            words,
                            x0=box[0],
                            bottom=box[3],
                            x1=box[2],
                        ),
                    )
                    for index, box in enumerate(candidates, start=1)
                )
                pages.append(
                    PDFPageLayout(
                        page_number=page_number,
                        width=float(page.width),
                        height=float(page.height),
                        words=words,
                        photos=photos,
                    )
                )
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError(
            "PDF page layout could not be read"
        ) from exc

    return PDFDocumentLayout(
        report_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        page_count=page_count,
        parser_name="pdfplumber",
        parser_version=pdfplumber.__version__,
        pages=tuple(pages),
    )
