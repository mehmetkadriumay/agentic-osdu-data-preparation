# Extending data types

## Layers

- `domain.models` and `tools.contracts`: immutable, versioned data contracts and
  the authoritative tool catalog. `ToolDefinition.public_name` is the MCP name;
  `tool_id` remains the internal API/CLI, approval, provenance, and persistence ID.
- `data_types`: explicitly composed detection rules, classification profiles,
  and extraction bindings, grouped into seismic/navigation, well logs,
  interpretation, and supporting documents.
- `formats`: read-only parsers using `FormatSource` capabilities, bounded
  options, cancellation checkpoints, and structured evidence.
- `tools.detection`: format-independent ranking, deterministic evidence IDs,
  classification envelopes, and error handling.
- `runtime`: composition, approved source resolution, job execution, and state.
  Both direct extraction and inventory jobs resolve bindings from the same
  `DataTypeRegistry`.
- `mcp_server`, `api`, and `cli`: transport adapters over the typed tool registry.

No plugin directory scanning, arbitrary imports, or runtime installation is
performed. Handlers are trusted application code, not user-supplied MCP input.

## Extend an existing format

A handler groups a `FormatId`, normalized filename extensions, a detector, a
classifier, and an optional `ExtractionBinding`. A detector receives bounded
bytes and a suffix, never a file path or read capability. A classifier receives
the uppercase workspace-relative name and returns a `ClassificationProfile`.
The shared services still create evidence, enforce ranking, and retain the
existing typed output envelopes.

For example, accept a project-specific CSV suffix without editing the runtime:

```python
from dataclasses import replace
from pathlib import Path

from agentic_osdu.data_types import BUILTIN_DATA_TYPES, DataTypeRegistry
from agentic_osdu.domain.models import FormatId
from agentic_osdu.runtime import create_runtime

csv_handler = BUILTIN_DATA_TYPES.get(FormatId.CSV)
assert csv_handler is not None
project_csv = replace(
    csv_handler,
    extensions=(*csv_handler.extensions, ".trajectory"),
)
data_types = DataTypeRegistry(
    project_csv if handler.format_id is FormatId.CSV else handler
    for handler in BUILTIN_DATA_TYPES.handlers
)
runtime = create_runtime(Path("project-state.db"), data_types=data_types)
try:
    # Invoke runtime.registry through an existing transport or application adapter.
    ...
finally:
    runtime.database.dispose()
```

This adds extension evidence; the existing CSV signature rule still applies
only to `.csv`. Replace `detect` as well if a variant has a distinct signature.
Replace `classify` for project-specific classification, or
`extraction=replace(csv_handler.extraction, extractor=...)` for a new compatible
parser. Return the existing tool's output contract and `ExtractionOutcome`
with evidence. Preserve cancellation and source validation.

For built-in changes, add the handler to the relevant family's `HANDLERS` tuple.
`BUILTIN_DATA_TYPES` is assembled explicitly in `data_types.__init__`; custom
registries are isolated from that default. Keep all built-in handlers when
composing a full MCP runtime, since its tool catalog remains unchanged.

## Share an extraction tool

An `ExtractionBinding` declares the stable tool ID, the registered Pydantic
input model, the parser callable, and optional selector fields. SGP, DAT, text,
and PDF share `TOOL-013` using their typed `InterpretationSubtype` selectors.
Use enum instances, not raw strings, for strict enum fields.

The binding's `build_input(file_id)` supplies keyword arguments and model
defaults; both direct and batch extraction route through `extraction_handler`.
The registry rejects duplicate format IDs, unknown or mismatched tool input
contracts, invalid selectors, and ambiguous routes for a shared tool.

## Add a genuinely new format or metadata schema

The persisted contracts deliberately remain closed and typed. A handler is
not permission to store arbitrary JSON or invent new string IDs.

1. Add an additive `FormatId` and, when needed, classification enum values in
   `domain.models`.
2. Reuse an existing extraction contract when semantically appropriate.
   Otherwise add typed input/output models, include the output in
   `ExtractedMetadataContract.payload`, and register an additive tool definition
   with a unique descriptive public name. Extend the stable tool-ID validators,
   catalog completeness checks, and any relevant workflow role permissions
   deliberately; the existing catalog currently defines 30 tools.
3. Implement a read-only parser in `formats` and a handler in the appropriate
   data family. Register it in the explicit built-in composition or inject a
   registry at the application composition point. No format switch or routing
   table in `runtime` or `tools.detection` needs editing.
4. Add focused detection, classification, parser, and batch tests. Update schema
   snapshots only for intentional contract changes. Adjust detector, classifier,
   or tool versions when changing their semantics.

Manifests and JSON schemas intentionally have detection/classification handlers
without generic metadata extractors; their existing manifest and schema
services retain their own lifecycle and approval policies.

## Compatibility

MCP names no longer have `tool_001_`-style prefixes. For example,
`tool_006_extract_seg_y_metadata` becomes `extract_segy_metadata`, and
`tool_005_classify_oil_and_gas_data` becomes `classify_subsurface_data`.
Reconnect MCP clients to refresh discovery, and update saved prompts or
allowlists that refer to old names. Legacy MCP aliases are not exposed.

The MCP workflow guide lists every public name alongside its unchanged internal
ID. API/CLI operations, typed request/result schemas, approval checks, and
existing stored state retain their original contracts.
