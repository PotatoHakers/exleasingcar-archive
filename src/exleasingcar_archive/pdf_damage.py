"""Structured damage observations extracted from inspection reports."""

from __future__ import annotations

from dataclasses import dataclass

from .pdf_fields import PDFFieldProvenance


@dataclass(frozen=True, slots=True)
class PDFDamagePhotoReference:
    """A photo crop inside the immutable source report PDF."""

    page_number: int
    image_index: int
    caption: str
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True, slots=True)
class PDFDamageObservation:
    """One separately retained damage/check item from a report."""

    item_number: int
    category: str
    component: str
    description: str
    provenance: PDFFieldProvenance
    photo: PDFDamagePhotoReference | None = None
