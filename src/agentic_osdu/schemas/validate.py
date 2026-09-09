"""TOOL-020 exact schema validation with bounded fail-closed reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, cast

from jsonschema import Draft7Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from referencing.exceptions import Unresolvable

from agentic_osdu.domain.models import (
    GeneratedManifestCandidate,
    OSDUKind,
    ValidationStatus,
)
from agentic_osdu.schemas.catalog import (
    LoadedSchemaCatalog,
    SchemaCatalogError,
    SchemaCatalogStore,
    parse_kind,
)
from agentic_osdu.tools.contracts import (
    ValidateSchemasInput,
    ValidationIssue,
    ValidationReport,
)


def json_pointer(path: Iterable[object]) -> str:
    """Encode a JSON Schema error path as RFC 6901."""

    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in path]
    return "/" + "/".join(parts) if parts else "/"


def manifest_records(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return current-parity contained records with document JSON pointers."""

    records: list[tuple[str, dict[str, Any]]] = []
    for section in ("ReferenceData", "MasterData"):
        values = document.get(section, [])
        if isinstance(values, list):
            records.extend(
                (f"/{section}/{index}", item)
                for index, item in enumerate(values)
                if isinstance(item, dict)
            )
    data = document.get("Data")
    if isinstance(data, dict):
        for section in ("Datasets", "WorkProductComponents"):
            values = data.get(section, [])
            if isinstance(values, list):
                records.extend(
                    (f"/Data/{section}/{index}", item)
                    for index, item in enumerate(values)
                    if isinstance(item, dict)
                )
        work_product = data.get("WorkProduct")
        if isinstance(work_product, dict):
            records.append(("/Data/WorkProduct", work_product))
    return records


def surrogate_id_map(
    records: list[tuple[str, dict[str, Any]]],
) -> dict[str, str]:
    """Create deterministic schema-compatible IDs for manifest surrogate keys."""

    replacements: dict[str, str] = {}
    for index, (_, record) in enumerate(records, start=1):
        record_id = record.get("id")
        kind = record.get("kind")
        if (
            isinstance(record_id, str)
            and record_id.startswith("surrogate-key:")
            and isinstance(kind, str)
        ):
            try:
                parsed = parse_kind(kind)
            except SchemaCatalogError:
                continue
            replacements[record_id] = (
                f"{parsed.authority}:{parsed.entity}:schema-validation-{index}"
            )
    return replacements


def normalize_record_surrogates(
    record: dict[str, Any],
    replacements: dict[str, str],
) -> dict[str, Any]:
    """Deep-copy one record and normalize IDs/references for schema validation only."""

    def replace(value: Any) -> Any:
        if isinstance(value, str) and value in replacements:
            return f"{replacements[value]}:0"
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        return value

    normalized = cast(dict[str, Any], replace(record))
    record_id = record.get("id")
    if isinstance(record_id, str) and record_id in replacements:
        normalized["id"] = replacements[record_id]
    return normalized


