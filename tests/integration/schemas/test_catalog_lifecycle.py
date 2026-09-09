from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from agentic_osdu.domain.models import WorkspaceRelativePath
from agentic_osdu.schemas.catalog import SchemaCatalogError, SchemaCatalogStore
from agentic_osdu.tools.contracts import (
    LocalSchemaCatalogImport,
    SchemaCatalogSource,
    SchemaChecksum,
)


def _request(source: Path, revision: str, payload: bytes) -> LocalSchemaCatalogImport:
    relative = "manifest/Manifest.1.0.0.json"
    target = source / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return LocalSchemaCatalogImport(
        source=SchemaCatalogSource.LOCAL_EXPORT,
        revision=revision,
        local_root=str(source),
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath(relative),
                sha256=sha256(payload).hexdigest(),
            ),
        ),
    )


def test_local_import_is_immutable_idempotent_and_activation_is_reversible(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    first_payload = json.dumps({"schema": {"type": "object"}}).encode()
    second_payload = json.dumps({"schema": {"type": "object", "required": ["kind"]}}).encode()
    first = store.import_local(_request(tmp_path / "one", "r1", first_payload))
    second = store.import_local(_request(tmp_path / "two", "r2", second_payload))

    assert store.active_catalog().schema_catalog_id == second.schema_catalog_id
    store.activate(first.schema_catalog_id)
    assert store.active_catalog().schema_catalog_id == first.schema_catalog_id
    replay = store.import_local(_request(tmp_path / "one", "r1", first_payload))
    assert replay.schema_catalog_id == first.schema_catalog_id
    assert replay.catalog_sha256 == first.catalog_sha256
    assert len(store.list_catalogs()) == 2


def test_import_rejects_checksum_mismatch_without_activation(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    request = _request(tmp_path / "source", "r1", b'{"schema":{"type":"object"}}')
    bad = request.model_copy(
        update={
            "expected_checksums": (
                SchemaChecksum(
                    relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                    sha256="0" * 64,
                ),
            )
        }
    )
    with pytest.raises(SchemaCatalogError, match="CHECKSUM_MISMATCH"):
        store.import_local(bad)
    with pytest.raises(SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        store.active_catalog()


def test_catalog_corruption_fails_closed(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        _request(tmp_path / "source", "r1", b'{"schema":{"type":"object"}}')
    )
    schema_path = store.catalog_path(descriptor.schema_catalog_id) / "manifest/Manifest.1.0.0.json"
    schema_path.write_text('{"schema":{"type":"string"}}', encoding="utf-8")

    with pytest.raises(SchemaCatalogError, match="CHECKSUM_MISMATCH"):
        store.open(descriptor.schema_catalog_id)


@pytest.mark.parametrize(
    ("revision", "payload", "code"),
    [
        ("../escape", b'{"schema":{"type":"object"}}', "CATALOG_INCOMPLETE"),
        ("r1", b"not-json", "CATALOG_INCOMPLETE"),
        ("r1", b'{"not_schema":{}}', "CATALOG_INCOMPLETE"),
    ],
)
def test_local_import_rejects_invalid_revision_or_wrapper(
    tmp_path: Path,
    revision: str,
    payload: bytes,
    code: str,
) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    with pytest.raises(SchemaCatalogError, match=code):
        store.import_local(_request(tmp_path / revision.replace("/", "_"), revision, payload))


def test_catalog_rejects_duplicate_expected_paths_and_unknown_ids(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    request = _request(tmp_path / "source", "r1", b'{"schema":{"type":"object"}}')
    duplicate = request.model_copy(update={"expected_checksums": request.expected_checksums * 2})
    with pytest.raises(SchemaCatalogError, match="CATALOG_INCOMPLETE"):
        store.import_local(duplicate)
    with pytest.raises(SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        store.open(__import__("uuid").uuid4())


def test_missing_local_export_fails_closed(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    request = LocalSchemaCatalogImport(
        source=SchemaCatalogSource.LOCAL_EXPORT,
        revision="r1",
        local_root=str(tmp_path / "missing"),
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                sha256="0" * 64,
            ),
        ),
    )
    with pytest.raises(SchemaCatalogError, match="CATALOG_INCOMPLETE"):
        store.import_local(request)


def test_corrupt_active_pointer_and_catalog_manifest_fail_closed(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        _request(tmp_path / "source", "r1", b'{"schema":{"type":"object"}}')
    )
    manifest = store.catalog_path(descriptor.schema_catalog_id) / "catalog.json"
    (tmp_path / "cache" / "active.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        store.active_catalog()

    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        store.open(descriptor.schema_catalog_id)


def test_catalog_identity_binds_manifest_provenance_and_directory(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        _request(tmp_path / "source", "r1", b'{"schema":{"type":"object"}}')
    )
    manifest_path = store.catalog_path(descriptor.schema_catalog_id) / "catalog.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"] = "approved_remote:https://attacker.invalid"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SchemaCatalogError, match="CHECKSUM_MISMATCH"):
        store.open(descriptor.schema_catalog_id)


def test_cancelled_import_does_not_replace_active_catalog(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    first = store.import_local(_request(tmp_path / "one", "r1", b'{"schema":{"type":"object"}}'))
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(SchemaCatalogError, match="CANCELLED"):
        store.import_local(
            _request(
                tmp_path / "two",
                "r2",
                b'{"schema":{"type":"object","required":["kind"]}}',
            ),
            cancellation=cancellation,
        )
    assert store.active_catalog().schema_catalog_id == first.schema_catalog_id
