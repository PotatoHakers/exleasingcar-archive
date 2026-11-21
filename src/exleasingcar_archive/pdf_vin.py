"""Conservative checks before a PDF VIN can enrich a vehicle record."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

MINIMUM_MASKED_VIN_FRAGMENT_LENGTH = 8


class PDFVINAssociationState(str, Enum):
    """Result of comparing a PDF VIN with an already-confirmed lot and HTML identity."""

    ELIGIBLE = "eligible"
    UNASSOCIATED = "unassociated"
    CONFLICT = "conflict"
    UNVERIFIABLE = "unverifiable"
    INVALID_PDF_VIN = "invalid_pdf_vin"


@dataclass(frozen=True, slots=True)
class PDFVINAssociation:
    """A review result only; it never writes or updates the canonical vehicle VIN."""

    candidate_vin: str
    state: PDFVINAssociationState
    can_enrich_vehicle: bool
    requires_review: bool
    reason: str


def _normalize_vin(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def evaluate_pdf_vin_association(
    pdf_vin: str,
    *,
    auction_lot_id: int | None,
    html_chassis_number: str | None = None,
    html_chassis_number_fragment: str | None = None,
    html_chassis_number_masked: bool | None = None,
) -> PDFVINAssociation:
    """Check a PDF VIN without guessing a lot link or changing vehicle identity.

    A full HTML VIN must match exactly. A masked HTML fragment is only accepted
    when its visible characters form a sufficiently long contiguous substring
    of the PDF VIN. Missing, short, or ambiguous evidence is never treated as
    a successful match.
    """
    if not isinstance(pdf_vin, str):
        raise TypeError("pdf_vin must be a string")
    candidate = _normalize_vin(pdf_vin)
    if (
        len(candidate) != 17
        or re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", candidate) is None
    ):
        return PDFVINAssociation(
            candidate_vin=candidate,
            state=PDFVINAssociationState.INVALID_PDF_VIN,
            can_enrich_vehicle=False,
            requires_review=True,
            reason="The extracted PDF value is not a valid 17-character VIN.",
        )

    if auction_lot_id is not None:
        if isinstance(auction_lot_id, bool) or not isinstance(auction_lot_id, int):
            raise TypeError("auction_lot_id must be an integer or None")
        if auction_lot_id <= 0:
            raise ValueError("auction_lot_id must be positive")

    if auction_lot_id is None:
        return PDFVINAssociation(
            candidate_vin=candidate,
            state=PDFVINAssociationState.UNASSOCIATED,
            can_enrich_vehicle=False,
            requires_review=True,
            reason="The report has no confirmed auction-lot association.",
        )

    if html_chassis_number is not None:
        if not isinstance(html_chassis_number, str):
            raise TypeError("html_chassis_number must be a string or None")
        html_vin = _normalize_vin(html_chassis_number)
        if (
            len(html_vin) != 17
            or re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", html_vin) is None
        ):
            return PDFVINAssociation(
                candidate_vin=candidate,
                state=PDFVINAssociationState.UNVERIFIABLE,
                can_enrich_vehicle=False,
                requires_review=True,
                reason="The HTML VIN value is not a complete, valid VIN.",
            )
        if html_vin != candidate:
            return PDFVINAssociation(
                candidate_vin=candidate,
                state=PDFVINAssociationState.CONFLICT,
                can_enrich_vehicle=False,
                requires_review=True,
                reason="The PDF VIN conflicts with the complete HTML VIN.",
            )

    if html_chassis_number_fragment is not None:
        if not isinstance(html_chassis_number_fragment, str):
            raise TypeError("html_chassis_number_fragment must be a string or None")
        if html_chassis_number_masked is not True:
            return PDFVINAssociation(
                candidate_vin=candidate,
                state=PDFVINAssociationState.UNVERIFIABLE,
                can_enrich_vehicle=False,
                requires_review=True,
                reason="The HTML fragment is not explicitly marked as masked.",
            )
        fragment = _normalize_vin(html_chassis_number_fragment)
        if len(fragment) < MINIMUM_MASKED_VIN_FRAGMENT_LENGTH:
            return PDFVINAssociation(
                candidate_vin=candidate,
                state=PDFVINAssociationState.UNVERIFIABLE,
                can_enrich_vehicle=False,
                requires_review=True,
                reason="The visible HTML VIN fragment is too short to verify safely.",
            )
        if fragment not in candidate:
            return PDFVINAssociation(
                candidate_vin=candidate,
                state=PDFVINAssociationState.CONFLICT,
                can_enrich_vehicle=False,
                requires_review=True,
                reason="The PDF VIN does not contain the visible HTML VIN fragment.",
            )

    if html_chassis_number is None and html_chassis_number_fragment is None:
        return PDFVINAssociation(
            candidate_vin=candidate,
            state=PDFVINAssociationState.UNVERIFIABLE,
            can_enrich_vehicle=False,
            requires_review=True,
            reason="No complete HTML VIN or usable masked VIN fragment is available.",
        )

    return PDFVINAssociation(
        candidate_vin=candidate,
        state=PDFVINAssociationState.ELIGIBLE,
        can_enrich_vehicle=True,
        requires_review=False,
        reason="The PDF VIN matches the HTML identity for the confirmed auction lot.",
    )
