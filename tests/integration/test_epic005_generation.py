from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    DataCategory,
    DataDomain,
    FileAssetRef,
    FormatId,
    GenerationStatus,
    ManifestJsonDocument,
    MetadataExtractionRef,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.generate import (
    GENERATION_POLICY_V1,
    GenerationError,
    GenerationItem,
    GenerationService,
)
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.tools.contracts import (
    DatMetadata,
    DlisChannelMetadata,
    DlisFrameMetadata,
    DlisMetadata,
    DlisOriginMetadata,
    ExtractedMetadataContract,
    FileRecordContract,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    GenerationFilters,
    InterpretationMetadataOutput,
    InterpretationSubtype,
    JsonCurveMetadata,
    JsonLogSetMetadata,
    JsonWellLogMetadata,
    LasCurveMetadata,
    LasMetadata,
    LearningModelContract,
    LisMetadata,
    NavigationPosition,
    P190LineSummary,
    P190Metadata,
    SegyBinaryHeader,
    SegyMetadata,
    SegySurveyMetadata,
)


def _model() -> LearningModelContract:
    prototype: dict[str, Any] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {
                "id": "surrogate-key:wp-1",
                "kind": "osdu:wks:work-product--WorkProduct:1.0.0",
                "data": {"Name": "source.sgy", "Components": ["surrogate-key:wpc-1"]},
            },
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "kind": "osdu:wks:work-product-component--SeismicTraceData:1.0.0",
                    "data": {
                        "Name": "source.sgy",
                        "SpatialArea": {
                            "AsIngestedCoordinates": {
                                "type": "AnyCrsFeatureCollection",
                                "features": [{"geometry": {"coordinates": []}}],
                            }
                        },
                        "Datasets": ["surrogate-key:file-1"],
                    },
                }
            ],
            "Datasets": [
                {
                    "id": "surrogate-key:file-1",
                    "kind": "osdu:wks:dataset--FileCollection.SEGY:1.0.0",
                    "data": {
                        "DatasetProperties": {
                            "FileSourceInfo": {
                                "FileSource": "s3://bucket/Data/source.sgy",
                                "Name": "source.sgy",
                            }
                        }
                    },
                }
            ],
        },
    }
    encoded = json.dumps(prototype, sort_keys=True, separators=(",", ":")).encode()
    digest = __import__("hashlib").sha256(encoded).hexdigest()
    from agentic_osdu.domain.models import ManifestJsonDocument

    return LearningModelContract(
        learning_model_id=uuid4(),
        category=DataCategory.SEISMIC,
        version=1,
        model_sha256="d" * 64,
        example_ids=(),
        example_identities=(),
        prototype=ManifestJsonDocument(sha256=digest, content=prototype),
        constants=(),
        prototype_source_path=WorkspaceRelativePath("Data/source.sgy"),
        file_source_prefix="s3://bucket/",
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )


def _model_with_component_fields(fields: dict[str, Any]) -> LearningModelContract:
    model = _model()
    content: dict[str, Any] = model.prototype.model_dump(mode="python")["content"]
    content["Data"]["WorkProductComponents"][0]["data"].update(fields)
    content["Data"]["Datasets"][0]["data"].update(
        {
            "Endian": "STALE",
            "EncodingFormatTypeID": "stale",
            "SchemaFormatTypeID": "stale",
            "TotalSize": 1,
        }
    )
    digest = (
        __import__("hashlib")
        .sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    return model.model_copy(
        update={"prototype": ManifestJsonDocument(sha256=digest, content=content)}
    )


def _item(model: LearningModelContract, path: str, *, matched: bool = False) -> GenerationItem:
    file_id = uuid4()
    return GenerationItem(
        file_record=FileRecordContract(
            file=FileAssetRef(
                file_id=file_id,
                workspace_id=uuid4(),
                relative_path=WorkspaceRelativePath(path),
                size_bytes=200,
                modified_at=datetime(2026, 1, 1, tzinfo=UTC),
                sha256="a" * 64,
                discovery_version=1,
            )
        ),
        category=DataCategory.SEISMIC,
        format_id=None,
        model=model,
        matched=matched,
        metadata={
            "traceCoordinateSamples": [
                {"x": 1.0, "y": 2.0},
                {"x": 3.0, "y": 4.0},
            ]
        },
    )


def _extraction(file_id: Any, format_id: FormatId, payload: Any) -> ExtractedMetadataContract:
    return ExtractedMetadataContract(
        reference=MetadataExtractionRef(
            extraction_id=uuid4(),
            file_id=file_id,
            file_sha256="a" * 64,
            format_id=format_id,
            parser_version="1.0.0",
            extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
            trust_level=TrustLevel.VERIFIED,
        ),
        payload=payload,
    )


def _service(tmp_path: Path, items: tuple[GenerationItem, ...]) -> GenerationService:
    output = tmp_path / "generated"
    output.mkdir(parents=True)
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={"generated": str(output)},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        allowed_source_output_subpaths={"generated": "generated"},
    )
    return GenerationService(policy, items)


