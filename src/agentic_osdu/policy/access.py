"""Policy-only path authorization without file discovery or domain I/O."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Never, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from agentic_osdu.domain.models import ContractModel, Rfc3339Timestamp, TrustLevel

_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|"
    r"COM(?:[1-9]|\u00b9|\u00b2|\u00b3)|"
    r"LPT(?:[1-9]|\u00b9|\u00b2|\u00b3))(?:\..*)?$",
    re.IGNORECASE,
)
_WINDOWS_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')
_SAFE_HOST = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


class PathStyle(StrEnum):
    WINDOWS = "windows"
    POSIX = "posix"


class PolicyErrorCode(StrEnum):
    ROOT_NOT_ABSOLUTE = "ROOT_NOT_ABSOLUTE"
    ROOT_NOT_APPROVED = "ROOT_NOT_APPROVED"
    TRAVERSAL_DENIED = "TRAVERSAL_DENIED"
    ABSOLUTE_CHILD_DENIED = "ABSOLUTE_CHILD_DENIED"
    DRIVE_RELATIVE_DENIED = "DRIVE_RELATIVE_DENIED"
    ROOT_ESCAPE_DENIED = "ROOT_ESCAPE_DENIED"
    PATH_OUTSIDE_ROOT = "PATH_OUTSIDE_ROOT"
    UNC_DENIED = "UNC_DENIED"
    DEVICE_PATH_DENIED = "DEVICE_PATH_DENIED"
    ALTERNATE_DATA_STREAM_DENIED = "ALTERNATE_DATA_STREAM_DENIED"
    RESERVED_NAME_DENIED = "RESERVED_NAME_DENIED"
    TRAILING_DOT_SPACE_DENIED = "TRAILING_DOT_SPACE_DENIED"
    INVALID_COMPONENT = "INVALID_COMPONENT"
    LINK_NOT_ALLOWED = "LINK_NOT_ALLOWED"
    LINK_ESCAPE_DENIED = "LINK_ESCAPE_DENIED"
    SOURCE_READ_ONLY = "SOURCE_READ_ONLY"
    OUTPUT_ROOT_NOT_APPROVED = "OUTPUT_ROOT_NOT_APPROVED"
    OUTPUT_ROOT_OVERLAPS_SOURCE = "OUTPUT_ROOT_OVERLAPS_SOURCE"
    NETWORK_DISABLED = "NETWORK_DISABLED"
    NETWORK_APPROVAL_REQUIRED = "NETWORK_APPROVAL_REQUIRED"
    NETWORK_APPROVAL_MISMATCH = "NETWORK_APPROVAL_MISMATCH"
    NETWORK_APPROVAL_NOT_ACTIVE = "NETWORK_APPROVAL_NOT_ACTIVE"
    NETWORK_APPROVAL_EXPIRED = "NETWORK_APPROVAL_EXPIRED"
    NETWORK_TIME_INVALID = "NETWORK_TIME_INVALID"
    TRUST_UPGRADE_DENIED = "TRUST_UPGRADE_DENIED"
    PATH_INSPECTION_FAILED = "PATH_INSPECTION_FAILED"


class PolicyError(ContractModel):
    """Structured, log-safe policy failure with no raw path value."""

    code: PolicyErrorCode
    message: str = Field(min_length=1, max_length=512)
    safe_context: dict[str, str] = Field(default_factory=dict)


class PolicyViolation(Exception):
    """Exception boundary carrying a structured policy error only."""

    def __init__(self, error: PolicyError) -> None:
        self.error = error
        super().__init__(f"{error.code.value}: {error.message}")

    def __repr__(self) -> str:
        return f"PolicyViolation(code={self.error.code.value!r})"


class LinkInspectionError(Exception):
    """Path inspection failed without retaining the sensitive path or host error."""


class LinkInspector(Protocol):
    """Project-owned boundary for symlink and Windows reparse-point inspection."""

    def is_link_or_reparse(self, path: str) -> bool:
        """Return whether an existing path component is a link or reparse point."""

    def resolve(self, path: str) -> str:
        """Resolve a path through the host filesystem."""


class PathPolicy(Protocol):
    """Interface for canonical approved-root and child authorization."""

    def approve_root(self, root: str) -> str: ...

    def contains(self, root: str, candidate: str) -> bool: ...

    def authorize_child(
        self,
        root: str,
        requested: str,
        *,
        follow_links: bool = False,
        root_id: str = "approved",
        read_only: bool = True,
    ) -> AuthorizedPath: ...


class WorkspacePolicy(Protocol):
    """Interface exposing read-only workspace capabilities."""

    def authorize_read(
        self,
        relative_path: str,
        *,
        follow_links: bool = False,
    ) -> AuthorizedPath: ...


class OutputPolicy(Protocol):
    """Interface exposing writes only through named approved output roots."""

    def authorize_output(
        self,
        root_id: str,
        relative_path: str,
        *,
        follow_links: bool = False,
    ) -> AuthorizedPath: ...


class NetworkAccessPolicy(Protocol):
    """Interface for explicit, scoped network authorization."""

    def authorize(
        self,
        request: NetworkRequest,
        *,
        approval: NetworkApproval | None = None,
        now: datetime | None = None,
    ) -> NetworkDecision: ...


class TrustPolicy(Protocol):
    """Interface for bounded trust-level transitions."""

    def authorize_transition(
        self,
        current: TrustLevel,
        target: TrustLevel,
        *,
        human_review: bool = False,
        schema_conformance: bool = False,
    ) -> TrustLevel: ...


@dataclass(frozen=True, slots=True)
class DefaultLinkInspector:
    """Host implementation covering symlinks, junctions, and other reparse points."""

    def is_link_or_reparse(self, path: str) -> bool:
        try:
            metadata = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            raise LinkInspectionError from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)

    def resolve(self, path: str) -> str:
        try:
            return str(Path(path).resolve(strict=False))
        except (OSError, RuntimeError):
            raise LinkInspectionError from None


@dataclass(frozen=True, slots=True)
class AuthorizedPath:
    """An authorization decision, not a file read/write operation."""

    canonical_path: str
    canonical_root: str
    relative_path: str
    root_id: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class WindowsAwarePathPolicy:
    """Canonical lexical containment policy with explicit Windows semantics."""

    style: PathStyle = PathStyle.WINDOWS
    allow_unc_roots: bool = False
    inspector: LinkInspector = DefaultLinkInspector()

    def _raise(
        self,
        code: PolicyErrorCode,
        message: str,
        **safe_context: str,
    ) -> Never:
        raise PolicyViolation(PolicyError(code=code, message=message, safe_context=safe_context))

    def _is_link_or_reparse(self, path: str, *, path_classification: str) -> bool:
        try:
            return self.inspector.is_link_or_reparse(path)
        except (LinkInspectionError, OSError):
            self._raise(
                PolicyErrorCode.PATH_INSPECTION_FAILED,
                "The path could not be inspected safely.",
                path_classification=path_classification,
            )

    def _resolve(self, path: str, *, path_classification: str) -> str:
        try:
            return self.inspector.resolve(path)
        except (LinkInspectionError, OSError, RuntimeError):
            self._raise(
                PolicyErrorCode.PATH_INSPECTION_FAILED,
                "The path could not be resolved safely.",
                path_classification=path_classification,
            )

    def approve_root(self, root: str) -> str:
        """Validate and normalize an explicitly configured absolute root."""

        if not root or "\x00" in root:
            self._raise(
                PolicyErrorCode.ROOT_NOT_ABSOLUTE,
                "An approved root must be a non-empty absolute path.",
                path_classification="invalid_root",
            )
        if self.style is PathStyle.WINDOWS:
            return self._approve_windows_root(root)
        if not posixpath.isabs(root):
            self._raise(
                PolicyErrorCode.ROOT_NOT_ABSOLUTE,
                "An approved root must be absolute.",
                path_classification="relative_root",
            )
        return posixpath.normpath(root)

    def _approve_windows_root(self, root: str) -> str:
        normalized_separators = root.replace("/", "\\")
        lowered = normalized_separators.casefold()
        if lowered.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\??\\")):
            self._raise(
                PolicyErrorCode.DEVICE_PATH_DENIED,
                "Windows device and extended-length paths are prohibited.",
                path_classification="device",
            )
        is_unc = normalized_separators.startswith("\\\\")
        if is_unc and not self.allow_unc_roots:
            self._raise(
                PolicyErrorCode.UNC_DENIED,
                "UNC roots require an explicit opt-in policy.",
                path_classification="unc",
            )
        drive, tail = ntpath.splitdrive(normalized_separators)
        if not is_unc and (not drive or not tail.startswith("\\")):
            self._raise(
                PolicyErrorCode.ROOT_NOT_ABSOLUTE,
                "An approved Windows root must include an absolute drive or UNC share.",
                path_classification="relative_root",
            )
        normalized = ntpath.normpath(normalized_separators)
        root_components = (
            tuple(normalized[2:].split("\\")) if is_unc else self._root_components(normalized)
        )
        self._validate_windows_components(root_components, root_context=True)
        return normalized

    def _root_components(self, path: str) -> tuple[str, ...]:
        drive, tail = ntpath.splitdrive(path)
        del drive
        return tuple(component for component in tail.split("\\") if component)

    def _validate_windows_child(self, child: str) -> tuple[str, ...]:
        normalized = child.replace("/", "\\")
        lowered = normalized.casefold()
        if lowered.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\??\\")):
            self._raise(
                PolicyErrorCode.DEVICE_PATH_DENIED,
                "Windows device and extended-length paths are prohibited.",
                path_classification="device",
            )
        if normalized.startswith("\\\\"):
            self._raise(
                PolicyErrorCode.UNC_DENIED,
                "A child path cannot introduce a UNC root.",
                path_classification="unc",
            )
        drive, tail = ntpath.splitdrive(normalized)
        if drive:
            code = (
                PolicyErrorCode.ABSOLUTE_CHILD_DENIED
                if tail.startswith("\\")
                else PolicyErrorCode.DRIVE_RELATIVE_DENIED
            )
            self._raise(
                code,
                "A child path cannot introduce a drive.",
                path_classification="absolute_child" if tail.startswith("\\") else "drive_relative",
            )
        if normalized.startswith("\\"):
            self._raise(
                PolicyErrorCode.ROOT_ESCAPE_DENIED,
                "A root-relative child path is prohibited.",
                path_classification="root_relative",
            )
        components = tuple(normalized.split("\\"))
        if any(component == ".." for component in components):
            self._raise(
                PolicyErrorCode.TRAVERSAL_DENIED,
                "Parent traversal is prohibited before canonicalization.",
                path_classification="traversal",
            )
        self._validate_windows_components(components, root_context=False)
        return components

    def _validate_windows_components(
        self,
        components: tuple[str, ...],
        *,
        root_context: bool,
    ) -> None:
        for index, component in enumerate(components):
            context = {
                "path_classification": "invalid_component",
                "component_index": str(index),
                "root_context": str(root_context).lower(),
            }
            if component in {"", "."}:
                self._raise(
                    PolicyErrorCode.INVALID_COMPONENT,
                    "Empty and current-directory components are prohibited.",
                    **context,
                )
            if component.endswith((" ", ".")):
                self._raise(
                    PolicyErrorCode.TRAILING_DOT_SPACE_DENIED,
                    "Windows components cannot end in a dot or space.",
                    **context,
                )
            if ":" in component:
                self._raise(
                    PolicyErrorCode.ALTERNATE_DATA_STREAM_DENIED,
                    "Alternate data stream syntax is prohibited.",
                    **context,
                )
            if _WINDOWS_RESERVED.fullmatch(component):
                self._raise(
                    PolicyErrorCode.RESERVED_NAME_DENIED,
                    "Reserved Windows device names are prohibited.",
                    **context,
                )
            if _WINDOWS_INVALID.search(component):
                self._raise(
                    PolicyErrorCode.INVALID_COMPONENT,
                    "The path contains a prohibited Windows component.",
                    **context,
                )

    def _validate_posix_child(self, child: str) -> tuple[str, ...]:
        if posixpath.isabs(child):
            self._raise(
                PolicyErrorCode.ABSOLUTE_CHILD_DENIED,
                "A child path must remain relative.",
                path_classification="absolute_child",
            )
        components = tuple(child.split("/"))
        if any(component == ".." for component in components):
            self._raise(
                PolicyErrorCode.TRAVERSAL_DENIED,
                "Parent traversal is prohibited before canonicalization.",
                path_classification="traversal",
            )
        if any(
            not component or component == "." or "\x00" in component for component in components
        ):
            self._raise(
                PolicyErrorCode.INVALID_COMPONENT,
                "The path contains an invalid component.",
                path_classification="invalid_component",
            )
        return components

    def contains(self, root: str, candidate: str) -> bool:
        """Return segment-aware containment with Windows case folding."""

        if self.style is PathStyle.WINDOWS:
            normalized_root = ntpath.normcase(ntpath.normpath(root))
            normalized_candidate = ntpath.normcase(ntpath.normpath(candidate))
            try:
                return ntpath.commonpath((normalized_root, normalized_candidate)) == normalized_root
            except ValueError:
                return False
        normalized_root = posixpath.normpath(root)
        normalized_candidate = posixpath.normpath(candidate)
        try:
            return posixpath.commonpath((normalized_root, normalized_candidate)) == normalized_root
        except ValueError:
            return False

    def _root_component_paths(self, root: str) -> tuple[str, ...]:
        if self.style is PathStyle.WINDOWS:
            drive, tail = ntpath.splitdrive(root)
            current = f"{drive}\\" if drive and not drive.startswith("\\\\") else drive
            paths: list[str] = []
            for component in (part for part in tail.split("\\") if part):
                current = ntpath.join(current, component)
                paths.append(current)
            return tuple(paths) or (root,)
        current = "/"
        paths = []
        for component in (part for part in root.split("/") if part):
            current = posixpath.join(current, component)
            paths.append(current)
        return tuple(paths) or (root,)

    def authorize_child(
        self,
        root: str,
        requested: str,
        *,
        follow_links: bool = False,
        root_id: str = "approved",
        read_only: bool = True,
    ) -> AuthorizedPath:
        """Authorize a normalized child path without opening it."""

        canonical_root = self.approve_root(root)
        root_has_link = any(
            self._is_link_or_reparse(
                component_path,
                path_classification="approved_root_inspection",
            )
            for component_path in self._root_component_paths(canonical_root)
        )
        if root_has_link and not follow_links:
            self._raise(
                PolicyErrorCode.LINK_NOT_ALLOWED,
                "Approved roots containing links or reparse points are not followed by default.",
                path_classification="approved_root_link",
            )
        if root_has_link:
            canonical_root = self.approve_root(
                self._resolve(
                    canonical_root,
                    path_classification="approved_root_resolution",
                )
            )
        if not requested or "\x00" in requested:
            self._raise(
                PolicyErrorCode.INVALID_COMPONENT,
                "A requested child path must be non-empty.",
                path_classification="invalid_component",
            )
        if self.style is PathStyle.WINDOWS:
            components = self._validate_windows_child(requested)
            relative = "\\".join(components)
            candidate = ntpath.normpath(ntpath.join(canonical_root, relative))
            join = ntpath.join
        else:
            components = self._validate_posix_child(requested)
            relative = "/".join(components)
            candidate = posixpath.normpath(posixpath.join(canonical_root, relative))
            join = posixpath.join
        if not self.contains(canonical_root, candidate):
            self._raise(
                PolicyErrorCode.PATH_OUTSIDE_ROOT,
                "The requested path is outside its approved root.",
                path_classification="containment_escape",
            )

        current = canonical_root
        link_found = False
        for component in components:
            current = join(current, component)
            if self._is_link_or_reparse(
                current,
                path_classification="child_component_inspection",
            ):
                link_found = True
                if not follow_links:
                    self._raise(
                        PolicyErrorCode.LINK_NOT_ALLOWED,
                        "Symlinks and reparse points are not followed by default.",
                        path_classification="link_or_reparse",
                    )
        if link_found and follow_links:
            resolved = self._resolve(
                candidate,
                path_classification="child_link_resolution",
            )
            normalized_resolved = (
                ntpath.normpath(resolved)
                if self.style is PathStyle.WINDOWS
                else posixpath.normpath(resolved)
            )
            if not self.contains(canonical_root, normalized_resolved):
                self._raise(
                    PolicyErrorCode.LINK_ESCAPE_DENIED,
                    "A followed link resolves outside its approved root.",
                    path_classification="link_escape",
                )
            candidate = normalized_resolved

        return AuthorizedPath(
            canonical_path=candidate,
            canonical_root=canonical_root,
            relative_path=relative,
            root_id=root_id,
            read_only=read_only,
        )


class WorkspaceAccessPolicy:
    """Immutable approved roots with separate source-read and output-write capabilities."""

    __slots__ = (
        "_output_roots",
        "_path_policy",
        "_source_identity",
        "_source_output_root_ids",
        "_source_root",
        "workspace_id",
    )

    def __init__(
        self,
        *,
        workspace_id: UUID,
        source_root: str,
        output_roots: Mapping[str, str],
        path_policy: WindowsAwarePathPolicy,
        allowed_source_output_subpaths: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self._path_policy = path_policy
        self._source_root = path_policy.approve_root(source_root)
        self._source_identity = path_policy.approve_root(
            path_policy._resolve(
                self._source_root,
                path_classification="source_root_resolution",
            )
        )
        approved_subpaths = allowed_source_output_subpaths or {}
        approved_outputs: dict[str, str] = {}
        source_output_root_ids: set[str] = set()
        for root_id, output_root in output_roots.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", root_id):
                raise ValueError("output root IDs must be stable lowercase identifiers")
            canonical = path_policy.approve_root(output_root)
            output_identity = path_policy.approve_root(
                path_policy._resolve(
                    canonical,
                    path_classification="output_root_resolution",
                )
            )
            output_contains_source = path_policy.contains(
                output_identity,
                self._source_identity,
            )
            source_contains_output = path_policy.contains(
                self._source_identity,
                output_identity,
            )
            if output_contains_source:
                raise PolicyViolation(
                    PolicyError(
                        code=PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE,
                        message="An output root cannot contain read-only source data.",
                        safe_context={"root_id": root_id},
                    )
                )
            if source_contains_output:
                allowed = approved_subpaths.get(root_id)
                if allowed is None:
                    raise PolicyViolation(
                        PolicyError(
                            code=PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE,
                            message=(
                                "An output under the read-only source root requires an "
                                "explicit approved output subpath."
                            ),
                            safe_context={"root_id": root_id},
                        )
                    )
                expected = path_policy.authorize_child(
                    self._source_root,
                    allowed,
                    root_id=root_id,
                    read_only=False,
                ).canonical_path
                expected_identity = path_policy.approve_root(
                    path_policy._resolve(
                        expected,
                        path_classification="output_subpath_resolution",
                    )
                )
                matches = (
                    ntpath.normcase(expected_identity) == ntpath.normcase(output_identity)
                    if path_policy.style is PathStyle.WINDOWS
                    else expected_identity == output_identity
                )
                if not matches:
                    raise PolicyViolation(
                        PolicyError(
                            code=PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE,
                            message="Configured output root does not match its approved subpath.",
                            safe_context={"root_id": root_id},
                        )
                    )
                source_output_root_ids.add(root_id)
            approved_outputs[root_id] = canonical
        self._output_roots = MappingProxyType(approved_outputs)
        self._source_output_root_ids = frozenset(source_output_root_ids)

    @property
    def source_root(self) -> str:
        return self._source_root

    @property
    def output_roots(self) -> Mapping[str, str]:
        return self._output_roots

    def authorize_read(
        self,
        relative_path: str,
        *,
        follow_links: bool = False,
    ) -> AuthorizedPath:
        return self._path_policy.authorize_child(
            self._source_root,
            relative_path,
            follow_links=follow_links,
            root_id="source",
            read_only=True,
        )

    def authorize_output(
        self,
        root_id: str,
        relative_path: str,
        *,
        follow_links: bool = False,
    ) -> AuthorizedPath:
        if root_id == "source":
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.SOURCE_READ_ONLY,
                    message="Source data is read-only.",
                    safe_context={"root_id": "source"},
                )
            )
        root = self._output_roots.get(root_id)
        if root is None:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.OUTPUT_ROOT_NOT_APPROVED,
                    message="The requested output root is not explicitly approved.",
                    safe_context={"root_id": "unknown"},
                )
            )
        return self._path_policy.authorize_child(
            root,
            relative_path,
            follow_links=follow_links,
            root_id=root_id,
            read_only=False,
        )


class NetworkPurpose(StrEnum):
    SCHEMA_CATALOG_REFRESH = "schema_catalog_refresh"
    GITHUB_OPERATION = "github_operation"


class NetworkRequest(ContractModel):
    purpose: NetworkPurpose
    host: str = Field(min_length=1, max_length=253)

    @model_validator(mode="after")
    def host_is_safe(self) -> NetworkRequest:
        if not _SAFE_HOST.fullmatch(self.host):
            raise ValueError("network host must be a bounded DNS name")
        return self


class NetworkApproval(ContractModel):
    approval_id: UUID
    purpose: NetworkPurpose
    approved_hosts: tuple[str, ...]
    approved_at: Rfc3339Timestamp
    expires_at: Rfc3339Timestamp
    actor_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_approval_window(self) -> NetworkApproval:
        if self.expires_at <= self.approved_at:
            raise ValueError("network approval must expire after it is granted")
        if not self.approved_hosts or any(
            not _SAFE_HOST.fullmatch(host) for host in self.approved_hosts
        ):
            raise ValueError("network approval requires explicit valid hosts")
        return self


@dataclass(frozen=True, slots=True)
class NetworkDecision:
    approved: bool
    approval_id: UUID
    purpose: NetworkPurpose
    host: str


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Default-disabled network policy requiring a scoped, live approval."""

    enabled: bool = False

    def authorize(
        self,
        request: NetworkRequest,
        *,
        approval: NetworkApproval | None = None,
        now: datetime | None = None,
    ) -> NetworkDecision:
        if not self.enabled:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_DISABLED,
                    message="Network access is disabled by default.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        if approval is None:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_APPROVAL_REQUIRED,
                    message="Network access requires explicit approval.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_TIME_INVALID,
                    message="Network authorization requires a timezone-aware clock.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        if current_time < approval.approved_at:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_APPROVAL_NOT_ACTIVE,
                    message="Network approval is not active yet.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        if current_time >= approval.expires_at:
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_APPROVAL_EXPIRED,
                    message="Network approval has expired.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        approved_hosts = {host.casefold().rstrip(".") for host in approval.approved_hosts}
        if (
            approval.purpose is not request.purpose
            or request.host.casefold().rstrip(".") not in approved_hosts
        ):
            raise PolicyViolation(
                PolicyError(
                    code=PolicyErrorCode.NETWORK_APPROVAL_MISMATCH,
                    message="Network approval does not match the requested purpose and host.",
                    safe_context={"purpose": request.purpose.value},
                )
            )
        return NetworkDecision(
            approved=True,
            approval_id=approval.approval_id,
            purpose=request.purpose,
            host=request.host,
        )


