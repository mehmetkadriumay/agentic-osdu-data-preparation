from __future__ import annotations

import ast
from pathlib import Path


def test_api_cli_and_ui_server_do_not_embed_domain_or_persistence_implementations() -> None:
    root = Path(__file__).parents[2]
    interface_files = [
        *sorted((root / "src" / "agentic_osdu" / "api").rglob("*.py")),
        root / "src" / "agentic_osdu" / "cli.py",
    ]
    forbidden = {
        "agentic_osdu.formats",
        "agentic_osdu.manifests",
        "agentic_osdu.schemas",
        "agentic_osdu.state.models",
        "agentic_osdu.state.repositories",
        "sqlalchemy",
    }
    for path in interface_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not {
            imported
            for imported in imports
            if any(imported == item or imported.startswith(f"{item}.") for item in forbidden)
        }, path


def test_no_migration_or_osdu_ingestion_surface_is_added() -> None:
    root = Path(__file__).parents[2]
    paths = [
        *sorted((root / "src" / "agentic_osdu" / "api").rglob("*.py")),
        root / "src" / "agentic_osdu" / "cli.py",
        *sorted((root / "web" / "src").rglob("*")),
    ]
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}
    ).casefold()
    assert "migration" not in source
    assert "osdu ingestion" not in source