class SchemaValidationService:
    """Validate parsed or generated manifests without any network fallback."""

    def __init__(self, store: SchemaCatalogStore) -> None:
        self._store = store

    def validate(
        self,
        request: ValidateSchemasInput,
        *,
        cancellation: Callable[[], bool] | None = None,
    ) -> ValidationReport:
        _check_cancelled(cancellation)
        document = (
            request.manifest.document.content
            if isinstance(request.manifest, GeneratedManifestCandidate)
            else request.manifest.content.content
        )
        try:
            catalog = self._store.open(request.schema_catalog_id)
        except SchemaCatalogError as error:
            return _unavailable_report(request, None, error)

        raw_kind = document.get("kind")
        if not isinstance(raw_kind, str):
            return self._report(
                catalog,
                document_kind=None,
                status=ValidationStatus.INVALID,
                schema_conformant=False,
                issues=(
                    ValidationIssue(
                        code="DOCUMENT_KIND_INVALID",
                        json_pointer="/kind",
                        message="A valid OSDU kind is required.",
                        scope="document",
                        validator="required",
                    ),
                ),
            )
        try:
            document_kind = OSDUKind(raw_kind)
            parsed_kind = parse_kind(raw_kind)
        except (ValueError, SchemaCatalogError):
            return self._report(
                catalog,
                document_kind=None,
                status=ValidationStatus.INVALID,
                schema_conformant=False,
                issues=(
                    ValidationIssue(
                        code="DOCUMENT_KIND_INVALID",
                        json_pointer="/kind",
                        message="The OSDU document kind is invalid.",
                        scope="document",
                        validator="kind",
                    ),
                ),
            )

        issues: list[ValidationIssue] = []
        schemas: list[str] = []
        record_count = 0
        truncated = False
        try:
            if parsed_kind.entity != "Manifest":
                schemas.append(raw_kind)
                record_count = 1
                truncated = _append_validation_errors(
                    issues,
                    document,
                    raw_kind,
                    "/",
                    catalog,
                    request.max_errors,
                )
            else:
                schemas.append(raw_kind)
                truncated = _append_validation_errors(
                    issues,
                    document,
                    raw_kind,
                    "/",
                    catalog,
                    request.max_errors,
                )
                records = manifest_records(document)
                record_count = len(records)
                replacements = surrogate_id_map(records)
                for pointer, record in records:
                    _check_cancelled(cancellation)
                    if len(issues) >= request.max_errors:
                        truncated = True
                        break
                    record_kind = record.get("kind")
                    if not isinstance(record_kind, str):
                        issues.append(
                            ValidationIssue(
                                code="DOCUMENT_KIND_INVALID",
                                json_pointer=f"{pointer}/kind",
                                message="A valid OSDU kind is required.",
                                scope=pointer,
                                validator="required",
                            )
                        )
                        continue
                    try:
                        OSDUKind(record_kind)
                        parse_kind(record_kind)
                    except (ValueError, SchemaCatalogError):
                        issues.append(
                            ValidationIssue(
                                code="DOCUMENT_KIND_INVALID",
                                json_pointer=f"{pointer}/kind",
                                message="The OSDU record kind is invalid.",
                                scope=pointer,
                                validator="kind",
                            )
                        )
                        continue
                    schemas.append(record_kind)
                    truncated = (
                        _append_validation_errors(
                            issues,
                            normalize_record_surrogates(record, replacements),
                            record_kind,
                            pointer,
                            catalog,
                            request.max_errors,
                        )
                        or truncated
                    )
        except SchemaCatalogError as error:
            if error.code == "CANCELLED":
                raise
            return _unavailable_report(request, catalog, error, document_kind=document_kind)
        except (SchemaError, Unresolvable) as error:
            return _unavailable_report(request, catalog, error, document_kind=document_kind)

        return self._report(
            catalog,
            document_kind=document_kind,
            status=ValidationStatus.INVALID if issues else ValidationStatus.VALID,
            schema_conformant=not issues,
            issues=tuple(issues),
            validated_record_count=record_count,
            validated_schemas=tuple(dict.fromkeys(schemas)),
            errors_truncated=truncated,
        )

    @staticmethod
    def _report(
        catalog: LoadedSchemaCatalog,
        *,
        document_kind: OSDUKind | None,
        status: ValidationStatus,
        schema_conformant: bool | None,
        issues: tuple[ValidationIssue, ...],
        validated_record_count: int = 0,
        validated_schemas: tuple[str, ...] = (),
        errors_truncated: bool = False,
    ) -> ValidationReport:
        descriptor = catalog.descriptor
        return ValidationReport(
            status=status,
            schema_conformant=schema_conformant,
            schema_catalog_id=descriptor.schema_catalog_id,
            schema_revision=descriptor.revision,
            catalog_sha256=descriptor.catalog_sha256,
            catalog_source=descriptor.source,
            document_kind=document_kind,
            issues=issues,
            validated_record_count=validated_record_count,
            validated_schemas=validated_schemas,
            errors_truncated=errors_truncated,
        )


def _append_validation_errors(
    issues: list[ValidationIssue],
    instance: object,
    kind: str,
    scope: str,
    catalog: LoadedSchemaCatalog,
    max_errors: int,
) -> bool:
    schema = catalog.load(kind)
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(
        schema,
        registry=catalog.registry(),
        format_checker=FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    available = max_errors - len(issues)
    for error in errors[:available]:
        suffix = json_pointer(error.absolute_path)
        pointer = suffix if scope == "/" else scope + ("" if suffix == "/" else suffix)
        issues.append(
            ValidationIssue(
                code="SCHEMA_VALIDATION_FAILED",
                json_pointer=pointer,
                message=error.message[:1024],
                scope=scope,
                kind=OSDUKind(kind),
                validator=str(error.validator),
                schema_uri=kind,
            )
        )
    return len(errors) > available


def _unavailable_report(
    request: ValidateSchemasInput,
    catalog: LoadedSchemaCatalog | None,
    error: Exception,
    *,
    document_kind: OSDUKind | None = None,
) -> ValidationReport:
    descriptor = catalog.descriptor if catalog is not None else None
    code = (
        error.code
        if isinstance(error, SchemaCatalogError)
        and error.code in {"CHECKSUM_MISMATCH", "SCHEMA_REFERENCE_FAILED"}
        else "SCHEMA_UNAVAILABLE"
    )
    return ValidationReport(
        status=ValidationStatus.UNAVAILABLE,
        schema_conformant=None,
        schema_catalog_id=request.schema_catalog_id,
        schema_revision=descriptor.revision if descriptor else "unavailable",
        catalog_sha256=descriptor.catalog_sha256 if descriptor else "0" * 64,
        catalog_source=descriptor.source if descriptor else "unavailable",
        document_kind=document_kind,
        issues=(
            ValidationIssue(
                code=code,
                json_pointer="/kind",
                message="The pinned schema catalog could not resolve validation.",
                scope="schema",
                kind=document_kind,
                validator="schema-resolution",
            ),
        ),
        validated_record_count=0,
    )


def _check_cancelled(cancellation: Callable[[], bool] | None) -> None:
    if cancellation is not None and cancellation():
        raise SchemaCatalogError("CANCELLED", "Schema validation was cancelled.")


__all__ = [
    "SchemaValidationService",
    "json_pointer",
    "manifest_records",
    "normalize_record_surrogates",
    "surrogate_id_map",
]
