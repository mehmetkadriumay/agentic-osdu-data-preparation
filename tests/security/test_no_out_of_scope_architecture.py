from __future__ import annotations

from pathlib import Path


def test_epic_007_does_not_create_later_architectural_packages_or_ci() -> None:
    root = Path(__file__).parents[2]
    prohibited = (
        ".github/workflows",
        "src/agentic_osdu/agents",
        "src/agentic_osdu/api",
        "src/agentic_osdu/migration",
        "web",
    )
    assert all(not (root / path).exists() for path in prohibited)


def test_epic_007_contains_no_osdu_ingestion_or_unscoped_network_code() -> None:
    root = Path(__file__).parents[2]
    package_files = (
        path
        for path in (root / "src" / "agentic_osdu").rglob("*.py")
        if path.as_posix().endswith("/schemas/catalog.py") is False
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_files).lower()
    prohibited_imports = (
        "import requests",
        "import httpx",
        "import urllib.request",
        "from socket",
    )
    assert all(value not in source for value in prohibited_imports)
    assert "osdu ingestion" not in source
