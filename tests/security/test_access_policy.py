from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import TrustLevel
from agentic_osdu.policy import (
    DefaultLinkInspector,
    DefaultTrustPolicy,
    LinkInspector,
    NetworkApproval,
    NetworkPolicy,
    NetworkPurpose,
    NetworkRequest,
    PathStyle,
    PolicyErrorCode,
    PolicyViolation,
    WindowsAwarePathPolicy,
    WorkspaceAccessPolicy,
)


@dataclass(frozen=True)
class FakeLinkInspector(LinkInspector):
    reparse_component: str | None = None
    resolved_path: str | None = None

    def is_link_or_reparse(self, path: str) -> bool:
        return self.reparse_component is not None and path.casefold().endswith(
            self.reparse_component.casefold()
        )

    def resolve(self, path: str) -> str:
        return self.resolved_path or path


def assert_policy_code(
    policy: WindowsAwarePathPolicy,
    root: str,
    requested: str,
    expected: PolicyErrorCode,
) -> None:
    with pytest.raises(PolicyViolation) as caught:
        policy.authorize_child(root, requested)
    assert caught.value.error.code is expected
    assert root not in str(caught.value)
    assert requested not in str(caught.value)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../secret", PolicyErrorCode.TRAVERSAL_DENIED),
        (r"..\secret", PolicyErrorCode.TRAVERSAL_DENIED),
        (r"C:\escape", PolicyErrorCode.ABSOLUTE_CHILD_DENIED),
        (r"C:drive-relative", PolicyErrorCode.DRIVE_RELATIVE_DENIED),
        (r"\rooted", PolicyErrorCode.ROOT_ESCAPE_DENIED),
        (r"\\server\share\file", PolicyErrorCode.UNC_DENIED),
        (r"\\?\C:\Data\file", PolicyErrorCode.DEVICE_PATH_DENIED),
        (r"\\.\PhysicalDrive0", PolicyErrorCode.DEVICE_PATH_DENIED),
        (r"\??\C:\Data\file", PolicyErrorCode.DEVICE_PATH_DENIED),
        ("file.txt:stream", PolicyErrorCode.ALTERNATE_DATA_STREAM_DENIED),
        ("CON.txt", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("CONIN$", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("CONOUT$", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("CLOCK$", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("COM\u00b9.txt", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("LPT\u00b2.log", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("aux", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("nested/LPT9.log", PolicyErrorCode.RESERVED_NAME_DENIED),
        ("trailing.", PolicyErrorCode.TRAILING_DOT_SPACE_DENIED),
        ("trailing ", PolicyErrorCode.TRAILING_DOT_SPACE_DENIED),
        ("bad<name", PolicyErrorCode.INVALID_COMPONENT),
        ("bad|name", PolicyErrorCode.INVALID_COMPONENT),
    ],
)
def test_windows_hostile_paths_fail_closed(path: str, code: PolicyErrorCode) -> None:
    assert_policy_code(
        WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        r"C:\Approved\Data",
        path,
        code,
    )


def test_windows_containment_is_case_insensitive_and_not_prefix_based() -> None:
    policy = WindowsAwarePathPolicy(style=PathStyle.WINDOWS)
    allowed = policy.authorize_child(r"C:\Approved\Data", r"Survey\File.SGY")
    assert allowed.canonical_path == r"C:\Approved\Data\Survey\File.SGY"
    assert policy.contains(r"C:\APPROVED\data", r"c:\approved\DATA\survey\file.sgy")
    assert not policy.contains(r"C:\Approved\Data", r"C:\Approved\Data-escape\file")
    assert_policy_code(
        policy,
        r"C:\Approved\Data",
        r"D:\other\file",
        PolicyErrorCode.ABSOLUTE_CHILD_DENIED,
    )


def test_approved_roots_must_be_explicit_absolute_and_unc_is_opt_in() -> None:
    policy = WindowsAwarePathPolicy(style=PathStyle.WINDOWS)
    with pytest.raises(PolicyViolation) as relative:
        policy.approve_root("relative")
    assert relative.value.error.code is PolicyErrorCode.ROOT_NOT_ABSOLUTE
    with pytest.raises(PolicyViolation) as unc:
        policy.approve_root(r"\\server\share")
    assert unc.value.error.code is PolicyErrorCode.UNC_DENIED

    unc_policy = WindowsAwarePathPolicy(style=PathStyle.WINDOWS, allow_unc_roots=True)
    root = unc_policy.approve_root(r"\\server\share\approved")
    child = unc_policy.authorize_child(root, "folder/file.json")
    assert child.canonical_path == r"\\server\share\approved\folder\file.json"
    assert_policy_code(
        unc_policy,
        root,
        r"\other-share",
        PolicyErrorCode.ROOT_ESCAPE_DENIED,
    )


def test_symlinks_and_windows_reparse_points_are_not_followed_by_default() -> None:
    policy = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=FakeLinkInspector(reparse_component="link"),
    )
    assert_policy_code(
        policy,
        r"C:\Approved",
        r"folder\link\file",
        PolicyErrorCode.LINK_NOT_ALLOWED,
    )


def test_followed_link_or_junction_must_still_resolve_inside_root() -> None:
    policy = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=FakeLinkInspector(
            reparse_component="junction",
            resolved_path=r"D:\Outside\file",
        ),
    )
    with pytest.raises(PolicyViolation) as escaped:
        policy.authorize_child(
            r"C:\Approved",
            r"folder\junction\file",
            follow_links=True,
        )
    assert escaped.value.error.code is PolicyErrorCode.LINK_ESCAPE_DENIED

    contained = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=FakeLinkInspector(
            reparse_component="junction",
            resolved_path=r"C:\Approved\inside\file",
        ),
    ).authorize_child(
        r"C:\Approved",
        r"folder\junction\file",
        follow_links=True,
    )
    assert contained.canonical_path == r"C:\Approved\inside\file"


def test_approved_root_components_are_also_subject_to_no_follow_policy() -> None:
    policy = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=FakeLinkInspector(reparse_component="link-root"),
    )
    with pytest.raises(PolicyViolation) as linked_root:
        policy.authorize_child(r"C:\Approved\link-root", "file.sgy")
    assert linked_root.value.error.code is PolicyErrorCode.LINK_NOT_ALLOWED


def test_workspace_policy_separates_read_only_source_and_configured_outputs() -> None:
    paths = WindowsAwarePathPolicy(style=PathStyle.WINDOWS)
    workspace = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=r"C:\Workspace\Data",
        output_roots={
            "state": r"C:\WorkspaceState",
            "generated": r"C:\Exports\Generated",
        },
        path_policy=paths,
    )
    source = workspace.authorize_read("Survey/input.sgy")
    output = workspace.authorize_output("generated", "Survey/candidate.json")
    assert source.read_only is True
    assert output.read_only is False
    assert output.root_id == "generated"

    with pytest.raises(PolicyViolation) as source_write:
        workspace.authorize_output("source", "overwrite.sgy")
    assert source_write.value.error.code is PolicyErrorCode.SOURCE_READ_ONLY
    with pytest.raises(PolicyViolation) as unknown:
        workspace.authorize_output("unapproved", "file.json")
    assert unknown.value.error.code is PolicyErrorCode.OUTPUT_ROOT_NOT_APPROVED
    with pytest.raises(PolicyViolation) as escape:
        workspace.authorize_output("generated", "../outside.json")
    assert escape.value.error.code is PolicyErrorCode.TRAVERSAL_DENIED


