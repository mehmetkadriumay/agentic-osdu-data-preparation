# MCP Workflow Guide

This guide shows how an MCP client such as GitHub Copilot CLI can use the
Agentic OSDU Data Preparation tools in an end-to-end workflow. The example
classifies oil-and-gas files, matches existing manifests, learns only from
approved pairs, generates missing manifests, validates them, and prepares
approved exports.

The MCP server prepares data and manifests only. It does not ingest records
into OSDU.

## 1. Example workspace

Use a workspace with explicit source and output directories:

```text
C:\OSDU\TrainingProject
├── Data\
├── Manifests\
├── SchemaCatalog\
├── generated\
└── exports\
```

- `Data` contains source SEG-Y, LAS, DLIS, LIS/LTI, JSON, CSV, P1/90, SGP,
  DAT, text, or PDF files.
- `Manifests` contains supplied JSON manifests.
- `SchemaCatalog` contains an approved, checksum-pinned local OSDU schema
  catalog.
- `generated` and `exports` are the only intended file-output locations.
- Source directories remain read-only.

Do not use symbolic links, junctions, reparse points, UNC paths, device paths,
or paths that escape the approved workspace.

## 2. Connect Copilot to the MCP server

### Stdio transport

Install the package and register the local MCP executable:

```powershell
copilot mcp add agentic-osdu-data-preparation -- agentic-osdu-mcp
copilot mcp get agentic-osdu-data-preparation
```

When Copilot CLI is started from this repository, the committed
`.github/mcp.json` provides the same configuration after folder trust is
confirmed.

### Streamable HTTP transport

Start the loopback service:

```powershell
agentic-osdu web-serve --host 127.0.0.1 --port 8000
```

Register its MCP endpoint:

```powershell
copilot mcp add --transport http agentic-osdu-http `
  http://127.0.0.1:8000/mcp/
copilot mcp get agentic-osdu-http
```

Do not expose the HTTP endpoint through a non-loopback address or reverse
proxy without a separately reviewed authentication and transport-security
design.

## 3. Start the workflow in Copilot

Grant Copilot access to the workspace:

```text
/add-dir C:\OSDU\TrainingProject
```

Use an initial prompt that establishes the safety boundary:

```text
Use the agentic-osdu-data-preparation MCP server.

Prepare C:\OSDU\TrainingProject:
- source data: Data
- supplied manifests: Manifests
- local schema catalog: SchemaCatalog
- generated candidates: generated
- approved exports: exports

