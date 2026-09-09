"""Versioned bounded-sample format detection and parser-free classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID, uuid5

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    EvidenceRecord,
    FileAssetRef,
    FormatCandidate,
    FormatDetectionResult,
    FormatId,
    OSDUKind,
    ProcessingLevel,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
)
from agentic_osdu.tools.contracts import (
    ClassifyDataInput,
    ClassifyDataOutput,
    DetectFormatOutput,
)

DETECTOR_VERSION = "1.0.0"
CLASSIFIER_VERSION = "1.0.0"

_EXTENSIONS = {
    ".sgy": FormatId.SEGY,
    ".segy": FormatId.SEGY,
    ".las": FormatId.LAS,
    ".dlis": FormatId.DLIS,
    ".lis": FormatId.LIS_LTI,
    ".lti": FormatId.LIS_LTI,
    ".csv": FormatId.CSV,
    ".p190": FormatId.P190,
    ".sgp": FormatId.SGP,
    ".dat": FormatId.DAT,
    ".txt": FormatId.TEXT,
    ".asc": FormatId.TEXT,
    ".pdf": FormatId.PDF,
}

_OSDU_KINDS = {
    DataCategory.SEISMIC: "osdu:wks:work-product-component--SeismicTraceData:1.0.0",
    DataCategory.WELL_LOG: "osdu:wks:work-product-component--WellLog:1.0.0",
    DataCategory.NAVIGATION: "osdu:wks:work-product-component--SeismicLineGeometry:1.0.0",
    DataCategory.GRID: "osdu:wks:work-product-component--SeismicBinGrid:1.0.0",
    DataCategory.INTERPRETATION: "osdu:wks:work-product-component--SeismicHorizon:1.0.0",
    DataCategory.SUPPORTING_DOCUMENT: "osdu:wks:work-product-component--Document:1.0.0",
    DataCategory.MANIFEST: "osdu:wks:Manifest:1.0.0",
    DataCategory.SCHEMA: "osdu:wks:Schema:1.0.0",
}


class DetectionError(RuntimeError):
    """Stable TOOL-004/005 failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DetectionOutcome:
    output: DetectFormatOutput
    evidence: tuple[EvidenceRecord, ...]


@dataclass(frozen=True, slots=True)
class ClassificationOutcome:
    output: ClassifyDataOutput
    evidence: tuple[EvidenceRecord, ...]


