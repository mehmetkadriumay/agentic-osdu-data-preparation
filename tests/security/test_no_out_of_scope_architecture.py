from __future__ import annotations

import json
from pathlib import Path


def test_release_ci_exists_only_after_recorded_human_parity_signoff() -> None:
    root = Path(__file__).parents[2]
    report = json.loads((root / "docs/parity-report.json").read_text(encoding="utf-8"))

    assert report["acceptance"]["human_sign_off"]["approved"] is True
    assert report["acceptance"]["human_sign_off"]["comment"] == "authorized, all good"
    assert (root / ".github/workflows/ci.yml").is_file()


def test_epic_009_contains_no_osdu_ingestion_or_unscoped_network_code() -> None:
    root = Path(__file__).parents[2]
    package_files = (
        path
        for path in (root / "src" / "agentic_osdu").rglob("*.py")
        if not path.as_posix().endswith(("/schemas/catalog.py", "/cli.py"))
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
