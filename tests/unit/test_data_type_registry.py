from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_osdu.data_types import BUILTIN_DATA_TYPES, DataTypeRegistry, FormatMatch
from agentic_osdu.domain.models import FileAssetRef, FormatId, WorkspaceRelativePath
from agentic_osdu.tools.contracts import ClassifyDataInput, ExtractLasInput
from agentic_osdu.tools.detection import DetectionError, classify_data, detect_format


def test_registry_rejects_duplicate_formats_and_ambiguous_extraction_routes() -> None:
    handler = BUILTIN_DATA_TYPES.handlers[0]
    with pytest.raises(ValueError, match="duplicate data-type handler"):
        DataTypeRegistry((handler, handler))
    with pytest.raises(ValueError, match="ambiguous extraction bindings"):
        DataTypeRegistry((handler, replace(handler, format_id=FormatId.TEXT)))


@pytest.mark.parametrize("extension", ["segy", ".SEGY"])
def test_registry_rejects_non_normalized_extensions(extension: str) -> None:
    with pytest.raises(ValueError, match="normalized lowercase suffixes"):
        DataTypeRegistry((replace(BUILTIN_DATA_TYPES.handlers[0], extensions=(extension,)),))


def test_registry_rejects_unregistered_tools_and_mismatched_input_contracts() -> None:
    handler = BUILTIN_DATA_TYPES.handlers[0]
    binding = handler.extraction
    assert binding is not None
    for invalid in (
        replace(binding, tool_id="missing"),
        replace(binding, input_model=ExtractLasInput),
    ):
        with pytest.raises(ValueError, match="registered tool input contract"):
            DataTypeRegistry((replace(handler, extraction=invalid),))


@pytest.mark.parametrize(
    "selectors",
    [
        (("missing", "value"),),
        (("file_id", "value"),),
        (("subtype", "text"), ("subtype", "pdf")),
    ],
)
def test_registry_rejects_invalid_selectors(selectors: tuple[tuple[str, str], ...]) -> None:
    handler = BUILTIN_DATA_TYPES.get(FormatId.TEXT)
    assert handler is not None
    assert handler.extraction is not None
    with pytest.raises(ValueError, match="unique input discriminator fields"):
        DataTypeRegistry(
            (replace(handler, extraction=replace(handler.extraction, selectors=selectors)),)
        )


def test_registry_validates_selector_values_with_the_strict_input_contract() -> None:
    handler = BUILTIN_DATA_TYPES.get(FormatId.TEXT)
    assert handler is not None
    assert handler.extraction is not None
    with pytest.raises(ValidationError):
        DataTypeRegistry(
            (
                replace(
                    handler,
                    extraction=replace(handler.extraction, selectors=(("subtype", "invalid"),)),
                ),
            )
        )


def test_shared_extraction_tool_routes_every_subtype_and_builds_typed_inputs() -> None:
    file_id = uuid4()
    for handler in BUILTIN_DATA_TYPES.handlers:
        binding = handler.extraction
        if binding is None:
            continue
        value = binding.build_input(file_id)
        assert value.file_id == file_id  # type: ignore[attr-defined]
        assert BUILTIN_DATA_TYPES.extraction_handler(binding.tool_id, value) is handler
    with pytest.raises(ValueError, match="no data-type handler"):
        BUILTIN_DATA_TYPES.extraction_handler("missing", ExtractLasInput(file_id=file_id))
    with pytest.raises(ValueError, match="no data-type handler"):
        BUILTIN_DATA_TYPES.extraction_handler("TOOL-013", ExtractLasInput(file_id=file_id))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan")])
def test_custom_detection_rules_cannot_emit_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        FormatMatch(confidence, "CUSTOM.RULE", "Custom detection.", "signature")


def test_missing_classifier_fails_closed_and_detection_ties_are_stable() -> None:
    file = FileAssetRef(
        file_id=uuid4(),
        workspace_id=uuid4(),
        relative_path=WorkspaceRelativePath("notes.txt"),
        size_bytes=4,
        modified_at=datetime.now(UTC),
        discovery_version=1,
    )
    text = BUILTIN_DATA_TYPES.get(FormatId.TEXT)
    pdf = BUILTIN_DATA_TYPES.get(FormatId.PDF)
    assert text is not None
    assert pdf is not None
    registry = DataTypeRegistry((replace(pdf, extensions=(".txt",)), text))
    outcome = detect_format(file, (b"note",), data_types=registry)
    assert [candidate.format_id for candidate in outcome.output.detection.candidates] == [
        FormatId.TEXT,
        FormatId.PDF,
    ]
    assert detect_format(file, (b"note",), data_types=registry) == outcome
    with pytest.raises(DetectionError, match="UNSUPPORTED_FORMAT"):
        classify_data(
            ClassifyDataInput(file_id=file.file_id, detection=outcome.output.detection),
            file,
            data_types=DataTypeRegistry(()),
        )


def test_json_envelope_precedence_is_preserved() -> None:
    file = FileAssetRef(
        file_id=uuid4(),
        workspace_id=uuid4(),
        relative_path=WorkspaceRelativePath("ambiguous.json"),
        size_bytes=100,
        modified_at=datetime.now(UTC),
        discovery_version=1,
    )
    result = detect_format(file, (b'{"$schema":"schema","type":"object","Data":{}}',))
    assert [item.format_id for item in result.output.detection.candidates] == [FormatId.OSDU_SCHEMA]
