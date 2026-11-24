"""Explicit data-type extension point, independent of transports and runtime state."""

from agentic_osdu.data_types import documents, interpretation, seismic, well_logs
from agentic_osdu.data_types.models import (
    ClassificationProfile,
    DataTypeHandler,
    DetectionContext,
    ExtractionBinding,
    FormatMatch,
)
from agentic_osdu.data_types.registry import DataTypeRegistry

BUILTIN_DATA_TYPES = DataTypeRegistry(
    (*seismic.HANDLERS, *well_logs.HANDLERS, *interpretation.HANDLERS, *documents.HANDLERS)
)

__all__ = [
    "BUILTIN_DATA_TYPES",
    "ClassificationProfile",
    "DataTypeHandler",
    "DataTypeRegistry",
    "DetectionContext",
    "ExtractionBinding",
    "FormatMatch",
]