def detect_format(
    file: FileAssetRef,
    samples: tuple[bytes, ...],
) -> DetectionOutcome:
    """Rank formats from a workspace-relative name and already-bounded samples."""

    if not samples or not any(samples):
        raise DetectionError("INSUFFICIENT_SAMPLE", "At least one non-empty sample is required.")
    sample = b"".join(samples)
    suffix = PurePosixPath(file.relative_path.root).suffix.casefold()
    matches: dict[FormatId, tuple[float, list[EvidenceRecord]]] = {}

    extension_format = _EXTENSIONS.get(suffix)
    if extension_format is not None:
        _record_match(
            matches,
            file.file_id,
            extension_format,
            0.75,
            f"FORMAT-1.EXT-{extension_format.value}",
            "A supported normalized extension was observed.",
            suffix,
        )

    stripped = sample.lstrip(b"\xef\xbb\xbf \t\r\n")
    upper = sample[:131072].upper()
    if sample.startswith(b"%PDF-"):
        _record_match(
            matches,
            file.file_id,
            FormatId.PDF,
            1.0,
            "FORMAT-1.SIG-PDF",
            "PDF magic bytes.",
            "%PDF-",
        )
    if len(sample) >= 3200 and (
        sample[:2] in {b"C ", b"\xc3@", b"\xc3\xf1"} or suffix in {".sgy", ".segy"}
    ):
        _record_match(
            matches,
            file.file_id,
            FormatId.SEGY,
            0.98,
            "FORMAT-1.SIG-SEGY",
            "A bounded SEG-Y textual header was observed.",
            "3200-byte textual header",
        )
    if b"~VERSION" in upper or (b"~V" in upper and b"~C" in upper):
        _record_match(
            matches,
            file.file_id,
            FormatId.LAS,
            0.98,
            "FORMAT-1.SIG-LAS",
            "LAS section markers were observed.",
            "~Version/~Curve",
        )
    if upper.startswith(b"V1.") and b"RECORD" in upper[:128]:
        _record_match(
            matches,
            file.file_id,
            FormatId.DLIS,
            0.95,
            "FORMAT-1.SIG-DLIS",
            "A bounded RP66 version record was observed.",
            "RP66 V1",
        )
    if re.search(rb"(?m)^H\d{4}", upper) and re.search(rb"(?m)^[SR]", upper):
        _record_match(
            matches,
            file.file_id,
            FormatId.P190,
            0.95,
            "FORMAT-1.SIG-P190",
            "P1/90 fixed-width records were observed.",
            "H/S records",
        )
    if re.search(rb"(?m)^H\s+\d+\s+\d+\s+[-+]?\d", upper):
        _record_match(
            matches,
            file.file_id,
            FormatId.SGP,
            0.95,
            "FORMAT-1.SIG-SGP",
            "SGP grid header coordinates were observed.",
            "H inline crossline X Y",
        )
    if suffix == ".csv" and (b"," in sample.splitlines()[0] or b";" in sample.splitlines()[0]):
        _record_match(
            matches,
            file.file_id,
            FormatId.CSV,
            0.9,
            "FORMAT-1.SIG-CSV",
            "A delimited header row was observed.",
            "delimited row",
        )
    if suffix == ".dat" and any(token in upper for token in (b"INLINE", b"CROSSLINE", b"FAULT")):
        _record_match(
            matches,
            file.file_id,
            FormatId.DAT,
            0.9,
            "FORMAT-1.SIG-DAT",
            "Interpretation field names were observed.",
            "interpretation fields",
        )
    _detect_json(matches, file.file_id, stripped)

    if not matches:
        raise DetectionError(
            "UNSUPPORTED_FORMAT", "No supported extension or bounded signature was detected."
        )
    ranked = sorted(matches.items(), key=lambda item: (-item[1][0], item[0].value))
    candidates = tuple(
        FormatCandidate(
            format_id=format_id,
            confidence=confidence,
            evidence_ids=tuple(item.evidence_id for item in evidence),
        )
        for format_id, (confidence, evidence) in ranked
    )
    evidence = tuple(item for _, (_, records) in ranked for item in records)
    key = ",".join(
        f"{candidate.format_id.value}:{candidate.confidence:.3f}" for candidate in candidates
    )
    detection = FormatDetectionResult(
        detection_id=uuid5(file.file_id, f"{DETECTOR_VERSION}:{key}"),
        file_id=file.file_id,
        candidates=candidates,
        detector_version=DETECTOR_VERSION,
    )
    return DetectionOutcome(output=DetectFormatOutput(detection=detection), evidence=evidence)


