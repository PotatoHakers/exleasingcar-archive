from exleasingcar_archive.pdf_vin import (
    PDFVINAssociationState,
    evaluate_pdf_vin_association,
)

PDF_VIN = "VF7SXYHTUNT6ABC12"


def _assert_raises(expected_exception, callback):
    try:
        callback()
    except expected_exception:
        return
    raise AssertionError(f"Expected {expected_exception.__name__} to be raised")


def test_pdf_vin_can_be_checked_against_a_complete_html_vin():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=12,
        html_chassis_number=PDF_VIN,
    )

    assert result.state is PDFVINAssociationState.ELIGIBLE
    assert result.can_enrich_vehicle is True
    assert result.requires_review is False
    assert result.candidate_vin == PDF_VIN


def test_pdf_vin_can_match_a_long_visible_masked_html_fragment():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=12,
        html_chassis_number_fragment="VF7SXYHTUNT6",
        html_chassis_number_masked=True,
    )

    assert result.state is PDFVINAssociationState.ELIGIBLE
    assert result.can_enrich_vehicle is True


def test_pdf_vin_conflict_with_html_identity_requires_review():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=12,
        html_chassis_number="VF7SXYHTUNT6ABC13",
    )

    assert result.state is PDFVINAssociationState.CONFLICT
    assert result.can_enrich_vehicle is False
    assert result.requires_review is True


def test_pdf_vin_conflict_with_masked_fragment_requires_review():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=12,
        html_chassis_number_fragment="WXYZ98765432",
        html_chassis_number_masked=True,
    )

    assert result.state is PDFVINAssociationState.CONFLICT
    assert result.can_enrich_vehicle is False
    assert result.requires_review is True


def test_pdf_vin_is_not_eligible_without_confirmed_lot_association():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=None,
        html_chassis_number=PDF_VIN,
    )

    assert result.state is PDFVINAssociationState.UNASSOCIATED
    assert result.can_enrich_vehicle is False
    assert result.requires_review is True


def test_ambiguous_html_fragment_does_not_allow_pdf_vin_enrichment():
    for fragment, masked in (
        ("", True),
        ("ABCD12", True),
        ("VF7SXYHTUNT6", False),
    ):
        result = evaluate_pdf_vin_association(
            PDF_VIN,
            auction_lot_id=12,
            html_chassis_number_fragment=fragment,
            html_chassis_number_masked=masked,
        )

        assert result.state is PDFVINAssociationState.UNVERIFIABLE
        assert result.can_enrich_vehicle is False
        assert result.requires_review is True


def test_pdf_vin_is_not_eligible_without_any_html_identity():
    result = evaluate_pdf_vin_association(
        PDF_VIN,
        auction_lot_id=12,
    )

    assert result.state is PDFVINAssociationState.UNVERIFIABLE
    assert result.can_enrich_vehicle is False


def test_invalid_pdf_vin_is_not_eligible():
    result = evaluate_pdf_vin_association(
        "not-a-vin",
        auction_lot_id=12,
        html_chassis_number="not-a-vin",
    )

    assert result.state is PDFVINAssociationState.INVALID_PDF_VIN
    assert result.can_enrich_vehicle is False


def test_boolean_lot_ids_are_not_accepted():
    _assert_raises(
        TypeError,
        lambda: evaluate_pdf_vin_association(
            PDF_VIN,
            auction_lot_id=True,
            html_chassis_number=PDF_VIN,
        ),
    )
