"""Well-log signatures, classification, and typed extraction bindings."""

from __future__ import annotations

from collections.abc import Callable

from agentic_osdu.data_types.models import (
    ClassificationProfile,
    DataTypeHandler,
    DetectionContext,
    ExtractionBinding,
    FormatMatch,
    no_signature,
)
from agentic_osdu.domain.models import (
    DataCategory,
    DataDomain,
    DataSubtype,
    FormatId,
    ProcessingLevel,
    WellDataType,
)
from agentic_osdu.formats.well_logs import (
    extract_dlis,
    extract_json_well_log,
    extract_las,
    extract_lis,
)
from agentic_osdu.tools.contracts import (
    ExtractDlisInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractLisInput,
)


def detect_las(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if b"~VERSION" in context.upper or (b"~V" in context.upper and b"~C" in context.upper):
        return (
            FormatMatch(
                0.98,
                "FORMAT-1.SIG-LAS",
                "LAS section markers were observed.",
                "~Version/~Curve",
            ),
        )
    return ()


def detect_dlis(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if context.upper.startswith(b"V1.") and b"RECORD" in context.upper[:128]:
        return (
            FormatMatch(
                0.95,
                "FORMAT-1.SIG-DLIS",
                "A bounded RP66 version record was observed.",
                "RP66 V1",
            ),
        )
    return ()


def detect_json_well_log(context: DetectionContext) -> tuple[FormatMatch, ...]:
    document = context.json_document
    if (
        isinstance(document, list)
        and document
        and isinstance(document[0], dict)
        and isinstance(document[0].get("curves"), list)
    ):
        return (
            FormatMatch(
                0.99,
                "FORMAT-1.JSON-WELL-LOG",
                "A bounded JSON Well Log curve envelope was observed.",
                "root array/curves",
            ),
        )
    return ()


def well_log_profile(subtype: DataSubtype) -> Callable[[str], ClassificationProfile]:
    def classify(name: str) -> ClassificationProfile:
        return ClassificationProfile(
            category=DataCategory.WELL_LOG,
            subtype=subtype,
            osdu_kind="osdu:wks:work-product-component--WellLog:1.0.0",
            domain=DataDomain.UNKNOWN,
            processing=ProcessingLevel.RAW,
            well=WellDataType.LOG,
        )

    return classify


HANDLERS = (
    DataTypeHandler(
        FormatId.LAS,
        (".las",),
        detect_las,
        well_log_profile(DataSubtype.LAS),
        ExtractionBinding("TOOL-007", ExtractLasInput, extract_las),
    ),
    DataTypeHandler(
        FormatId.DLIS,
        (".dlis",),
        detect_dlis,
        well_log_profile(DataSubtype.DLIS),
        ExtractionBinding("TOOL-009", ExtractDlisInput, extract_dlis),
    ),
    DataTypeHandler(
        FormatId.LIS_LTI,
        (".lis", ".lti"),
        no_signature,
        well_log_profile(DataSubtype.LIS_LTI),
        ExtractionBinding("TOOL-010", ExtractLisInput, extract_lis),
    ),
    DataTypeHandler(
        FormatId.JSON_WELL_LOG,
        (),
        detect_json_well_log,
        well_log_profile(DataSubtype.JSON_WELL_LOG),
        ExtractionBinding("TOOL-008", ExtractJsonWellLogInput, extract_json_well_log),
    ),
)
