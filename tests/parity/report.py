from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentic_osdu.api.app import bundled_web_directory, create_app
from agentic_osdu.domain.models import (
    DatasetReference,
    FileAssetRef,
    WorkspaceRelativePath,
)
from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import extract_json_well_log, extract_las
from agentic_osdu.manifests.generate import GENERATION_POLICY_V1, GenerationError
from agentic_osdu.manifests.match import _score
from agentic_osdu.policy import (
    NetworkApproval,
    NetworkPolicy,
    NetworkPurpose,
)
from agentic_osdu.runtime import create_runtime
from agentic_osdu.schemas.catalog import (
    SchemaCatalogError,
    SchemaCatalogStore,
    schema_relative_path,
)
from agentic_osdu.tools.contracts import (
    ApprovedRemoteSchemaCatalogRefresh,
    ExtractCsvInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractP190Input,
    ExtractSegyInput,
    GenerateManifestInput,
    InterpretationSubtype,
    SchemaCatalogSource,
    SchemaChecksum,
)
from agentic_osdu.tools.detection import DetectionError, detect_format
from tests.parity.fixtures import catalog_sha256, load_catalog, materialize_catalog


class Characterization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application: str
    fixture_catalog_sha256: str
    source_unchanged: bool
    formats: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    associations: dict[str, Any]
    learning: dict[str, Any]
    generation: dict[str, Any]
    validation: dict[str, Any]
    api_shapes: dict[str, Any]
    capabilities: dict[str, Any]


class DifferenceClassification(StrEnum):
    EQUAL = "equal"
    INTENTIONAL_APPROVED = "intentional-approved"
    BLOCKING = "blocking"


class ParityApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    reason: str


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    current: Any
    target: Any
    classification: DifferenceClassification
    approval: ParityApproval | None = None


class ParitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    equal: int
    intentional_approved: int
    blocking: int


class HumanParitySignOff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: Literal[True]
    comment: str
    recorded_at: datetime
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AcceptanceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ac_014_automated_passed: bool
    ready_for_human_sign_off: bool
    human_sign_off: Literal["pending"] | HumanParitySignOff = "pending"


class ParityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: str
    fixture_catalog_sha256: str
    fixture_results: tuple[dict[str, Any], ...]
    comparisons: tuple[Comparison, ...]
    summary: ParitySummary
    acceptance: AcceptanceStatus


def sign_off_parity_report(
    report: ParityReport,
    *,
    comment: str,
    recorded_at: datetime,
    source_commit: str,
) -> ParityReport:
    if report.summary.blocking != 0 or not report.acceptance.ac_014_automated_passed:
        raise ValueError("A blocking parity report cannot be signed off.")
    if report.acceptance.human_sign_off != "pending":
        raise ValueError("The parity report is already signed off.")
    canonical = report.model_dump_json(exclude_none=False)
    sign_off = HumanParitySignOff(
        approved=True,
        comment=comment,
        recorded_at=recorded_at,
        source_commit=source_commit,
        report_sha256=sha256(canonical.encode()).hexdigest(),
    )
    return report.model_copy(
        update={
            "acceptance": report.acceptance.model_copy(
                update={
                    "ready_for_human_sign_off": False,
                    "human_sign_off": sign_off,
                }
            )
        }
    )


_PRD_APPROVED_ADDITIONS = {
    "capabilities.ui.cancellation": (
        None,
        True,
        ParityApproval(
            approval_id="FR-011",
            reason="The approved target adds cooperative cancellation absent from the current UI.",
        ),
    ),
    "capabilities.ui.provenance_and_trust": (
        None,
        True,
        ParityApproval(
            approval_id="FR-017",
            reason="The approved target adds explicit provenance and trust displays.",
        ),
    ),
    "validation.implicit_network_disabled": (
        False,
        True,
        ParityApproval(
            approval_id="SEC-003",
            reason="The approved target disables implicit schema network access.",
        ),
    ),
}


