from __future__ import annotations

from pathlib import Path

import pytest

from agentic_osdu.api.server import ServerSettings


def test_server_defaults_to_loopback() -> None:
    assert ServerSettings().host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.test"])
def test_non_loopback_binding_is_rejected_without_explicit_configuration(host: str) -> None:
    with pytest.raises(ValueError, match="explicit"):
        ServerSettings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_binding_is_allowed(host: str) -> None:
    assert ServerSettings(host=host).host == host


def test_explicit_non_loopback_binding_can_be_enabled() -> None:
    settings = ServerSettings(host="0.0.0.0", allow_non_loopback=True)
    assert settings.host == "0.0.0.0"


def test_manifest_validation_issues_never_use_inner_html() -> None:
    source = Path("web/src/manifest.ts").read_text(encoding="utf-8")
    assert ".innerHTML" not in source
