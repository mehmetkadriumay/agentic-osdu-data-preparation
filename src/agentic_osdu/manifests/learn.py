"""TOOL-017 deterministic cumulative category learning."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agentic_osdu.domain.models import DataCategory, LearningExampleRef, ReviewStatus
from agentic_osdu.manifests.parse import has_generation_lineage, normalize_manifest_path
from agentic_osdu.tools.contracts import (
    FileRecordContract,
    LearningConstant,
    LearningDelta,
    LearningMaterialSnapshot,
    LearningModelContract,
    LearnManifestPatternsInput,
    LearnManifestPatternsOutput,
    ParsedManifest,
)

_CONSTANT_KEYS = {
    "ResourceSecurityClassification",
    "Source",
    "ExistenceKind",
    "IsExtendedLoad",
    "IsDiscoverable",
    "BusinessActivities",
}
CancellationCheck = Callable[[], bool]


class LearningError(RuntimeError):
    """Stable learning eligibility or material failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class LearningMaterial:
    """Resolved reviewed pair used without widening the public tool contract."""

    example: LearningExampleRef
    file_record: FileRecordContract
    manifest: ParsedManifest


def learn_manifest_patterns(
    request: LearnManifestPatternsInput,
    materials: tuple[LearningMaterial, ...],
    *,
    existing_model: LearningModelContract | None = None,
    cancellation: CancellationCheck | None = None,
) -> LearnManifestPatternsOutput:
    """Build one category model with deterministic deduplication and lineage."""

    if existing_model is not None and existing_model.category is not request.category:
        raise LearningError(
            "EXAMPLE_CONFLICT",
            "The existing model category does not match the requested category.",
        )
    for supplied_material in materials:
        _check_cancelled(cancellation)
        _validate_material_category(request.category, supplied_material)
    by_identity = {material.example: material for material in materials}
    existing_identities = {
        example.example_id: example
        for example in (existing_model.example_identities if existing_model is not None else ())
    }
    if (
        existing_model is not None
        and existing_model.example_ids
        and not existing_model.material_snapshots
    ):
        raise LearningError(
            "EXAMPLE_CONFLICT",
            "The existing model lacks normalized historical learning material.",
        )
    existing_snapshots = {
        snapshot.example_id: snapshot
        for snapshot in (existing_model.material_snapshots if existing_model is not None else ())
    }
    added: list[LearningMaterial] = []
    ignored: list[UUID] = []
    seen: set[tuple[UUID, UUID, str, str]] = set()
    eligible: list[LearningMaterial] = []
    for example in request.examples:
        _check_cancelled(cancellation)
        material = by_identity.get(example)
        if material is None:
            raise LearningError("EXAMPLE_CONFLICT", "Learning material is missing.")
        _validate_material(request.category, example, material)
        identity = (
            example.example_id,
            example.association_id,
            example.source_sha256,
            example.manifest_sha256,
        )
        if identity in seen:
            ignored.append(example.example_id)
            continue
        seen.add(identity)
        eligible.append(material)
        persisted = existing_identities.get(example.example_id)
        if persisted is not None and persisted != example:
            raise LearningError(
                "EXAMPLE_CONFLICT",
                "A persisted example ID was reused with changed lineage.",
            )
        if persisted is not None:
            supplied_snapshot = _snapshot(material)
            if existing_snapshots.get(example.example_id) != supplied_snapshot:
                raise LearningError(
                    "EXAMPLE_CONFLICT",
                    "Persisted normalized learning material changed for an existing example.",
                )
            ignored.append(example.example_id)
        else:
            added.append(material)
    if not eligible and existing_model is None:
        raise LearningError("NO_ELIGIBLE_EXAMPLES", "No eligible learning examples were supplied.")
    if existing_model is not None and not added:
        _check_cancelled(cancellation)
        return LearnManifestPatternsOutput(
            model=existing_model,
            delta=LearningDelta(
                added_example_ids=(),
                ignored_duplicate_ids=tuple(ignored),
            ),
        )

    lineage: tuple[UUID, ...] = tuple(
        dict.fromkeys(
            (
                *(existing_model.example_ids if existing_model is not None else ()),
                *(material.example.example_id for material in eligible),
            )
        )
    )
    example_identities = tuple(
        dict.fromkeys(
            (
                *(existing_model.example_identities if existing_model is not None else ()),
                *(material.example for material in eligible),
            )
        )
    )
    material_snapshots = tuple(
        {
            snapshot.example_id: snapshot
            for snapshot in (
                *(existing_model.material_snapshots if existing_model is not None else ()),
                *(_snapshot(material) for material in eligible),
            )
        }.values()
    )
    prototype_material = max(
        material_snapshots,
        key=lambda item: _prototype_rank(item.manifest.content),
        default=None,
    )
    if prototype_material is None:
        if existing_model is None:
            raise LearningError("NO_ELIGIBLE_EXAMPLES", "No prototype manifest is available.")
        _check_cancelled(cancellation)
        return LearnManifestPatternsOutput(
            model=existing_model,
            delta=LearningDelta(
                added_example_ids=(),
                ignored_duplicate_ids=tuple(ignored),
            ),
        )
    document = prototype_material.manifest.model_dump(mode="python")["content"]
    model_version = (existing_model.version + 1) if added and existing_model is not None else 1
    constants = _constants(document, lineage)
    source_path = prototype_material.source_path
    file_prefixes = tuple(
        prefix
        for material in material_snapshots
        if (
            prefix := _file_source_prefix(
                material.manifest.content,
                material.source_path.root,
            )
        )
    )
    payload = {
        "category": request.category.value,
        "example_ids": [str(item) for item in lineage],
        "example_identities": [
            {
                "example_id": str(item.example_id),
                "association_id": str(item.association_id),
                "source_sha256": item.source_sha256,
                "manifest_sha256": item.manifest_sha256,
            }
            for item in example_identities
        ],
        "learning_policy_version": request.learning_policy_version,
        "prototype_sha256": prototype_material.manifest.sha256,
        "prototype_source_path": source_path.root,
        "material_snapshots": [
            {
                "example_id": str(item.example_id),
                "source_path": item.source_path.root,
                "manifest_sha256": item.manifest.sha256,
            }
            for item in material_snapshots
        ],
        "version": model_version,
    }
    model_hash = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    work_product, component, dataset = _top_records(document)
    model = LearningModelContract(
        learning_model_id=uuid5(
            NAMESPACE_URL,
            f"learning:{request.category.value}:{request.learning_policy_version}:{model_hash}",
        ),
        category=request.category,
        version=model_version,
        model_sha256=model_hash,
        example_ids=lineage,
        example_identities=example_identities,
        material_snapshots=material_snapshots,
        prototype=prototype_material.manifest,
        constants=constants,
        prototype_source_path=source_path,
        file_source_prefix=_most_common(file_prefixes),
        work_product_envelope=_copy_envelope(work_product),
        component_envelope=_copy_envelope(component),
        dataset_envelope=_copy_envelope(dataset),
    )
    _check_cancelled(cancellation)
    return LearnManifestPatternsOutput(
        model=model,
        delta=LearningDelta(
            added_example_ids=tuple(material.example.example_id for material in added),
            ignored_duplicate_ids=tuple(ignored),
        ),
    )