class DefaultTrustPolicy:
    """Trust transitions that never let review assert domain authority."""

    _rank: ClassVar[Mapping[TrustLevel, int]] = {
        TrustLevel.UNTRUSTED: 0,
        TrustLevel.HEURISTIC: 1,
        TrustLevel.DERIVED: 2,
        TrustLevel.VERIFIED: 3,
        TrustLevel.AUTHORITATIVE: 4,
    }

    def authorize_transition(
        self,
        current: TrustLevel,
        target: TrustLevel,
        *,
        human_review: bool = False,
        schema_conformance: bool = False,
    ) -> TrustLevel:
        if self._rank[target] <= self._rank[current]:
            return target
        approved_review_upgrade = (
            human_review
            and target is TrustLevel.VERIFIED
            and current in {TrustLevel.HEURISTIC, TrustLevel.DERIVED}
        )
        approved_schema_claim = (
            schema_conformance
            and target is TrustLevel.AUTHORITATIVE
            and current is TrustLevel.VERIFIED
        )
        if approved_review_upgrade or approved_schema_claim:
            return target
        raise PolicyViolation(
            PolicyError(
                code=PolicyErrorCode.TRUST_UPGRADE_DENIED,
                message="The requested trust upgrade is not permitted by explicit policy.",
                safe_context={
                    "current": current.value,
                    "target": target.value,
                },
            )
        )


__all__ = [
    "AuthorizedPath",
    "DefaultLinkInspector",
    "DefaultTrustPolicy",
    "LinkInspectionError",
    "LinkInspector",
    "NetworkAccessPolicy",
    "NetworkApproval",
    "NetworkDecision",
    "NetworkPolicy",
    "NetworkPurpose",
    "NetworkRequest",
    "OutputPolicy",
    "PathPolicy",
    "PathStyle",
    "PolicyError",
    "PolicyErrorCode",
    "PolicyViolation",
    "TrustPolicy",
    "WindowsAwarePathPolicy",
    "WorkspaceAccessPolicy",
    "WorkspacePolicy",
]
