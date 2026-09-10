# Changelog

All notable changes follow semantic versioning and are recorded here.

## [1.0.0] - 2026-09-10

### Added

- Complete deterministic local data-preparation, manifest, validation,
  persistence, job, agent, API, CLI, and review UI workflows from EPIC-001
  through EPIC-010.
- Approved final parity report with 88 comparisons, 85 equal, 3 intentional
  approved differences, and no blocking differences.
- Required CI, security, parity, UI, packaging, clean-install, coverage, and
  rollback gates.
- Operational backup, restore, migration, recovery, cancellation, logging,
  troubleshooting, and rollback runbooks and executable rehearsals.

### Security

- Processing remains local-first, loopback-only by default, approved-root
  bounded, read-only for source data, and without implicit schema network
  access.

Generated manifests remain heuristic and review-required. This release does
not include OSDU ingestion.
