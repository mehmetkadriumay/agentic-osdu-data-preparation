"""TOOL-018/019 deterministic review-required manifest generation."""

from __future__ import annotations

import copy
import errno
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentic_osdu.domain.models import (
    DataCategory,
    DataDomain,
    FormatId,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    GenerationStatus,
    ManifestJsonDocument,
    TrustLevel,
    ValidationStatus,
    WorkspaceRelativePath,
)
from agentic_osdu.policy import OutputPolicy, PolicyViolation
from agentic_osdu.tools.contracts import (
    DatMetadata,
    DlisMetadata,
    ExtractedMetadataContract,
    FileRecordContract,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    GenerateManifestOutput,
    GenerationBatchResult,
    InterpretationMetadataOutput,
    JsonLogSetMetadata,
    JsonWellLogMetadata,
    LasMetadata,
    LearningModelContract,
    LisMetadata,
    NavigationPosition,
    P190LineSummary,
    P190Metadata,
    SegyMetadata,
    ValidationPreflight,
)

CancellationCheck = Callable[[], bool]

_GENERATION_POLICY = b"typed-metadata-overlays|deterministic-policy-key|atomic-no-overwrite"


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    """Immutable supported manifest-generation policy."""

    version: str
    policy_sha256: str


GENERATION_POLICY_V1 = GenerationPolicy(
    version="1.0.0",
    policy_sha256=sha256(_GENERATION_POLICY).hexdigest(),
)
_SUPPORTED_POLICIES = {GENERATION_POLICY_V1.version: GENERATION_POLICY_V1}