def classify_data(
    request: ClassifyDataInput,
    file: FileAssetRef,
) -> ClassificationOutcome:
    """Normalize classification without opening the source file."""

    if request.file_id != file.file_id or request.detection.file_id != file.file_id:
        raise DetectionError("METADATA_CONFLICT", "File and detection identifiers do not match.")
    if not request.detection.candidates:
        raise DetectionError(
            "CLASSIFICATION_INCONCLUSIVE", "No detected format is available to classify."
        )
    candidate = request.detection.candidates[0]
    name = file.relative_path.root.upper()
    values = _classification_values(candidate.format_id, name)
    category, subtype, stack, domain, processing, survey, well, dimensions = values
    osdu_kind_value = _OSDU_KINDS.get(category)
    if category is DataCategory.INTERPRETATION and subtype is DataSubtype.FAULT:
        osdu_kind_value = "osdu:wks:work-product-component--FaultSystem:1.0.0"
    rule_id = f"CLASSIFY-1.{candidate.format_id.value}"
    evidence_id = uuid5(file.file_id, f"{CLASSIFIER_VERSION}:{rule_id}:{subtype.value}")
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        evidence_type="classification_rule",
        rule_id=rule_id,
        summary="Normalized format and controlled filename rules were applied.",
        location=file.relative_path.root,
        observed_value=candidate.format_id.value,
        trust_level=TrustLevel.DERIVED,
        source_file_id=file.file_id,
    )
    extraction_ids = tuple(
        metadata.reference.extraction_id for metadata in request.extracted_metadata
    )
    record = ClassificationRecord(
        classification_id=uuid5(
            file.file_id,
            (
                f"{CLASSIFIER_VERSION}:{request.detection.detection_id}:"
                f"{category.value}:{subtype.value}:{stack.value}:{domain.value}:"
                f"{processing.value}:{survey.value}:{well.value}"
            ),
        ),
        file_id=file.file_id,
        format_id=candidate.format_id,
        category=category,
        subtype=subtype,
        dimensions=dimensions,
        stack=stack,
        domain=domain,
        processing=processing,
        survey=survey,
        well=well,
        osdu_kind=OSDUKind(osdu_kind_value) if osdu_kind_value else None,
        confidence=candidate.confidence,
        detection_id=request.detection.detection_id,
        extraction_ids=extraction_ids,
        evidence_ids=(*candidate.evidence_ids, evidence_id),
        trust_level=TrustLevel.DERIVED,
    )
    return ClassificationOutcome(
        output=ClassifyDataOutput(classification=record),
        evidence=(evidence,),
    )


def _detect_json(
    matches: dict[FormatId, tuple[float, list[EvidenceRecord]]],
    file_id: UUID,
    sample: bytes,
) -> None:
    if not sample.startswith((b"{", b"[")):
        return
    try:
        document = json.loads(sample)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if (
        isinstance(document, dict)
        and "$schema" in document
        and ("type" in document or "properties" in document)
    ):
        _record_match(
            matches,
            file_id,
            FormatId.OSDU_SCHEMA,
            0.99,
            "FORMAT-1.JSON-SCHEMA",
            "A bounded JSON Schema document was observed.",
            "$schema",
        )
    elif isinstance(document, dict) and (
        str(document.get("kind", "")).casefold().startswith("osdu:wks:manifest:")
        or "Data" in document
    ):
        _record_match(
            matches,
            file_id,
            FormatId.OSDU_MANIFEST,
            0.99,
            "FORMAT-1.JSON-MANIFEST",
            "A bounded OSDU manifest envelope was observed.",
            "kind/Data",
        )
    elif (
        isinstance(document, list)
        and document
        and isinstance(document[0], dict)
        and isinstance(document[0].get("curves"), list)
    ):
        _record_match(
            matches,
            file_id,
            FormatId.JSON_WELL_LOG,
            0.99,
            "FORMAT-1.JSON-WELL-LOG",
            "A bounded JSON Well Log curve envelope was observed.",
            "root array/curves",
        )


def _record_match(
    matches: dict[FormatId, tuple[float, list[EvidenceRecord]]],
    file_id: UUID,
    format_id: FormatId,
    confidence: float,
    rule_id: str,
    summary: str,
    observed: str,
) -> None:
    evidence = EvidenceRecord(
        evidence_id=uuid5(file_id, f"{DETECTOR_VERSION}:{rule_id}:{observed}"),
        evidence_type="format_rule",
        rule_id=rule_id,
        summary=summary,
        observed_value=observed,
        trust_level=TrustLevel.DERIVED,
        source_file_id=file_id,
    )
    current = matches.get(format_id)
    if current is None:
        matches[format_id] = (confidence, [evidence])
        return
    current[1].append(evidence)
    matches[format_id] = (max(current[0], confidence), current[1])


