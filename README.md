# Agentic OSDU Data Preparation

A local-first Python project for deterministic, agent-orchestrated OSDU data
preparation workflows.

## Status

This repository currently contains only the EPIC-001 project bootstrap. Domain
tools, orchestration, APIs, persistence, and user interfaces are intentionally
deferred to later approved epics.

## Requirements

- Python 3.12 or later

## Development install

```powershell
python -m pip install -e .
```

## Scope

The project prepares and reviews candidate OSDU data and manifests. It does not
ingest records into OSDU, mutate source datasets, or treat generated manifests
as authoritative without human review.

## License

Licensed under the [MIT License](LICENSE).
