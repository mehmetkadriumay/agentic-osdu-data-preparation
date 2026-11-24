"""Grid and interpretation handlers sharing the typed subtype extraction tool."""

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
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.tools.contracts import ExtractInterpretationInput, InterpretationSubtype


def interpretation_binding(subtype: InterpretationSubtype) -> ExtractionBinding:
    return ExtractionBinding(
        "TOOL-013",
        ExtractInterpretationInput,
        extract_interpretation,
        (("subtype", subtype),),
    )


def detect_sgp(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if re.search(rb"(?m)^H\s+\d+\s+\d+\s+[-+]?\d", context.upper):
        return (
            FormatMatch(
                0.95,
                "FORMAT-1.SIG-SGP",
                "SGP grid header coordinates were observed.",
                "H inline crossline X Y",
            ),
        )
    return ()


def classify_sgp(name: str) -> ClassificationProfile:
    return ClassificationProfile(
        category=DataCategory.GRID,
        subtype=DataSubtype.SGP,
        osdu_kind="osdu:wks:work-product-component--SeismicBinGrid:1.0.0",
        stack=StackType.POST_STACK if "POST" in name else StackType.UNKNOWN,
        domain=DataDomain.DEPTH if "DEPTH" in name else DataDomain.TIME,
        processing=ProcessingLevel.PROCESSED if "MIG" in name else ProcessingLevel.RAW,
        survey=SurveyType.THREE_D,
    )


def detect_dat(context: DetectionContext) -> tuple[FormatMatch, ...]:
    if context.suffix == ".dat" and any(
        token in context.upper for token in (b"INLINE", b"CROSSLINE", b"FAULT")
    ):
        return (
            FormatMatch(
                0.9,
                "FORMAT-1.SIG-DAT",
                "Interpretation field names were observed.",
                "interpretation fields",
            ),
        )
    return ()


def classify_dat(name: str) -> ClassificationProfile:
    fault = "FAULT" in name
    return ClassificationProfile(
        category=DataCategory.INTERPRETATION,
        subtype=DataSubtype.FAULT if fault else DataSubtype.HORIZON,
        osdu_kind=(
            "osdu:wks:work-product-component--FaultSystem:1.0.0"
            if fault
            else "osdu:wks:work-product-component--SeismicHorizon:1.0.0"
        ),
        domain=DataDomain.DEPTH if "DEPTH" in name else DataDomain.UNKNOWN,
        processing=ProcessingLevel.DERIVED,
        survey=SurveyType.THREE_D,
    )


HANDLERS = (
    DataTypeHandler(
        FormatId.SGP,
        (".sgp",),
        detect_sgp,
        classify_sgp,
        interpretation_binding(InterpretationSubtype.SGP),
    ),
    DataTypeHandler(
        FormatId.DAT,
        (".dat",),
        detect_dat,
        classify_dat,
        interpretation_binding(InterpretationSubtype.DAT),
    ),
)