def _validate_material(
    requested_category: DataCategory,
    example: LearningExampleRef,
    material: LearningMaterial,
) -> None:
    _validate_material_category(requested_category, material)
    if (
        example.generated_manifest
        or material.manifest.document.generated
        or has_generation_lineage(material.manifest.content.content)
    ):
        raise LearningError(
            "GENERATED_EXAMPLE_REJECTED", "Generated manifests cannot be learned from."
        )
    if example.review_status is not ReviewStatus.APPROVED:
        raise LearningError("EXAMPLE_CONFLICT", "Learning requires an approved association.")
    if (
        example != material.example
        or example.source_file_id != material.file_record.file.file_id
        or material.file_record.file.sha256 is None
        or example.source_sha256 != material.file_record.file.sha256
        or example.manifest_id != material.manifest.document.manifest_id
        or example.manifest_sha256 != material.manifest.document.sha256
    ):
        raise LearningError("EXAMPLE_CONFLICT", "Learning lineage does not match its material.")


def _validate_material_category(
    requested_category: DataCategory,
    material: LearningMaterial,
) -> None:
    classification = material.file_record.classification
    if (
        classification is None
        or classification.file_id != material.file_record.file.file_id
        or classification.category is not requested_category
    ):
        raise LearningError(
            "EXAMPLE_CONFLICT",
            "Learning requires typed source classification matching the requested category.",
        )


