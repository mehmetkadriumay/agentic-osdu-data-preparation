import { envelope, postTool } from "./api.ts";
import type { Filters, InventoryItem, InventoryView } from "./types.ts";
import "./style.css";

export function filterItems(items: InventoryItem[], filters: Filters): InventoryItem[] {
  const search = filters.search.trim().toLocaleLowerCase();
  return items.filter(item => {
    const classification = item.classification;
    const status = item.association?.review_status ?? "unassociated";
    return (!search || JSON.stringify(item).toLocaleLowerCase().includes(search))
      && (!filters.category || classification?.category === filters.category)
      && (!filters.format || classification?.format_id === filters.format)
      && (!filters.status || status === filters.status);
  });
}

export function summarise(items: InventoryItem[]): {
  total: number;
  categories: Record<string, number>;
  formats: Record<string, number>;
} {
  const categories: Record<string, number> = {};
  const formats: Record<string, number> = {};
  for (const item of items) {
    const category = item.classification?.category ?? "unknown";
    const format = item.classification?.format_id ?? "unknown";
    categories[category] = (categories[category] ?? 0) + 1;
    formats[format] = (formats[format] ?? 0) + 1;
  }
  return { total: items.length, categories, formats };
}

const storage = typeof localStorage === "undefined" ? null : localStorage;
let workspaceId = storage?.getItem("workspaceId") ?? crypto.randomUUID();
const inventoryId = storage?.getItem("inventoryId") ?? crypto.randomUUID();
storage?.setItem("workspaceId", workspaceId);
storage?.setItem("inventoryId", inventoryId);
let items: InventoryItem[] = [];
let activeJobId: string | null = null;
let inventoryVersion = 0;
let activeLearningModel: InventoryView["active_learning_model"] = null;
let schemaCatalogId = storage?.getItem("schemaCatalogId") ?? "";

export function buildWorkspaceRegistration(
  generatedOutputSubpath: string,
  exportOutputSubpath: string,
): { read_only: true; allowed_output_subpaths: string[] } {
  const outputSubpaths = [generatedOutputSubpath.trim(), exportOutputSubpath.trim()];
  if (outputSubpaths.some(value => !value)) {
    throw new Error("Select explicit generated and export output subpaths.");
  }
  if (registeredOutputRootIds([outputSubpaths[0] ?? ""])[0] !== "generated") {
    throw new Error("The generated output subpath must be under generated/.");
  }
  return { read_only: true, allowed_output_subpaths: outputSubpaths };
}

export function registeredOutputRootIds(outputSubpaths: string[]): string[] {
  const rootIds = outputSubpaths
    .map(path => path.split(/[\\/]/, 1)[0])
    .filter((value): value is string => Boolean(value));
  return [...new Set(rootIds)];
}

export function manifestReviewUrl(manifestId: string, sourceFileId?: string): string {
  const parameters = new URLSearchParams({ id: manifestId });
  if (sourceFileId) parameters.set("source_file_id", sourceFileId);
  return `/manifest.html?${parameters.toString()}`;
}

export function buildClassificationJob(
  workspace: string,
  inventory: string,
): { workflow_id: string; [key: string]: unknown } {
  return {
    workflow_id: "WF-001",
    inventory_id: inventory,
    discovery: {
      workspace_id: workspace,
      include_paths: [],
      exclude_globs: [],
      max_files: 100000,
      follow_symlinks: false,
    },
    definition: {
      job_type: "WF-001",
      steps: [
        { sequence: 1, tool_id: "TOOL-002", input_ref: "approved-workspace" },
        { sequence: 2, tool_id: "TOOL-003", input_ref: "discovered-files" },
        { sequence: 3, tool_id: "TOOL-004", input_ref: "bounded-samples" },
        { sequence: 4, tool_id: "TOOL-005", input_ref: "format-detections" },
        { sequence: 5, tool_id: "TOOL-022", input_ref: "classification-results" },
      ],
      max_concurrency: 1,
      continue_on_error: false,
    },
    deduplication_key: `WF-001:${workspace}:${inventory}:${crypto.randomUUID()}`,
  };
}

export function buildCancelRequest(jobId: string): { action: string; job_id: string } {
  return { action: "cancel", job_id: jobId };
}

export function buildLearnRequest(category: string, examples: object[]): object {
  return { examples, category, learning_policy_version: "1.0.0" };
}

interface LearningBatch {
  category: string;
  examples: Array<{
    example_id: string;
    source_file_id: string;
    manifest_id: string;
    association_id: string;
    source_sha256: string;
    manifest_sha256: string;
    review_status: "approved";
    generated_manifest: false;
  }>;
}

