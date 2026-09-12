# Agentic OSDU Data Preparation

A local-first Python project for deterministic, agent-orchestrated OSDU data
preparation workflows.

## Status

Version 1.0 provides the approved deterministic tools, bounded agent workflows,
transactional state and jobs, migration and parity validation, typed API and
CLI, local review UI, and native MCP transports. The final parity report
contains 88 comparisons: 85 equal, 3 intentional approved differences, and
0 blocking differences.

## Requirements

- Python 3.12 or later

## Development install

```powershell
uv sync --python 3.12
```

Install a release wheel and verify the CLI:

```powershell
uv tool install .\dist\agentic_osdu_data_preparation-1.0.0-py3-none-any.whl
agentic-osdu --help
agentic-osdu web-serve --host 127.0.0.1 --port 8000
```

## MCP server

The official MCP Python SDK projects the existing typed `TOOL-001` through
`TOOL-030` registry without duplicating domain logic.

For Copilot CLI stdio, install the package and use the repository-level
`.github/mcp.json`, or add it explicitly:

```powershell
copilot mcp add agentic-osdu-data-preparation -- agentic-osdu-mcp
```

For Streamable HTTP, start the existing loopback service:

```powershell
agentic-osdu web-serve --host 127.0.0.1 --port 8000
copilot mcp add --transport http agentic-osdu-http http://127.0.0.1:8000/mcp/
```

Both transports use `create_runtime()` and the same typed registry. MCP does
not add shell, unrestricted filesystem, or OSDU ingestion tools. Workspace
roots remain explicitly approved and read-only. Write-mode generation, remote
schema refresh, learning-model mutations, and human review decisions remain
approval-gated; generated manifests remain review-required. Local TOOL-021
catalog imports must be contained by the persisted approved workspace root,
and direct MCP TOOL-024 create, activate, deactivate, and clear calls fail
closed because signed approval is available only through WF-003.

## Local quality gates

Run the CI-ready checks from the repository root:

```powershell
uv run --frozen pytest
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv build
uv run --frozen twine check dist/*
uv run --frozen bandit -c pyproject.toml -r src
uv run --frozen pip-audit
uv lock --check
uv run --frozen pre-commit run --all-files
Set-Location web
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

See [operations](docs/operations.md), [release policy](docs/release-policy.md),
and the [approved parity report](docs/parity-report.json).

## Scope

The project prepares and reviews candidate OSDU data and manifests. It does not
ingest records into OSDU, mutate source datasets, or treat generated manifests
as authoritative without human review.

## License

Licensed under the [MIT License](LICENSE).