def test_generation_dry_run_then_atomic_no_overwrite_write(tmp_path: Path) -> None:
    model = _model()
    item = _item(model, "Data/target.sgy")
    service = _service(tmp_path, (item,))
    request = GenerateManifestInput(
        file_id=item.file_record.file.file_id,
        learning_model_id=model.learning_model_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
        dry_run=True,
    )

    dry_run = service.generate_one(request)
    assert dry_run.candidate.reference.trust_level.value == "heuristic"
    assert dry_run.candidate.reference.review_required is True
    assert dry_run.candidate.reference.generation_status is GenerationStatus.PROPOSED
    assert not list((tmp_path / "generated").rglob("*.json"))
    content: Any = dry_run.candidate.document.content
    assert content["Data"]["WorkProduct"]["data"]["Name"] == "target.sgy"
    assert (
        content["Data"]["Datasets"][0]["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]
        == "s3://bucket/Data/target.sgy"
    )
    coordinates = content["Data"]["WorkProductComponents"][0]["data"]["SpatialArea"][
        "AsIngestedCoordinates"
    ]["features"][0]["geometry"]["coordinates"]
    assert coordinates == [[1.0, 2.0], [3.0, 4.0]]

    written = service.generate_one(request.model_copy(update={"dry_run": False}))
    target = tmp_path / "generated" / written.candidate.reference.proposed_path.root
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == written.candidate.document.content
    assert not list(target.parent.glob("*.tmp"))
    with pytest.raises(GenerationError, match="OUTPUT_EXISTS"):
        service.generate_one(request.model_copy(update={"dry_run": False}))


def test_batch_filters_counts_continue_on_error_and_cancellation(tmp_path: Path) -> None:
    model = _model()
    eligible = _item(model, "Data/eligible.sgy")
    matched = _item(model, "Data/matched.sgy", matched=True)
    outside_filter = _item(model, "Other/outside.sgy")
    service = _service(tmp_path, (eligible, matched, outside_filter))
    inventory_id = service.inventory_id
    request = GenerateAllManifestsInput(
        inventory_id=inventory_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
        filters=GenerationFilters(relative_prefixes=(WorkspaceRelativePath("Data"),)),
        continue_on_error=True,
        dry_run=True,
    )
    result = service.generate_all(request)
    assert (result.generated, result.existing, result.failed, result.skipped, result.cancelled) == (
        1,
        0,
        0,
        2,
        0,
    )

    calls = 0

    def cancel_after_first() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    cancelled = service.generate_all(
        request.model_copy(update={"filters": GenerationFilters()}),
        cancellation=cancel_after_first,
    )
    assert cancelled.generated == 1
    assert cancelled.cancelled == 1
    assert cancelled.skipped == 1


def test_generation_policy_is_supported_immutable_and_changes_content_path_key(
    tmp_path: Path,
) -> None:
    model = _model()
    item = _item(model, "Data/policy.sgy")
    service = _service(tmp_path, (item,))
    request = GenerateManifestInput(
        file_id=item.file_record.file.file_id,
        learning_model_id=model.learning_model_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
    )
    output = service.generate_one(request)
    content: Any = output.candidate.document.content
    assert content["x-agentic-generation-policy"] == {
        "version": GENERATION_POLICY_V1.version,
        "sha256": GENERATION_POLICY_V1.policy_sha256,
    }
    assert GENERATION_POLICY_V1.policy_sha256[:12] in output.candidate.reference.proposed_path.root
    with pytest.raises(GenerationError, match="GENERATION_POLICY_UNAVAILABLE"):
        service.generate_one(request.model_copy(update={"generation_policy_version": "9.9.9"}))


def test_typed_metadata_overlays_replace_or_remove_stale_prototype_values(tmp_path: Path) -> None:
    model = _model_with_component_fields(
        {
            "TopMeasuredDepth": 999,
            "Curves": [{"Mnemonic": "STALE"}],
            "Precision": {"WordFormat": "stale", "WordWidth": 99},
            "ProcessingParameters": [
                {
                    "ProcessingParameterTypeID": (
                        "osdu:reference-data--ProcessingParameterType:SamplingInterval:"
                    ),
                    "ProcessingParameterValue": "stale",
                },
                {
                    "ProcessingParameterTypeID": (
                        "osdu:reference-data--ProcessingParameterType:NumCMP:"
                    ),
                    "ProcessingParameterValue": "stale",
                },
                {
                    "ProcessingParameterTypeID": (
                        "osdu:reference-data--ProcessingParameterType:MaxTime:"
                    ),
                    "ProcessingParameterValue": "stale",
                },
            ],
            "SeismicDomainTypeID": "stale",
            "TraceDomainUOM": "stale",
        }
    )
    file_record = _item(model, "Data/typed.sgy").file_record
    file_record = file_record.model_copy(
        update={
            "file": file_record.file.model_copy(update={"size_bytes": 4321}),
            "metadata_extractions": (
                _extraction(
                    file_record.file.file_id,
                    FormatId.SEGY,
                    SegyMetadata(
                        endian="little",
                        textual_header_encoding="cp500",
                        sample_interval_microseconds=2000,
                        samples_per_trace=500,
                        sampled_trace_count=2,
                        domain=DataDomain.TIME,
                        dimensions=ClassificationDimensions(trace_count=12),
                        binary_header=SegyBinaryHeader(sample_format_code=5),
                        survey_metadata=SegySurveyMetadata(),
                        trace_samples=(),
                    ),
                ),
            ),
        }
    )
    item = GenerationItem(
        file_record=file_record,
        category=DataCategory.SEISMIC,
        format_id=FormatId.SEGY,
        model=model,
    )
    content: Any = (
        _service(tmp_path, (item,))
        .generate_one(
            GenerateManifestInput(
                file_id=file_record.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        .candidate.document.content
    )
    component = content["Data"]["WorkProductComponents"][0]["data"]
    dataset = content["Data"]["Datasets"][0]["data"]
    assert (component["SampleInterval"], component["SampleCount"], component["TraceCount"]) == (
        2.0,
        500,
        12,
    )
    assert dataset["Endian"] == "LITTLE"
    assert dataset["EncodingFormatTypeID"].endswith(":SEGY:")
    assert dataset["TotalSize"] == 4321
    assert dataset["DatasetProperties"]["FileSourceInfo"]["FileSize"] == 4321
    assert component["Precision"] == {
        "WordFormat": "osdu:reference-data--WordFormatType:IEEEFLOAT:",
        "WordWidth": 4,
    }
    assert [value["ProcessingParameterValue"] for value in component["ProcessingParameters"]] == [
        "2ms",
        "12",
        "1000ms",
    ]
    assert "TopMeasuredDepth" not in component


def test_p190_dat_and_well_log_typed_overlays_are_format_specific(tmp_path: Path) -> None:
    model = _model()
    base = _item(model, "Data/navigation.p190").file_record
    p190 = P190Metadata(
        headers=(),
        line_names=("LINE-A",),
        line_summaries=(
            P190LineSummary(
                line_name="LINE-A",
                position_count=2,
                first_point_number=10,
                last_point_number=11,
                point_increment=1,
            ),
        ),
        position_count=2,
        sampled_positions=(
            NavigationPosition(
                line_name="LINE-A",
                point_number=10,
                latitude=58.0,
                longitude=2.0,
                easting=500000,
                northing=6400000,
            ),
            NavigationPosition(
                line_name="LINE-A",
                point_number=11,
                latitude=58.1,
                longitude=2.1,
                easting=500100,
                northing=6400100,
            ),
        ),
        inferred_epsg=32631,
    )
    p190_file = base.model_copy(
        update={"metadata_extractions": (_extraction(base.file.file_id, FormatId.P190, p190),)}
    )
    p190_item = GenerationItem(
        file_record=p190_file,
        category=DataCategory.NAVIGATION,
        format_id=FormatId.P190,
        model=model,
    )
    p190_content: Any = (
        _service(tmp_path / "p190", (p190_item,))
        .generate_one(
            GenerateManifestInput(
                file_id=base.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        .candidate.document.content
    )
    line = p190_content["Data"]["WorkProductComponents"][0]["data"]
    assert (line["Name"], line["FirstCMP"], line["LastCMP"]) == ("LINE-A", 10, 11)
    assert line["SpatialArea"]["Wgs84Coordinates"]["features"][0]["geometry"] == {
        "type": "LineString",
        "coordinates": [[2.0, 58.0], [2.1, 58.1]],
    }

    dat_file = _item(model, "Data/fault.dat").file_record
    dat_file = dat_file.model_copy(
        update={
            "metadata_extractions": (
                _extraction(
                    dat_file.file.file_id,
                    FormatId.DAT,
                    InterpretationMetadataOutput(
                        subtype=InterpretationSubtype.DAT,
                        dat=DatMetadata(
                            interpretation_type="fault",
                            point_count=99,
                            crs="EPSG:32631",
                        ),
                    ),
                ),
            )
        }
    )
    dat_content: Any = (
        _service(
            tmp_path / "dat",
            (
                GenerationItem(
                    file_record=dat_file,
                    category=DataCategory.INTERPRETATION,
                    format_id=FormatId.DAT,
                    model=model,
                ),
            ),
        )
        .generate_one(
            GenerateManifestInput(
                file_id=dat_file.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        .candidate.document.content
    )
    assert dat_content["Data"]["WorkProductComponents"][0]["data"]["PointCount"] == 99

    log_file = _item(model, "Data/log.json").file_record
    log_file = log_file.model_copy(
        update={
            "metadata_extractions": (
                _extraction(
                    log_file.file.file_id,
                    FormatId.JSON_WELL_LOG,
                    JsonWellLogMetadata(
                        well_name="F-1",
                        curves=(
                            JsonCurveMetadata(name="MD", unit="m", value_type="float"),
                            JsonCurveMetadata(name="GR", unit="gAPI", value_type="float"),
                        ),
                        row_count=20,
                        column_count=2,
                        index_curve="MD",
                    ),
                ),
            )
        }
    )
    log_content: Any = (
        _service(
            tmp_path / "log",
            (
                GenerationItem(
                    file_record=log_file,
                    category=DataCategory.WELL_LOG,
                    format_id=FormatId.JSON_WELL_LOG,
                    model=model,
                ),
            ),
        )
        .generate_one(
            GenerateManifestInput(
                file_id=log_file.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        .candidate.document.content
    )
    log_component = log_content["Data"]["WorkProductComponents"][0]["data"]
    assert [curve["Mnemonic"] for curve in log_component["Curves"]] == ["MD", "GR"]
    assert log_component["ReferenceCurveID"] == "MD"
    assert "TopMeasuredDepth" not in log_component


def test_json_well_log_overlay_generates_one_bound_component_per_log_set(
    tmp_path: Path,
) -> None:
    model = _model()
    base = _item(model, "Data/multiple.json").file_record
    log_sets = (
        JsonLogSetMetadata(
            name="main",
            well_name="F-1",
            curves=(
                JsonCurveMetadata(name="MD", unit="m", value_type="float"),
                JsonCurveMetadata(name="GR", unit="gAPI", value_type="float"),
            ),
            row_count=20,
            column_count=2,
            index_curve="MD",
            data_uri="file:main-data.json",
        ),
        JsonLogSetMetadata(
            name="repeat",
            well_name="F-2",
            curves=(JsonCurveMetadata(name="TIME", unit="ms", value_type="float"),),
            row_count=30,
            column_count=1,
            index_curve="TIME",
            data_uri="file:repeat-data.json",
        ),
    )
    metadata = JsonWellLogMetadata(
        well_name="F-1",
        curves=log_sets[0].curves,
        row_count=20,
        column_count=2,
        index_curve="MD",
        log_set_count=2,
        log_sets=log_sets,
    )
    file_record = base.model_copy(
        update={
            "metadata_extractions": (
                _extraction(base.file.file_id, FormatId.JSON_WELL_LOG, metadata),
            )
        }
    )
    content: Any = (
        _service(
            tmp_path,
            (
                GenerationItem(
                    file_record=file_record,
                    category=DataCategory.WELL_LOG,
                    format_id=FormatId.JSON_WELL_LOG,
                    model=model,
                ),
            ),
        )
        .generate_one(
            GenerateManifestInput(
                file_id=base.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        .candidate.document.content
    )

    components = content["Data"]["WorkProductComponents"]
    assert [component["id"] for component in components] == [
        "surrogate-key:wpc-1",
        "surrogate-key:wpc-2",
    ]
    assert content["Data"]["WorkProduct"]["data"]["Components"] == [
        "surrogate-key:wpc-1",
        "surrogate-key:wpc-2",
    ]
    assert [
        (
            component["data"]["Name"],
            [curve["Mnemonic"] for curve in component["data"]["Curves"]],
            component["data"]["RowCount"],
            component["data"]["ColumnCount"],
            component["data"]["ReferenceCurveID"],
        )
        for component in components
    ] == [
        ("main", ["MD", "GR"], 20, 2, "MD"),
        ("repeat", ["TIME"], 30, 1, "TIME"),
    ]
    assert components[0]["data"]["Datasets"] == ["surrogate-key:file-2"]
    assert components[1]["data"]["Datasets"] == ["surrogate-key:file-3"]
    datasets = content["Data"]["Datasets"]
    assert [dataset["id"] for dataset in datasets] == [
        "surrogate-key:file-1",
        "surrogate-key:file-2",
        "surrogate-key:file-3",
    ]
    assert (
        datasets[1]["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]
        == "file:main-data.json"
    )
    assert (
        datasets[2]["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]
        == "file:repeat-data.json"
    )


def test_json_well_log_mixed_inline_and_external_sets_keep_stable_distinct_datasets(
    tmp_path: Path,
) -> None:
    model = _model()
    base = _item(model, "Data/mixed.json").file_record
    external_z = JsonLogSetMetadata(
        name="external-z",
        curves=(JsonCurveMetadata(name="Z", unit=None, value_type="float"),),
        row_count=1,
        column_count=1,
        data_uri="file:z-data.json",
    )
    inline = JsonLogSetMetadata(
        name="inline",
        curves=(JsonCurveMetadata(name="I", unit=None, value_type="float"),),
        row_count=1,
        column_count=1,
    )
    external_a = JsonLogSetMetadata(
        name="external-a",
        curves=(JsonCurveMetadata(name="A", unit=None, value_type="float"),),
        row_count=1,
        column_count=1,
        data_uri="file:a-data.json",
    )

    def generate(log_sets: tuple[JsonLogSetMetadata, ...], root: Path) -> dict[str, Any]:
        metadata = JsonWellLogMetadata(
            curves=log_sets[0].curves,
            row_count=1,
            column_count=1,
            log_set_count=len(log_sets),
            log_sets=log_sets,
        )
        file_record = base.model_copy(
            update={
                "metadata_extractions": (
                    _extraction(base.file.file_id, FormatId.JSON_WELL_LOG, metadata),
                )
            }
        )
        result = _service(
            root,
            (
                GenerationItem(
                    file_record=file_record,
                    category=DataCategory.WELL_LOG,
                    format_id=FormatId.JSON_WELL_LOG,
                    model=model,
                ),
            ),
        ).generate_one(
            GenerateManifestInput(
                file_id=base.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )
        return result.candidate.document.content

    content = generate((external_z, inline, external_a), tmp_path / "first")
    reversed_content = generate((external_a, inline, external_z), tmp_path / "second")

    datasets = content["Data"]["Datasets"]
    assert [dataset["id"] for dataset in datasets] == [
        "surrogate-key:file-1",
        "surrogate-key:file-2",
        "surrogate-key:file-3",
    ]
    assert (
        datasets[0]["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]
        == "s3://bucket/Data/mixed.json"
    )
    assert {
        dataset["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]: dataset["id"]
        for dataset in datasets[1:]
    } == {
        "file:a-data.json": "surrogate-key:file-2",
        "file:z-data.json": "surrogate-key:file-3",
    }
    assert {
        component["data"]["Name"]: component["data"]["Datasets"][0]
        for component in content["Data"]["WorkProductComponents"]
    } == {
        "external-z": "surrogate-key:file-3",
        "inline": "surrogate-key:file-1",
        "external-a": "surrogate-key:file-2",
    }
    assert {
        dataset["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]: dataset["id"]
        for dataset in reversed_content["Data"]["Datasets"][1:]
    } == {
        "file:a-data.json": "surrogate-key:file-2",
        "file:z-data.json": "surrogate-key:file-3",
    }


def test_candidate_identity_path_content_and_lineage_bind_source_and_model_hashes(
    tmp_path: Path,
) -> None:
    model = _model()
    item = _item(model, "Data/bound.sgy")
    request = GenerateManifestInput(
        file_id=item.file_record.file.file_id,
        learning_model_id=model.learning_model_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
    )
    original = _service(tmp_path / "original", (item,)).generate_one(request).candidate
    changed_source = GenerationItem(
        file_record=item.file_record.model_copy(
            update={"file": item.file_record.file.model_copy(update={"sha256": "b" * 64})}
        ),
        category=item.category,
        format_id=item.format_id,
        model=model,
        metadata=item.metadata,
    )
    source_candidate = (
        _service(tmp_path / "source", (changed_source,)).generate_one(request).candidate
    )
    changed_model = model.model_copy(update={"model_sha256": "e" * 64})
    model_item = GenerationItem(
        file_record=item.file_record,
        category=item.category,
        format_id=item.format_id,
        model=changed_model,
        metadata=item.metadata,
    )
    model_candidate = _service(tmp_path / "model", (model_item,)).generate_one(request).candidate

    assert (
        len(
            {
                original.reference.candidate_id,
                source_candidate.reference.candidate_id,
                model_candidate.reference.candidate_id,
            }
        )
        == 3
    )
    assert (
        len(
            {
                original.reference.proposed_path,
                source_candidate.reference.proposed_path,
                model_candidate.reference.proposed_path,
            }
        )
        == 3
    )
    assert original.reference.source_sha256 == "a" * 64
    assert original.reference.model_sha256 == model.model_sha256
    assert original.document.content["x-agentic-generation-lineage"] == {
        "source_sha256": "a" * 64,
        "model_sha256": model.model_sha256,
    }


@pytest.mark.parametrize("current_hash", [None, "b" * 64])
def test_generation_rejects_missing_or_stale_metadata_source_hash(
    tmp_path: Path,
    current_hash: str | None,
) -> None:
    model = _model()
    base = _item(model, "Data/stale.sgy").file_record
    file_record = base.model_copy(
        update={
            "file": base.file.model_copy(update={"sha256": current_hash}),
            "metadata_extractions": (
                _extraction(
                    base.file.file_id,
                    FormatId.SEGY,
                    SegyMetadata(
                        endian="big",
                        sampled_trace_count=0,
                        domain=DataDomain.TIME,
                        dimensions=ClassificationDimensions(),
                        binary_header=SegyBinaryHeader(),
                        survey_metadata=SegySurveyMetadata(),
                        trace_samples=(),
                    ),
                ),
            ),
        }
    )
    item = GenerationItem(
        file_record=file_record,
        category=DataCategory.SEISMIC,
        format_id=FormatId.SEGY,
        model=model,
    )

    with pytest.raises(GenerationError, match="GENERATION_CONFLICT"):
        _service(tmp_path, (item,)).generate_one(
            GenerateManifestInput(
                file_id=base.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version=GENERATION_POLICY_V1.version,
            )
        )


def test_batch_classifies_entire_suffix_on_cancel_and_fail_fast(tmp_path: Path) -> None:
    model = _model()
    invalid_content: dict[str, Any] = {"kind": "osdu:wks:Manifest:1.0.0"}
    invalid_digest = (
        __import__("hashlib")
        .sha256(json.dumps(invalid_content, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    bad_model = model.model_copy(
        update={"prototype": ManifestJsonDocument(sha256=invalid_digest, content=invalid_content)}
    )
    bad = GenerationItem(
        file_record=_item(model, "Data/00-bad.sgy").file_record,
        category=DataCategory.SEISMIC,
        format_id=FormatId.SEGY,
        model=bad_model,
    )
    eligible = _item(model, "Data/01-eligible.sgy")
    matched = _item(model, "Data/02-matched.sgy", matched=True)
    outside = _item(model, "Other/03-outside.sgy")
    fail_service = _service(tmp_path / "fail", (bad, eligible, matched, outside))
    fail = fail_service.generate_all(
        GenerateAllManifestsInput(
            inventory_id=fail_service.inventory_id,
            generation_policy_version=GENERATION_POLICY_V1.version,
            filters=GenerationFilters(relative_prefixes=(WorkspaceRelativePath("Data"),)),
            continue_on_error=False,
        )
    )
    assert (fail.failed, fail.skipped, fail.cancelled) == (1, 3, 0)
    assert sum((fail.generated, fail.existing, fail.failed, fail.skipped, fail.cancelled)) == 4

    cancel_service = _service(tmp_path / "cancel", (eligible, matched, outside))
    cancelled = cancel_service.generate_all(
        GenerateAllManifestsInput(
            inventory_id=cancel_service.inventory_id,
            generation_policy_version=GENERATION_POLICY_V1.version,
            filters=GenerationFilters(relative_prefixes=(WorkspaceRelativePath("Data"),)),
        ),
        cancellation=lambda: True,
    )
    assert (cancelled.cancelled, cancelled.skipped) == (1, 2)
    assert (
        sum(
            (
                cancelled.generated,
                cancelled.existing,
                cancelled.failed,
                cancelled.skipped,
                cancelled.cancelled,
            )
        )
        == 3
    )


def test_atomic_write_ignores_cancellation_once_started_and_uses_unique_temp_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    item = _item(model, "Data/atomic.sgy")
    service = _service(tmp_path, (item,))
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    original_write = service._write_atomic

    def cancellation_during_write(relative: WorkspaceRelativePath, content: bytes) -> None:
        assert cancellation()
        original_write(relative, content)

    monkeypatch.setattr(service, "_write_atomic", cancellation_during_write)

    output = service.generate_one(
        GenerateManifestInput(
            file_id=item.file_record.file.file_id,
            learning_model_id=model.learning_model_id,
            generation_policy_version=GENERATION_POLICY_V1.version,
            dry_run=False,
        ),
        cancellation=cancellation,
    )
    assert output.candidate.reference.generation_status is GenerationStatus.GENERATED
    assert calls == 3

    names: list[str] = []
    original_open = Path.open

    def recording_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.suffix == ".tmp":
            names.append(path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    other = _item(model, "Data/other.sgy")
    other_service = _service(tmp_path / "unique", (other,))
    request = GenerateManifestInput(
        file_id=other.file_record.file.file_id,
        learning_model_id=model.learning_model_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
        dry_run=False,
    )
    other_service.generate_one(request)
    target = tmp_path / "unique" / "generated"
    for path in target.rglob("*.json"):
        path.unlink()
    other_service.generate_one(request)
    assert len(names) == len(set(names)) == 2


def test_generation_cancels_after_transform_before_write_and_batch_forwards_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    first = _item(model, "Data/first.sgy")
    second = _item(model, "Data/second.sgy")
    service = _service(tmp_path, (first, second))
    writes = 0

    def forbidden_write(relative: WorkspaceRelativePath, content: bytes) -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr(service, "_write_atomic", forbidden_write)
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls == 3

    result = service.generate_all(
        GenerateAllManifestsInput(
            inventory_id=service.inventory_id,
            generation_policy_version=GENERATION_POLICY_V1.version,
            continue_on_error=True,
            dry_run=False,
        ),
        cancellation=cancellation,
    )
    assert (result.cancelled, result.skipped, result.generated, result.failed) == (2, 0, 0, 0)
    assert writes == 0


def test_las_dlis_and_lis_typed_overlays_remove_stale_cross_format_fields(
    tmp_path: Path,
) -> None:
    model = _model_with_component_fields(
        {
            "SampleCount": 999,
            "TraceCount": 999,
            "TopMeasuredDepth": 999,
            "SamplingStart": 999,
        }
    )
    payloads = (
        (
            FormatId.LAS,
            LasMetadata(
                version="2.0",
                well_name="LAS-WELL",
                sections=("VERSION", "CURVE"),
                curves=(LasCurveMetadata(mnemonic="DEPT", unit="M"),),
                row_count=10,
            ),
            "Data/log.las",
        ),
        (
            FormatId.DLIS,
            DlisMetadata(
                logical_file_ids=("LF",),
                frames=(DlisFrameMetadata(logical_file_id="LF", frame_id="F", channel_count=1),),
                channels=(
                    DlisChannelMetadata(
                        logical_file_id="LF",
                        channel_id="C",
                        mnemonic="GR",
                        unit="GAPI",
                    ),
                ),
                origins=(
                    DlisOriginMetadata(
                        logical_file_id="LF",
                        origin_id="O",
                        well_name="DLIS-WELL",
                    ),
                ),
                well_name="DLIS-WELL",
                library_version="1.0",
            ),
            "Data/log.dlis",
        ),
        (
            FormatId.LIS_LTI,
            LisMetadata(
                record_count=7,
                record_types=("0", "1"),
                well_name="LIS-WELL",
            ),
            "Data/log.lis",
        ),
    )
    for index, (format_id, payload, path) in enumerate(payloads):
        base = _item(model, path).file_record
        file_record = base.model_copy(
            update={"metadata_extractions": (_extraction(base.file.file_id, format_id, payload),)}
        )
        item = GenerationItem(
            file_record=file_record,
            category=DataCategory.WELL_LOG,
            format_id=format_id,
            model=model,
        )
        content: Any = (
            _service(tmp_path / str(index), (item,))
            .generate_one(
                GenerateManifestInput(
                    file_id=base.file.file_id,
                    learning_model_id=model.learning_model_id,
                    generation_policy_version=GENERATION_POLICY_V1.version,
                )
            )
            .candidate.document.content
        )
        component = content["Data"]["WorkProductComponents"][0]["data"]
        dataset = content["Data"]["Datasets"][0]["data"]
        assert "SampleCount" not in component
        assert "TraceCount" not in component
        assert dataset["EncodingFormatTypeID"].endswith(
            {FormatId.LAS: ":LAS:", FormatId.DLIS: ":DLIS:", FormatId.LIS_LTI: ":LIS:"}[format_id]
        )
    assert component["RecordCount"] == 7


def test_concurrent_publication_has_one_winner_and_maps_collision_to_output_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    item = _item(model, "Data/concurrent.sgy")
    service = _service(tmp_path, (item,))
    request = GenerateManifestInput(
        file_id=item.file_record.file.file_id,
        learning_model_id=model.learning_model_id,
        generation_policy_version=GENERATION_POLICY_V1.version,
        dry_run=False,
    )
    barrier = Barrier(2)
    original_link = os.link

    def synchronized_link(source: Any, target: Any) -> None:
        barrier.wait()
        original_link(source, target)

    monkeypatch.setattr(os, "link", synchronized_link)

    def publish() -> str:
        try:
            service.generate_one(request)
        except GenerationError as error:
            return error.code
        return "GENERATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(outcomes) == ["GENERATED", "OUTPUT_EXISTS"]
    assert not list((tmp_path / "generated").rglob("*.tmp"))