export function groupLearningExamplesByCategory(
  inventoryItems: InventoryItem[],
): LearningBatch[] {
  const grouped = new Map<string, LearningBatch["examples"]>();
  for (const item of inventoryItems) {
    const classification = item.classification;
    const association = item.association;
    if (
      !classification
      || association?.review_status !== "approved"
      || !association.association_id
      || !association.manifest_id
      || !item.file.sha256
      || !item.manifest_sha256
    ) continue;
    const examples = grouped.get(classification.category) ?? [];
    examples.push({
      example_id: crypto.randomUUID(),
      source_file_id: item.file.file_id,
      manifest_id: association.manifest_id,
      association_id: association.association_id,
      source_sha256: item.file.sha256,
      manifest_sha256: item.manifest_sha256,
      review_status: "approved",
      generated_manifest: false,
    });
    grouped.set(classification.category, examples);
  }
  return [...grouped].map(([category, examples]) => ({ category, examples }));
}

interface JobProgress {
  snapshot: {
    job: { status: string };
    last_event_sequence: number;
    counts: Record<string, number>;
  };
  events: Array<{
    sequence: number;
    current_item?: string | null;
    counts: Record<string, number>;
  }>;
}

export function jobProgressPresentation(
  label: string,
  output: JobProgress,
): { completed: number; text: string } {
  const counts = output.snapshot.counts;
  const countText = Object.entries(counts)
    .map(([name, count]) => `${name.replaceAll("_", " ")} ${count}`)
    .join(", ");
  const currentItem = [...output.events]
    .sort((left, right) => right.sequence - left.sequence)
    .find(event => event.current_item)?.current_item;
  const completed = label === "Bulk generation"
    ? ["generated", "skipped", "failed"].reduce((total, name) => total + (counts[name] ?? 0), 0)
    : Math.max(0, ...Object.values(counts));
  return {
    completed,
    text: [
      `${label} ${output.snapshot.job.status}`,
      countText,
      currentItem,
    ].filter(Boolean).join(" · "),
  };
}

export function buildGenerateOneRequest(fileId: string, modelId: string): object {
  return {
    file_id: fileId,
    learning_model_id: modelId,
    generation_policy_version: "1.0.0",
    dry_run: true,
  };
}

export function buildGenerateAllRequest(inventory: string, catalogId: string): object {
  return {
    inventory_id: inventory,
    schema_catalog_id: catalogId,
    generation_policy_version: "1.0.0",
    continue_on_error: true,
    dry_run: true,
  };
}

export function buildResetRequest(inventory: string, expectedStateVersion: number): object {
  return { inventory_id: inventory, expected_state_version: expectedStateVersion };
}

export function buildLearningMutation(
  action: "deactivate" | "clear",
  model: InventoryView["active_learning_model"],
): object {
  return action === "clear"
    ? { mutation: { action: "clear" } }
    : {
        mutation: {
          action,
          learning_model_id: model?.learning_model_id,
          expected_version: model?.version,
        },
      };
}

