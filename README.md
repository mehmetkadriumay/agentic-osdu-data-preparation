# Agentic OSDU Data Preparation

A local-first Python project for deterministic, agent-orchestrated OSDU data
preparation workflows.

## Status

Version 1.0 provides the approved deterministic tools, bounded agent workflows,
transactional state and jobs, migration and parity validation, typed API and
CLI, and local review UI. The final parity report contains 88 comparisons:
85 equal, 3 intentional approved differences, and 0 blocking differences.

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