def _classification_values(
    format_id: FormatId,
    name: str,
) -> tuple[
    DataCategory,
    DataSubtype,
    StackType,
    DataDomain,
    ProcessingLevel,
    SurveyType,
    WellDataType,
    ClassificationDimensions,
]:
    none = ClassificationDimensions()
    if format_id is FormatId.SEGY:
        stack = (
            StackType.PRE_STACK
            if any(token in name for token in ("PRESTACK", "PRE-STACK", "PRSDM", "GATHER"))
            else StackType.POST_STACK
            if any(token in name for token in ("POST", "STACK", "MIG_FIN"))
            else StackType.UNKNOWN
        )
        domain = DataDomain.DEPTH if "DEPTH" in name else DataDomain.TIME
        processing = (
            ProcessingLevel.PROCESSED
            if any(token in name for token in ("MIG", "PSDM", "PSTM", "PRSDM"))
            else ProcessingLevel.RAW
        )
        survey = SurveyType.THREE_D if "3D" in name else SurveyType.TWO_D
        return (
            DataCategory.SEISMIC,
            DataSubtype.SEGY,
            stack,
            domain,
            processing,
            survey,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    well_formats = {
        FormatId.LAS: DataSubtype.LAS,
        FormatId.JSON_WELL_LOG: DataSubtype.JSON_WELL_LOG,
        FormatId.DLIS: DataSubtype.DLIS,
        FormatId.LIS_LTI: DataSubtype.LIS_LTI,
    }
    if format_id in well_formats:
        return (
            DataCategory.WELL_LOG,
            well_formats[format_id],
            StackType.NOT_APPLICABLE,
            DataDomain.UNKNOWN,
            ProcessingLevel.RAW,
            SurveyType.NOT_APPLICABLE,
            WellDataType.LOG,
            none,
        )
    if format_id is FormatId.P190:
        return (
            DataCategory.NAVIGATION,
            DataSubtype.P190,
            StackType.NOT_APPLICABLE,
            DataDomain.NOT_APPLICABLE,
            ProcessingLevel.RAW,
            SurveyType.TWO_D,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    if format_id is FormatId.SGP:
        return (
            DataCategory.GRID,
            DataSubtype.SGP,
            StackType.POST_STACK if "POST" in name else StackType.UNKNOWN,
            DataDomain.DEPTH if "DEPTH" in name else DataDomain.TIME,
            ProcessingLevel.PROCESSED if "MIG" in name else ProcessingLevel.RAW,
            SurveyType.THREE_D,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    if format_id is FormatId.DAT:
        subtype = DataSubtype.FAULT if "FAULT" in name else DataSubtype.HORIZON
        return (
            DataCategory.INTERPRETATION,
            subtype,
            StackType.NOT_APPLICABLE,
            DataDomain.DEPTH if "DEPTH" in name else DataDomain.UNKNOWN,
            ProcessingLevel.DERIVED,
            SurveyType.THREE_D,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    if format_id is FormatId.OSDU_MANIFEST:
        return (
            DataCategory.MANIFEST,
            DataSubtype.OSDU_MANIFEST,
            StackType.NOT_APPLICABLE,
            DataDomain.NOT_APPLICABLE,
            ProcessingLevel.NOT_APPLICABLE,
            SurveyType.NOT_APPLICABLE,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    if format_id is FormatId.OSDU_SCHEMA:
        return (
            DataCategory.SCHEMA,
            DataSubtype.OSDU_SCHEMA,
            StackType.NOT_APPLICABLE,
            DataDomain.NOT_APPLICABLE,
            ProcessingLevel.NOT_APPLICABLE,
            SurveyType.NOT_APPLICABLE,
            WellDataType.NOT_APPLICABLE,
            none,
        )
    subtype = {
        FormatId.CSV: DataSubtype.CSV,
        FormatId.TEXT: DataSubtype.TEXT,
        FormatId.PDF: DataSubtype.PDF,
    }.get(format_id, DataSubtype.UNKNOWN)
    return (
        DataCategory.SUPPORTING_DOCUMENT,
        subtype,
        StackType.NOT_APPLICABLE,
        DataDomain.NOT_APPLICABLE,
        ProcessingLevel.NOT_APPLICABLE,
        SurveyType.NOT_APPLICABLE,
        WellDataType.NOT_APPLICABLE,
        none,
    )