function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing UI element: ${id}`);
  return element as T;
}

function render(): void {
  const filters: Filters = {
    search: byId<HTMLInputElement>("search").value,
    category: byId<HTMLSelectElement>("category").value,
    format: byId<HTMLSelectElement>("format").value,
    status: byId<HTMLSelectElement>("status-filter").value,
  };
  const visible = filterItems(items, filters);
  const summary = summarise(visible);
  byId("summary").textContent =
    `${summary.total} files · ${Object.keys(summary.categories).length} categories`;
  byId("inventory").innerHTML = visible.map((item, index) => {
    const classification = item.classification;
    const evidence = item.evidence?.map(value =>
      typeof value === "string" ? value : value.observed_value ?? value.summary
    ) ?? classification?.evidence_ids ?? [];
    const provenance = item.provenance?.map(value =>
      typeof value === "string" ? value : `${value.tool_id} ${value.tool_version}: ${value.source_ref}`
    ) ?? classification?.extraction_ids ?? [];
    const manifestLink = item.association?.manifest_id
      ? `<a href="${manifestReviewUrl(item.association.manifest_id, item.file.file_id)}">Review associated manifest</a>`
      : "";
    const candidateLink = item.candidate
      ? `<a href="${manifestReviewUrl(item.candidate.candidate_id, item.file.file_id)}">Review generated candidate</a>`
      : "";
    return `<article class="inventory-card">
      <button class="row-toggle" aria-expanded="false" aria-controls="detail-${index}">
        <strong>${escapeText(item.file.relative_path)}</strong>
        <span>${escapeText(classification?.format_id ?? "unknown")}</span>
        <span>${escapeText(classification?.category ?? "unknown")}</span>
        <span class="trust">${escapeText(classification?.trust_level ?? "untrusted")}</span>
      </button>
      <button class="generate-one" data-file-id="${escapeText(item.file.file_id)}"
        aria-label="Generate manifest for ${escapeText(item.file.relative_path)}"
        ${activeLearningModel ? "" : "disabled"}>Generate one</button>
      <section id="detail-${index}" hidden>
        <h3>Evidence</h3>
        <ul>${evidence.map(value => `<li>${escapeText(value)}</li>`).join("") || "<li>None</li>"}</ul>
        <h3>Provenance</h3>
        <ul>${provenance.map(value => `<li>${escapeText(value)}</li>`).join("") || "<li>None</li>"}</ul>
        <p>Association: ${escapeText(item.association?.method ?? "none")}</p>
        ${manifestLink}
        ${candidateLink}
      </section>
    </article>`;
  }).join("");
  document.querySelectorAll<HTMLButtonElement>(".row-toggle").forEach(button => {
    button.addEventListener("click", () => {
      const panel = document.getElementById(button.getAttribute("aria-controls") ?? "");
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      if (panel) panel.hidden = expanded;
    });
  });
  document.querySelectorAll<HTMLButtonElement>(".generate-one").forEach(button => {
    button.addEventListener("click", () => void generateOne(button.dataset.fileId ?? ""));
  });
}

function escapeText(value: unknown): string {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

async function loadInventory(): Promise<void> {
  const output = await postTool<InventoryView>(
    "/api/v1/review/inventories",
    envelope(workspaceId, {
      inventory_id: inventoryId,
      query: { limit: 100, offset: 0 },
    }),
  );
  items = output.items;
  inventoryVersion = output.state_version;
  activeLearningModel = output.active_learning_model;
  render();
}

async function pollJob(
  jobId: string,
  label: "Classification" | "Bulk generation",
  afterEventSequence = 0,
): Promise<void> {
  const output = await postTool<JobProgress>("/api/v1/jobs/events", envelope(workspaceId, {
    action: "query",
    job_id: jobId,
    after_event_sequence: afterEventSequence,
  }));
  if (activeJobId !== jobId) return;
  const status = output.snapshot.job.status;
  const presentation = jobProgressPresentation(label, output);
  byId("message").textContent = `${presentation.text}.`;
  const progress = byId<HTMLProgressElement>("progress");
  progress.max = Math.max(presentation.completed, 1);
  progress.value = presentation.completed;
  if (["succeeded", "partially_succeeded", "failed", "cancelled"].includes(status)) {
    activeJobId = null;
    if (status === "succeeded" || status === "partially_succeeded") await loadInventory();
    return;
  }
  window.setTimeout(
    () => void pollJob(jobId, label, output.snapshot.last_event_sequence),
    250,
  );
}

async function registerAndDiscover(): Promise<void> {
  const path = byId<HTMLInputElement>("workspace-path").value.trim();
  if (!path) {
    byId("message").textContent = "Enter an explicit local workspace path.";
    return;
  }
  const generatedOutput = byId<HTMLInputElement>("generated-output-subpath").value.trim();
  const exportOutput = byId<HTMLInputElement>("export-output-subpath").value.trim();
  schemaCatalogId = byId<HTMLInputElement>("schema-catalog-id").value.trim();
  if (!generatedOutput || !exportOutput || !schemaCatalogId) {
    byId("message").textContent =
      "Select generated/export subpaths and a pinned schema catalog ID.";
    return;
  }
  const registration = buildWorkspaceRegistration(generatedOutput, exportOutput);
  byId("progress-panel").hidden = false;
  const registered = await postTool<{
    workspace_id?: string;
    allowed_output_subpaths: string[];
  }>(
    "/api/v1/workspaces",
    envelope(workspaceId, {
      root_path: path,
      ...registration,
    }),
  );
  if (registered.workspace_id) {
    workspaceId = registered.workspace_id;
    storage?.setItem("workspaceId", workspaceId);
    storage?.setItem("schemaCatalogId", schemaCatalogId);
    storage?.setItem(
      "outputRootIds",
      JSON.stringify(registeredOutputRootIds(registered.allowed_output_subpaths)),
    );
  }
  const started = await postTool<{ descriptor: { job: { job_id: string } } }>(
    "/api/v1/jobs",
    envelope(workspaceId, buildClassificationJob(workspaceId, inventoryId)),
  );
  activeJobId = started.descriptor.job.job_id;
  void pollJob(activeJobId, "Classification");
}

async function cancelJob(): Promise<void> {
  if (!activeJobId) {
    byId("message").textContent = "No active job to cancel.";
    return;
  }
  await postTool("/api/v1/jobs/cancel", envelope(workspaceId, buildCancelRequest(activeJobId)));
  byId("message").textContent = "Cancellation requested.";
}

async function resetInventory(): Promise<void> {
  const reset = buildResetRequest(inventoryId, inventoryVersion) as {
    inventory_id: string; expected_state_version: number;
  };
  await postTool("/api/v1/inventories/reset", envelope(workspaceId, {
    mutation: {
      inventory_id: reset.inventory_id,
      files: [],
      detections: [],
      extractions: [],
      classifications: [],
      remove_file_ids: [],
    },
    expected_state_version: reset.expected_state_version,
    archive_before_reset: true,
  }));
  items = [];
  inventoryVersion += 1;
  render();
}

async function mutateLearning(action: "deactivate" | "clear"): Promise<void> {
  if (action === "deactivate" && !activeLearningModel) {
    byId("message").textContent = "No active learning model to deactivate.";
    return;
  }
  const input = buildLearningMutation(action, activeLearningModel);
  await postTool("/api/v1/learning/models", envelope(workspaceId, input));
  await loadInventory();
  byId("message").textContent = `Learning ${action} completed; inventory was unchanged.`;
}

async function learnPatterns(): Promise<void> {
  const batches = groupLearningExamplesByCategory(items);
  if (batches.length === 0) {
    byId("message").textContent = "No approved data/manifest pairs are available to learn.";
    return;
  }
  for (const batch of batches) {
    const learned = await postTool<{ model: object }>(
      "/api/v1/learning/learn",
      envelope(workspaceId, buildLearnRequest(batch.category, batch.examples)),
    );
    const created = await postTool<{ learning_model_id: string; version: number }>(
      "/api/v1/learning/models",
      envelope(workspaceId, {
        mutation: { action: "create", model: learned.model, expected_version: 0 },
      }),
    );
    await postTool(
      "/api/v1/learning/models",
      envelope(workspaceId, {
        mutation: {
          action: "activate",
          learning_model_id: created.learning_model_id,
          expected_version: created.version,
        },
      }),
    );
  }
  await loadInventory();
  byId("message").textContent = `${batches.length} category learning model(s) activated.`;
}

async function generateOne(fileId: string): Promise<void> {
  if (!activeLearningModel) {
    byId("message").textContent = "Activate a learning model before generation.";
    return;
  }
  const generated = await postTool<{
    candidate: { reference: { candidate_id: string } };
  }>(
    "/api/v1/generation/one",
    envelope(
      workspaceId,
      buildGenerateOneRequest(fileId, activeLearningModel.learning_model_id),
    ),
  );
  location.assign(manifestReviewUrl(generated.candidate.reference.candidate_id, fileId));
}

async function generateAll(): Promise<void> {
  if (!schemaCatalogId) {
    byId("message").textContent = "Select a pinned schema catalog before batch generation.";
    return;
  }
  const started = await postTool<{ descriptor: { job: { job_id: string } } }>(
    "/api/v1/generation/all",
    envelope(workspaceId, buildGenerateAllRequest(inventoryId, schemaCatalogId)),
  );
  activeJobId = started.descriptor.job.job_id;
  byId("progress-panel").hidden = false;
  void pollJob(activeJobId, "Bulk generation");
}

function bind(): void {
  for (const id of ["search", "category", "format", "status-filter"]) {
    byId(id).addEventListener("input", render);
  }
  byId("register").addEventListener("click", () => void registerAndDiscover());
  byId("cancel").addEventListener("click", () => void cancelJob());
  byId("reset").addEventListener("click", () => void resetInventory());
  byId("learn").addEventListener("click", () => void learnPatterns());
  byId("generate-all").addEventListener("click", () => void generateAll());
  byId("deactivate-learning").addEventListener("click", () => void mutateLearning("deactivate"));
  byId("clear-learning").addEventListener("click", () => void mutateLearning("clear"));
  void loadInventory().catch(error => {
    byId("message").textContent = error instanceof Error ? error.message : "Inventory unavailable.";
  });
}

if (typeof document !== "undefined" && document.getElementById("inventory")) bind();
