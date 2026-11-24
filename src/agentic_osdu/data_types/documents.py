"""Supporting documents and structured JSON envelope detection."""

from __future__ import annotations

from collections.abc import Callable

from agentic_osdu.data_types.interpretation import interpretation_binding
from agentic_osdu.data_types.models import (
    ClassificationProfile,
    DataTypeHandler,
    DetectionContext,
    ExtractionBinding,
    FormatMatch,
    no_signature,
)
from agentic_osdu.domain.models import DataCategory, DataSubtype, FormatId
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.tools.contracts import ExtractCsvInput, InterpretationSubtype


def detect_csv(context: DetectionContext) -> tuple[FormatMatch, ...]:
    lines = context.sample.splitlines()
    if context.suffix == ".csv" and lines and (b"," in lines[0] or b";" in lines[0]):
        return (
            FormatMatch(
                0.9,
                "FORMAT-1.SIG-CSV",
                "A delimited header row was observed.",
                "delimited row",
            ),
        )
    return ()


def detect_pdf(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if context.sample.startswith(b"%PDF-"):
        return (FormatMatch(1.0, "FORMAT-1.SIG-PDF", "PDF magic bytes.", "%PDF-"),)
    return ()


def document_profile(
    subtype: DataSubtype,
    category: DataCategory = DataCategory.SUPPORTING_DOCUMENT,
    osdu_kind: str = "osdu:wks:work-product-component--Document:1.0.0",
) -> Callable[[str], ClassificationProfile]:
    def classify(name: str) -> ClassificationProfile:
        return ClassificationProfile(category=category, subtype=subtype, osdu_kind=osdu_kind)

    return classify


def _json_format(context: DetectionContext) -> FormatId | None:
    document = context.json_document
    if (
        isinstance(document, dict)
        and "$schema" in document
        and ("type" in document or "properties" in document)
    ):
        return FormatId.OSDU_SCHEMA
    if isinstance(document, dict) and (
        str(document.get("kind", "")).casefold().startswith("osdu:wks:manifest:")
        or "Data" in document
    ):
        return FormatId.OSDU_MANIFEST
    return None


def json_detector(
    format_id: FormatId, rule_id: str, summary: str, observed: str
) -> Callable[[DetectionContext], tuple[FormatMatch, ...]]:
    def detect(context: DetectionContext) -> tuple[FormatMatch, ...]:
        if _json_format(context) is format_id:
            return (FormatMatch(0.99, rule_id, summary, observed),)
        return ()

    return detect


HANDLERS = (
    DataTypeHandler(
        FormatId.CSV,
        (".csv",),
        detect_csv,
        document_profile(DataSubtype.CSV),
        ExtractionBinding("TOOL-011", ExtractCsvInput, extract_csv),
    ),
    DataTypeHandler(
        FormatId.TEXT,
        (".txt", ".asc"),
        no_signature,
        document_profile(DataSubtype.TEXT),
        interpretation_binding(InterpretationSubtype.TEXT),
    ),
    DataTypeHandler(
        FormatId.PDF,
        (".pdf",),
        detect_pdf,
        document_profile(DataSubtype.PDF),
        interpretation_binding(InterpretationSubtype.PDF),
    ),
    DataTypeHandler(
        FormatId.OSDU_MANIFEST,
        (),
        json_detector(
            FormatId.OSDU_MANIFEST,
            "FORMAT-1.JSON-MANIFEST",
            "A bounded OSDU manifest envelope was observed.",
            "kind/Data",
        ),
        document_profile(
            DataSubtype.OSDU_MANIFEST, DataCategory.MANIFEST, "osdu:wks:Manifest:1.0.0"
        ),
    ),
    DataTypeHandler(
        FormatId.OSDU_SCHEMA,
        (),
        json_detector(
            FormatId.OSDU_SCHEMA,
            "FORMAT-1.JSON-SCHEMA",
            "A bounded JSON Schema document was observed.",
            "$schema",
        ),
        document_profile(DataSubtype.OSDU_SCHEMA, DataCategory.SCHEMA, "osdu:wks:Schema:1.0.0"),
    ),
)
