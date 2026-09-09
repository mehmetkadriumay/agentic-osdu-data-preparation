import os

import pytest

from agentic_osdu.observability.redaction import Redactor
from agentic_osdu.policy.access import (
    DefaultLinkInspector,
    PathStyle,
    PolicyErrorCode,
    PolicyViolation,
    WindowsAwarePathPolicy,
)


def test_link_inspection_failure_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_lstat(path: str) -> os.stat_result:
        del path
        raise PermissionError("sensitive path must not escape")

    monkeypatch.setattr(os, "lstat", deny_lstat)
    policy = WindowsAwarePathPolicy(
        style=PathStyle.WINDOWS,
        inspector=DefaultLinkInspector(),
    )

    with pytest.raises(PolicyViolation) as captured:
        policy.authorize_child(r"C:\approved", r"data\sample.sgy")

    assert captured.value.error.code is PolicyErrorCode.PATH_INSPECTION_FAILED
    assert "sensitive path must not escape" not in str(captured.value)


@pytest.mark.parametrize(
    "prohibited_path",
    [
        r"\outside\client\survey.sgy",
        r"C:outside\client\survey.sgy",
    ],
)
def test_prohibited_windows_path_forms_are_redacted(prohibited_path: str) -> None:
    redacted = Redactor().redact({"message": f"Access denied for {prohibited_path}"})

    assert prohibited_path not in redacted["message"]
    assert redacted["message"] == "[REDACTED:PATH]"
