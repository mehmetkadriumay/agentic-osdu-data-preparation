# Release Policy

The project uses semantic versioning. `MAJOR.MINOR.PATCH` in `pyproject.toml`,
the UI package version, Git tag `vMAJOR.MINOR.PATCH`, changelog heading, and
release title must agree. Every release starts from protected `main` after CI
passes and requires an approved parity report with zero blocking comparisons.

Release candidates must pass Python lint, format, typing, unit, contract,
integration, acceptance, coverage, security, parity, UI, packaging,
clean-machine installation, and rollback rehearsal gates. The wheel and sdist
must pass Twine validation. A clean temporary Python 3.12 environment must run
the CLI and serve the UI only on loopback.

Required assets are the wheel, sdist, `uv.lock`, final approved parity report,
migration package, and schema catalog manifest when supplied. Release notes
must state that generated manifests remain heuristic and review-required and
that no OSDU ingestion capability is included. Failed or withdrawn releases
remain immutable; publish a corrected patch version rather than replacing
artifacts.
