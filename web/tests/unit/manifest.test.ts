import { describe, expect, it } from "vitest";

import {
  buildDecision,
  buildExportRequest,
  buildManifestReviewRequest,
  candidateTrustPresentation,
  associationDetails,
  isCandidateExportable,
  outputRootOptions,
  validationLabel,
} from "../../src/manifest.ts";

describe("manifest validation presentation", () => {
  it.each([
    ["valid", "Schema valid"],
    ["invalid", "Schema errors"],
    ["unavailable", "Schema unavailable"],
  ])("maps %s without claiming semantic authority", (status, label) => {
    expect(validationLabel(status)).toBe(label);
  });

  it("loads persisted references and reviews the actual target version and type", () => {
    expect(buildManifestReviewRequest("candidate-id")).toEqual({
      manifest_id: "candidate-id",
    });
    expect(buildDecision({
      targetType: "generated_candidate",
      targetId: "candidate-id",
      targetVersion: "abc123",
      decision: "approve",
      reason: "reviewed",
      decisionId: "decision-id",
      decidedAt: "2026-09-09T00:00:00Z",
    })).toMatchObject({
      target_type: "generated_candidate",
      target_id: "candidate-id",
      target_version: "abc123",
    });

  });

  it("exports only an exact approved candidate to an explicit output root", () => {
    expect(buildExportRequest("candidate-id", "abc123", "approved", "review/out.json"))
      .toEqual({
        request: {
          export_kind: "approved_manifest",
          target_id: "candidate-id",
          output_root_id: "approved",
          relative_path: "review/out.json",
          expected_target_version: "abc123",
        },
      });
  });

  it("renders only output root IDs persisted by workspace registration", () => {
    expect(outputRootOptions(["generated", "exports"])).toEqual([
      { label: "generated", value: "generated" },
      { label: "exports", value: "exports" },
    ]);
  });

  it("prominently identifies generated candidate trust and review requirements", () => {
    expect(candidateTrustPresentation({
      trust_level: "heuristic",
      review_required: true,
    })).toEqual({
      trust: "Trust level: heuristic",
      review: "Human review required",
    });
  });

  it("enables export only for a persisted approved candidate", () => {
    expect(isCandidateExportable({ review_status: "approved" })).toBe(true);
    expect(isCandidateExportable({ review_status: "proposed" })).toBe(false);
    expect(isCandidateExportable({ review_status: "rejected" })).toBe(false);
  });

  it("renders association evidence IDs and review details", () => {
    expect(associationDetails({
      method: "normalized_identifier",
      score: 0.8,
      review_status: "approved",
      trust_level: "verified",
      target_version: "manifest-sha",
      evidence_ids: ["evidence-1", "evidence-2"],
    })).toBe(
      "normalized_identifier (0.8) · approved · verified · target manifest-sha · evidence evidence-1, evidence-2",
    );
  });
});
