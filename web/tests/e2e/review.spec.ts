import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const inventory = {
  output: {
    inventory_id: "00000000-0000-0000-0000-000000000010",
    state_version: 7,
    active_learning_model: {
      learning_model_id: "00000000-0000-0000-0000-000000000030",
      version: 4,
      status: "active",
    },
    total_count: 1,
    category_summaries: [{ value: "seismic", count: 1 }],
    format_summaries: [{ value: "FMT-001", count: 1 }],
    review_status_summaries: [{ value: "proposed", count: 1 }],
    items: [{
      file: { file_id: "00000000-0000-0000-0000-000000000011", relative_path: "survey/a.sgy", size_bytes: 10 },
      classification: {
        category: "seismic",
        format_id: "FMT-001",
        confidence: 0.95,
        evidence_ids: ["SEG-Y header"],
        extraction_ids: ["TOOL-006"],
        trust_level: "verified",
      },
      association: {
        association_id: "00000000-0000-0000-0000-000000000012",
        manifest_id: "00000000-0000-0000-0000-000000000013",
        review_status: "proposed",
        method: "normalized_identifier",
        score: 70,
        evidence_ids: ["association-evidence-1"],
        trust_level: "heuristic",
        target_version: "manifest-version-1",
      },
      candidate: {
        candidate_id: "00000000-0000-0000-0000-000000000020",
        candidate_sha256: "a".repeat(64),
        review_status: "proposed",
        trust_level: "heuristic",
        review_required: true,
      },
    }],
  },
};

const requests: Array<{ path: string; body: unknown }> = [];

test.beforeEach(async ({ page }) => {
  requests.length = 0;
  await page.addInitScript(() => {
    localStorage.setItem("outputRootIds", JSON.stringify(["generated", "exports"]));
  });
  let jobQueries = 0;
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    requests.push({ path, body: route.request().postDataJSON() });
    if (path.endsWith("/review/inventories")) {
      await route.fulfill({ json: inventory });
    } else if (path.endsWith("/workspaces")) {
      await route.fulfill({ json: { output: {
        workspace_id: "00000000-0000-0000-0000-000000000001",
        allowed_output_subpaths: ["generated/review", "exports/approved"],
      } } });
    } else if (path.endsWith("/jobs")) {
      await route.fulfill({ json: { output: { descriptor: { job: {
        job_id: "00000000-0000-0000-0000-000000000040",
      } } } } });
    } else if (path.endsWith("/generation/all")) {
      await route.fulfill({ json: { output: { descriptor: { job: {
        job_id: "00000000-0000-0000-0000-000000000041",
      } } } } });
    } else if (path.endsWith("/jobs/events")) {
      jobQueries += 1;
      await route.fulfill({ json: { output: { snapshot: {
        job: { status: jobQueries > 1 ? "succeeded" : "running" },
        last_event_sequence: jobQueries,
        counts: { processed: jobQueries, classified: jobQueries },
      }, events: [{
        sequence: jobQueries,
        current_item: `survey/item-${jobQueries}.sgy`,
        counts: { processed: jobQueries, classified: jobQueries },
      }] } } });
    } else if (path.endsWith("/review/manifests")) {
      await route.fulfill({ json: { output: {
        manifest: {
          reference: {
            candidate_id: "00000000-0000-0000-0000-000000000020",
            candidate_sha256: "a".repeat(64),
            review_status: "proposed",
            trust_level: "heuristic",
            review_required: true,
          },
          document: { sha256: "a".repeat(64), content: {} },
        },
        content: { content: { kind: "osdu:wks:work-product-component--SeismicTraceData:1.0.0" } },
        validation: { status: "invalid", issues: [{ json_pointer: "/data", message: "Required" }] },
        provenance: [{ provenance_id: "p1", source: "TOOL-020" }],
        association: {
          method: "exact_filename",
          score: 1,
          review_status: "proposed",
          trust_level: "heuristic",
          target_version: "manifest-version-1",
          evidence_ids: ["evidence-match-1", "evidence-match-2"],
        },
        generation_diff: { changed_pointers: ["/data/Name"], added_pointers: [], removed_pointers: [] },
      } } });
    } else {
      await route.fulfill({ json: { output: { accepted: true }, status: "succeeded" } });
    }
  });
});

