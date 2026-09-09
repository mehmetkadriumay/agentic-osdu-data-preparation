from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import (
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatId,
    ProcessingLevel,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.tools.contracts import ClassifyDataInput
from agentic_osdu.tools.detection import (
    CLASSIFIER_VERSION,
    DETECTOR_VERSION,
    DetectionError,
    classify_data,
    detect_format,
)


def asset(name: str) -> FileAssetRef:
    return FileAssetRef(
        file_id=uuid4(),
        workspace_id=uuid4(),
        relative_path=WorkspaceRelativePath(name),
        size_bytes=4096,
        modified_at=datetime.now(UTC),
        discovery_version=1,
    )


@pytest.mark.parametrize(
    ("name", "sample", "expected"),
    [
        ("cube.sgy", b"C 1 CLIENT TEST" + b" " * 3585, FormatId.SEGY),
        ("well.las", b"~Version\nVERS. 2.0\n~Curve\nDEPT.M", FormatId.LAS),
        (
            "well.json",
            b'[{"header":{"name":"LOG"},"curves":[{"mnemonic":"DEPT","unit":"m"}]}]',
            FormatId.JSON_WELL_LOG,
        ),
        ("records.dlis", b"V1.00 RECORD", FormatId.DLIS),
        ("records.lti", b"REEL HEADER", FormatId.LIS_LTI),
        ("table.csv", b"MD,AZIMUTH,INCLINATION\n1,2,3", FormatId.CSV),
        ("line.p190", b"H0100 TEST\nS1234", FormatId.P190),
        ("grid.sgp", b"H 1 1 100.0 200.0\n", FormatId.SGP),
        ("horizon.dat", b"INLINE CROSSLINE X Y Z", FormatId.DAT),
        ("notes.txt", b"plain text", FormatId.TEXT),
        ("report.pdf", b"%PDF-1.7\n", FormatId.PDF),
        (
            "manifest.json",
            b'{"kind":"osdu:wks:Manifest:1.0.0","Data":{"Datasets":[]}}',
            FormatId.OSDU_MANIFEST,
        ),
        (
            "schema.json",
            b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}',
            FormatId.OSDU_SCHEMA,
        ),
    ],
)
def test_supported_formats_use_versioned_extension_and_signature_evidence(
    name: str, sample: bytes, expected: FormatId
) -> None:
    outcome = detect_format(asset(name), (sample,))

    assert outcome.output.detection.detector_version == DETECTOR_VERSION
    assert outcome.output.detection.candidates[0].format_id is expected
    assert outcome.output.detection.candidates[0].evidence_ids
    assert all(evidence.rule_id.startswith("FORMAT-1.") for evidence in outcome.evidence)


def test_signature_can_override_misleading_extension_and_unknown_is_explicit() -> None:
    outcome = detect_format(asset("renamed.bin"), (b"%PDF-1.7",))
    assert outcome.output.detection.candidates[0].format_id is FormatId.PDF
    assert outcome.output.detection.candidates[0].confidence == 1.0

    with pytest.raises(DetectionError, match="UNSUPPORTED_FORMAT"):
        detect_format(asset("unknown.bin"), (b"\x00\x01\x02",))


@pytest.mark.parametrize(
    (
        "name",
        "sample",
        "category",
        "subtype",
        "stack",
        "domain",
        "processing",
        "survey",
        "well",
    ),
    [
        (
            "VOLVE_3D_PRESTACK_DEPTH_PSDM.sgy",
            b"C 1 TEST" + b" " * 3592,
            DataCategory.SEISMIC,
            DataSubtype.SEGY,
            StackType.PRE_STACK,
            DataDomain.DEPTH,
            ProcessingLevel.PROCESSED,
            SurveyType.THREE_D,
            WellDataType.NOT_APPLICABLE,
        ),
        (
            "well.las",
            b"~Version\n~Curve\nDEPT.M",
            DataCategory.WELL_LOG,
            DataSubtype.LAS,
            StackType.NOT_APPLICABLE,
            DataDomain.UNKNOWN,
            ProcessingLevel.RAW,
            SurveyType.NOT_APPLICABLE,
            WellDataType.LOG,
        ),
        (
            "line.p190",
            b"H0100 TEST",
            DataCategory.NAVIGATION,
            DataSubtype.P190,
            StackType.NOT_APPLICABLE,
            DataDomain.NOT_APPLICABLE,
            ProcessingLevel.RAW,
            SurveyType.TWO_D,
            WellDataType.NOT_APPLICABLE,
        ),
        (
            "POST_STACK_DEPTH_MIG_FIN.sgp",
            b"H 1 1 1.0 2.0",
            DataCategory.GRID,
            DataSubtype.SGP,
            StackType.POST_STACK,
            DataDomain.DEPTH,
            ProcessingLevel.PROCESSED,
            SurveyType.THREE_D,
            WellDataType.NOT_APPLICABLE,
        ),
        (
            "fault_sticks.dat",
            b"INLINE CROSSLINE X Y Z",
            DataCategory.INTERPRETATION,
            DataSubtype.FAULT,
            StackType.NOT_APPLICABLE,
            DataDomain.UNKNOWN,
            ProcessingLevel.DERIVED,
            SurveyType.THREE_D,
            WellDataType.NOT_APPLICABLE,
        ),
        (
            "report.pdf",
            b"%PDF-1.7",
            DataCategory.SUPPORTING_DOCUMENT,
            DataSubtype.PDF,
            StackType.NOT_APPLICABLE,
            DataDomain.NOT_APPLICABLE,
            ProcessingLevel.NOT_APPLICABLE,
            SurveyType.NOT_APPLICABLE,
            WellDataType.NOT_APPLICABLE,
        ),
    ],
)
def test_classification_is_normalized_and_parser_free(
    name: str,
    sample: bytes,
    category: DataCategory,
    subtype: DataSubtype,
    stack: StackType,
    domain: DataDomain,
    processing: ProcessingLevel,
    survey: SurveyType,
    well: WellDataType,
) -> None:
    file = asset(name)
    detection = detect_format(file, (sample,))
    outcome = classify_data(
        ClassifyDataInput(file_id=file.file_id, detection=detection.output.detection),
        file,
    )
    record = outcome.output.classification

    assert (
        record.category,
        record.subtype,
        record.stack,
        record.domain,
        record.processing,
        record.survey,
        record.well,
    ) == (category, subtype, stack, domain, processing, survey, well)
    assert record.osdu_kind is not None
    assert record.osdu_kind.root.startswith("osdu:wks:")
    assert record.confidence > 0
    assert record.trust_level in {TrustLevel.DERIVED, TrustLevel.HEURISTIC}
    assert CLASSIFIER_VERSION == "1.0.0"
