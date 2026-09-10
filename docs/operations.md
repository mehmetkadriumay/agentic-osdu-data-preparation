# Operations Runbook

This runbook applies to the local-first 1.0 release. Stop the service and all
workers before restore, downgrade, or package rollback. OSDU ingestion is not a capability
of this project; operations cover preparation and human review only.
No procedure reads from or writes to the `Volve` source tree.

## Workspace policy

Register an absolute local workspace through the UI, API, or CLI. Keep source
roots read-only, leave link following disabled, and permit writes only to
explicit output subpaths and the configured state directory. Canonical path
checks reject traversal, junction, symlink, UNC/device, and output-escape
attempts. Re-register a workspace when its root or output policy changes.

## State location, backup, and restore

Keep SQLite state in a user data directory outside source control. Back up a
live database with SQLite's online backup API:

```powershell
python -m agentic_osdu.operations backup `
  --source C:\state\agentic-osdu.db `
  --destination C:\backups\agentic-osdu-2026-09-10.db
```

The command refuses overwrite unless `--overwrite` is explicit, runs
`PRAGMA integrity_check`, and emits a SHA-256 receipt. To restore, stop the
service, preserve the current database under a separate backup name, then run:

```powershell
python -m agentic_osdu.operations restore `
  --source C:\backups\agentic-osdu-2026-09-10.db `
  --destination C:\state\agentic-osdu.db `
  --overwrite
```

Start the service and check inventory, active learning model, catalog, and job
history before resuming writes.

## Legacy migration

Legacy migration consumes read-only copies or references under an explicitly
approved legacy root. It never edits source JSON:

```powershell
agentic-osdu migration `
  --inventory C:\migration-copy\classification-inventory.json `
  --learning C:\migration-copy\manifest-learning.json `
  --database C:\state\agentic-osdu.db `
  --legacy-root C:\migration-copy
```

Archive the JSON report. Re-running identical input is idempotent. Resolve any
reported failed fields before enabling production review workflows.

## Schema catalog operations and rollback

Schema catalogs are immutable and checksum-pinned. Remote access is disabled by
default and requires an explicit matching approval. Import an approved local
export through `SchemaCatalogStore.import_local`, archive its `catalog.json`,
and record the returned catalog ID. Verify all wrappers before activation.

For schema catalog rollback, stop validation work and activate the prior ID:

```python
from pathlib import Path
from uuid import UUID
from agentic_osdu.schemas.catalog import SchemaCatalogStore

store = SchemaCatalogStore(Path(r"C:\state\schema-catalogs"))
store.activate(UUID("prior-catalog-id"))
assert store.active_catalog().schema_catalog_id == UUID("prior-catalog-id")
```

Do not delete either catalog. A checksum or reference failure is fail-closed.

## Job recovery and cancellation

At startup, invoke `JobService.recover_interrupted()` after opening state and
before accepting new work. Expired running or cancelling jobs become
`interrupted`; writes do not resume automatically. Review ordered events and
explicitly submit a new job if safe.

Request cancellation with `agentic-osdu job-cancel --json <payload>` or the UI.
The persisted token is honored at documented discovery, parser, learning, and
generation checkpoints. Wait for a terminal `cancelled`, `failed`, or completed
state before backup, restore, or shutdown. Atomic generation leaves no partial
target file.

## Structured logs

Use JSON structured logs with correlation ID, job ID, tool ID/version, safe
event code, duration, and status. Store rolling logs in a protected local user
data directory. Never enable raw samples, secrets, absolute prohibited paths,
or external analytics. Use correlation and request IDs to join logs with
append-only audit records.

## Rollback rehearsals

Run all executable database rollback, schema catalog rollback, and learning
model rollback checks:

```powershell
uv run --frozen pytest tests\integration\rollback --no-cov
```

The database rollback rehearsal upgrades to Alembic head, creates a verified
backup, downgrades to base, restores the backup, and confirms the prior schema.
For a real database migration rollback, export/backup first, run
`uv run alembic downgrade <prior-revision>`, and restore the verified backup if
the downgrade is destructive or validation fails.

Learning model rollback is append-only: activate the prior model ID using
TOOL-024 with its latest expected version. This deactivates the current model
for the category while preserving every version. Confirm exactly one active
model for that category before resuming generation.

Package rollback is rehearsed against the approved EPIC-010 commit:

```powershell
uv build
.\scripts\rehearse_package_rollback.ps1 `
  -CurrentWheel (Get-ChildItem dist\*.whl).FullName
```

CI and releases use isolated environments and resolve locked dependencies. If
the local package registry is unavailable, add
`-OfflineReuseInstalledDependencies` to verify wheel replacement against the
already validated development environment; this mode is not accepted for CI
or release publication.

For an incident, stop the service, back up state, force-reinstall the prior
wheel from the approved release assets, run `agentic-osdu --help`, apply only
compatible database migration changes, then start the loopback UI and validate
its health. Keep the current wheel and backup until recovery is accepted.

## Troubleshooting

| Symptom | Safe response |
| --- | --- |
| `BACKUP_EXISTS` | Choose a timestamped path or explicitly use `--overwrite`. |
| `DATABASE_INVALID` | Do not restore; use another verified backup. |
| `SCHEMA_UNAVAILABLE` or checksum failure | Activate a prior immutable catalog; do not bypass validation. |
| Job remains running after restart | Check its lease expiry, run interruption recovery, and never resume writes implicitly. |
| Cancellation remains requested | Wait for the next cooperative checkpoint; do not terminate during atomic publication. |
| `STATE_VERSION_CONFLICT` | Reload state and retry with the current expected version. |
| UI cannot start | Confirm Python 3.12, loopback host, free port, and installed wheel metadata. |
| Migration reports failures | Retain the report and correct copied inputs; never edit `Volve` state. |