def _snapshot(material: LearningMaterial) -> LearningMaterialSnapshot:
    return LearningMaterialSnapshot(
        example_id=material.example.example_id,
        source_path=material.file_record.file.relative_path,
        manifest=material.manifest.content,
    )


def _prototype_rank(document: dict[str, Any]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    _, component, dataset = _top_records(document)
    return (
        _version_tuple(str(component.get("kind", "")) if component else ""),
        _version_tuple(str(dataset.get("kind", "")) if dataset else ""),
    )


def _version_tuple(kind: str) -> tuple[int, ...]:
    match = re.search(r":(\d+(?:\.\d+)*)$", kind)
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def _top_records(
    document: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    data = document.get("Data")
    if not isinstance(data, dict):
        return None, None, None
    work_product = data.get("WorkProduct")
    components = data.get("WorkProductComponents")
    datasets = data.get("Datasets")
    component = (
        next((item for item in components if isinstance(item, dict)), None)
        if isinstance(components, list)
        else None
    )
    dataset = (
        next((item for item in datasets if isinstance(item, dict)), None)
        if isinstance(datasets, list)
        else None
    )
    return (
        work_product if isinstance(work_product, dict) else None,
        component,
        dataset,
    )


def _copy_envelope(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {}
    return {key: copy.deepcopy(record[key]) for key in ("acl", "legal") if key in record}


def _constants(
    document: dict[str, Any],
    lineage: tuple[UUID, ...],
) -> tuple[LearningConstant, ...]:
    result: list[LearningConstant] = []
    for pointer, record in (
        ("/Data/WorkProduct", _top_records(document)[0]),
        ("/Data/WorkProductComponents/0", _top_records(document)[1]),
        ("/Data/Datasets/0", _top_records(document)[2]),
    ):
        data = record.get("data") if isinstance(record, dict) else None
        if not isinstance(data, dict):
            continue
        for key in sorted(_CONSTANT_KEYS & data.keys()):
            value = data[key]
            if isinstance(value, str | int | float | bool) or value is None:
                result.append(
                    LearningConstant(
                        json_pointer=f"{pointer}/data/{key}",
                        value=value,
                        source_example_ids=tuple(lineage),
                    )
                )
    return tuple(result)


def _file_source_prefix(document: dict[str, Any], source_path: str) -> str | None:
    target = normalize_manifest_path(source_path)
    for _, value in _walk_strings(document):
        normalized = value.replace("\\", "/")
        index = normalize_manifest_path(normalized).find(target)
        if index >= 0:
            raw_index = normalized.casefold().find(source_path.replace("\\", "/").casefold())
            return normalized[:raw_index] if raw_index >= 0 else normalized[:index]
    return None


def _walk_strings(value: object, pointer: str = "") -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, str):
        result.append((pointer, value))
    elif isinstance(value, dict):
        for key, child in value.items():
            result.extend(_walk_strings(child, f"{pointer}/{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_walk_strings(child, f"{pointer}/{index}"))
    return result


def _most_common(values: tuple[str, ...]) -> str:
    counts = Counter(value for value in values if value)
    return counts.most_common(1)[0][0] if counts else ""


def _check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise LearningError("CANCELLED", "Manifest learning was cancelled.")
