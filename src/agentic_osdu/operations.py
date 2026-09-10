"""Bounded operational backup and restore commands for local SQLite state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Never
from uuid import uuid4


class OperationsError(RuntimeError):
    """Fail-closed operational error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DatabaseBackupReceipt:
    source: str
    backup: str
    sha256: str
    integrity_check: str


def _resolved_distinct(source: Path, destination: Path) -> tuple[Path, Path]:
    resolved_source = source.resolve(strict=True)
    resolved_destination = destination.resolve(strict=False)
    if resolved_source == resolved_destination:
        raise OperationsError("BACKUP_PATH_INVALID", "Source and destination must differ.")
    return resolved_source, resolved_destination


def _integrity_check(path: Path) -> str:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise OperationsError(
            "DATABASE_INVALID", "SQLite integrity verification failed."
        ) from error
    if result != ("ok",):
        raise OperationsError("DATABASE_INVALID", "SQLite integrity verification did not pass.")
    return str(result[0])


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_database_backup(
    database_path: Path,
    backup_path: Path,
    *,
    overwrite: bool = False,
) -> DatabaseBackupReceipt:
    """Create and verify an atomic SQLite online backup."""

    source, destination = _resolved_distinct(database_path, backup_path)
    if destination.exists() and not overwrite:
        raise OperationsError("BACKUP_EXISTS", "The backup destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with (
            closing(
                sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
            ) as source_connection,
            closing(sqlite3.connect(temporary)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        integrity = _integrity_check(temporary)
        temporary.replace(destination)
    except (OSError, sqlite3.Error) as error:
        temporary.unlink(missing_ok=True)
        raise OperationsError("BACKUP_FAILED", "The SQLite backup could not be created.") from error
    return DatabaseBackupReceipt(
        source=str(source),
        backup=str(destination),
        sha256=_sha256_file(destination),
        integrity_check=integrity,
    )


def restore_database_backup(
    backup_path: Path,
    database_path: Path,
    *,
    overwrite: bool = True,
) -> DatabaseBackupReceipt:
    """Verify and atomically restore a SQLite backup to a closed database path."""

    backup, destination = _resolved_distinct(backup_path, database_path)
    if destination.exists() and not overwrite:
        raise OperationsError("RESTORE_TARGET_EXISTS", "The restore target already exists.")
    _integrity_check(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.restore")
    try:
        with (
            closing(
                sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
            ) as source_connection,
            closing(sqlite3.connect(temporary)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        integrity = _integrity_check(temporary)
        temporary.replace(destination)
    except (OSError, sqlite3.Error) as error:
        temporary.unlink(missing_ok=True)
        raise OperationsError(
            "RESTORE_FAILED", "The SQLite backup could not be restored."
        ) from error
    return DatabaseBackupReceipt(
        source=str(backup),
        backup=str(destination),
        sha256=_sha256_file(destination),
        integrity_check=integrity,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentic_osdu.operations")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--destination", required=True, type=Path)
        command.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "backup":
            receipt = create_database_backup(
                arguments.source,
                arguments.destination,
                overwrite=arguments.overwrite,
            )
        else:
            receipt = restore_database_backup(
                arguments.source,
                arguments.destination,
                overwrite=arguments.overwrite,
            )
    except OperationsError as error:
        print(json.dumps({"errors": [{"code": error.code, "message": str(error)}]}))
        return 3
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


def entrypoint() -> Never:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
