"""Production composition for the local API and review UI."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from agentic_osdu.agents.orchestrator import OrchestrationError, ToolRegistryAdapter
from agentic_osdu.domain.models import (
    EvidenceRecord,
    FileAssetRef,
    FormatId,
    MetadataExtractionRef,
    ProvenanceRecord,
    TrustLevel,
    ValidationStatus,
)
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import (
    extract_dlis,
    extract_json_well_log,
    extract_las,
    extract_lis,
)
from agentic_osdu.jobs.service import JobCancelled, JobError, JobExecutionContext, JobService
from agentic_osdu.manifests.generate import GenerationItem, GenerationService
from agentic_osdu.manifests.learn import LearningMaterial, learn_manifest_patterns
from agentic_osdu.manifests.match import match_manifest
from agentic_osdu.manifests.parse import ManifestService
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.schemas import SchemaCatalogStore, SchemaValidationService
from agentic_osdu.state.database import StateDatabase, create_sqlite_state
from agentic_osdu.state.models import Base
from agentic_osdu.state.repositories import StateRepository
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    BuildInventoryReviewInput,
    BuildManifestReviewInput,
    ClassifyDataInput,
    DetectFormatInput,
    DetectFormatOutput,
    DiscoveryOutput,
    ExportToolInput,
    ExtractCsvInput,
    ExtractDlisInput,
    ExtractedMetadataContract,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractLisInput,
    ExtractP190Input,
    ExtractSegyInput,
    FileRecordContract,
    FileSampleOutput,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    InterpretationSubtype,
    InventoryMutation,
    LearningModelContract,
    LearnManifestPatternsInput,
    MatchManifestInput,
    ParsedManifest,
    ParseManifestsInput,
    PersistAssociationsInput,
    PersistInventoryInput,
    PersistLearningModelInput,
    QueryOrCancelJobInput,
    ReadFileSampleInput,
    RecordReviewDecisionInput,
    SampleMode,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    TrackJobInput,
    ValidateSchemasInput,
    WorkspaceDescriptor,
)
from agentic_osdu.tools.detection import classify_data, detect_format
from agentic_osdu.tools.discovery import (
    DiscoveryError,
    DiscoveryService,
    InMemoryWorkspacePolicyStore,
)
from agentic_osdu.tools.review import ReviewService

_TOOL_FORMATS = {
    "TOOL-006": FormatId.SEGY,
    "TOOL-007": FormatId.LAS,
    "TOOL-008": FormatId.JSON_WELL_LOG,
    "TOOL-009": FormatId.DLIS,
    "TOOL-010": FormatId.LIS_LTI,
    "TOOL-011": FormatId.CSV,
    "TOOL-012": FormatId.P190,
    "TOOL-013": FormatId.TEXT,
}

_FORMAT_TOOL_INPUTS: dict[FormatId, tuple[str, Callable[..., Any]]] = {
    FormatId.SEGY: ("TOOL-006", ExtractSegyInput),
    FormatId.LAS: ("TOOL-007", ExtractLasInput),
    FormatId.JSON_WELL_LOG: ("TOOL-008", ExtractJsonWellLogInput),
    FormatId.DLIS: ("TOOL-009", ExtractDlisInput),
    FormatId.LIS_LTI: ("TOOL-010", ExtractLisInput),
    FormatId.CSV: ("TOOL-011", ExtractCsvInput),
    FormatId.P190: ("TOOL-012", ExtractP190Input),
}

_INTERPRETATION_SUBTYPES = {
    FormatId.SGP: InterpretationSubtype.SGP,
    FormatId.DAT: InterpretationSubtype.DAT,
    FormatId.TEXT: InterpretationSubtype.TEXT,
    FormatId.PDF: InterpretationSubtype.PDF,
}


class _DenyOutputPolicy:
    def authorize_output(self, root_id: str, relative_path: str, **_kwargs: object) -> Any:
        del root_id, relative_path
        raise ValueError("No export output root is configured.")


def _result(
    tool_id: str,
    request: ToolRequest[Any],
    output: Any,
    *,
    evidence: tuple[EvidenceRecord, ...] | None = None,
    provenance: tuple[ProvenanceRecord, ...] | None = None,
    source_ref: str | None = None,
) -> ToolResult[Any]:
    now = datetime.now(UTC)
    definition = TOOL_REGISTRY[tool_id]
    return ToolResult[Any](
        request_id=request.request_id,
        tool_id=tool_id,
        tool_version=definition.version,
        status=ToolResultStatus.SUCCEEDED,
        output=output,
        provenance=provenance
        or (
            ProvenanceRecord(
                provenance_id=uuid4(),
                source_type="local_runtime",
                source_ref=source_ref or tool_id,
                tool_id=tool_id,
                tool_version=definition.version,
                recorded_at=now,
            ),
        ),
        evidence=evidence
        if evidence is not None
        else (
            EvidenceRecord(
                evidence_id=uuid4(),
                evidence_type="runtime_execution",
                rule_id=f"{tool_id}-RUNTIME",
                summary="The registered deterministic tool completed.",
                trust_level=definition.default_trust or TrustLevel.UNTRUSTED,
            ),
        ),
        trust_level=definition.default_trust or TrustLevel.UNTRUSTED,
        started_at=now,
        finished_at=now,
    )


class RuntimeComposition:
    """Own the stateful dependencies behind the registered tool adapter."""

    def __init__(self, database: StateDatabase) -> None:
        self.database = database
        self.repository = StateRepository(database.session_factory)
        self.workspace_store = InMemoryWorkspacePolicyStore()
        self.discovery = DiscoveryService(store=self.workspace_store)
        self.jobs = JobService(database.session_factory)
        self.review = ReviewService(self.repository, _DenyOutputPolicy())
        self.schema_catalog_store = SchemaCatalogStore(
            Path(database.engine.url.database or ".").parent / "schemas"
        )
        self.schema_validation = SchemaValidationService(self.schema_catalog_store)
        self._files: dict[UUID, FileAssetRef] = {}
        self._samples: dict[UUID, FileSampleOutput] = {}
        self._file_records: dict[UUID, FileRecordContract] = {}
        self._parsed_manifests: dict[UUID, ParsedManifest] = {}
        self._extracted_metadata: dict[UUID, list[ExtractedMetadataContract]] = {}
        self._learning_models: dict[UUID, LearningModelContract] = {}
        self._models_by_category: dict[str, LearningModelContract] = {}
        self._inventory_files: dict[UUID, set[UUID]] = {}
        self._matched_files: set[UUID] = set()
        self._evidence_by_id: dict[UUID, EvidenceRecord] = {}
        self._provenance_by_file: dict[UUID, list[ProvenanceRecord]] = {}
        self._cancellation = threading.local()
        self.registry = ToolRegistryAdapter(
            {
                "TOOL-001": self._register_workspace,
                "TOOL-002": self._discover,
                "TOOL-003": self._read_sample,
                "TOOL-004": self._detect,
                "TOOL-005": self._classify,
                "TOOL-006": lambda request: self._extract_format("TOOL-006", extract_segy, request),
                "TOOL-007": lambda request: self._extract_format("TOOL-007", extract_las, request),
                "TOOL-008": lambda request: self._extract_format(
                    "TOOL-008", extract_json_well_log, request
                ),
                "TOOL-009": lambda request: self._extract_format("TOOL-009", extract_dlis, request),
                "TOOL-010": lambda request: self._extract_format("TOOL-010", extract_lis, request),
                "TOOL-011": lambda request: self._extract_format("TOOL-011", extract_csv, request),
                "TOOL-012": lambda request: self._extract_format("TOOL-012", extract_p190, request),
                "TOOL-013": lambda request: self._extract_format(
                    "TOOL-013", extract_interpretation, request
                ),
                "TOOL-014": self._parse_manifests,
                "TOOL-016": self._match_manifest,
                "TOOL-017": self._learn_patterns,
                "TOOL-018": self._generate_one,
                "TOOL-019": self._generate_all,
                "TOOL-020": self._validate,
                "TOOL-022": self._persist_inventory,
                "TOOL-023": self._persist_associations,
                "TOOL-024": self._persist_learning,
                "TOOL-025": self._start_job,
                "TOOL-026": self._control_job,
                "TOOL-027": self._review_inventory,
                "TOOL-028": self._review_manifest,
                "TOOL-029": self._record_decision,
                "TOOL-030": self._export,
            }
        )

    def _register_workspace(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        descriptor = self.discovery.register_workspace(request.input)
        self.repository.persist_workspace(descriptor, request.actor)
        return _result("TOOL-001", request, descriptor)

    def _discover(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        output: DiscoveryOutput = self.discovery.discover_files(
            request.input,
            cancellation=cast(Any, _CancellationView(self._current_cancellation())),
        )
        for file in output.files:
            self._files[file.file_id] = file
            self._file_records[file.file_id] = FileRecordContract(file=file)
        return _result("TOOL-002", request, output)

    def _read_sample(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: ReadFileSampleInput = request.input
        output = self.discovery.read_file_sample(value)
        self._samples[output.sample.sample_id] = output
        return _result("TOOL-003", request, output)

    def _detect(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: DetectFormatInput = request.input
        file = self._require_file(value.file_id)
        samples = tuple(
            self._require_sample(sample_id).decoded_bytes() for sample_id in value.sample_refs
        )
        outcome = detect_format(file, samples)
        current = self._file_records.get(file.file_id, FileRecordContract(file=file))
        self._file_records[file.file_id] = current.model_copy(
            update={"detection": outcome.output.detection}
        )
        self._remember_observations(file.file_id, outcome.evidence)
        provenance = self._file_provenance("TOOL-004", file.file_id)
        return _result(
            "TOOL-004",
            request,
            DetectFormatOutput(detection=outcome.output.detection),
            evidence=outcome.evidence,
            provenance=provenance,
        )

    def _classify(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: ClassifyDataInput = request.input
        file = self._require_file(value.file_id)
        outcome = classify_data(value, file)
        current = self._file_records.get(file.file_id, FileRecordContract(file=file))
        self._file_records[file.file_id] = current.model_copy(
            update={
                "detection": value.detection,
                "classification": outcome.output.classification,
                "metadata_extractions": value.extracted_metadata,
            }
        )
        self._remember_observations(file.file_id, outcome.evidence)
        provenance = self._file_provenance("TOOL-005", file.file_id)
        return _result(
            "TOOL-005",
            request,
            outcome.output,
            evidence=outcome.evidence,
            provenance=provenance,
        )

    def _record_extraction(
        self,
        tool_id: str,
        request: ToolRequest[Any],
        outcome: Any,
    ) -> ToolResult[Any]:
        file = self._require_file(request.input.file_id)
        if file.sha256 is None:
            file = file.model_copy(update={"sha256": self.discovery.complete_sha256(file.file_id)})
            self._files[file.file_id] = file
            current_record = self._file_records.get(file.file_id, FileRecordContract(file=file))
            self._file_records[file.file_id] = current_record.model_copy(update={"file": file})
        if file.sha256 is None:
            raise OrchestrationError(
                "FILE_CHANGED", "The complete source fingerprint is unavailable."
            )
        provenance = self._file_provenance(tool_id, file.file_id)
        reference = MetadataExtractionRef(
            extraction_id=uuid5(
                file.file_id,
                f"{tool_id}:{TOOL_REGISTRY[tool_id].version}:{file.sha256}",
            ),
            file_id=file.file_id,
            file_sha256=file.sha256,
            format_id=(
                {
                    InterpretationSubtype.SGP: FormatId.SGP,
                    InterpretationSubtype.DAT: FormatId.DAT,
                    InterpretationSubtype.TEXT: FormatId.TEXT,
                    InterpretationSubtype.PDF: FormatId.PDF,
                }[request.input.subtype]
                if tool_id == "TOOL-013"
                else _TOOL_FORMATS[tool_id]
            ),
            parser_version=TOOL_REGISTRY[tool_id].version,
            extracted_at=datetime.now(UTC),
            evidence_ids=tuple(item.evidence_id for item in outcome.evidence),
            provenance_ids=tuple(item.provenance_id for item in provenance),
            trust_level=TrustLevel.VERIFIED,
        )
        extracted = ExtractedMetadataContract(reference=reference, payload=outcome.output)
        values = self._extracted_metadata.setdefault(file.file_id, [])
        values[:] = [
            item for item in values if item.reference.extraction_id != reference.extraction_id
        ]
        values.append(extracted)
        current = self._file_records.get(file.file_id, FileRecordContract(file=file))
        self._file_records[file.file_id] = current.model_copy(
            update={"metadata_extractions": tuple(values)}
        )
        self._remember_observations(file.file_id, outcome.evidence)
        return _result(
            tool_id,
            request,
            outcome.output,
            evidence=outcome.evidence,
            provenance=provenance,
        )

    def _extract_format(
        self,
        tool_id: str,
        extractor: Callable[..., Any],
        request: ToolRequest[Any],
    ) -> ToolResult[Any]:
        return self._record_extraction(
            tool_id,
            request,
            extractor(
                request.input,
                self.discovery.format_source(request.input.file_id),
                cancellation=self._current_cancellation(),
            ),
        )

    def _parse_manifests(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: ParseManifestsInput = request.input
        output = ManifestService(self._workspace_policy(value.workspace_id)).parse_manifests(value)
        self._parsed_manifests.update(
            (manifest.document.manifest_id, manifest) for manifest in output.manifests
        )
        for manifest in output.manifests:
            self.repository.persist_manifest(
                manifest.document,
                manifest.content,
                request_id=request.request_id,
                actor=request.actor,
            )
        return _result("TOOL-014", request, output)

    def _match_manifest(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: MatchManifestInput = request.input
        return _result("TOOL-016", request, match_manifest(value))

    def _learn_patterns(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: LearnManifestPatternsInput = request.input
        examples = self.repository.resolve_learning_examples(value.examples)
        materials = tuple(
            LearningMaterial(
                example=example,
                file_record=self._require_file_record(example.source_file_id),
                manifest=self._require_manifest(example.manifest_id),
            )
            for example in examples
        )
        value = value.model_copy(update={"examples": examples})
        existing = self._models_by_category.get(value.category.value)
        output = learn_manifest_patterns(value, materials, existing_model=existing)
        return _result("TOOL-017", request, output)

    def _generate_one(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: GenerateManifestInput = request.input
        model = self._learning_models.get(value.learning_model_id)
        record = self._file_records.get(value.file_id)
        items = () if model is None or record is None else (self._generation_item(record, model),)
        output = GenerationService(
            self._workspace_policy(request.workspace_id),
            items,
        ).generate_one(value)
        self.repository.persist_generated_candidate(
            output.candidate,
            source_content=model.prototype if model is not None else None,
            request_id=request.request_id,
            actor=request.actor,
        )
        return _result("TOOL-018", request, output)

    def _generate_all(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: GenerateAllManifestsInput = request.input
        records = (
            self._file_records[file_id]
            for file_id in self._inventory_files.get(value.inventory_id, set())
            if file_id in self._file_records
        )
        items = tuple(
            self._generation_item(record, model)
            for record in records
            if record.classification is not None
            and (model := self._models_by_category.get(record.classification.category.value))
            is not None
        )
        output = GenerationService(
            self._workspace_policy(request.workspace_id),
            items,
            inventory_id=value.inventory_id,
        ).generate_all(value, cancellation=self._current_cancellation())
        for candidate in output.candidates:
            model = self._learning_models.get(candidate.reference.learning_model_id)
            self.repository.persist_generated_candidate(
                candidate,
                source_content=model.prototype if model is not None else None,
                request_id=uuid4(),
                actor=request.actor,
            )
        return _result("TOOL-019", request, output)

    def _validate(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: ValidateSchemasInput = request.input
        report = self.schema_validation.validate(value)
        result = _result("TOOL-020", request, report)
        if isinstance(value.manifest, ParsedManifest):
            self.repository.persist_manifest_validation(
                value.manifest.document,
                value.manifest.content,
                report,
                result.provenance,
                request_id=request.request_id,
                actor=request.actor,
            )
        return result

    def _persist_inventory(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        input_value: PersistInventoryInput = request.input
        current_file_ids = set(self._inventory_files.get(input_value.mutation.inventory_id, set()))
        output = self.repository.persist_inventory(
            request_id=request.request_id,
            actor=request.actor,
            request=input_value,
        )
        file_ids = self._inventory_files.setdefault(input_value.mutation.inventory_id, set())
        removed_file_ids = (
            current_file_ids
            if input_value.archive_before_reset
            else set(input_value.mutation.remove_file_ids)
        )
        file_ids.difference_update(removed_file_ids)
        for file_id in removed_file_ids:
            self._files.pop(file_id, None)
            self._file_records.pop(file_id, None)
            self._matched_files.discard(file_id)
            self._provenance_by_file.pop(file_id, None)
            self._evidence_by_id = {
                evidence_id: evidence
                for evidence_id, evidence in self._evidence_by_id.items()
                if evidence.source_file_id != file_id
            }
            for sample_id, sample in tuple(self._samples.items()):
                if sample.sample.file_id == file_id:
                    self._samples.pop(sample_id, None)
        for file in input_value.mutation.files:
            file_ids.add(file.file_id)
            self._files[file.file_id] = file
            self._file_records.setdefault(file.file_id, FileRecordContract(file=file))
        for classification in input_value.mutation.classifications:
            record = self._file_records.get(classification.file_id)
            if record is not None:
                self._file_records[classification.file_id] = record.model_copy(
                    update={"classification": classification}
                )
        return _result("TOOL-022", request, output)

    def _persist_associations(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: PersistAssociationsInput = request.input
        output = self.repository.persist_associations(
            request_id=request.request_id,
            actor=request.actor,
            request=value,
        )
        self._matched_files.update(mutation.association.file_id for mutation in value.mutations)
        return _result("TOOL-023", request, output)

    def _persist_learning(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        input_value: PersistLearningModelInput = request.input
        output = self.repository.persist_learning_model(
            request_id=request.request_id,
            actor=request.actor,
            request=input_value,
        )
        self._reconcile_learning_cache()
        return _result("TOOL-024", request, output)

    def _start_job(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: TrackJobInput = request.input
        created = self.jobs.create_job(
            request_id=request.request_id,
            actor=request.actor,
            request=value,
        )
        if value.workflow_id == "WF-001" and value.discovery is not None and value.inventory_id:
            discovery_input = value.discovery
            inventory_id = value.inventory_id
            discovered: dict[str, Any] = {}
            samples: dict[Any, Any] = {}
            detections: dict[Any, Any] = {}
            classifications: list[Any] = []

            def execute_step(step: Any, context: JobExecutionContext) -> None:
                if step.tool_id == "TOOL-002":
                    discovered["output"] = self._invoke_child(
                        "TOOL-002", request, discovery_input, cancellation=context.cancelled
                    )
                    context.progress({"discovered": len(discovered["output"].files)})
                elif step.tool_id == "TOOL-003":
                    for file in discovered["output"].files:
                        context.checkpoint()
                        sample = self._invoke_child(
                            "TOOL-003",
                            request,
                            ReadFileSampleInput(
                                file_id=file.file_id,
                                mode=SampleMode.PREFIX,
                                max_bytes=min(max(file.size_bytes, 1), 1024 * 1024),
                            ),
                            cancellation=context.cancelled,
                        )
                        samples[file.file_id] = sample
                    context.progress({"sampled": len(samples)})
                elif step.tool_id == "TOOL-004":
                    for file in discovered["output"].files:
                        context.checkpoint()
                        detection_output = self._invoke_child(
                            "TOOL-004",
                            request,
                            DetectFormatInput(
                                file_id=file.file_id,
                                sample_refs=(samples[file.file_id].sample.sample_id,),
                            ),
                            cancellation=context.cancelled,
                        )
                        detections[file.file_id] = detection_output.detection
                    context.progress({"detected": len(detections)})
                elif step.tool_id == "TOOL-005":
                    for file in discovered["output"].files:
                        context.checkpoint()
                        detection = detections[file.file_id]
                        extracted: tuple[ExtractedMetadataContract, ...] = ()
                        if detection.candidates:
                            format_id = detection.candidates[0].format_id
                            binding = _FORMAT_TOOL_INPUTS.get(format_id)
                            if binding is not None:
                                extractor_tool, input_factory = binding
                                self._invoke_child(
                                    extractor_tool,
                                    request,
                                    input_factory(file.file_id),
                                    cancellation=context.cancelled,
                                )
                            elif format_id in _INTERPRETATION_SUBTYPES:
                                self._invoke_child(
                                    "TOOL-013",
                                    request,
                                    ExtractInterpretationInput(
                                        file_id=file.file_id,
                                        subtype=_INTERPRETATION_SUBTYPES[format_id],
                                    ),
                                    cancellation=context.cancelled,
                                )
                            extracted = tuple(self._extracted_metadata.get(file.file_id, ()))
                        classification_output = self._invoke_child(
                            "TOOL-005",
                            request,
                            ClassifyDataInput(
                                file_id=file.file_id,
                                detection=detection,
                                extracted_metadata=extracted,
                            ),
                            cancellation=context.cancelled,
                        )
                        classifications.append(classification_output.classification)
                    context.progress({"classified": len(classifications)})
                elif step.tool_id == "TOOL-022":
                    current = self.repository.get_inventory_version(inventory_id) or 0
                    self._invoke_child(
                        "TOOL-022",
                        request,
                        PersistInventoryInput(
                            mutation=InventoryMutation(
                                inventory_id=inventory_id,
                                files=tuple(
                                    self._files[file.file_id] for file in discovered["output"].files
                                ),
                                detections=tuple(detections.values()),
                                extractions=tuple(
                                    item.reference
                                    for values in self._extracted_metadata.values()
                                    for item in values
                                    if item.reference.file_id
                                    in {file.file_id for file in discovered["output"].files}
                                ),
                                extracted_metadata=tuple(
                                    item
                                    for values in self._extracted_metadata.values()
                                    for item in values
                                    if item.reference.file_id
                                    in {file.file_id for file in discovered["output"].files}
                                ),
                                classifications=tuple(classifications),
                                evidence=tuple(self._evidence_by_id.values()),
                                provenance=tuple(
                                    item
                                    for values in self._provenance_by_file.values()
                                    for item in values
                                ),
                            ),
                            expected_state_version=current,
                        ),
                        cancellation=context.cancelled,
                    )
                else:
                    raise ValueError("WF-001 contains an unsupported runtime step")

            threading.Thread(
                target=self.jobs.execute,
                args=(created.descriptor.job.job_id, execute_step),
                daemon=True,
                name=f"wf-001-{created.descriptor.job.job_id}",
            ).start()
        elif value.workflow_id == "WF-005" and value.generation is not None:
            generation_input = value.generation
            generation_state: dict[str, Any] = {}
            validation_failures = 0

            def execute_generation(step: Any, context: JobExecutionContext) -> None:
                nonlocal validation_failures
                if step.tool_id == "TOOL-019":
                    output = self._invoke_child(
                        "TOOL-019",
                        request,
                        generation_input,
                        cancellation=context.cancelled,
                    )
                    generation_state["output"] = output
                    context.progress(
                        {
                            "generated": output.generated,
                            "existing": output.existing,
                            "failed": output.failed,
                            "skipped": output.skipped,
                            "cancelled": output.cancelled,
                        }
                    )
                elif step.tool_id == "TOOL-020":
                    output = generation_state["output"]
                    schema_catalog_id = generation_input.schema_catalog_id
                    if schema_catalog_id is None:
                        raise ValueError("WF-005 requires a pinned schema catalog ID")
                    validated = []
                    for candidate in output.candidates:
                        context.checkpoint()
                        report = self._invoke_child(
                            "TOOL-020",
                            request,
                            ValidateSchemasInput(
                                manifest=candidate,
                                schema_catalog_id=schema_catalog_id,
                            ),
                            cancellation=context.cancelled,
                        )
                        updated = candidate.model_copy(
                            update={
                                "reference": candidate.reference.model_copy(
                                    update={"validation_status": report.status}
                                )
                            }
                        )
                        self.repository.persist_generated_candidate(
                            updated,
                            validation=report,
                            request_id=uuid4(),
                            actor=request.actor,
                        )
                        validated.append(updated)
                        if report.status is not ValidationStatus.VALID:
                            validation_failures += 1
                    generation_state["candidates"] = tuple(validated)
                    context.progress({"validated": len(validated)})
                elif step.tool_id == "TOOL-022":
                    current = (
                        self.repository.get_inventory_version(generation_input.inventory_id) or 0
                    )
                    self._invoke_child(
                        "TOOL-022",
                        request,
                        PersistInventoryInput(
                            mutation=InventoryMutation(inventory_id=generation_input.inventory_id),
                            expected_state_version=current,
                        ),
                        cancellation=context.cancelled,
                    )
                elif step.tool_id == "TOOL-023":
                    self._invoke_child(
                        "TOOL-023",
                        request,
                        PersistAssociationsInput(
                            mutations=(),
                            expected_state_version=self.repository.get_association_version(),
                        ),
                        cancellation=context.cancelled,
                    )
                elif step.tool_id == "TOOL-027":
                    self._invoke_child(
                        "TOOL-027",
                        request,
                        BuildInventoryReviewInput(inventory_id=generation_input.inventory_id),
                        cancellation=context.cancelled,
                    )
                    output = generation_state["output"]
                    total_failed = output.failed + validation_failures
                    succeeded = len(output.candidates) - validation_failures
                    if total_failed and succeeded == 0:
                        raise JobError(
                            "JOB_ALL_ITEMS_FAILED",
                            "Every attempted generation candidate failed.",
                        )
                    if total_failed:
                        raise JobError(
                            "JOB_PARTIAL_ITEMS_FAILED",
                            "Some generation candidates failed.",
                        )
                else:
                    raise ValueError("WF-005 contains an unsupported runtime step")

            threading.Thread(
                target=self.jobs.execute,
                args=(created.descriptor.job.job_id, execute_generation),
                daemon=True,
                name=f"wf-005-{created.descriptor.job.job_id}",
            ).start()
        return _result("TOOL-025", request, created)

    def _control_job(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: QueryOrCancelJobInput = request.input
        return _result("TOOL-026", request, self.jobs.query(value, actor=request.actor))

    def _review_inventory(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: BuildInventoryReviewInput = request.input
        return _result("TOOL-027", request, self.review.build_inventory_review(value))

    def _review_manifest(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: BuildManifestReviewInput = request.input
        return _result("TOOL-028", request, self.review.build_manifest_review(value))

    def _record_decision(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: RecordReviewDecisionInput = request.input
        output = self.review.record_decision(request_id=request.request_id, request=value)
        return _result("TOOL-029", request, output)

    def _export(self, request: ToolRequest[Any]) -> ToolResult[Any]:
        value: ExportToolInput = request.input
        review = ReviewService(self.repository, self._workspace_policy(request.workspace_id))
        return _result("TOOL-030", request, review.export(value))

    def _workspace_policy(self, workspace_id: UUID) -> WorkspaceAccessPolicy:
        descriptor: WorkspaceDescriptor | None = self.workspace_store.get(workspace_id)
        if descriptor is None:
            raise OrchestrationError(
                "ROOT_POLICY_DENIED", "The workspace has no approved runtime policy."
            )
        relative_outputs = {
            Path(path.root).parts[0]: path.root for path in descriptor.allowed_output_subpaths
        }
        output_roots = {
            root_id: str(Path(descriptor.canonical_root.root).joinpath(*relative.split("/")))
            for root_id, relative in relative_outputs.items()
        }
        return WorkspaceAccessPolicy(
            workspace_id=workspace_id,
            source_root=descriptor.canonical_root.root,
            output_roots=output_roots,
            path_policy=WindowsAwarePathPolicy(
                style=PathStyle.WINDOWS if os.name == "nt" else PathStyle.POSIX
            ),
            allowed_source_output_subpaths=relative_outputs,
        )

    def _require_file(self, file_id: UUID) -> FileAssetRef:
        try:
            return self._files[file_id]
        except KeyError as error:
            raise OrchestrationError(
                "FILE_NOT_FOUND", "The file was not discovered in this runtime."
            ) from error

    def _require_file_record(self, file_id: UUID) -> FileRecordContract:
        try:
            return self._file_records[file_id]
        except KeyError as error:
            raise OrchestrationError(
                "FILE_NOT_FOUND", "The file record is unavailable in this runtime."
            ) from error

    def _require_sample(self, sample_id: UUID) -> FileSampleOutput:
        try:
            return self._samples[sample_id]
        except KeyError as error:
            raise OrchestrationError(
                "INSUFFICIENT_SAMPLE", "The bounded sample is unavailable in this runtime."
            ) from error

    def _require_manifest(self, manifest_id: UUID) -> ParsedManifest:
        try:
            return self._parsed_manifests[manifest_id]
        except KeyError as error:
            raise OrchestrationError(
                "MANIFEST_NOT_FOUND", "The parsed manifest is unavailable in this runtime."
            ) from error

    def _generation_item(
        self,
        record: FileRecordContract,
        model: LearningModelContract,
    ) -> GenerationItem:
        classification = record.classification
        if classification is None:
            raise OrchestrationError(
                "NO_COMPATIBLE_MODEL", "The file has no classification for generation."
            )
        return GenerationItem(
            file_record=record,
            category=classification.category,
            format_id=classification.format_id,
            model=model,
            matched=record.file.file_id in self._matched_files,
        )

    def _invoke_child(
        self,
        tool_id: str,
        parent: ToolRequest[Any],
        value: Any,
        *,
        cancellation: Callable[[], bool] | None = None,
    ) -> Any:
        prior = getattr(self._cancellation, "callback", None)
        self._cancellation.callback = cancellation
        try:
            result = self.registry.invoke(
                tool_id,
                ToolRequest[Any](
                    request_id=uuid4(),
                    workspace_id=parent.workspace_id,
                    actor=parent.actor,
                    input=value,
                    cancellation_token_id=parent.cancellation_token_id,
                ),
            )
        except Exception as error:
            if getattr(error, "code", None) == "CANCELLED":
                raise JobCancelled from error
            raise
        finally:
            self._cancellation.callback = prior
        if result.output is None:
            raise OrchestrationError(
                "TOOL_RESULT_INVALID", "A successful workflow step returned no output."
            )
        return result.output

    def _current_cancellation(self) -> Callable[[], bool] | None:
        return getattr(self._cancellation, "callback", None)

    def _remember_observations(self, file_id: UUID, evidence: tuple[EvidenceRecord, ...]) -> None:
        self._evidence_by_id.update((item.evidence_id, item) for item in evidence)

    def _file_provenance(self, tool_id: str, file_id: UUID) -> tuple[ProvenanceRecord, ...]:
        item = ProvenanceRecord(
            provenance_id=uuid4(),
            source_type="discovered_file",
            source_ref=str(file_id),
            tool_id=tool_id,
            tool_version=TOOL_REGISTRY[tool_id].version,
            recorded_at=datetime.now(UTC),
        )
        self._provenance_by_file.setdefault(file_id, []).append(item)
        return (item,)

    def _reconcile_learning_cache(self) -> None:
        active = self.repository.get_active_learning_models()
        self._learning_models = {item.learning_model_id: item for item in active}
        self._models_by_category = {item.category.value: item for item in active}

    def hydrate(self) -> None:
        """Restore persisted runtime capabilities required by learning and generation."""

        for descriptor in self.repository.list_workspaces():
            self.workspace_store.save(descriptor)
        for inventory_id, record in self.repository.load_runtime_file_records():
            file_id = record.file.file_id
            try:
                self.discovery.restore_discovered_file(record.file)
            except DiscoveryError:
                continue
            self._files[file_id] = record.file
            self._file_records[file_id] = record
            self._inventory_files.setdefault(inventory_id, set()).add(file_id)
            self._extracted_metadata[file_id] = list(record.metadata_extractions)
        for document, content in self.repository.list_manifest_documents():
            self._parsed_manifests[document.manifest_id] = ParsedManifest(
                document=document,
                content=content,
                parser_version=TOOL_REGISTRY["TOOL-014"].version,
                parsed_at=datetime.now(UTC),
            )
        self._matched_files = set(self.repository.list_associated_file_ids())
        self._reconcile_learning_cache()


class _CancellationView:
    def __init__(self, callback: Callable[[], bool] | None) -> None:
        self._callback = callback

    @property
    def is_cancelled(self) -> bool:
        return self._callback is not None and self._callback()


def create_runtime(state_path: Path | None = None) -> RuntimeComposition:
    path = state_path or Path(
        os.environ.get(
            "AGENTIC_OSDU_STATE_PATH",
            Path.home() / ".agentic-osdu" / "state.db",
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    database = create_sqlite_state(f"sqlite:///{path}")
    Base.metadata.create_all(database.engine)
    composition = RuntimeComposition(database)
    composition.hydrate()
    composition.jobs.recover_interrupted()
    return composition


__all__ = ["RuntimeComposition", "create_runtime"]