def test_network_is_disabled_by_default_and_requires_matching_live_approval() -> None:
    request = NetworkRequest(
        purpose=NetworkPurpose.SCHEMA_CATALOG_REFRESH,
        host="schemas.example.test",
    )
    with pytest.raises(PolicyViolation) as disabled:
        NetworkPolicy().authorize(request)
    assert disabled.value.error.code is PolicyErrorCode.NETWORK_DISABLED

    enabled = NetworkPolicy(enabled=True)
    with pytest.raises(PolicyViolation) as unapproved:
        enabled.authorize(request)
    assert unapproved.value.error.code is PolicyErrorCode.NETWORK_APPROVAL_REQUIRED

    now = datetime.now(UTC)
    approval = NetworkApproval(
        approval_id=uuid4(),
        purpose=request.purpose,
        approved_hosts=(request.host,),
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
        actor_id="local-operator",
    )
    assert enabled.authorize(request, approval=approval, now=now).approved is True
    with pytest.raises(PolicyViolation) as mismatch:
        enabled.authorize(
            NetworkRequest(
                purpose=NetworkPurpose.GITHUB_OPERATION,
                host=request.host,
            ),
            approval=approval,
            now=now,
        )
    assert mismatch.value.error.code is PolicyErrorCode.NETWORK_APPROVAL_MISMATCH
    with pytest.raises(PolicyViolation) as expired:
        enabled.authorize(request, approval=approval, now=approval.expires_at)
    assert expired.value.error.code is PolicyErrorCode.NETWORK_APPROVAL_EXPIRED
    future_approval = NetworkApproval(
        approval_id=uuid4(),
        purpose=request.purpose,
        approved_hosts=(request.host,),
        approved_at=now + timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
        actor_id="local-operator",
    )
    with pytest.raises(PolicyViolation) as not_active:
        enabled.authorize(request, approval=future_approval, now=now)
    assert not_active.value.error.code is PolicyErrorCode.NETWORK_APPROVAL_NOT_ACTIVE
    with pytest.raises(PolicyViolation) as naive_clock:
        enabled.authorize(request, approval=approval, now=datetime.now())
    assert naive_clock.value.error.code is PolicyErrorCode.NETWORK_TIME_INVALID


