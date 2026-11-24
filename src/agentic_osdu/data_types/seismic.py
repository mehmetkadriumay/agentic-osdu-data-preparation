"""SEG-Y and P1/90 navigation data-type handlers."""

from __future__ import annotations

import re

from agentic_osdu.data_types.models import (
    ClassificationProfile,
    DataTypeHandler,
    DetectionContext,
    ExtractionBinding,
    FormatMatch,
)
from agentic_osdu.domain.models import (
    DataCategory,
    DataDomain,
    DataSubtype,
    FormatId,
    ProcessingLevel,
    StackType,
    SurveyType,
)
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.tools.contracts import ExtractP190Input, ExtractSegyInput


def detect_segy(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if len(context.sample) >= 3200 and (
        context.sample[:2] in {b"C ", b"\xc3@", b"\xc3\xf1"} or context.suffix in {".sgy", ".segy"}
    ):
        return (
            FormatMatch(
                0.98,
                "FORMAT-1.SIG-SEGY",
                "A bounded SEG-Y textual header was observed.",
                "3200-byte textual header",
            ),
        )
    return ()


def classify_segy(name: str) -> ClassificationProfile:
    return ClassificationProfile(
        category=DataCategory.SEISMIC,
        subtype=DataSubtype.SEGY,
        osdu_kind="osdu:wks:work-product-component--SeismicTraceData:1.0.0",
        stack=(
            StackType.PRE_STACK
            if any(token in name for token in ("PRESTACK", "PRE-STACK", "PRSDM", "GATHER"))
            else StackType.POST_STACK
            if any(token in name for token in ("POST", "STACK", "MIG_FIN"))
            else StackType.UNKNOWN
        ),
        domain=DataDomain.DEPTH if "DEPTH" in name else DataDomain.TIME,
        processing=(
            ProcessingLevel.PROCESSED
            if any(token in name for token in ("MIG", "PSDM", "PSTM", "PRSDM"))
            else ProcessingLevel.RAW
        ),
        survey=SurveyType.THREE_D if "3D" in name else SurveyType.TWO_D,
    )


def detect_p190(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if re.search(rb"(?m)^H\d{4}", context.upper) and re.search(rb"(?m)^[SR]", context.upper):
        return (
            FormatMatch(
                0.95,
                "FORMAT-1.SIG-P190",
                "P1/90 fixed-width records were observed.",
                "H/S records",
            ),
        )
    return ()


def classify_p190(name: str) -> ClassificationProfile:
    return ClassificationProfile(
        category=DataCategory.NAVIGATION,
        subtype=DataSubtype.P190,
        osdu_kind="osdu:wks:work-product-component--SeismicLineGeometry:1.0.0",
        processing=ProcessingLevel.RAW,
        survey=SurveyType.TWO_D,
    )


HANDLERS = (
    DataTypeHandler(
        FormatId.SEGY,
        (".sgy", ".segy"),
        detect_segy,
        classify_segy,
        ExtractionBinding("TOOL-006", ExtractSegyInput, extract_segy),
    ),
    DataTypeHandler(
        FormatId.P190,
        (".p190",),
        detect_p190,
        classify_p190,
        ExtractionBinding("TOOL-012", ExtractP190Input, extract_p190),
    ),
)
