"""Label-driven PDF field extraction with page-level provenance.

The parser deliberately requires verified labels and regular expressions from
its caller. It does not infer report fields or normalize their meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .pdf_extractor import PDFTextExtraction


class PDFFieldState(StrEnum):
    """Source state retained without collapsing distinct report meanings."""

    VALUE = "value"
    BLANK = "blank"
    EXPLICIT_NO = "explicit_no"
    NOT_APPLICABLE = "not_applicable"
    NOT_TESTED = "not_tested"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PDFFieldRule:
    """A verified source label and caller-supplied expression for its value."""

    key: str
    label: str
    pattern: str
    state_tokens: tuple[tuple[str, PDFFieldState], ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("field key cannot be empty")
        if not self.label.strip():
            raise ValueError("source label cannot be empty")
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError("field pattern is not a valid regular expression") from exc
        if "value" not in compiled.groupindex:
            raise ValueError("field pattern must define a named 'value' group")
        normalized_tokens = [token.casefold().strip() for token, _ in self.state_tokens]
        if any(not token for token in normalized_tokens):
            raise ValueError("state tokens cannot be empty")
        if len(normalized_tokens) != len(set(normalized_tokens)):
            raise ValueError("state tokens must be unique ignoring case")
        if any(not isinstance(state, PDFFieldState) for _, state in self.state_tokens):
            raise TypeError("state token values must be PDFFieldState members")


@dataclass(frozen=True, slots=True)
class PDFFieldProvenance:
    """Trace from one extracted field to its immutable source report."""

    report_sha256: str
    lot_id: str | None
    page_number: int
    source_label: str
    parser_name: str
    parser_version: str
    extracted_at: datetime


@dataclass(frozen=True, slots=True)
class PDFExtractedField:
    """One source observation; raw and whitespace-normalized text stay separate."""

    key: str
    raw_text: str
    normalized_text: str | None
    state: PDFFieldState
    provenance: PDFFieldProvenance


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def extract_labelled_fields(
    extraction: PDFTextExtraction,
    rules: tuple[PDFFieldRule, ...],
    *,
    lot_id: str | None = None,
) -> tuple[PDFExtractedField, ...]:
    """Extract only fields matched by caller-supplied, verified label rules.

    A lot ID is optional because a report-to-lot relationship must be
    established by the caller; this function never guesses that association.
    Every matching occurrence is retained, including repeated labels on
    different pages.
    """
    if not isinstance(extraction, PDFTextExtraction):
        raise TypeError("extraction must be a PDFTextExtraction")
    if not isinstance(rules, tuple) or any(
        not isinstance(rule, PDFFieldRule) for rule in rules
    ):
        raise TypeError("rules must be a tuple of PDFFieldRule values")
    if lot_id is not None and (not isinstance(lot_id, str) or not lot_id.strip()):
        raise ValueError("lot_id must be a non-empty string or None")

    fields: list[PDFExtractedField] = []
    for rule in rules:
        expression = re.compile(rule.pattern)
        state_tokens = {
            token.casefold().strip(): state for token, state in rule.state_tokens
        }
        for page in extraction.pages:
            for line in page.text.splitlines():
                match = expression.search(line)
                if match is None:
                    continue
                raw_text = match.group("value")
                normalized_text = _normalize_whitespace(raw_text)
                state = (
                    PDFFieldState.BLANK
                    if not normalized_text
                    else state_tokens.get(
                        normalized_text.casefold(), PDFFieldState.VALUE
                    )
                )
                fields.append(
                    PDFExtractedField(
                        key=rule.key,
                        raw_text=raw_text,
                        normalized_text=normalized_text or None,
                        state=state,
                        provenance=PDFFieldProvenance(
                            report_sha256=extraction.report_sha256,
                            lot_id=lot_id.strip() if lot_id is not None else None,
                            page_number=page.page_number,
                            source_label=rule.label,
                            parser_name=extraction.parser_name,
                            parser_version=extraction.parser_version,
                            extracted_at=extraction.extracted_at,
                        ),
                    )
                )
    return tuple(fields)