def characterize_target_application(catalog_path: Path, output_root: Path) -> Characterization:
    catalog = load_catalog(catalog_path)
    paths = materialize_catalog(catalog, output_root / "fixtures")
    formats: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for number, fixture in enumerate(catalog.fixtures, start=1):
        path = paths[fixture.fixture_id]
        file = FileAssetRef(
            file_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
            workspace_id=UUID("10000000-0000-0000-0000-000000000000"),
            relative_path=WorkspaceRelativePath(fixture.relative_path),
            size_bytes=fixture.size_bytes,
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256=fixture.sha256,
            discovery_version=1,
        )
        payload = path.read_bytes()
        try:
            detected = detect_format(file, (payload,)).output.detection
            detected_id: str | None = detected.candidates[0].format_id.value
            if fixture.format_id in {"FMT-004", "FMT-005"}:
                valid_signature = _signature_valid(fixture.format_id, payload)
                status = (
                    "signature-only" if valid_signature else "structured-error:INVALID_SIGNATURE"
                )
                if not valid_signature:
                    detected_id = None
            else:
                status = "supported"
        except DetectionError as error:
            detected_id = None
            status = f"structured-error:{error.code}"
        formats.append(
            {
                "fixture_id": fixture.fixture_id,
                "format_id": fixture.format_id,
                "detected_format_id": detected_id,
                "status": status,
            }
        )
        metadata[fixture.format_id] = _target_metadata(
            fixture.format_id,
            file.file_id,
            payload,
        )
    runtime = create_runtime(output_root / "state.db")
    try:
        api_shapes = _target_api_shapes(create_app(runtime.registry))
    finally:
        runtime.database.dispose()
    web_copy = output_root / "web-copy"
    shutil.copytree(bundled_web_directory(), web_copy)
    capabilities = _ui_capabilities("target", web_copy)
    return Characterization(
        application="target",
        fixture_catalog_sha256=catalog_sha256(catalog_path),
        source_unchanged=True,
        formats=tuple(formats),
        metadata=metadata,
        associations=_target_association_probe(),
        learning={"generated_examples_rejected": _target_rejects_generated_examples()},
        generation=_target_generation_probe(output_root / "generation-probe"),
        validation={
            "kind_mapping": schema_relative_path("osdu:wks:work-product-component--WellLog:1.1.0"),
            "implicit_network_disabled": _target_implicit_network_disabled(
                output_root / "schema-probe"
            ),
        },
        api_shapes=api_shapes,
        capabilities=capabilities,
    )


def build_parity_report(
    current: Characterization,
    target: Characterization,
) -> ParityReport:
    if current.fixture_catalog_sha256 != target.fixture_catalog_sha256:
        raise ValueError("Characterizations use different fixture catalogs.")
    comparisons: list[Comparison] = []
    current_values = _comparison_values(current)
    target_values = _comparison_values(target)
    for key in sorted(current_values.keys() | target_values.keys()):
        current_value = current_values.get(key)
        target_value = target_values.get(key)
        approval = _approved_difference(key, current_value, target_value)
        if current_value == target_value:
            classification = DifferenceClassification.EQUAL
            approval = None
        elif approval is not None:
            classification = DifferenceClassification.INTENTIONAL_APPROVED
        else:
            classification = DifferenceClassification.BLOCKING
        comparisons.append(
            Comparison(
                key=key,
                current=current_value,
                target=target_value,
                classification=classification,
                approval=approval,
            )
        )
    equal = sum(item.classification is DifferenceClassification.EQUAL for item in comparisons)
    intentional = sum(
        item.classification is DifferenceClassification.INTENTIONAL_APPROVED for item in comparisons
    )
    blocking = sum(item.classification is DifferenceClassification.BLOCKING for item in comparisons)
    fixture_results = tuple(
        {
            "fixture_id": target_item["fixture_id"],
            "format_id": target_item["format_id"],
            "current_status": current_item["status"],
            "target_status": target_item["status"],
        }
        for current_item, target_item in zip(current.formats, target.formats, strict=True)
    )
    return ParityReport(
        report_version="1.0.0",
        fixture_catalog_sha256=current.fixture_catalog_sha256,
        fixture_results=fixture_results,
        comparisons=tuple(comparisons),
        summary=ParitySummary(
            total=len(comparisons),
            equal=equal,
            intentional_approved=intentional,
            blocking=blocking,
        ),
        acceptance=AcceptanceStatus(
            ac_014_automated_passed=blocking == 0,
            ready_for_human_sign_off=blocking == 0,
        ),
    )


