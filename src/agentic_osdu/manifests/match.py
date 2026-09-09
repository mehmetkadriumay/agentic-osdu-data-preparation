"""TOOL-016 versioned explainable five-level manifest matching."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from agentic_osdu.domain.models import (
    DatasetReference,
    ManifestAssociation,
    ReviewStatus,
    TrustLevel,
)
from agentic_osdu.manifests.parse import normalize_manifest_path
from agentic_osdu.tools.contracts import (
    MatchingPolicyVersion,
    MatchManifestInput,
    MatchManifestOutput,
)

_POLICY = b"".join(
    (
        b"exact_dataset_path>exact_path>exact_dataset_filename>",
        b"exact_filename>normalized_identifier",
    )
)
MATCHING_POLICY_V1 = MatchingPolicyVersion(
    version="1.0.0",
    policy_sha256=sha256(_POLICY).hexdigest(),
)
CancellationCheck = Callable[[], bool]


class MatchingError(RuntimeError):
    """Stable matching-policy failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def match_manifest(
    request: MatchManifestInput,
    *,
    cancellation: CancellationCheck | None = None,
) -> MatchManifestOutput:
    if request.matching_policy != MATCHING_POLICY_V1:
        raise MatchingError(
            "MATCH_POLICY_UNAVAILABLE",
            "The requested matching policy is unknown.",
        )
    target_path = normalize_manifest_path(request.file_record.file.relative_path.root)
    target_name = PurePosixPath(target_path).name.casefold()
    target_stem = PurePosixPath(target_name).stem
    references_by_manifest = {
        manifest_id: [
            reference
            for reference in request.manifest_index.dataset_references
            if reference.manifest_id == manifest_id
        ]
        for manifest_id in request.manifest_index.manifest_ids
    }
    documents = {
        document.manifest_id: document for document in request.manifest_index.manifest_documents
    }
    scored: list[ManifestAssociation] = []
    for manifest_id, references in references_by_manifest.items():
        _check_cancelled(cancellation)
        dataset = [
            reference
            for reference in references
            if reference.json_pointer.startswith("/Data/Datasets/")
        ]
        document = documents.get(manifest_id)
        score_method = _score(
            target_path,
            target_name,
            target_stem,
            dataset,
            references,
            document.path.root if document is not None else "",
        )
        if score_method is None:
            continue
        score, method = score_method
        evidence_id = uuid5(
            NAMESPACE_URL,
            f"manifest-match:{MATCHING_POLICY_V1.version}:{manifest_id}:{target_path}:{method}",
        )
        scored.append(
            ManifestAssociation(
                association_id=uuid5(
                    request.file_record.file.file_id,
                    f"{manifest_id}:{MATCHING_POLICY_V1.version}:{method}",
                ),
                file_id=request.file_record.file.file_id,
                manifest_id=manifest_id,
                score=score,
                method=method,
                evidence_ids=(evidence_id,),
                review_status=ReviewStatus.PROPOSED,
                trust_level=(
                    TrustLevel.VERIFIED if method.startswith("exact_") else TrustLevel.HEURISTIC
                ),
                target_version=MATCHING_POLICY_V1.version,
            )
        )
    scored.sort(key=lambda item: (-item.score, str(item.manifest_id)))
    _check_cancelled(cancellation)
    if not scored:
        return MatchManifestOutput(matches=())
    best = scored[0].score
    return MatchManifestOutput(matches=tuple(item for item in scored if item.score == best))


def _score(
    target_path: str,
    target_name: str,
    target_stem: str,
    dataset: Sequence[DatasetReference],
    all_references: Sequence[DatasetReference],
    manifest_path: str,
) -> tuple[float, str] | None:
    dataset_values = [item.normalized_value for item in dataset]
    all_values = [item.normalized_value for item in all_references]
    if any(value == target_path or value.endswith(f"/{target_path}") for value in dataset_values):
        return 1.0, "exact_dataset_path"
    if any(value == target_path or value.endswith(f"/{target_path}") for value in all_values):
        return 0.95, "exact_path"
    qualified_mismatch = any(
        "/" in value
        and PurePosixPath(value).name.casefold() == target_name
        and value != target_path
        and not value.endswith(f"/{target_path}")
        for value in dataset_values
    )
    if not qualified_mismatch and any(
        PurePosixPath(value).name.casefold() == target_name for value in dataset_values
    ):
        return 0.85, "exact_dataset_filename"
    if any(PurePosixPath(value).name.casefold() == target_name for value in all_values):
        return 0.75, "exact_filename"
    normalized_stem = _normalize_name(target_stem)
    manifest_name = _normalize_name(PurePosixPath(manifest_path).stem)
    data_parent = PurePosixPath(target_path).parent.name.casefold()
    manifest_parent = PurePosixPath(manifest_path).parent.name.casefold()
    parent_related = (
        not manifest_parent
        or data_parent in manifest_parent
        or manifest_parent.rstrip("_0123456789") in data_parent
    )
    if len(normalized_stem) >= 8 and normalized_stem in manifest_name and parent_related:
        return 0.55, "normalized_identifier"
    return None


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise MatchingError("CANCELLED", "Manifest matching was cancelled.")
