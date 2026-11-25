# Capability-oriented MCP servers

Four explicitly composed MCP servers expose bounded application operations:

| Directory | Endpoint | Responsibility |
| --- | --- | --- |
| `workspace_discovery` | `/mcp/workspace-discovery/` | Approved workspace, Data-only discovery, bounded samples/detection |
| `extraction` | `/mcp/extraction/` | Registered parser dispatch from actual content evidence |
| `classification` | `/mcp/classification/` | Actual detection/extraction joins and rule-derived classification |
| `inventory_review` | `/mcp/inventory-review/` | Failure accounting, versioned inventory commits, paginated full review |

Each `server.py` defines its own tools and schemas. `shared` implements independent
use cases, typed-registry adaptation, receipt storage and concurrency; it imports
no hosted-agent or swarm code. `host` is the Entra-authenticated ASGI composition
root and container image. The existing local `agentic-osdu-mcp` aggregate server,
its descriptive tool names and internal `TOOL-*` IDs remain compatible.

Cloud operation tools compose those existing deterministic implementations rather
than duplicating parsers. For example, `extract_detected_metadata` dispatches to
the existing registered SEG-Y, LAS, CSV, P190 or other supported extractor.
The server rejects extension-only/ambiguous choices. To add a data type, extend
the explicit `DataTypeRegistry` and its typed contracts; this MCP dispatch does
not need a new hardcoded agent stage.

## Identity and call contract

Each call includes a UUID `request_id` and its operation-specific arguments.
Actor identity comes from a verified Entra token, never a model-supplied actor.
The seven explicit specialist role IDs limit both endpoint access and operations
within a shared endpoint. The supervisor/operator reader permission grants only
receipt/version reads, not specialist tool execution.

The source root, read-only policy, Data-only discovery scope, sample budget and
file budget are server configuration. Clients cannot supply a different root,
follow links, write sources, generate manifests, or ingest records.

Tools return durable operation receipts:

```json
{
  "request_id": "<actual-request-UUID>",
  "operation": "extract_detected_metadata",
  "arguments": {"file_id": "<observed-file-UUID>"},
  "principal_id": "<verified-principal-object-id>",
  "instance_id": "<runtime-instance-UUID>",
  "status": "succeeded",
  "result": {
    "tool_id": "TOOL-011",
    "tool_version": "<actual-tool-version>",
    "output": {"...": "actual extracted metadata"}
  }
}
```

This is an illustrative shape, not usable identifiers or metadata.
`status=failed` carries an explicit error code. `/operations/{request_id}` gives
allowlisted readers the same authoritative receipt. Never substitute specialist
prose for this result. An identical request ID/operation/arguments/principal
replays its terminal receipt; changing any of them is a conflict.

An `in_progress` receipt with no terminal result means the outcome is uncertain,
not safe to retry. In particular, inventory commits and receipt updates are
separate database transactions. Reconcile the actual inventory version before
retrying an interrupted commit.

## Concurrency and lifecycle

Four servers share **one filesystem-capability runtime**, not four independent
process-local caches. Async MCP handlers offload deterministic work to threads.
Independent files can extract/classify concurrently; one file's operations use
a per-file mutex. A writer-priority barrier drains active file work before
discovery, accounting, inventory snapshot/commit or review. Inventory writes
retain the existing optimistic version and file-ownership checks.

The host must still run one worker/replica. PostgreSQL does not distribute
filesystem capability caches, and separate Azure Files mounts can produce
different filesystem identities. This is concurrent agent orchestration over
one protected data authority, **not horizontally distributed parser workers**.
Do not scale or independently launch these servers with separate runtimes and
then pass file/sample IDs between them.

One bounded discovery workset is retained per runtime. Repeating discovery with
the same budget does not erase extracted observations; another budget is a
conflict. Restart/redeploy to discover a changed source snapshot, after persisting
active work. A restart invalidates pre-persistence file/sample capabilities.
Historical receipts do not recreate those capabilities. New extraction work
must register/discover/inspect again; persisted inventories remain independently
reviewable, with full extraction payloads and persisted failure evidence.

The host uses sessionless MCP, including legacy stateless HTTP, to avoid passing
an authenticated user's protocol session to another principal. Exact host/origin
checks and bearer validation remain enabled. There is no public unauthenticated
copy of the local API.

## Host configuration

`mcp_servers\host\settings.py` validates operator configuration:

```text
OSDU_DATABASE_URL               postgresql+psycopg URL, TLS required
OSDU_SOURCE_ROOT                /mnt/source, read-only mount required
OSDU_SCAN_SUBPATH               Data
OSDU_TENANT_ID                  actual tenant GUID
OSDU_API_AUDIENCE               actual API application client GUID
OSDU_AGENT_PRINCIPALS_JSON      object-ID -> capability-role (1..7) mapping
OSDU_READER_PRINCIPALS_JSON     explicit reader object-ID array
OSDU_ALLOWED_HOSTS             exact served hostname array
OSDU_SCHEMA_CATALOG_ROOT       writable cache outside the source root
OSDU_MAX_FILES                 bounded workset limit
```

Run from a prepared container using
`uvicorn mcp_servers.host.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1`.
Windows source paths are for the unchanged local MCP workflow; the cloud host
requires the approved POSIX read-only volume. `/healthz` is anonymous process
readiness only, not proof of database/model/identity readiness.

See [agent deployment](../agents/README.md) for infrastructure and access setup.