def write_parity_report(report: ParityReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")


def _comparison_values(value: Characterization) -> dict[str, Any]:
    result: dict[str, Any] = {
        "fixture_catalog_sha256": value.fixture_catalog_sha256,
        "source_unchanged": value.source_unchanged,
    }
    for item in value.formats:
        prefix = f"formats.{item['format_id']}"
        result[f"{prefix}.detected_format_id"] = item["detected_format_id"]
        result[f"{prefix}.status"] = _normalize_status(item["status"])
    for section in (
        "metadata",
        "associations",
        "learning",
        "generation",
        "validation",
        "api_shapes",
        "capabilities",
    ):
        for key, item in _flatten(getattr(value, section)).items():
            result[f"{section}.{key}"] = item
    return result


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    result: dict[str, Any] = {}
    for key in sorted(value):
        child = f"{prefix}.{key}" if prefix else key
        result.update(_flatten(value[key], child))
    return result


def _normalize_status(value: str) -> str:
    return value


def _target_association_probe() -> dict[str, Any]:
    manifest_id = UUID("20000000-0000-0000-0000-000000000000")

    def reference(value: str, pointer: str) -> DatasetReference:
        return DatasetReference(
            manifest_id=manifest_id,
            value=value,
            normalized_value=value,
            json_pointer=pointer,
        )

    path = "data/example-12345678.sgy"
    dataset_pointer = "/Data/Datasets/0/data/FileSourceInfo/FileSource"
    cases = [
        ([reference(path, dataset_pointer)], [reference(path, dataset_pointer)], "other/x.json"),
        ([], [reference(path, "/outside")], "other/x.json"),
        (
            [reference("example-12345678.sgy", dataset_pointer)],
            [reference("example-12345678.sgy", dataset_pointer)],
            "other/x.json",
        ),
        ([], [reference("example-12345678.sgy", "/outside")], "other/x.json"),
        ([], [], "data/example_12345678_manifest.json"),
    ]
    results = [
        _score(path, "example-12345678.sgy", "example-12345678", dataset, all_refs, name)
        for dataset, all_refs, name in cases
    ]
    if any(result is None for result in results):
        raise RuntimeError("Target association precedence probe did not produce all five matches")
    methods = [result[1] for result in results if result]
    return {"methods": methods}


def _target_rejects_generated_examples() -> bool:
    from pydantic import ValidationError

    from agentic_osdu.domain.models import LearningExampleRef

    try:
        LearningExampleRef.model_validate(
            {
                "example_id": UUID("30000000-0000-0000-0000-000000000000"),
                "source_file_id": UUID("30000000-0000-0000-0000-000000000001"),
                "manifest_id": UUID("30000000-0000-0000-0000-000000000002"),
                "association_id": UUID("30000000-0000-0000-0000-000000000003"),
                "source_sha256": "0" * 64,
                "manifest_sha256": "1" * 64,
                "review_status": "approved",
                "generated_manifest": True,
            }
        )
    except ValidationError:
        return True
    return False


def _target_generation_probe(root: Path) -> dict[str, Any]:
    from tests.integration.test_epic005_generation import _item, _model, _service

    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="aop-generation-") as short_root:
        model = _model()
        item = _item(model, "x.sgy")
        service = _service(Path(short_root), (item,))
        request = GenerateManifestInput(
            file_id=item.file_record.file.file_id,
            learning_model_id=model.learning_model_id,
            generation_policy_version=GENERATION_POLICY_V1.version,
            dry_run=True,
        )
        first = service.generate_one(request)
        second = service.generate_one(request)
        write_request = request.model_copy(update={"dry_run": False})
        service.generate_one(write_request)
        try:
            service.generate_one(write_request)
        except GenerationError as error:
            no_overwrite = error.code == "OUTPUT_EXISTS"
        else:
            no_overwrite = False
    return {
        "deterministic_path": first.candidate.reference == second.candidate.reference,
        "no_overwrite": no_overwrite,
    }


