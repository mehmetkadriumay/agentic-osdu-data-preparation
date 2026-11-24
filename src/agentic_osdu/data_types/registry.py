"""Immutable, explicitly composed data-type handlers."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel

from agentic_osdu.data_types.models import DataTypeHandler
from agentic_osdu.domain.models import FormatId
from agentic_osdu.tools.contracts import TOOL_REGISTRY


class DataTypeRegistry:
    """One routing source for detection, classification, and metadata extraction."""

    def __init__(self, handlers: Iterable[DataTypeHandler]) -> None:
        by_format: dict[FormatId, DataTypeHandler] = {}
        by_tool: dict[str, list[DataTypeHandler]] = {}
        for handler in handlers:
            if handler.format_id in by_format:
                raise ValueError(f"duplicate data-type handler: {handler.format_id}")
            if any(
                not extension.startswith(".") or extension != extension.casefold()
                for extension in handler.extensions
            ):
                raise ValueError("handler extensions must be normalized lowercase suffixes")
            by_format[handler.format_id] = handler
            binding = handler.extraction
            if binding is None:
                continue
            definition = TOOL_REGISTRY.get(binding.tool_id)
            if definition is None or binding.input_model is not definition.input_model:
                raise ValueError("extraction binding must use a registered tool input contract")
            if len(dict(binding.selectors)) != len(binding.selectors) or any(
                key not in binding.input_model.model_fields or key == "file_id"
                for key, _ in binding.selectors
            ):
                raise ValueError("extraction selectors must be unique input discriminator fields")
            binding.build_input(UUID(int=0))
            siblings = by_tool.setdefault(binding.tool_id, [])
            for sibling in siblings:
                other = sibling.extraction
                assert other is not None
                left, right = dict(binding.selectors), dict(other.selectors)
                if not any(left[key] != right[key] for key in left.keys() & right.keys()):
                    raise ValueError("ambiguous extraction bindings for the same tool")
            siblings.append(handler)
        self._by_format = MappingProxyType(by_format)
        self._by_tool = MappingProxyType({key: tuple(value) for key, value in by_tool.items()})

    @property
    def handlers(self) -> tuple[DataTypeHandler, ...]:
        return tuple(self._by_format.values())

    @property
    def extraction_tool_ids(self) -> tuple[str, ...]:
        return tuple(self._by_tool)

    def get(self, format_id: FormatId) -> DataTypeHandler | None:
        return self._by_format.get(format_id)

    def extraction_handler(self, tool_id: str, value: BaseModel) -> DataTypeHandler:
        for handler in self._by_tool.get(tool_id, ()):
            binding = handler.extraction
            if binding is not None and binding.accepts(value):
                return handler
        raise ValueError("no data-type handler accepts this extraction request")