class GenerationError(RuntimeError):
    """Stable generation, collision, policy, or cancellation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class GenerationItem:
    """Resolved inventory/model material for deterministic generation."""

    file_record: FileRecordContract
    category: DataCategory
    format_id: FormatId | None
    model: LearningModelContract
    matched: bool = False
    eligible: bool = True
    metadata: dict[str, object] | None = None


class GenerationService:
    """Generate one or a filtered batch through an approved output capability."""

    def __init__(
        self,
        output_policy: OutputPolicy,
        items: tuple[GenerationItem, ...],
        *,
        output_root_id: str = "generated",
        inventory_id: UUID | None = None,
    ) -> None:
        self._policy = output_policy
        self._items = tuple(
            sorted(items, key=lambda item: item.file_record.file.relative_path.root.casefold())
        )
        self._by_file = {item.file_record.file.file_id: item for item in self._items}
        self._output_root_id = output_root_id
        key = "\n".join(str(item.file_record.file.file_id) for item in self._items)
        self.inventory_id = inventory_id or uuid5(NAMESPACE_URL, f"generation-inventory:{key}")

    def generate_one(
        self,
        request: GenerateManifestInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> GenerateManifestOutput:
        _check_cancelled(cancellation)
        item = self._by_file.get(request.file_id)
        if item is None or item.model.learning_model_id != request.learning_model_id:
            raise GenerationError(
                "NO_COMPATIBLE_MODEL", "No compatible learning model is available."
            )
        if not item.eligible:
            raise GenerationError(
                "NO_COMPATIBLE_MODEL", "The inventory record is not eligible for generation."
            )
        source_sha256 = item.file_record.file.sha256
        if source_sha256 is None:
            raise GenerationError(
                "GENERATION_CONFLICT",
                "Generation requires the current complete source SHA-256.",
            )
        policy = _policy_for(request.generation_policy_version)
        document = _generate_document(item, policy)
        encoded = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("utf-8")
        digest = sha256(encoded).hexdigest()
        proposed = _generated_path(item, policy, digest, source_sha256)
        status = GenerationStatus.PROPOSED
        if not request.dry_run:
            _check_cancelled(cancellation)
            self._write_atomic(proposed, encoded)
            status = GenerationStatus.GENERATED
        reference = GeneratedCandidateRef(
            candidate_id=uuid5(
                request.file_id,
                (
                    f"{item.model.learning_model_id}:{policy.version}:"
                    f"{policy.policy_sha256}:{source_sha256}:"
                    f"{item.model.model_sha256}:{digest}"
                ),
            ),
            source_file_id=request.file_id,
            source_sha256=source_sha256,
            learning_model_id=item.model.learning_model_id,
            model_sha256=item.model.model_sha256,
            candidate_sha256=digest,
            proposed_path=proposed,
            generation_status=status,
            validation_status=ValidationStatus.NOT_RUN,
            trust_level=TrustLevel.HEURISTIC,
            review_required=True,
        )
        return GenerateManifestOutput(
            candidate=GeneratedManifestCandidate(
                reference=reference,
                document=ManifestJsonDocument(sha256=digest, content=document),
            ),
            validation_preflight=ValidationPreflight(
                status=ValidationStatus.NOT_RUN,
                error_count=0,
            ),
        )

    def generate_all(
        self,
        request: GenerateAllManifestsInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> GenerationBatchResult:
        if request.inventory_id != self.inventory_id:
            raise GenerationError("BATCH_POLICY_INVALID", "The inventory identity does not match.")
        _policy_for(request.generation_policy_version)
        counts = {"generated": 0, "existing": 0, "failed": 0, "skipped": 0, "cancelled": 0}
        candidates: list[GeneratedManifestCandidate] = []
        for index, item in enumerate(self._items):
            if cancellation is not None and cancellation():
                cancelled, skipped = _cancelled_suffix(self._items[index:], request)
                counts["cancelled"] += cancelled
                counts["skipped"] += skipped
                break
            if not _matches_filters(item, request):
                counts["skipped"] += 1
                continue
            if item.matched or not item.eligible:
                counts["skipped"] += 1
                continue
            child = GenerateManifestInput(
                file_id=item.file_record.file.file_id,
                learning_model_id=item.model.learning_model_id,
                generation_policy_version=request.generation_policy_version,
                dry_run=request.dry_run,
            )
            try:
                output = self.generate_one(child, cancellation=cancellation)
            except GenerationError as error:
                if error.code == "CANCELLED":
                    counts["cancelled"] += 1
                    cancelled, skipped = _cancelled_suffix(self._items[index + 1 :], request)
                    counts["cancelled"] += cancelled
                    counts["skipped"] += skipped
                    break
                if error.code == "OUTPUT_EXISTS":
                    counts["existing"] += 1
                else:
                    counts["failed"] += 1
                    if not request.continue_on_error:
                        counts["skipped"] += len(self._items) - index - 1
                        break
            else:
                counts["generated"] += 1
                candidates.append(output.candidate)
        return GenerationBatchResult(candidates=tuple(candidates), **counts)

    def _write_atomic(
        self,
        relative: WorkspaceRelativePath,
        content: bytes,
    ) -> None:
        try:
            authorized = self._policy.authorize_output(
                self._output_root_id, relative.root, follow_links=False
            )
        except PolicyViolation as error:
            raise GenerationError(
                "GENERATION_CONFLICT", "The generated output path is not approved."
            ) from error
        target = Path(authorized.canonical_path)
        try:
            if target.exists():
                raise GenerationError("OUTPUT_EXISTS", "The generated output already exists.")
            target.parent.mkdir(parents=True, exist_ok=True)
        except GenerationError:
            raise
        except OSError as error:
            raise GenerationError(
                "GENERATION_CONFLICT", "The generated output path could not be prepared."
            ) from error
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        primary_error: GenerationError | None = None
        published = False
        try:
            with temporary.open("xb") as stream:
                written = stream.write(content)
                if written != len(content):
                    raise OSError("The temporary output write was incomplete.")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
                published = True
            except FileExistsError as error:
                raise GenerationError(
                    "OUTPUT_EXISTS", "The generated output already exists."
                ) from error
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise GenerationError(
                        "OUTPUT_EXISTS", "The generated output already exists."
                    ) from error
                raise GenerationError(
                    "GENERATION_CONFLICT", "The atomic generated output write failed."
                ) from error
        except GenerationError as error:
            primary_error = error
        except OSError as error:
            primary_error = GenerationError(
                "GENERATION_CONFLICT", "The temporary generated output write failed."
            )
            primary_error.__cause__ = error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                if primary_error is None and not published:
                    primary_error = GenerationError(
                        "GENERATION_CONFLICT",
                        "The temporary generated output cleanup failed.",
                    )
                    primary_error.__cause__ = error
        if primary_error is not None:
            raise primary_error


def _generate_document(item: GenerationItem, policy: GenerationPolicy) -> dict[str, Any]:
    source = item.model.prototype_source_path.root
    target = item.file_record.file.relative_path.root
    document = cast(
        dict[str, Any],
        _rewrite(
            item.model.prototype.content,
            _replacements(source, target),
        ),
    )
    data = document.get("Data")
    if not isinstance(data, dict):
        raise GenerationError(
            "GENERATION_CONFLICT", "The prototype manifest has no supported Data object."
        )
    filename = PurePosixPath(target).name
    description = f"Generated manifest for {item.category.value}: {filename}"
    work_product = data.get("WorkProduct")
    if isinstance(work_product, dict):
        work_data = work_product.setdefault("data", {})
        if isinstance(work_data, dict):
            work_data["Name"] = filename
            work_data["Description"] = description
    components = data.get("WorkProductComponents")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            component_data = component.setdefault("data", {})
            if isinstance(component_data, dict):
                component_data["Name"] = filename
                component_data["Description"] = description
                _update_geometry(component_data, item.metadata or {})
    datasets = data.get("Datasets")
    if isinstance(datasets, list):
        for dataset in datasets:
            if not isinstance(dataset, dict):
                continue
            dataset_data = dataset.setdefault("data", {})
            if not isinstance(dataset_data, dict):
                continue
            dataset_data["Name"] = filename
            dataset_data["Description"] = description
            properties = dataset_data.setdefault("DatasetProperties", {})
            if not isinstance(properties, dict):
                continue
            info = properties.get("FileSourceInfo")
            if not isinstance(info, dict):
                infos = properties.get("FileSourceInfos")
                info = (
                    infos[0]
                    if isinstance(infos, list) and infos and isinstance(infos[0], dict)
                    else {}
                )
                properties["FileSourceInfo"] = info
            source_value = f"{item.model.file_source_prefix}{target}"
            info.update(
                {
                    "FileSource": source_value,
                    "PreloadFilePath": source_value,
                    "Name": filename,
                    "FileSize": item.file_record.file.size_bytes,
                }
            )
            dataset_data["TotalSize"] = item.file_record.file.size_bytes
            _overlay_dataset_format(dataset, dataset_data, item)
    _overlay_typed_metadata(document, item)
    document["x-agentic-generation-policy"] = {
        "version": policy.version,
        "sha256": policy.policy_sha256,
    }
    source_sha256 = item.file_record.file.sha256
    if source_sha256 is None:
        raise GenerationError(
            "GENERATION_CONFLICT",
            "Generation requires the current complete source SHA-256.",
        )
    document["x-agentic-generation-lineage"] = {
        "source_sha256": source_sha256,
        "model_sha256": item.model.model_sha256,
    }
    return document


def _replacements(source_path: str, target_path: str) -> tuple[tuple[str, str], ...]:
    source_name = PurePosixPath(source_path).name
    target_name = PurePosixPath(target_path).name
    replacements = {
        (source_path, target_path),
        (source_path.replace("/", "\\"), target_path.replace("/", "\\")),
        (source_name, target_name),
        (PurePosixPath(source_name).stem, PurePosixPath(target_name).stem),
    }
    return tuple(sorted(replacements, key=lambda item: len(item[0]), reverse=True))


def _rewrite(value: object, replacements: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements:
            if source and source.casefold() != target.casefold():

                def replacement(_: re.Match[str], value: str = target) -> str:
                    return value

                result = re.sub(
                    re.escape(source),
                    replacement,
                    result,
                    flags=re.IGNORECASE,
                )
        return result
    if isinstance(value, dict):
        return {key: _rewrite(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite(child, replacements) for child in value]
    return copy.deepcopy(value)


def _update_geometry(component_data: dict[str, Any], metadata: dict[str, object]) -> None:
    samples = metadata.get("traceCoordinateSamples")
    if not isinstance(samples, list):
        return
    coordinates = [
        [float(sample["x"]), float(sample["y"])]
        for sample in samples
        if isinstance(sample, dict)
        and isinstance(sample.get("x"), int | float)
        and not isinstance(sample.get("x"), bool)
        and isinstance(sample.get("y"), int | float)
        and not isinstance(sample.get("y"), bool)
    ]
    if len(coordinates) < 2:
        return
    spatial = component_data.get("SpatialArea")
    ingested = spatial.get("AsIngestedCoordinates") if isinstance(spatial, dict) else None
    features = ingested.get("features") if isinstance(ingested, dict) else None
    if isinstance(features, list) and features and isinstance(features[0], dict):
        geometry = features[0].get("geometry")
        if isinstance(geometry, dict):
            geometry["type"] = "AnyCrsLineString"
            geometry["coordinates"] = coordinates


_ENCODING_BY_FORMAT = {
    FormatId.SEGY: "SEGY",
    FormatId.LAS: "LAS",
    FormatId.JSON_WELL_LOG: "JSON",
    FormatId.DLIS: "DLIS",
    FormatId.LIS_LTI: "LIS",
    FormatId.CSV: "CSV",
    FormatId.P190: "ASCII",
    FormatId.SGP: "ASCII",
    FormatId.DAT: "ASCII",
    FormatId.TEXT: "ASCII",
    FormatId.PDF: "PDF",
}
_DATASET_KIND_BY_FORMAT = {
    FormatId.SEGY: "osdu:wks:dataset--FileCollection.SEGY:1.0.0",
    FormatId.LAS: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.JSON_WELL_LOG: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.DLIS: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.LIS_LTI: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.CSV: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.P190: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.SGP: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.DAT: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.TEXT: "osdu:wks:dataset--File.Generic:1.0.0",
    FormatId.PDF: "osdu:wks:dataset--File.Generic:1.0.0",
}
_SEISMIC_ONLY_FIELDS = {
    "EndTime",
    "FirstCMP",
    "FirstShotPoint",
    "LastCMP",
    "LastShotPoint",
    "LiveTraceOutline",
    "Precision",
    "ProcessingParameters",
    "SampleCount",
    "SampleInterval",
    "Seismic2DName",
    "SeismicDomainTypeID",
    "SeismicTraceDataDimensionalityTypeID",
    "TraceCount",
    "TraceDomainUOM",
    "TraceLength",
}
_WELL_ONLY_FIELDS = {
    "BottomMeasuredDepth",
    "Curves",
    "ReferenceCurveID",
    "SamplingDomainTypeID",
    "SamplingInterval",
    "SamplingStart",
    "SamplingStop",
    "TopMeasuredDepth",
}


def _overlay_dataset_format(
    dataset: dict[str, Any],
    dataset_data: dict[str, Any],
    item: GenerationItem,
) -> None:
    if item.format_id is None:
        return
    dataset["kind"] = _DATASET_KIND_BY_FORMAT[item.format_id]
    dataset_data["EncodingFormatTypeID"] = (
        f"osdu:reference-data--EncodingFormatType:{_ENCODING_BY_FORMAT[item.format_id]}:"
    )
    if item.format_id is not FormatId.SEGY:
        dataset_data.pop("Endian", None)
    if item.format_id not in {FormatId.DLIS, FormatId.LIS_LTI}:
        dataset_data.pop("SchemaFormatTypeID", None)


def _overlay_typed_metadata(document: dict[str, Any], item: GenerationItem) -> None:
    data = document.get("Data")
    if not isinstance(data, dict):
        return
    components = data.get("WorkProductComponents")
    datasets = data.get("Datasets")
    component = (
        next((value for value in components if isinstance(value, dict)), None)
        if isinstance(components, list)
        else None
    )
    dataset = (
        next((value for value in datasets if isinstance(value, dict)), None)
        if isinstance(datasets, list)
        else None
    )
    if component is None or dataset is None:
        return
    component_data = component.setdefault("data", {})
    dataset_data = dataset.setdefault("data", {})
    if not isinstance(component_data, dict) or not isinstance(dataset_data, dict):
        return
    if item.file_record.classification is not None:
        kind = item.file_record.classification.osdu_kind
        if kind is not None:
            component["kind"] = kind.root
    extraction = _matching_extraction(item)
    if extraction is None:
        return
    payload = extraction.payload
    if isinstance(payload, SegyMetadata):
        _overlay_segy(component_data, dataset_data, payload)
    elif isinstance(payload, P190Metadata):
        _overlay_p190(data, component, dataset_data, payload)
    elif isinstance(payload, InterpretationMetadataOutput) and payload.dat is not None:
        _overlay_dat(component_data, payload.dat)
    elif isinstance(payload, JsonWellLogMetadata):
        _overlay_json_well_log(data, component, dataset, payload)
    elif isinstance(payload, LasMetadata):
        _overlay_las(component_data, payload)
    elif isinstance(payload, DlisMetadata):
        _overlay_dlis(component_data, dataset_data, payload)
    elif isinstance(payload, LisMetadata):
        _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
        component_data["RecordCount"] = payload.record_count
        if payload.well_name:
            component_data["WellName"] = payload.well_name
        dataset_data["SchemaFormatTypeID"] = "osdu:reference-data--SchemaFormatType:LIS:"


def _matching_extraction(item: GenerationItem) -> ExtractedMetadataContract | None:
    source_sha256 = item.file_record.file.sha256
    if item.format_id is None:
        return None
    candidates = tuple(
        extraction
        for extraction in item.file_record.metadata_extractions
        if extraction.reference.file_id == item.file_record.file.file_id
        and extraction.reference.format_id is item.format_id
    )
    extraction = next(
        (value for value in candidates if value.reference.file_sha256 == source_sha256),
        None,
    )
    if extraction is None:
        raise GenerationError(
            "GENERATION_CONFLICT",
            "No metadata extraction matches the current complete source SHA-256.",
        )
    return extraction


def _remove_fields(target: dict[str, Any], fields: set[str]) -> None:
    for field in fields:
        target.pop(field, None)


def _overlay_segy(
    component_data: dict[str, Any],
    dataset_data: dict[str, Any],
    metadata: SegyMetadata,
) -> None:
    _remove_fields(component_data, _WELL_ONLY_FIELDS)
    interval = (
        metadata.sample_interval_microseconds / 1000
        if metadata.sample_interval_microseconds is not None
        else None
    )
    trace_count = metadata.dimensions.trace_count
    trace_length = (
        interval * metadata.samples_per_trace
        if interval is not None and metadata.samples_per_trace is not None
        else None
    )
    values = {
        "SampleInterval": interval,
        "SampleCount": metadata.samples_per_trace,
        "TraceCount": trace_count,
        "TraceLength": trace_length,
        "Seismic2DName": metadata.survey_metadata.survey_name,
    }
    for key, value in values.items():
        if value is None:
            component_data.pop(key, None)
        else:
            component_data[key] = value
    dataset_data["Endian"] = metadata.endian.upper()
    if metadata.textual_header_encoding:
        dataset_data["TextHeaderEncoding"] = metadata.textual_header_encoding
    else:
        dataset_data.pop("TextHeaderEncoding", None)
    precision = component_data.get("Precision")
    if isinstance(precision, dict):
        sample_format = metadata.binary_header.sample_format_code
        word_formats: dict[int | None, str] = {
            1: "osdu:reference-data--WordFormatType:IBMFLOAT:",
            5: "osdu:reference-data--WordFormatType:IEEEFLOAT:",
        }
        precision["WordFormat"] = word_formats.get(
            sample_format, "osdu:reference-data--WordFormatType:UNKNOWN:"
        )
        widths: dict[int | None, int] = {1: 4, 3: 2, 5: 4, 8: 1}
        width = widths.get(sample_format)
        if width is not None:
            precision["WordWidth"] = width
    parameters = component_data.get("ProcessingParameters")
    if isinstance(parameters, list):
        replacements = {
            "SamplingInterval": f"{interval:g}ms" if interval is not None else None,
            "NumCMP": str(trace_count) if trace_count is not None else None,
            "MaxTime": f"{trace_length:g}ms" if trace_length is not None else None,
        }
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            parameter_type = str(parameter.get("ProcessingParameterTypeID", ""))
            for suffix, value in replacements.items():
                if f":{suffix}:" in parameter_type:
                    if value is None:
                        parameter.pop("ProcessingParameterValue", None)
                    else:
                        parameter["ProcessingParameterValue"] = value
    domain = metadata.domain
    if "SeismicDomainTypeID" in component_data:
        component_data["SeismicDomainTypeID"] = (
            f"osdu:reference-data--SeismicDomainType:{domain.value.title()}:"
        )
    if "TraceDomainUOM" in component_data:
        component_data["TraceDomainUOM"] = (
            "osdu:reference-data--UnitOfMeasure:ms:"
            if domain is DataDomain.TIME
            else "osdu:reference-data--UnitOfMeasure:m:"
        )


def _feature_collection(geometry_type: str, coordinates: object) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": geometry_type, "coordinates": coordinates},
            }
        ],
    }


def _any_crs_feature_collection(geometry_type: str, coordinates: object) -> dict[str, Any]:
    return {
        "type": "AnyCrsFeatureCollection",
        "features": [
            {
                "type": "AnyCrsFeature",
                "properties": {},
                "geometry": {"type": geometry_type, "coordinates": coordinates},
            }
        ],
    }


def _overlay_p190(
    data: dict[str, Any],
    prototype: dict[str, Any],
    dataset_data: dict[str, Any],
    metadata: P190Metadata,
) -> None:
    components: list[dict[str, Any]] = []
    references: list[str] = []
    positions_by_line = {
        line: [position for position in metadata.sampled_positions if position.line_name == line]
        for line in metadata.line_names
    }
    summaries = {summary.line_name: summary for summary in metadata.line_summaries}
    for index, line_name in enumerate(metadata.line_names, start=1):
        positions = positions_by_line[line_name]
        if not positions:
            continue
        component = copy.deepcopy(prototype)
        component_id = f"surrogate-key:wpc-{index}"
        component["id"] = component_id
        component_data = component.setdefault("data", {})
        if not isinstance(component_data, dict):
            continue
        _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
        summary = summaries.get(line_name)
        component_data["Name"] = line_name
        component_data["Description"] = f"P1/90 2D line geometry: {line_name}"
        if summary is not None:
            _overlay_line_summary(component_data, summary)
        projected = [
            [position.easting, position.northing]
            for position in positions
            if position.easting is not None and position.northing is not None
        ]
        geographic = [[position.longitude, position.latitude] for position in positions]
        spatial = component_data.setdefault("SpatialArea", {})
        if isinstance(spatial, dict):
            if len(projected) >= 2:
                spatial["AsIngestedCoordinates"] = _any_crs_feature_collection(
                    "AnyCrsLineString", projected
                )
                if metadata.inferred_epsg is not None:
                    spatial["AsIngestedCoordinates"]["CoordinateReferenceSystemID"] = (
                        f"osdu:reference-data--CoordinateReferenceSystem:EPSG::{metadata.inferred_epsg}:"
                    )
            spatial["Wgs84Coordinates"] = _feature_collection("LineString", geographic)
        _overlay_endpoint(component_data, "FirstLocation", positions[0], metadata.inferred_epsg)
        _overlay_endpoint(component_data, "LastLocation", positions[-1], metadata.inferred_epsg)
        components.append(component)
        references.append(component_id)
    if not components:
        return
    data["WorkProductComponents"] = components
    work_product = data.get("WorkProduct")
    if isinstance(work_product, dict):
        work_data = work_product.setdefault("data", {})
        if isinstance(work_data, dict):
            work_data["Components"] = references
            work_data["Description"] = f"P1/90 navigation with {len(components)} line geometries"
    dataset_data["Description"] = f"P1/90 navigation with {len(components)} line geometries"


def _overlay_line_summary(
    component_data: dict[str, Any],
    summary: P190LineSummary,
) -> None:
    values = {
        "FirstCMP": summary.first_point_number,
        "LastCMP": summary.last_point_number,
        "HasCMPIncreaseByOne": summary.point_increment == 1,
        "PositionCount": summary.position_count,
    }
    for key, value in values.items():
        if value is not None:
            component_data[key] = value


def _overlay_endpoint(
    component_data: dict[str, Any],
    key: str,
    position: NavigationPosition,
    epsg: int | None,
) -> None:
    longitude = float(position.longitude)
    latitude = float(position.latitude)
    easting = position.easting
    northing = position.northing
    location: dict[str, Any] = {
        "Wgs84Coordinates": _feature_collection("Point", [longitude, latitude])
    }
    if easting is not None and northing is not None:
        location["AsIngestedCoordinates"] = _any_crs_feature_collection(
            "AnyCrsPoint", [easting, northing]
        )
        if epsg is not None:
            location["AsIngestedCoordinates"]["CoordinateReferenceSystemID"] = (
                f"osdu:reference-data--CoordinateReferenceSystem:EPSG::{epsg}:"
            )
    component_data[key] = location


def _overlay_dat(component_data: dict[str, Any], metadata: DatMetadata) -> None:
    _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
    component_data["InterpretationType"] = metadata.interpretation_type
    component_data["PointCount"] = metadata.point_count
    if metadata.crs:
        component_data["CoordinateReferenceSystem"] = metadata.crs
    else:
        component_data.pop("CoordinateReferenceSystem", None)
    if metadata.interpretation_type == "fault":
        for key in ("GeologicalUnitAgePeriod", "GeologicalUnitName"):
            component_data.pop(key, None)
    else:
        component_data.pop("Faults", None)


def _curve(mnemonic: str, unit: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"Mnemonic": mnemonic, "NumberOfColumns": 1}
    if unit:
        result["CurveUnit"] = f"osdu:reference-data--UnitOfMeasure:{unit.upper()}:"
    return result


def _overlay_json_well_log(
    data: dict[str, Any],
    component_prototype: dict[str, Any],
    dataset_prototype: dict[str, Any],
    metadata: JsonWellLogMetadata,
) -> None:
    log_sets = metadata.log_sets or (
        JsonLogSetMetadata(
            name=str(component_prototype.get("data", {}).get("Name") or "log set 1"),
            well_name=metadata.well_name,
            curves=metadata.curves,
            row_count=metadata.row_count,
            column_count=metadata.column_count,
            index_curve=metadata.index_curve,
        ),
    )
    components: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = [dataset_prototype]
    dataset_prototype["id"] = "surrogate-key:file-1"
    external_dataset_ids = {
        uri: f"surrogate-key:file-{index}"
        for index, uri in enumerate(
            sorted({log_set.data_uri for log_set in log_sets if log_set.data_uri}),
            start=2,
        )
    }
    for uri, dataset_id in external_dataset_ids.items():
        external_dataset = copy.deepcopy(dataset_prototype)
        external_dataset["id"] = dataset_id
        external_data = external_dataset.setdefault("data", {})
        if isinstance(external_data, dict):
            properties = external_data.setdefault("DatasetProperties", {})
            if isinstance(properties, dict):
                info = properties.setdefault("FileSourceInfo", {})
                if isinstance(info, dict):
                    info["FileSource"] = uri
                    info["PreloadFilePath"] = uri
                    info["Name"] = PurePosixPath(uri).name
                    info.pop("FileSize", None)
            external_data.pop("TotalSize", None)
        datasets.append(external_dataset)
    for index, log_set in enumerate(log_sets, start=1):
        component = component_prototype if index == 1 else copy.deepcopy(component_prototype)
        component["id"] = f"surrogate-key:wpc-{index}"
        component_data = component.setdefault("data", {})
        if not isinstance(component_data, dict):
            continue
        component_data["Name"] = log_set.name
        component_data["Description"] = f"Generated WellLog manifest component for {log_set.name}"
        dataset_id = "surrogate-key:file-1"
        if log_set.data_uri:
            dataset_id = external_dataset_ids[log_set.data_uri]
        component_data["Datasets"] = [dataset_id]
        _overlay_json_well_log_set(component_data, log_set)
        components.append(component)
    data["WorkProductComponents"] = components
    data["Datasets"] = datasets
    work_product = data.get("WorkProduct")
    if isinstance(work_product, dict):
        work_data = work_product.setdefault("data", {})
        if isinstance(work_data, dict):
            work_data["Components"] = [component["id"] for component in components]
    for dataset in datasets:
        dataset_data = dataset.get("data")
        if isinstance(dataset_data, dict):
            dataset_data.pop("SchemaFormatTypeID", None)


def _overlay_json_well_log_set(
    component_data: dict[str, Any],
    metadata: JsonLogSetMetadata,
) -> None:
    _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
    component_data["Curves"] = [_curve(value.name, value.unit) for value in metadata.curves]
    component_data["ReferenceCurveID"] = metadata.index_curve
    component_data["RowCount"] = metadata.row_count
    component_data["ColumnCount"] = metadata.column_count
    if metadata.well_name:
        component_data["WellName"] = metadata.well_name


def _overlay_las(component_data: dict[str, Any], metadata: LasMetadata) -> None:
    _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
    component_data["Curves"] = [_curve(value.mnemonic, value.unit) for value in metadata.curves]
    if metadata.row_count is not None:
        component_data["RowCount"] = metadata.row_count
    if metadata.well_name:
        component_data["WellName"] = metadata.well_name


def _overlay_dlis(
    component_data: dict[str, Any],
    dataset_data: dict[str, Any],
    metadata: DlisMetadata,
) -> None:
    _remove_fields(component_data, _SEISMIC_ONLY_FIELDS | _WELL_ONLY_FIELDS)
    component_data["Curves"] = [
        _curve(value.mnemonic or value.channel_id, value.unit) for value in metadata.channels
    ]
    component_data["FrameCount"] = len(metadata.frames)
    if metadata.well_name:
        component_data["WellName"] = metadata.well_name
    dataset_data["SchemaFormatTypeID"] = "osdu:reference-data--SchemaFormatType:DLIS:"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "manifest"


def _generated_path(
    item: GenerationItem,
    policy: GenerationPolicy,
    digest: str,
    source_sha256: str,
) -> WorkspaceRelativePath:
    category = _safe_filename(item.category.value)
    identity = _safe_filename(item.file_record.file.relative_path.root.replace("/", "__"))
    return WorkspaceRelativePath(
        f"{category}/generated_{identity}_{source_sha256[:12]}_"
        f"{item.model.model_sha256[:12]}_{policy.policy_sha256[:12]}_{digest[:12]}.json"
    )


def _policy_for(version: str) -> GenerationPolicy:
    policy = _SUPPORTED_POLICIES.get(version)
    if policy is None:
        raise GenerationError(
            "GENERATION_POLICY_UNAVAILABLE",
            "The requested generation policy is unknown.",
        )
    return policy


def _cancelled_suffix(
    items: tuple[GenerationItem, ...],
    request: GenerateAllManifestsInput,
) -> tuple[int, int]:
    cancelled = sum(
        _matches_filters(item, request) and item.eligible and not item.matched for item in items
    )
    return cancelled, len(items) - cancelled


def _matches_filters(item: GenerationItem, request: GenerateAllManifestsInput) -> bool:
    filters = request.filters
    if filters.categories and item.category not in filters.categories:
        return False
    if filters.format_ids and item.format_id not in filters.format_ids:
        return False
    path = item.file_record.file.relative_path.root
    return not filters.relative_prefixes or any(
        path == prefix.root or path.startswith(f"{prefix.root}/")
        for prefix in filters.relative_prefixes
    )


def _check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise GenerationError("CANCELLED", "Manifest generation was cancelled.")