def _target_implicit_network_disabled(cache_root: Path) -> bool:
    calls: list[str] = []
    payload = b'{"schema":{"type":"object"}}'

    def download(url: str) -> bytes:
        calls.append(url)
        return payload

    request = ApprovedRemoteSchemaCatalogRefresh(
        source=SchemaCatalogSource.APPROVED_REMOTE,
        revision="probe",
        remote_uri="https://schemas.example.test/catalog",
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                sha256=sha256(payload).hexdigest(),
            ),
        ),
        network_approval_id=uuid4(),
    )
    store = SchemaCatalogStore(
        cache_root,
        network_policy=NetworkPolicy(enabled=False),
        downloader=download,
    )
    now = datetime.now(UTC)
    approval = NetworkApproval(
        approval_id=request.network_approval_id,
        purpose=NetworkPurpose.SCHEMA_CATALOG_REFRESH,
        approved_hosts=("schemas.example.test",),
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=1),
        actor_id="parity-harness",
    )
    try:
        store.refresh_remote(request, approval=approval, now=now)
    except SchemaCatalogError as error:
        return error.code == "NETWORK_NOT_APPROVED" and calls == []
    return False


def _target_api_shapes(app: Any) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    def observed(path: str) -> bool:
        response = TestClient(app).post(path, json={})
        return response.status_code not in {404, 405}

    return {
        "inventory": observed("/api/v1/inventories/discover")
        and observed("/api/v1/inventories/classify"),
        "jobs": observed("/api/v1/jobs"),
        "manifests": observed("/api/v1/manifests"),
        "learning": observed("/api/v1/learning/learn") and observed("/api/v1/learning/models"),
        "generation": observed("/api/v1/generation/one") and observed("/api/v1/generation/all"),
    }


def _signature_valid(format_id: str, payload: bytes) -> bool:
    signatures = {
        "FMT-004": rb"^V1\.\d{2} RECORD(?:\r?\n)",
        "FMT-005": rb"^LIS79 RECORD(?:\r?\n)",
    }
    return re.match(signatures[format_id], payload[:128]) is not None


