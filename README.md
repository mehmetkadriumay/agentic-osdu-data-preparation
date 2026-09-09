# Agentic OSDU Data Preparation

A local-first Python project for deterministic, agent-orchestrated OSDU data
preparation workflows.

## Status

EPIC-004 provides approved workspace registration, deterministic no-follow
file discovery, bounded reads with change detection, versioned format
detection, normalized parser-free classification, and bounded typed metadata
extraction for SEG-Y, LAS, JSON Well Log, DLIS, LIS/LTI, CSV, P1/90, SGP, DAT,
text, and PDF through `TOOL-001` through `TOOL-013`. Persistence, APIs, agents,
jobs, and user interfaces remain deferred to later approved epics.

## Requirements

- Python 3.12 or later

## Development install

```powershell
uv sync --python 3.12
```

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
```

## Scope

The project prepares and reviews candidate OSDU data and manifests. It does not
ingest records into OSDU, mutate source datasets, or treat generated manifests
as authoritative without human review.

## License

Licensed under the [MIT License](LICENSE).
