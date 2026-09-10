import { envelope, postTool } from "./api.ts";
import "./style.css";

interface ManifestView {
  manifest:
    | { manifest_id: string; sha256: string; generated: false }
    | {
        reference: {
          candidate_id: string;
          candidate_sha256: string;
          review_status: string;
          trust_level: string;
          review_required: boolean;
        };
        document: { sha256: string; content: object };
      };
  content: { content: object };
  validation: { status: string; issues?: Array<{ json_pointer?: string; message: string }> } | null;
  provenance: Array<{ source?: string; tool_id?: string; provenance_id: string }>;
  association: {
    association_id: string;
    method: string;
    score: number;
    review_status: string;
    target_version: string;
    trust_level: string;
    evidence_ids: string[];
  } | null;
  generation_diff: {
    changed_pointers: string[];
    added_pointers: string[];
    removed_pointers: string[];
  } | null;
}

export function validationLabel(status: string): string {
  if (status === "valid") return "Schema valid";
  if (status === "invalid") return "Schema errors";
  return "Schema unavailable";
}

export function buildManifestReviewRequest(manifestId: string): object {
  return { manifest_id: manifestId };
}

export function outputRootOptions(
  rootIds: string[],
): Array<{ label: string; value: string }> {
  return rootIds.map(rootId => ({ label: rootId, value: rootId }));
}

export function candidateTrustPresentation(reference: {
  trust_level: string;
  review_required: boolean;
}): { trust: string; review: string } {
  return {
    trust: `Trust level: ${reference.trust_level}`,
    review: reference.review_required ? "Human review required" : "Human review not required",
  };
}

export function isCandidateExportable(reference: { review_status: string }): boolean {
  return reference.review_status === "approved";
}

export function associationDetails(association: {
  method: string;
  score: number;
  review_status: string;
  target_version: string;
  trust_level: string;
  evidence_ids: string[];
}): string {
  return [
    `${association.method} (${association.score})`,
    association.review_status,
    association.trust_level,
    `target ${association.target_version}`,
    `evidence ${association.evidence_ids.join(", ") || "none"}`,
  ].join(" · ");
}

interface DecisionParameters {
  targetType: string;
  targetId: string;
  targetVersion: string;
  decision: string;
  reason: string;
  decisionId?: string;
  decidedAt?: string;
}

type GeneratedManifest = Extract<ManifestView["manifest"], { reference: object }>;

function isGeneratedManifest(value: ManifestView["manifest"]): value is GeneratedManifest {
  return "reference" in value;
}

export function buildDecision(parameters: DecisionParameters): object {
  return {
    decision_id: parameters.decisionId ?? crypto.randomUUID(),
    actor: { actor_id: "local-user" },
    target_type: parameters.targetType,
    target_id: parameters.targetId,
    target_version: parameters.targetVersion,
    decision: parameters.decision,
    reason: parameters.reason,
    decided_at: parameters.decidedAt ?? new Date().toISOString(),
  };
}

export function buildExportRequest(
  candidateId: string,
  candidateVersion: string,
  outputRootId: string,
  relativePath: string,
): object {
  return {
    request: {
      export_kind: "approved_manifest",
      target_id: candidateId,
      output_root_id: outputRootId,
      relative_path: relativePath,
      expected_target_version: candidateVersion,
    },
  };
}

export function renderValidationIssues(
  list: HTMLElement,
  issues: Array<{ json_pointer?: string; message: string }>,
): void {
  list.replaceChildren();
  for (const issue of issues) {
    const item = document.createElement("li");
    const pointer = document.createElement("code");
    pointer.textContent = issue.json_pointer ?? "/";
    item.append(pointer, document.createTextNode(` ${issue.message}`));
    list.append(item);
  }
}

const storage = typeof localStorage === "undefined" ? null : localStorage;
const workspaceId = storage?.getItem("workspaceId") ?? crypto.randomUUID();
let manifest: ManifestView | null = null;

