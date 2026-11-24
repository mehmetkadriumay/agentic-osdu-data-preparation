"""Versioned bounded-sample detection and parser-free classification services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import uuid5

from agentic_osdu.data_types import (
    BUILTIN_DATA_TYPES,
    DataTypeRegistry,
    DetectionContext,
    FormatMatch,
)
from agentic_osdu.domain.models import (
    ClassificationRecord,
    EvidenceRecord,
    FileAssetRef,
    FormatCandidate,
    FormatDetectionResult,
    FormatId,
    OSDUKind,
    TrustLevel,
)
from agentic_osdu.tools.contracts import ClassifyDataInput, ClassifyDataOutput, DetectFormatOutput

DETECTOR_VERSION = "1.0.0"
CLASSIFIER_VERSION = "1.0.0"


class DetectionError(RuntimeError):
    """Stable detection or classification failure."""

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
    *,
    data_types: DataTypeRegistry = BUILTIN_DATA_TYPES,
) -> DetectionOutcome:
    """Rank registered formats without giving detectors filesystem access."""

    if not samples or not any(samples):
        raise DetectionError("INSUFFICIENT_SAMPLE", "At least one non-empty sample is required.")
    sample = b"".join(samples)
    context = DetectionContext(
        sample=sample,
        suffix=PurePosixPath(file.relative_path.root).suffix.casefold(),
        upper=sample[:131072].upper(),
        stripped=sample.lstrip(b"\xef\xbb\xbf \t\r\n"),
    )
    matches: dict[FormatId, tuple[float, list[EvidenceRecord]]] = {}
    for handler in data_types.handlers:
        rules = handler.detect(context)
        if context.suffix in handler.extensions:
            rules = (
                FormatMatch(
                    0.75,
                    f"FORMAT-1.EXT-{handler.format_id.value}",
                    "A supported normalized extension was observed.",
                    context.suffix,
                ),
                *rules,
            )
        for rule in rules:
            evidence = EvidenceRecord(
                evidence_id=uuid5(
                    file.file_id, f"{DETECTOR_VERSION}:{rule.rule_id}:{rule.observed}"
                ),
                evidence_type="format_rule",
                rule_id=rule.rule_id,
                summary=rule.summary,
                observed_value=rule.observed,
                trust_level=TrustLevel.DERIVED,
                source_file_id=file.file_id,
            )
            current = matches.setdefault(handler.format_id, (rule.confidence, []))
            current[1].append(evidence)
            matches[handler.format_id] = (max(current[0], rule.confidence), current[1])
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
    key = ",".join(
        f"{candidate.format_id.value}:{candidate.confidence:.3f}" for candidate in candidates
    )
    detection = FormatDetectionResult(
        detection_id=uuid5(file.file_id, f"{DETECTOR_VERSION}:{key}"),
        file_id=file.file_id,
        candidates=candidates,
        detector_version=DETECTOR_VERSION,
    )
    return DetectionOutcome(
        output=DetectFormatOutput(detection=detection),
        evidence=tuple(item for _, (_, records) in ranked for item in records),
    )


def classify_data(
    request: ClassifyDataInput,
    file: FileAssetRef,
    *,
    data_types: DataTypeRegistry = BUILTIN_DATA_TYPES,
) -> ClassificationOutcome:
    """Normalize classification using the same registry as detection and extraction."""

    if request.file_id != file.file_id or request.detection.file_id != file.file_id:
        raise DetectionError("METADATA_CONFLICT", "File and detection identifiers do not match.")
    if not request.detection.candidates:
        raise DetectionError(
            "CLASSIFICATION_INCONCLUSIVE", "No detected format is available to classify."
        )
    candidate = request.detection.candidates[0]
    handler = data_types.get(candidate.format_id)
    if handler is None:
        raise DetectionError("UNSUPPORTED_FORMAT", "No classification handler is registered.")
    profile = handler.classify(file.relative_path.root.upper())
    rule_id = f"CLASSIFY-1.{candidate.format_id.value}"
    evidence_id = uuid5(file.file_id, f"{CLASSIFIER_VERSION}:{rule_id}:{profile.subtype.value}")
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
    record = ClassificationRecord(
        classification_id=uuid5(
            file.file_id,
            (
                f"{CLASSIFIER_VERSION}:{request.detection.detection_id}:"
                f"{profile.category.value}:{profile.subtype.value}:"
                f"{profile.stack.value}:{profile.domain.value}:"
                f"{profile.processing.value}:{profile.survey.value}:{profile.well.value}"
            ),
        ),
        file_id=file.file_id,
        format_id=candidate.format_id,
        category=profile.category,
        subtype=profile.subtype,
        dimensions=profile.dimensions,
        stack=profile.stack,
        domain=profile.domain,
        processing=profile.processing,
        survey=profile.survey,
        well=profile.well,
        osdu_kind=OSDUKind(profile.osdu_kind) if profile.osdu_kind else None,
        confidence=candidate.confidence,
        detection_id=request.detection.detection_id,
        extraction_ids=tuple(
            metadata.reference.extraction_id for metadata in request.extracted_metadata
        ),
        evidence_ids=(*candidate.evidence_ids, evidence_id),
        trust_level=TrustLevel.DERIVED,
    )
    return ClassificationOutcome(
        output=ClassifyDataOutput(classification=record),
        evidence=(evidence,),
    )