Keep source files read-only. Do not follow links or reparse points. Do not
perform OSDU ingestion. Stop at every human-approval boundary and show the
evidence, trust level, validation result, and proposed side effect.
```

Copilot discovers the exact request schemas from MCP. Each call uses the
standard typed request envelope:

```json
{
  "request_id": "<unique UUID>",
  "workspace_id": "<registered workspace UUID>",
  "actor": "<current actor>",
  "input": {},
  "cancellation_token_id": null,
  "expected_state_version": null
}
```

Use values returned by earlier tools rather than inventing identifiers,
versions, paths, checksums, or approval receipts.

## 4. Register the approved workspace

Copilot first calls:

```text
tool_001_register_approved_workspace
```

The request identifies the absolute workspace root, approved source
subdirectories, and allowed output subdirectories. The result provides the
`workspace_id` required by later calls.

Expected behavior:

1. The root is canonicalized.
2. traversal and root escape are rejected;
3. links, junctions, and reparse points are rejected;
4. source locations are registered as read-only;
5. writes are restricted to approved output locations;
6. a filesystem-identity-bound policy fingerprint is persisted.

Save the returned `workspace_id`. If the workspace directory or its policy
changes, register it again.

## 5. Discover and classify files (WF-001)

Ask Copilot:

```text
Discover every supported file under the approved Data directory. Classify
each file using bounded reads and the appropriate format-specific extractor.
Persist one versioned inventory, then show a review table with filename,
detected format, oil-and-gas category, subtype, processing/stack information,
survey or well evidence, OSDU kind, confidence, trust, and warnings.
```

Copilot executes this dependency chain:

| Order | MCP tool | Purpose |
|---|---|---|
| 1 | `tool_002_discover_workspace_files` | Enumerate files below the approved `Data` root. |
| 2 | `tool_003_read_bounded_file_sample` | Read only the bounded bytes or text needed for identification. |
| 3 | `tool_004_detect_file_format` | Combine extension, magic bytes, and structural evidence. |
| 4 | `tool_006` through `tool_013` extractors | Extract format-specific metadata for each compatible file. |
| 5 | `tool_005_classify_oil_and_gas_data` | Produce the normalized domain classification and OSDU-kind recommendation. |
| 6 | `tool_022_persist_inventory` | Persist the joined, versioned inventory transactionally. |
| 7 | `tool_027_build_inventory_review_view` | Build a redacted table for human review. |

The format-specific tools are:

| Tool | Use |
|---|---|
| `tool_006_extract_seg_y_metadata` | SEG-Y headers, endianness, dimensions, and sampled trace structure |
| `tool_007_extract_las_metadata` | LAS sections, curves, well metadata, and bounded statistics |
| `tool_008_extract_json_well_log_metadata` | JSON well-log metadata, curves, and data shape |
| `tool_009_extract_dlis_metadata` | DLIS logical files, frames, channels, origins, and well metadata |
| `tool_010_extract_lis_lti_metadata` | LIS/LTI records and well-log metadata |
| `tool_011_extract_csv_metadata` | CSV delimiter, headers, row samples, and shape |
| `tool_012_extract_p1_90_navigation_metadata` | P1/90 headers, navigation positions, lines, and coordinates |
| `tool_013_extract_interpretation_and_document_metadata` | Explicit SGP, DAT, text, and PDF subtype metadata |

Review low-confidence or conflicting classifications before using them for
manifest generation. Detection and classification results are derived
evidence, not authoritative source records.

## 6. Parse and match supplied manifests (WF-002)

Ask Copilot:

```text
Parse the approved Manifests directory and match each classified data file to
the best manifest. Use dataset references when filenames do not match. Show
the ranked candidates, match level, score, evidence, and unresolved files.
Persist proposed associations, but do not treat heuristic matches as approved.
```

Copilot calls:

1. `tool_014_read_and_parse_manifests` to discover and parse JSON manifests.
2. `tool_015_extract_manifest_records_and_dataset_references` to index record
   kinds, surrogate IDs, dataset paths, and relationships.
3. `tool_016_match_manifest` once per eligible file to rank candidates using
   explicit precedence rules.
4. `tool_023_persist_manifest_associations` to save the versioned proposals.
5. `tool_028_build_manifest_review_view` for detailed manifest, association,
   provenance, and validation evidence.

`TOOL-016` output is heuristic and review-required. A high score is evidence,
not human approval.

## 7. Review the inventory and associations

Use:

```text
tool_027_build_inventory_review_view
tool_028_build_manifest_review_view
```

Ask Copilot to highlight:

- unsupported or ambiguous files;
- format evidence that conflicts with the extension;
- pre-stack versus post-stack seismic evidence;
- time versus depth domain;
- well and survey identifiers;
- unmatched files;
- multiple high-scoring manifest candidates;
- stale or failed schema validation;
- generated values and their provenance;
- trust levels: authoritative, verified, derived, heuristic, or untrusted.

Do not learn from or export an association until the user has reviewed it.

## 8. Learn from approved examples (WF-003)

After reviewing supplied, non-generated pairs, ask:

```text
Learn category-scoped manifest patterns only from the associations I approved.
Show the proposed learning-model version and its source examples. Do not
activate or mutate the model until I approve the signed workflow action.
```

The workflow uses:

1. `tool_017_learn_manifest_patterns` to derive patterns from approved,
   non-generated examples.
2. `tool_024_persist_learning_models` to create, activate, deactivate, or
   clear a model version.

Direct MCP mutations through `TOOL-024` fail with
`HUMAN_APPROVAL_REQUIRED`. The second step must run through WF-003's signed,
payload-bound approval boundary in the application. Copilot cannot claim that
the user approved an action.

## 9. Import and use a pinned schema catalog

For an approved local catalog, Copilot can call:

```text
tool_021_refresh_or_import_schema_catalog
```

The local catalog must be contained within the persisted approved workspace
root. The import:

- verifies checksums;
- rejects symlinks, junctions, and reparse points;
- binds reads to the approved filesystem identity;
- detects directory or file replacement during import;
- installs immutable catalog revisions;
- fails closed if the workspace was registered using an obsolete policy
  fingerprint.

Remote catalog refresh is disabled at the direct MCP boundary and requires a
separately trusted network approval.

## 10. Generate one manifest as a dry run (WF-004)

Ask Copilot:

```text
For the selected unmatched inventory record, generate one manifest in dry-run
mode using the active approved learning model. Validate it against the pinned
schema catalog and show a manifest review diff. Do not write a file or record
a review decision.
```

Copilot calls:

1. `tool_018_generate_one_manifest` with `dry_run` enabled.
2. `tool_020_validate_osdu_schemas` using the exact pinned catalog ID.
3. `tool_028_build_manifest_review_view` to display the candidate, source
   evidence, generation diff, validation report, and provenance.

Generated fields have heuristic trust and remain review-required. Direct MCP
write-mode calls to `TOOL-018` fail with `HUMAN_APPROVAL_REQUIRED`.

At the human gate, inspect:

- the proposed OSDU kind and schema version;
- dataset references;
- identifiers and relationships;
- inherited versus generated values;
- validation errors and warnings;
- the complete diff from the selected prototype;
- output path and side effects.

Writing the candidate and recording the decision must run through the signed
WF-004 approval path.

## 11. Generate missing manifests as a job (WF-005)

For a reviewed batch, ask:

```text
Prepare a batch-generation plan for all eligible unmatched inventory records.
Use the active approved learning model and pinned schema catalog. Show the
number of candidates, proposed job steps, output paths, and review impact.
Wait for my approval before creating the generation job.
```

After signed approval, WF-005 executes:

| Order | Tool | Purpose |
|---|---|---|
| 1 | `tool_025_track_job_execution` | Create the approved persisted job. |
| 2 | `tool_019_generate_all_missing_manifests` | Generate bounded candidate manifests. |
| 3 | `tool_020_validate_osdu_schemas` | Validate each candidate against the pinned catalog. |
| 4 | `tool_022_persist_inventory` | Persist candidate inventory state. |
| 5 | `tool_023_persist_manifest_associations` | Persist proposed generated associations. |
| 6 | `tool_027_build_inventory_review_view` | Produce the joined batch review view. |

Direct MCP write-mode generation and WF-005 job creation fail closed unless
they execute through the signed workflow boundary.

## 12. Monitor or cancel a job (WF-007)

Use:

```text
tool_026_query_or_cancel_job
```

Example prompt:

```text
Query the current job and summarize its ordered events, completed steps,
partial failures, remaining work, and cancellation state. If I request
cancellation, send a cooperative cancellation request and continue polling
until the persisted state is terminal.
```

Cancellation is cooperative. A cancellation request is not confirmation that
all work has already stopped. Continue querying until the job reports a
terminal status.

## 13. Review, decide, and export (WF-006)

Ask Copilot:

```text
Build the final manifest review view. Refresh validation if it is stale.
Present the candidate, provenance, trust, association evidence, schema result,
and exact export destination. Stop for my decision.
```

The workflow uses:

1. `tool_028_build_manifest_review_view`;
2. optional `tool_020_validate_osdu_schemas`;
3. `tool_029_record_review_decision`;
4. optional `tool_030_export_approved_results`.

`TOOL-029` and `TOOL-030` are human actions. Direct MCP calls fail with
`HUMAN_APPROVAL_REQUIRED`; they must execute through the signed WF-006 review
boundary. Export is permitted only when the reviewed version is approved and
the destination is an approved output root.

An export does not ingest anything into OSDU. It writes only the approved
machine-readable report or candidate manifest.

## 14. Recommended conversational sequence

The following prompts keep Copilot within the intended gates:

1. **Classify**

   ```text
   Register the workspace, run WF-001, and show the inventory review. Stop
   before manifest association.
   ```

2. **Associate**

   ```text
   Run WF-002 against the supplied Manifests directory. Show unresolved and
   heuristic matches. Do not approve them.
   ```

3. **Review**

   ```text
   Show detailed evidence for rows with ambiguous classification, multiple
   candidate manifests, or low-confidence matches.
   ```

4. **Learn**

   ```text
   Use only the supplied manifest pairs I approved to propose a learning
   model. Stop before TOOL-024 mutation.
   ```

5. **Generate**

   ```text
   Dry-run one generated manifest, validate it, and show the complete diff.
   Do not write the candidate.
   ```

6. **Approve in the application**

   ```text
   Prepare the signed workflow action and show exactly what will be written.
   Wait for my explicit decision in the review interface.
   ```

7. **Batch**

   ```text
   After approval, start WF-005 and monitor it with TOOL-026. Report partial
   failures instead of hiding them.
   ```

8. **Export**

   ```text
   Build the final WF-006 review. Export only the approved version to the
   approved exports directory after the signed human decision.
   ```

## 15. Complete MCP tool map

| ID | MCP name | Role in a workflow |
|---|---|---|
| TOOL-001 | `tool_001_register_approved_workspace` | Establish the path and write policy |
| TOOL-002 | `tool_002_discover_workspace_files` | Discover bounded source files |
| TOOL-003 | `tool_003_read_bounded_file_sample` | Read bounded identification evidence |
| TOOL-004 | `tool_004_detect_file_format` | Detect candidate formats |
| TOOL-005 | `tool_005_classify_oil_and_gas_data` | Normalize oil-and-gas classification |
| TOOL-006 | `tool_006_extract_seg_y_metadata` | Extract SEG-Y metadata |
| TOOL-007 | `tool_007_extract_las_metadata` | Extract LAS metadata |
| TOOL-008 | `tool_008_extract_json_well_log_metadata` | Extract JSON well-log metadata |
| TOOL-009 | `tool_009_extract_dlis_metadata` | Extract DLIS metadata |
| TOOL-010 | `tool_010_extract_lis_lti_metadata` | Extract LIS/LTI metadata |
| TOOL-011 | `tool_011_extract_csv_metadata` | Extract CSV metadata |
| TOOL-012 | `tool_012_extract_p1_90_navigation_metadata` | Extract P1/90 navigation metadata |
| TOOL-013 | `tool_013_extract_interpretation_and_document_metadata` | Extract SGP, DAT, text, or PDF metadata |
| TOOL-014 | `tool_014_read_and_parse_manifests` | Parse supplied manifests |
| TOOL-015 | `tool_015_extract_manifest_records_and_dataset_references` | Index manifest records and references |
| TOOL-016 | `tool_016_match_manifest` | Rank file-to-manifest matches |
| TOOL-017 | `tool_017_learn_manifest_patterns` | Derive patterns from approved pairs |
| TOOL-018 | `tool_018_generate_one_manifest` | Dry-run or approved single generation |
| TOOL-019 | `tool_019_generate_all_missing_manifests` | Approved batch generation |
| TOOL-020 | `tool_020_validate_osdu_schemas` | Validate against exact pinned schemas |
| TOOL-021 | `tool_021_refresh_or_import_schema_catalog` | Import or approved-refresh schema catalogs |
| TOOL-022 | `tool_022_persist_inventory` | Persist versioned inventory state |
| TOOL-023 | `tool_023_persist_manifest_associations` | Persist association state |
| TOOL-024 | `tool_024_persist_learning_models` | Persist approved learning-model state |
| TOOL-025 | `tool_025_track_job_execution` | Create and execute bounded jobs |
| TOOL-026 | `tool_026_query_or_cancel_job` | Query or cooperatively cancel jobs |
| TOOL-027 | `tool_027_build_inventory_review_view` | Build the inventory review projection |
| TOOL-028 | `tool_028_build_manifest_review_view` | Build manifest details and generation diff |
| TOOL-029 | `tool_029_record_review_decision` | Record a signed human decision |
| TOOL-030 | `tool_030_export_approved_results` | Export approved reviewed results |

## 16. Failure handling

Every MCP tool returns the existing typed `ToolResult` or a redacted error
envelope. The client should:

1. check the result status before using output;
2. preserve warnings, evidence, provenance, and trust;
3. stop on access, approval, schema, or version conflicts;
4. never interpret `partially_succeeded` as success;
5. use the returned retryability indicator rather than retrying blindly;
6. use `expected_state_version` for state-changing operations when supplied;
7. generate a new `request_id` for a new operation;
8. avoid putting credentials or unnecessary sensitive values in MCP requests;
9. surface unresolved items instead of fabricating defaults;
10. require a signed application workflow for every human-gated action.

The intended result is a traceable preparation pipeline in which Copilot can
inspect and coordinate deterministic tools, while filesystem writes, learned
state, generated manifests, review decisions, and exports remain bounded by
explicit policy and human approval.
