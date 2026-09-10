from __future__ import annotations

import json
import tomllib
from pathlib import Path

from agentic_osdu.cli import main

ROOT = Path(__file__).parents[2]


def test_approved_parity_gate_is_recorded() -> None:
    report = json.loads((ROOT / "docs/parity-report.json").read_text(encoding="utf-8"))

    assert report["summary"] == {
        "blocking": 0,
        "equal": 85,
        "intentional_approved": 3,
        "total": 88,
    }
    assert report["acceptance"]["human_sign_off"]["comment"] == "authorized, all good"
    assert report["acceptance"]["human_sign_off"]["approved"] is True


def test_ci_covers_every_required_release_gate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for job in {
        "python-quality",
        "python-tests",
        "security",
        "parity",
        "ui",
        "package",
        "rollback",
    }:
        assert f"  {job}:" in workflow
    assert "coverage" in workflow.lower()
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "branches: [main]" in workflow


def test_release_metadata_and_workflow_are_complete() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert metadata["project"]["version"] == "1.0.0"
    assert (ROOT / "CHANGELOG.md").is_file()
    assert (ROOT / "docs/release-policy.md").is_file()
    for asset in (
        "dist/*.whl",
        "dist/*.tar.gz",
        "uv.lock",
        "docs/parity-report.json",
        "migration-package.zip",
    ):
        assert asset in workflow
    assert "verify_clean_install.ps1" in workflow


def test_operations_runbook_covers_required_procedures() -> None:
    runbook = (ROOT / "docs/operations.md").read_text(encoding="utf-8").lower()

    for topic in (
        "workspace policy",
        "schema catalog",
        "backup",
        "restore",
        "legacy migration",
        "job recovery",
        "cancellation",
        "structured logs",
        "troubleshooting",
        "database rollback",
        "learning model rollback",
        "package rollback",
    ):
        assert topic in runbook
    assert "osdu ingestion is not a capability" in runbook


def test_release_scripts_are_present_and_explicitly_bounded() -> None:
    clean_install = (ROOT / "scripts/verify_clean_install.ps1").read_text(encoding="utf-8")
    package_rollback = (ROOT / "scripts/rehearse_package_rollback.ps1").read_text(encoding="utf-8")

    assert "127.0.0.1" in clean_install
    assert "agentic-osdu --help" in clean_install
    assert "51702e705968dc63b3b2bc160ed66ee182bf9bfc" in package_rollback
    assert "agentic-osdu --help" in package_rollback


def test_cli_help_is_a_successful_clean_install_probe(capsys: object) -> None:
    assert main(["--help"]) == 0
