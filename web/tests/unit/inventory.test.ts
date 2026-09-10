import { describe, expect, it } from "vitest";

import {
  buildCancelRequest,
  buildClassificationJob,
  buildGenerateAllRequest,
  buildGenerateOneRequest,
  buildLearnRequest,
  buildLearningMutation,
  buildResetRequest,
  buildWorkspaceRegistration,
  groupLearningExamplesByCategory,
  jobProgressPresentation,
  filterItems,
  manifestReviewUrl,
  registeredOutputRootIds,
  summarise,
} from "../../src/inventory.ts";
import type { InventoryItem } from "../../src/types.ts";

const items: InventoryItem[] = [
  {
    file: { file_id: "one", relative_path: "survey/a.sgy", size_bytes: 10 },
    classification: {
      category: "seismic",
      format_id: "FMT-001",
      confidence: 0.95,
      evidence_ids: ["SEG-Y header"],
      extraction_ids: ["TOOL-006"],
      trust_level: "verified",
    },
    association: null,
    candidate: null,
  },
  {
    file: { file_id: "two", relative_path: "well/b.las", size_bytes: 20 },
    classification: {
      category: "well_log",
      format_id: "FMT-002",
      confidence: 0.8,
      evidence_ids: [],
      extraction_ids: [],
      trust_level: "derived",
    },
    association: { review_status: "approved", method: "exact_filename", score: 100 },
    candidate: null,
  },
];

describe("inventory projections", () => {
  it("filters by text, category, format, and association status", () => {
    expect(filterItems(items, { search: "survey", category: "", format: "", status: "" }))
      .toHaveLength(1);
    expect(filterItems(items, { search: "", category: "well_log", format: "FMT-002", status: "approved" }))
      .toEqual([items[1]]);
  });

  it("summarises visible categories and formats", () => {
    expect(summarise(items)).toEqual({
      total: 2,
      categories: { seismic: 1, well_log: 1 },
      formats: { "FMT-001": 1, "FMT-002": 1 },
    });
  });

  it("starts WF-001 and retains the real TOOL-025 job identity", () => {
    expect(buildClassificationJob("workspace", "inventory").workflow_id).toBe("WF-001");
    expect(buildCancelRequest("job-from-tool-025").job_id).toBe("job-from-tool-025");
  });

  it("builds learning, single generation, and persisted WF-005 requests", () => {
    expect(buildLearnRequest("seismic", [])).toMatchObject({
      category: "seismic",
      learning_policy_version: "1.0.0",
    });
    expect(buildGenerateOneRequest("file", "model")).toMatchObject({
      file_id: "file",
      learning_model_id: "model",
      generation_policy_version: "1.0.0",
      dry_run: true,
    });
    expect(buildGenerateAllRequest(
      "inventory",
      "00000000-0000-0000-0000-000000000099",
    )).toMatchObject({
      inventory_id: "inventory",
      schema_catalog_id: "00000000-0000-0000-0000-000000000099",
      generation_policy_version: "1.0.0",
      dry_run: true,
    });
  });

  it("groups approved learning examples into separate category workflows", () => {
    const approved: InventoryItem[] = [
      {
        ...items[0]!,
        file: { ...items[0]!.file, sha256: "a".repeat(64) },
        association: {
          association_id: "association-seismic",
          manifest_id: "manifest-seismic",
          review_status: "approved",
          method: "exact_filename",
          score: 1,
        },
        manifest_sha256: "b".repeat(64),
      },
      {
        ...items[1]!,
        file: { ...items[1]!.file, sha256: "c".repeat(64) },
        association: {
          association_id: "association-well",
          manifest_id: "manifest-well",
          review_status: "approved",
          method: "exact_filename",
          score: 1,
        },
        manifest_sha256: "d".repeat(64),
      },
    ];

    expect(groupLearningExamplesByCategory(approved)).toEqual([
      expect.objectContaining({ category: "seismic", examples: [expect.objectContaining({
        source_file_id: "one",
        manifest_id: "manifest-seismic",
      })] }),
      expect.objectContaining({ category: "well_log", examples: [expect.objectContaining({
        source_file_id: "two",
        manifest_id: "manifest-well",
      })] }),
    ]);
  });

  it("presents real job counts and the latest current item from events", () => {
    expect(jobProgressPresentation("Bulk generation", {
      snapshot: {
        job: { status: "running" },
        last_event_sequence: 4,
        counts: { generated: 3, skipped: 1, failed: 0 },
      },
      events: [
        { sequence: 3, current_item: "survey/a.sgy", counts: { generated: 2 } },
        { sequence: 4, current_item: "survey/b.sgy", counts: { generated: 3, skipped: 1 } },
      ],
    })).toEqual({
      completed: 4,
      text: "Bulk generation running · generated 3, skipped 1, failed 0 · survey/b.sgy",
    });
  });

  it("registers explicit generated/export paths and exposes only their root IDs", () => {
    expect(buildWorkspaceRegistration("generated/review", "exports/approved")).toEqual({
      read_only: true,
      allowed_output_subpaths: ["generated/review", "exports/approved"],
    });
    expect(registeredOutputRootIds(["generated/review", "exports/approved"]))
      .toEqual(["generated", "exports"]);
  });

  it("builds persisted manifest and candidate review links with source context", () => {
    expect(manifestReviewUrl("manifest-id")).toBe("/manifest.html?id=manifest-id");
    expect(manifestReviewUrl("candidate-id", "file-id"))
      .toBe("/manifest.html?id=candidate-id&source_file_id=file-id");
  });

  it("resets the current inventory version and deactivates the loaded model version", () => {
    expect(buildResetRequest("inventory", 7)).toEqual({
      inventory_id: "inventory",
      expected_state_version: 7,
    });
    expect(buildLearningMutation("deactivate", {
      learning_model_id: "model",
      version: 4,
      status: "active",
    })).toEqual({
      mutation: {
        action: "deactivate",
        learning_model_id: "model",
        expected_version: 4,
      },
    });
  });
});
