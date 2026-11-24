"""Extension contracts for bounded detection, classification, and extraction."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    DataCategory,
    DataDomain,
    DataSubtype,
    FormatId,
    ProcessingLevel,
    StackType,
    SurveyType,
    WellDataType,
)
from agentic_osdu.formats import ExtractionOutcome


@dataclass(frozen=True)
class DetectionContext:
    """Already-bounded content; detection handlers receive no file capability."""

    sample: bytes
    suffix: str
    upper: bytes
    stripped: bytes

    @cached_property
    def json_document(self) -> object:
        """Decode a bounded JSON sample at most once across all handlers."""

        if not self.stripped.startswith((b"{", b"[")):
            return None
        try:
            document: object = json.loads(self.stripped)
            return document
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def no_signature(context: DetectionContext) -> tuple[FormatMatch, ...]:
    return ()


@dataclass(frozen=True, slots=True)
class FormatMatch:
    confidence: float
    rule_id: str
    summary: str
    observed: str

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("format confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class ClassificationProfile:
    category: DataCategory
    subtype: DataSubtype
    osdu_kind: str | None
    stack: StackType = StackType.NOT_APPLICABLE
    domain: DataDomain = DataDomain.NOT_APPLICABLE
    processing: ProcessingLevel = ProcessingLevel.NOT_APPLICABLE
    survey: SurveyType = SurveyType.NOT_APPLICABLE
    well: WellDataType = WellDataType.NOT_APPLICABLE
    dimensions: ClassificationDimensions = field(default_factory=ClassificationDimensions)


@dataclass(frozen=True, slots=True)
class ExtractionBinding:
    """Connect a handler to an existing typed tool and optional subtype selector."""

    tool_id: str
    input_model: type[BaseModel]
    extractor: Callable[..., ExtractionOutcome[Any]]
    selectors: tuple[tuple[str, str], ...] = ()

    def build_input(self, file_id: UUID) -> BaseModel:
        return self.input_model.model_validate({"file_id": file_id, **dict(self.selectors)})

    def accepts(self, value: BaseModel) -> bool:
        return isinstance(value, self.input_model) and all(
            getattr(value, key, None) == expected for key, expected in self.selectors
        )


@dataclass(frozen=True, slots=True)
class DataTypeHandler:
    format_id: FormatId
    extensions: tuple[str, ...]
    detect: Callable[[DetectionContext], tuple[FormatMatch, ...]]
    classify: Callable[[str], ClassificationProfile]
    extraction: ExtractionBinding | None = None