function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing UI element: ${id}`);
  return element as T;
}

async function load(): Promise<void> {
  const parameters = new URLSearchParams(location.search);
  const id = parameters.get("id") ?? "";
  const sourceFileId = parameters.get("source_file_id");
  manifest = await postTool<ManifestView>(
    "/api/v1/review/manifests",
    envelope(workspaceId, {
      ...buildManifestReviewRequest(id),
      ...(sourceFileId ? { source_file_id: sourceFileId } : {}),
    }),
  );
  byId("json").textContent = JSON.stringify(manifest.content.content, null, 2);
  const status = manifest.validation?.status ?? "unavailable";
  byId("validation").textContent = validationLabel(status);
  renderValidationIssues(
    byId("validation-errors"),
    manifest.validation?.issues ?? [],
  );
  byId("association").textContent = manifest.association
    ? associationDetails(manifest.association)
    : "No association";
  byId("provenance").textContent = manifest.provenance
    .map(item => item.source ?? item.tool_id ?? item.provenance_id).join(", ");
  byId("diff").textContent = manifest.generation_diff
    ? [...manifest.generation_diff.changed_pointers, ...manifest.generation_diff.added_pointers, ...manifest.generation_diff.removed_pointers].join(", ")
    : "No generated changes";
  const manifestTarget: ManifestView["manifest"] = manifest.manifest;
  const candidateReference = isGeneratedManifest(manifestTarget)
    ? manifestTarget.reference
    : null;
  const trust = candidateReference
    ? candidateTrustPresentation(candidateReference)
    : { trust: "Trust level: persisted source", review: "Generated review not applicable" };
  byId("candidate-trust").textContent = trust.trust;
  byId("candidate-review-required").textContent = trust.review;
  byId<HTMLButtonElement>("export").disabled =
    candidateReference === null || !isCandidateExportable(candidateReference);
}

async function decide(decision: string): Promise<void> {
  if (!manifest) throw new Error("Load the persisted manifest before reviewing it.");
  const generated = isGeneratedManifest(manifest.manifest);
  if (!generated && !manifest.association) {
    throw new Error("The persisted manifest has no reviewable association.");
  }
  const targetId = isGeneratedManifest(manifest.manifest)
    ? manifest.manifest.reference.candidate_id
    : manifest.association!.association_id;
  const targetVersion = isGeneratedManifest(manifest.manifest)
    ? manifest.manifest.reference.candidate_sha256
    : manifest.association!.target_version;
  const reason = byId<HTMLInputElement>("reason").value || "Reviewed in local UI";
  await postTool("/api/v1/review/decisions", envelope(workspaceId, {
    decision: buildDecision({
      targetType: generated ? "generated_candidate" : "manifest_association",
      targetId,
      targetVersion,
      decision,
      reason,
    }),
  }));
  if (generated && decision === "approve") {
    byId<HTMLButtonElement>("export").disabled = false;
  }
  byId("decision-status").textContent = `${decision} recorded`;
}

async function exportApproved(): Promise<void> {
  if (!manifest || !isGeneratedManifest(manifest.manifest)) {
    throw new Error("Only a persisted generated candidate may be exported.");
  }
  const outputRoot = byId<HTMLInputElement>("output-root").value.trim();
  const relativePath = byId<HTMLInputElement>("output-path").value.trim();
  if (!outputRoot || !relativePath) {
    throw new Error("Select an explicitly approved output root and relative path.");
  }
  await postTool(
    "/api/v1/exports",
    envelope(
      workspaceId,
      buildExportRequest(
        manifest.manifest.reference.candidate_id,
        manifest.manifest.reference.candidate_sha256,
        outputRoot,
        relativePath,
      ),
    ),
  );
  byId("decision-status").textContent = "Approved candidate exported.";
}

function bind(): void {
  const rootIds = JSON.parse(storage?.getItem("outputRootIds") ?? "[]") as string[];
  const outputRoot = byId<HTMLSelectElement>("output-root");
  outputRoot.replaceChildren(
    ...outputRootOptions(rootIds).map(({ label, value }) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    }),
  );
  byId("copy").addEventListener("click", async () => {
    if (manifest) await navigator.clipboard.writeText(JSON.stringify(manifest.content.content, null, 2));
  });
  document.querySelectorAll<HTMLButtonElement>("[data-decision]").forEach(button => {
    button.addEventListener("click", () => void decide(button.dataset.decision ?? ""));
  });
  byId("export").addEventListener("click", () => void exportApproved());
  void load().catch(error => {
    byId("validation").textContent = validationLabel("unavailable");
    byId("validation-errors").textContent =
      error instanceof Error ? error.message : "Manifest unavailable.";
  });
}

if (typeof document !== "undefined" && document.getElementById("json")) bind();
