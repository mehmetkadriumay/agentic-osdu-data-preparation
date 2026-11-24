"""Cloud policy is operator configuration, never model-provided input."""

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


def _unique_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy configuration key")
        result[key] = value
    return result


def principal_map(value: str) -> dict[str, int]:
    mapping = json.loads(value, object_pairs_hook=_unique_pairs)
    if not isinstance(mapping, dict):
        raise ValueError("Agent principal configuration must be an object")
    result = {}
    for key, stage in mapping.items():
        if type(stage) is not int or not 1 <= stage <= 7:
            raise ValueError("Agent stages must be integers from 1 to 7")
        principal = str(UUID(key))
        if principal in result:
            raise ValueError("Duplicate canonical principal")
        result[principal] = stage
    return result


@dataclass(frozen=True)
class Settings:
    database_url: str
    source_root: Path
    scan_subpath: str
    tenant_id: str
    audience: str
    agents: dict[str, int]
    readers: frozenset[str]
    allowed_hosts: frozenset[str]
    schema_catalog_root: Path
    max_files: int

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.environ["OSDU_DATABASE_URL"]
        database = urlsplit(database_url)
        if database.scheme != "postgresql+psycopg" or parse_qs(database.query).get(
            "sslmode"
        ) not in (["require"], ["verify-ca"], ["verify-full"]):
            raise ValueError("The cloud backend requires PostgreSQL with TLS")
        source = Path(os.environ.get("OSDU_SOURCE_ROOT", "/mnt/source"))
        schema_root = Path(os.environ.get("OSDU_SCHEMA_CATALOG_ROOT", "/tmp/osdu-schemas"))
        subpath = os.environ.get("OSDU_SCAN_SUBPATH", "Data")
        parts = PurePosixPath(subpath).parts
        if (
            not source.is_absolute()
            or not schema_root.is_absolute()
            or source == schema_root
            or source in schema_root.parents
            or not parts
            or subpath.startswith("/")
            or "\\" in subpath
            or ".." in parts
        ):
            raise ValueError("Source, schema cache, and relative discovery scope are invalid")
        hosts = json.loads(os.environ["OSDU_ALLOWED_HOSTS"])
        if (
            not isinstance(hosts, list)
            or not hosts
            or any(
                not isinstance(host, str) or not host or any(c in host for c in "/:* \r\n")
                for host in hosts
            )
        ):
            raise ValueError("Exact backend hostnames must be configured")
        reader_values = json.loads(os.environ.get("OSDU_READER_PRINCIPALS_JSON", "[]"))
        if not isinstance(reader_values, list):
            raise ValueError("Reader principals must be an array")
        max_files = int(os.environ.get("OSDU_MAX_FILES", "100"))
        if not 1 <= max_files <= 10_000:
            raise ValueError("OSDU_MAX_FILES must be between 1 and 10000")
        return cls(
            database_url=database_url,
            source_root=source,
            scan_subpath=subpath,
            tenant_id=str(UUID(os.environ["OSDU_TENANT_ID"])),
            audience=str(UUID(os.environ["OSDU_API_AUDIENCE"])),
            agents=principal_map(os.environ.get("OSDU_AGENT_PRINCIPALS_JSON", "{}")),
            readers=frozenset(str(UUID(value)) for value in reader_values),
            allowed_hosts=frozenset(host.casefold() for host in hosts),
            schema_catalog_root=schema_root,
            max_files=max_files,
        )
