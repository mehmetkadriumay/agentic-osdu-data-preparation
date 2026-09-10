from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.parity.fixtures import catalog_sha256, load_catalog, materialize_catalog
from tests.parity.report import Characterization


def characterize_current_application(
    catalog_path: Path,
    volve_root: Path,
    output_root: Path,
) -> Characterization:
    catalog = load_catalog(catalog_path)
    fixture_root = output_root / "fixture-copies"
    materialize_catalog(catalog, fixture_root)
    before = _source_snapshot(volve_root)
    current_copy = output_root / "current-copy"
    current_copy.mkdir(parents=True, exist_ok=True)
    for source in volve_root.glob("*.py"):
        shutil.copy2(source, current_copy / source.name)
    shutil.copytree(volve_root / "web", current_copy / "web")
    request_path = output_root / "request.json"
    response_path = output_root / "response.json"
    output_root.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps(
            {
                "volve_root": str(current_copy.resolve()),
                "fixture_root": str(fixture_root.resolve()),
                "fixtures": [item.model_dump() for item in catalog.fixtures],
                "response_path": str(response_path.resolve()),
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("current_worker.py")),
            str(request_path),
        ],
        cwd=output_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Current characterization failed "
            f"({completed.returncode}): {(completed.stderr or completed.stdout)[-2000:]}"
        )
    payload = json.loads(response_path.read_text(encoding="utf-8"))
    after = _source_snapshot(volve_root)
    return Characterization(
        application="current",
        fixture_catalog_sha256=catalog_sha256(catalog_path),
        source_unchanged=before == after,
        formats=tuple(payload["formats"]),
        metadata=payload["metadata"],
        associations=payload["associations"],
        learning=payload["learning"],
        generation=payload["generation"],
        validation=payload["validation"],
        api_shapes=payload["api_shapes"],
        capabilities=payload["capabilities"],
    )


def _source_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    paths = (path for path in root.rglob("*") if ".git" not in path.relative_to(root).parts)
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            _bounded_fingerprint(path),
        )
        for path in sorted(paths)
        if path.is_file()
    }


def _bounded_fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode())
    with path.open("rb") as source:
        digest.update(source.read(64 * 1024))
        if size > 64 * 1024:
            source.seek(max(0, size - 64 * 1024))
            digest.update(source.read(64 * 1024))
    return digest.hexdigest()


__all__ = ["characterize_current_application"]