def test_trust_policy_prevents_implicit_or_authoritative_upgrades() -> None:
    policy = DefaultTrustPolicy()
    assert (
        policy.authorize_transition(
            TrustLevel.HEURISTIC,
            TrustLevel.VERIFIED,
            human_review=True,
        )
        is TrustLevel.VERIFIED
    )
    with pytest.raises(PolicyViolation) as implicit:
        policy.authorize_transition(TrustLevel.HEURISTIC, TrustLevel.VERIFIED)
    assert implicit.value.error.code is PolicyErrorCode.TRUST_UPGRADE_DENIED
    with pytest.raises(PolicyViolation) as authoritative:
        policy.authorize_transition(
            TrustLevel.HEURISTIC,
            TrustLevel.AUTHORITATIVE,
            human_review=True,
        )
    assert authoritative.value.error.code is PolicyErrorCode.TRUST_UPGRADE_DENIED
    assert (
        policy.authorize_transition(
            TrustLevel.VERIFIED,
            TrustLevel.AUTHORITATIVE,
            schema_conformance=True,
        )
        is TrustLevel.AUTHORITATIVE
    )


def test_posix_policy_and_default_link_inspector_contract(tmp_path: Path) -> None:
    policy = WindowsAwarePathPolicy(style=PathStyle.POSIX)
    root = policy.approve_root("/approved/data")
    allowed = policy.authorize_child(root, "survey/file.sgy")
    assert allowed.canonical_path == "/approved/data/survey/file.sgy"
    assert policy.contains(root, allowed.canonical_path)
    assert not policy.contains(root, "/approved/data-escape/file.sgy")
    with pytest.raises(PolicyViolation) as absolute:
        policy.authorize_child(root, "/outside")
    assert absolute.value.error.code is PolicyErrorCode.ABSOLUTE_CHILD_DENIED
    with pytest.raises(PolicyViolation) as invalid:
        policy.authorize_child(root, "survey//file")
    assert invalid.value.error.code is PolicyErrorCode.INVALID_COMPONENT

    inspector = DefaultLinkInspector()
    missing = str(tmp_path / "missing")
    assert inspector.is_link_or_reparse(missing) is False
    assert inspector.resolve(missing).endswith("missing")


@pytest.mark.parametrize(
    ("mode", "attributes"),
    [
        (stat.S_IFDIR, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)),
        (stat.S_IFLNK, 0),
    ],
)
def test_default_inspector_detects_reparse_points_and_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    attributes: int,
) -> None:
    metadata = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    monkeypatch.setattr("agentic_osdu.policy.access.os.lstat", lambda _path: metadata)
    assert DefaultLinkInspector().is_link_or_reparse(r"C:\Approved\junction")


def test_output_subpath_overlap_requires_an_exact_explicit_approval() -> None:
    paths = WindowsAwarePathPolicy(style=PathStyle.WINDOWS)
    with pytest.raises(PolicyViolation) as unapproved:
        WorkspaceAccessPolicy(
            workspace_id=uuid4(),
            source_root=r"C:\Workspace",
            output_roots={"generated": r"C:\Workspace\generated"},
            path_policy=paths,
        )
    assert unapproved.value.error.code is PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE

    workspace = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=r"C:\Workspace",
        output_roots={"generated": r"C:\Workspace\generated"},
        path_policy=paths,
        allowed_source_output_subpaths={"generated": "generated"},
    )
    assert workspace.output_roots["generated"] == r"C:\Workspace\generated"
    assert workspace.source_root == r"C:\Workspace"

    with pytest.raises(PolicyViolation) as mismatch:
        WorkspaceAccessPolicy(
            workspace_id=uuid4(),
            source_root=r"C:\Workspace",
            output_roots={"generated": r"C:\Workspace\generated"},
            path_policy=paths,
            allowed_source_output_subpaths={"generated": "other"},
        )
    assert mismatch.value.error.code is PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE


def test_output_root_cannot_be_an_ancestor_or_alias_of_read_only_source() -> None:
    paths = WindowsAwarePathPolicy(style=PathStyle.WINDOWS)
    with pytest.raises(PolicyViolation) as ancestor:
        WorkspaceAccessPolicy(
            workspace_id=uuid4(),
            source_root=r"C:\Workspace\Data",
            output_roots={"generated": r"C:\Workspace"},
            path_policy=paths,
        )
    assert ancestor.value.error.code is PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE

    @dataclass(frozen=True)
    class AliasInspector(LinkInspector):
        def is_link_or_reparse(self, path: str) -> bool:
            return False

        def resolve(self, path: str) -> str:
            return path.replace("PROGRA~1", "Program Files")

    alias_paths = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=AliasInspector(),
    )
    with pytest.raises(PolicyViolation) as alias:
        WorkspaceAccessPolicy(
            workspace_id=uuid4(),
            source_root=r"C:\Program Files",
            output_roots={"generated": r"C:\PROGRA~1"},
            path_policy=alias_paths,
        )
    assert alias.value.error.code is PolicyErrorCode.OUTPUT_ROOT_OVERLAPS_SOURCE