test("inventory supports explicit paths, progress, cancellation, filters, evidence and independent controls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Inventory review" })).toBeVisible();
  await page.getByLabel("Workspace path").fill("C:\\approved");
  await page.getByLabel("Generated output subpath").fill("generated/review");
  await page.getByLabel("Export output subpath").fill("exports/approved");
  await page.getByLabel("Pinned schema catalog ID")
    .fill("00000000-0000-0000-0000-000000000099");
  await page.getByRole("button", { name: "Register and discover" }).click();
  expect(requests.find(item => item.path.endsWith("/workspaces"))?.body)
    .toMatchObject({ input: {
      allowed_output_subpaths: ["generated/review", "exports/approved"],
    } });
  await expect(page.getByRole("progressbar")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("processed 1");
  await expect(page.getByRole("status")).toContainText("survey/item-1.sgy");
  await page.getByRole("button", { name: "Cancel job" }).click();
  expect(requests.find(item => item.path.endsWith("/jobs/cancel"))?.body)
    .toMatchObject({ input: { job_id: "00000000-0000-0000-0000-000000000040" } });
  await page.getByLabel("Search inventory").fill("survey");
  await expect(page.getByText("survey/a.sgy")).toBeVisible();
  await page.getByText("survey/a.sgy").click();
  await expect(page.getByText("SEG-Y header")).toBeVisible();
  await expect(page.getByText("verified")).toBeVisible();
  await expect(page.getByText("TOOL-006")).toBeVisible();
  await expect(page.getByRole("link", { name: "Review associated manifest" }))
    .toHaveAttribute(
      "href",
      "/manifest.html?id=00000000-0000-0000-0000-000000000013&source_file_id=00000000-0000-0000-0000-000000000011",
    );
  await expect(page.getByRole("link", { name: "Review generated candidate" }))
    .toHaveAttribute(
      "href",
      "/manifest.html?id=00000000-0000-0000-0000-000000000020&source_file_id=00000000-0000-0000-0000-000000000011",
    );
  await expect(page.getByRole("button", { name: "Archive and reset inventory" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Learn patterns" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Generate all missing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Generate manifest for survey/a.sgy" })).toBeVisible();
  await page.getByRole("button", { name: "Generate all missing" }).click();
  expect(requests.find(item => item.path.endsWith("/generation/all"))?.body)
    .toMatchObject({ input: {
      inventory_id: expect.any(String),
      schema_catalog_id: "00000000-0000-0000-0000-000000000099",
      generation_policy_version: "1.0.0",
    } });
  await expect(page.getByRole("status")).toContainText("Bulk generation succeeded");
  await expect(page.getByRole("status")).toContainText("survey/item-");
  await expect(page.getByRole("button", { name: "Deactivate learning" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Clear learning" })).toBeVisible();
  await page.getByRole("button", { name: "Archive and reset inventory" }).click();
  expect(requests.find(item => item.path.endsWith("/inventories/reset"))?.body)
    .toMatchObject({ input: { expected_state_version: 7, archive_before_reset: true } });
  await page.getByRole("button", { name: "Deactivate learning" }).click();
  expect(requests.find(item => item.path.endsWith("/learning/models"))?.body)
    .toMatchObject({ input: { mutation: {
      learning_model_id: "00000000-0000-0000-0000-000000000030",
      expected_version: 4,
    } } });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("single generation opens returned candidate with its source file context", async ({ page }) => {
  await page.route("**/api/v1/generation/one", async route => {
    requests.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    await route.fulfill({ json: { output: { candidate: { reference: {
      candidate_id: "00000000-0000-0000-0000-000000000020",
    } } } } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Generate manifest for survey/a.sgy" }).click();
  await expect(page).toHaveURL(
    /manifest\.html\?id=00000000-0000-0000-0000-000000000020&source_file_id=00000000-0000-0000-0000-000000000011/,
  );
});

test("learning runs a persisted activation sequence for every approved category", async ({ page }) => {
  const approvedItems = [
    {
      ...inventory.output.items[0],
      file: {
        ...inventory.output.items[0]!.file,
        sha256: "a".repeat(64),
      },
      association: {
        ...inventory.output.items[0]!.association,
        review_status: "approved",
      },
      manifest_sha256: "b".repeat(64),
    },
    {
      ...inventory.output.items[0],
      file: {
        ...inventory.output.items[0]!.file,
        file_id: "00000000-0000-0000-0000-000000000021",
        relative_path: "well/b.las",
        sha256: "c".repeat(64),
      },
      classification: {
        ...inventory.output.items[0]!.classification,
        category: "well_log",
        format_id: "FMT-002",
      },
      association: {
        ...inventory.output.items[0]!.association,
        association_id: "00000000-0000-0000-0000-000000000022",
        manifest_id: "00000000-0000-0000-0000-000000000023",
        review_status: "approved",
      },
      manifest_sha256: "d".repeat(64),
    },
  ];
  await page.route("**/api/v1/review/inventories", async route => {
    await route.fulfill({ json: { output: { ...inventory.output, items: approvedItems } } });
  });
  let created = 0;
  await page.route("**/api/v1/learning/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON();
    requests.push({ path, body });
    if (path.endsWith("/learn")) {
      await route.fulfill({ json: { output: { model: {
        category: body.input.category,
      } } } });
    } else {
      const action = body.input.mutation.action;
      if (action === "create") created += 1;
      await route.fulfill({ json: { output: {
        learning_model_id: `00000000-0000-0000-0000-00000000003${created}`,
        version: 1,
        status: action === "activate" ? "active" : "draft",
      } } });
    }
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Learn patterns" }).click();
  await expect(page.getByRole("status")).toHaveText("2 category learning model(s) activated.");

  const learningCalls = requests.filter(item => item.path.includes("/learning/"));
  expect(learningCalls).toHaveLength(6);
  expect(learningCalls.map(item => [
    item.path,
    (item.body as { input: { category?: string; mutation?: { action: string } } })
      .input.category
      ?? (item.body as { input: { mutation: { action: string } } }).input.mutation.action,
  ])).toEqual([
    ["/api/v1/learning/learn", "seismic"],
    ["/api/v1/learning/models", "create"],
    ["/api/v1/learning/models", "activate"],
    ["/api/v1/learning/learn", "well_log"],
    ["/api/v1/learning/models", "create"],
    ["/api/v1/learning/models", "activate"],
  ]);
});

test("manifest review shows JSON, validation, evidence, diff, copy and decisions", async ({ page }) => {
  await page.goto("/manifest.html?id=00000000-0000-0000-0000-000000000020");
  await expect(page.getByLabel("Approved output root").locator("option")).toHaveText([
    "generated",
    "exports",
  ]);
  await expect(page.getByText("Schema errors")).toBeVisible();
  await expect(page.getByText("exact_filename")).toBeVisible();
  await expect(page.getByText("evidence-match-1")).toBeVisible();
  await expect(page.getByText("/data/Name")).toBeVisible();
  await expect(page.getByText("TOOL-020")).toBeVisible();
  await expect(page.getByText("Trust level: heuristic")).toBeVisible();
  await expect(page.getByText("Human review required")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy JSON" })).toBeEnabled();
  for (const name of ["Approve", "Reject", "Needs changes"]) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }
  await page.getByRole("button", { name: "Approve" }).click();
  expect(requests.find(item => item.path.endsWith("/review/decisions"))?.body)
    .toMatchObject({ input: { decision: {
      target_type: "generated_candidate",
      target_id: "00000000-0000-0000-0000-000000000020",
      target_version: "a".repeat(64),
    } } });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("persisted approved candidate enables export after page reload", async ({ page }) => {
  await page.route("**/api/v1/review/manifests", async route => {
    await route.fulfill({ json: { output: {
      manifest: {
        reference: {
          candidate_id: "00000000-0000-0000-0000-000000000020",
          candidate_sha256: "a".repeat(64),
          review_status: "approved",
          trust_level: "heuristic",
          review_required: true,
        },
        document: { sha256: "a".repeat(64), content: {} },
      },
      content: { content: {} },
      validation: { status: "valid", issues: [] },
      provenance: [],
      association: null,
      generation_diff: null,
    } } });
  });
  await page.goto("/manifest.html?id=00000000-0000-0000-0000-000000000020");
  await expect(page.getByRole("button", { name: "Export candidate" })).toBeEnabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "Export candidate" })).toBeEnabled();
});