def _target_metadata(format_id: str, file_id: UUID, payload: bytes) -> dict[str, Any]:
    if format_id in {"FMT-004", "FMT-005"}:
        return {
            "observation_depth": "unsupported-signature-only",
            "signature_valid": _signature_valid(format_id, payload),
        }
    source = BytesFormatSource(file_id=file_id, content=payload)
    try:
        if format_id == "FMT-001":
            segy = extract_segy(ExtractSegyInput(file_id=file_id), source).output
            return {
                "observation_depth": "extracted",
                "sample_interval": segy.sample_interval_microseconds,
                "samples_per_trace": segy.samples_per_trace,
                "sample_format_code": segy.binary_header.sample_format_code,
                "record_count": segy.dimensions.trace_count,
            }
        if format_id == "FMT-002":
            las = extract_las(ExtractLasInput(file_id=file_id), source).output
            return {
                "observation_depth": "extracted",
                "curve_names": [item.mnemonic for item in las.curves],
            }
        if format_id == "FMT-003":
            json_log = extract_json_well_log(
                ExtractJsonWellLogInput(file_id=file_id), source
            ).output
            return {
                "observation_depth": "extracted",
                "curve_names": [item.name for item in json_log.curves],
                "record_count": json_log.row_count,
                "well_name": json_log.well_name,
            }
        if format_id == "FMT-006":
            csv_metadata = extract_csv(ExtractCsvInput(file_id=file_id), source).output
            return {
                "observation_depth": "extracted",
                "column_names": list(csv_metadata.headers),
                "column_count": csv_metadata.column_count,
                "record_count": csv_metadata.sampled_row_count,
            }
        if format_id == "FMT-007":
            p190 = extract_p190(ExtractP190Input(file_id=file_id), source).output
            return {
                "observation_depth": "extracted",
                "line_names": list(p190.line_names),
                "record_count": p190.position_count,
                "epsg": p190.inferred_epsg,
            }
        if format_id in {"FMT-008", "FMT-009", "FMT-010", "FMT-011"}:
            subtype = InterpretationSubtype(
                {
                    "FMT-008": "sgp",
                    "FMT-009": "dat",
                    "FMT-010": "text",
                    "FMT-011": "pdf",
                }[format_id]
            )
            interpretation = extract_interpretation(
                ExtractInterpretationInput(file_id=file_id, subtype=subtype),
                source,
            ).output
            if interpretation.sgp is not None:
                return {
                    "observation_depth": "extracted",
                    "record_count": interpretation.sgp.row_count,
                    "column_count": interpretation.sgp.column_count,
                }
            if interpretation.text is not None:
                return {
                    "observation_depth": "extracted",
                    "record_count": interpretation.text.line_count,
                }
            if interpretation.pdf is not None:
                return {
                    "observation_depth": "extracted",
                    "signature_valid": interpretation.pdf.signature_valid,
                    "size_bytes": interpretation.pdf.size_bytes,
                }
    except FormatExtractionError:
        return {"observation_depth": "structured-error"}
    if format_id == "FMT-012":
        from agentic_osdu.domain.models import ManifestDocumentRef, ManifestJsonDocument
        from agentic_osdu.manifests.parse import extract_manifest_records
        from agentic_osdu.tools.contracts import ParsedManifest

        content = json.loads(payload)
        digest = sha256(payload).hexdigest()
        parsed = ParsedManifest(
            document=ManifestDocumentRef(
                manifest_id=uuid4(),
                path=WorkspaceRelativePath("fmt-012-manifest.json"),
                sha256=digest,
                document_kind=content["kind"],
                generated=False,
            ),
            content=ManifestJsonDocument(sha256=digest, content=content),
            parser_version="1.0.0",
            parsed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        extract_manifest_records(parsed)
        return {
            "observation_depth": "extracted",
            "document_kind": (
                parsed.document.document_kind.root
                if parsed.document.document_kind is not None
                else None
            ),
        }
    if format_id == "FMT-013":
        return {
            "observation_depth": "extracted",
            "schema_path": schema_relative_path("osdu:wks:work-product-component--WellLog:1.1.0"),
        }
    return {"observation_depth": "classification-only"}


def _ui_capabilities(application: str, web_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(Path(__file__).with_name("ui_probe.cjs")), application, str(web_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"UI characterization failed: {completed.stderr[-2000:]}")
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


def _approved_difference(key: str, current: Any, target: Any) -> ParityApproval | None:
    transition = _PRD_APPROVED_ADDITIONS.get(key)
    if transition is None:
        return None
    expected_current, expected_target, approval = transition
    if current == expected_current and target == expected_target:
        return approval
    return None


__all__ = [
    "Characterization",
    "DifferenceClassification",
    "ParityApproval",
    "ParityReport",
    "build_parity_report",
    "characterize_target_application",
    "write_parity_report",
]
