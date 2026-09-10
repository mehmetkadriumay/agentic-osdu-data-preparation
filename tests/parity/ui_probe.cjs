const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { pathToFileURL } = require("url");

const [application, webRoot] = process.argv.slice(2);
const current = application === "current";
const elements = new Map();
const dynamic = new Map();
const calls = [];
let clipboardWrites = 0;

class Element {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.className = "";
    this.classList = {
      add: (...names) => names.forEach(name => {
        if (!this.className.split(" ").includes(name)) this.className += ` ${name}`;
      }),
      remove: (...names) => {
        this.className = this.className.split(" ").filter(name => !names.includes(name)).join(" ");
      },
      contains: name => this.className.split(" ").includes(name),
      toggle: name => this.classList.contains(name) ? this.classList.remove(name) : this.classList.add(name),
    };
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  async dispatch(type, extra = {}) {
    const event = { target: this, preventDefault() {}, stopPropagation() {}, ...extra };
    for (const handler of this.listeners[type] || []) await handler(event);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
  removeAttribute(name) { delete this.attributes[name]; }
  set innerHTML(value) {
    this._innerHTML = String(value);
    for (const className of ["file-row", "row-toggle", "generate-one", "manifest-option"]) {
      if (this._innerHTML.includes(className)) {
        const item = new Element();
        item.className = className;
        const control = /aria-controls="([^"]+)"/.exec(this._innerHTML);
        if (control) item.setAttribute("aria-controls", control[1]);
        const fileId = /data-file-id="([^"]+)"/.exec(this._innerHTML);
        if (fileId) item.dataset.fileId = fileId[1];
        const generateId = /data-generate-id="([^"]+)"/.exec(this._innerHTML);
        if (generateId) item.dataset.generateId = generateId[1];
        dynamic.set(className, [item]);
      }
    }
    const generateId = /data-generate-id="([^"]+)"/.exec(this._innerHTML);
    if (generateId) {
      const item = new Element();
      item.dataset.generateId = generateId[1];
      dynamic.set("generate-one-current", [item]);
    }
  }
  get innerHTML() { return this._innerHTML || this.textContent; }
}

function loadHtml(file) {
  const html = fs.readFileSync(file, "utf8");
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  for (const match of html.matchAll(/data-decision="([^"]+)"/g)) {
    const item = new Element();
    item.dataset.decision = match[1];
    (dynamic.get("data-decision") || dynamic.set("data-decision", []).get("data-decision")).push(item);
  }
}

const document = {
  body: new Element("body"),
  documentElement: new Element("html"),
  referrer: "",
  getElementById: id => elements.get(id) || null,
  querySelector: selector => selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null,
  querySelectorAll: selector => {
    const key = selector === "[data-decision]" ? "data-decision"
      : selector === "[data-generate-id]" ? "generate-one-current"
      : selector.replace(/^\./, "");
    if (selector === "[data-generate-id]") return dynamic.get("generate-one-current") || [];
    if (selector === "[data-manifest-link]") return [];
    return dynamic.get(key) || [];
  },
  createElement: () => new Element(),
  createTextNode: text => ({ textContent: text }),
};

const file = {
  file_id: "00000000-0000-0000-0000-000000000001",
  relative_path: "well/a.las",
  sha256: "0".repeat(64),
};
const currentRecord = {
  recordId: file.file_id, filename: "a.las", path: "well/a.las", dataDirectory: "Data",
  format: "LAS", category: "Well log", subtype: "Log", confidence: "high",
  dimensions: "1D", stack: null, processing: null, domain: "Depth", survey: null,
  details: { curveCount: 1 }, evidence: ["LAS signature"], osduKind: "WellLog",
  manifests: [], sizeBytes: 10,
};
const targetItem = {
  file,
  classification: { format_id: "FMT-002", category: "well_log", trust_level: "derived", evidence_ids: ["e1"], extraction_ids: ["x1"] },
  evidence: [{ observed_value: "LAS signature" }],
  provenance: [{ tool_id: "TOOL-007", tool_version: "1.0.0", source_ref: "well/a.las" }],
  association: {
    association_id: "association", manifest_id: "manifest", review_status: "approved",
    method: "exact_path", target_version: "1", trust_level: "verified",
  },
  manifest_sha256: "1".repeat(64),
};

global.document = document;
global.window = {
  location: { href: "http://local/", search: "", origin: "http://local/", assign() {}, replace() {} },
  history: { length: 1, back() {} },
  matchMedia: () => ({ matches: false }),
  setInterval: () => 0,
  setTimeout: () => 0,
  open: () => null,
  close() {},
  closed: false,
};
global.location = global.window.location;
Object.defineProperty(global, "navigator", {
  configurable: true,
  value: { clipboard: { writeText: async () => { clipboardWrites += 1; } } },
});
global.localStorage = {
  values: new Map([["schemaCatalogId", "catalog"], ["outputRootIds", '["review"]']]),
  getItem(key) { return this.values.get(key) ?? null; },
  setItem(key, value) { this.values.set(key, value); },
};
global.crypto = { randomUUID: () => "00000000-0000-4000-8000-000000000001" };

global.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), method: options.method || "GET" });
  const route = String(url).split("?")[0];
  let payload = {};
  if (current && route === "/api/files") payload = {
    count: 1, files: [currentRecord], categories: { "Well log": 1 },
    sources: [], manifestSummary: { mappedDataFiles: 0, associations: 0 },
    selectedDataDirectory: "Data",
    learningSummary: { categoriesLearned: 1, manifestAssociations: 1 },
  };
  else if (current && route === "/api/jobs") payload = {};
  else if (current && route === "/api/classify") payload = { job: { id: "job", type: "classification", status: "running", total: 1, completed: 0 } };
  else if (current && route === "/api/generate-manifest") payload = {
    inventory: {
      count: 1, files: [{ ...currentRecord, manifests: [{ path: "generated/a.json", matchMethod: "generated" }] }],
      categories: { "Well log": 1 }, sources: [],
      manifestSummary: { mappedDataFiles: 1, associations: 1 },
      selectedDataDirectory: "Data",
    },
    learningSummary: { categoriesLearned: 1, manifestAssociations: 1 },
    generated: { path: "generated/a.json" },
  };
  else if (current && route === "/api/generate-all") payload = { job: { id: "bulk", type: "manifest-generation", status: "running", total: 1, completed: 0 } };
  else if (current && route === "/api/learn") payload = { categoriesLearned: 1, pairedDataFiles: 1, manifestAssociations: 1 };
  else if (current && route === "/api/reset") payload = { status: "idle", learningSummary: null };
  else if (current && route === "/api/clear-learning") payload = { status: "cleared" };
  else if (current && route === "/api/manifest") payload = {
    path: "m.json", document: { kind: "osdu:wks:Manifest:1.0.0" },
    validation: { status: "valid", recordsChecked: 1, schemas: [], errors: [] },
  };
  else if (!current && route === "/api/v1/review/inventories") payload = { items: [targetItem], state_version: 1, active_learning_model: { learning_model_id: "model", version: 1 } };
  else if (!current && route === "/api/v1/workspaces") payload = { workspace_id: "workspace", allowed_output_subpaths: ["generated", "review"] };
  else if (!current && route === "/api/v1/jobs") payload = { descriptor: { job: { job_id: "job" } } };
  else if (!current && route === "/api/v1/jobs/events") payload = { snapshot: { job: { status: "running" }, counts: {} }, events: [], last_event_sequence: 0 };
  else if (!current && route === "/api/v1/generation/one") payload = { candidate: { reference: { candidate_id: "candidate" } } };
  else if (!current && route === "/api/v1/generation/all") payload = { descriptor: { job: { job_id: "bulk" } } };
  else if (!current && route === "/api/v1/learning/learn") payload = { model: { learning_model_id: "model", version: 1 } };
  else if (!current && route === "/api/v1/learning/models") payload = { learning_model_id: "model", version: 1 };
  else if (!current && route === "/api/v1/review/manifests") payload = {
    content: { content: { kind: "osdu:wks:Manifest:1.0.0" } }, validation: { status: "valid", issues: [] },
    association: null, provenance: [], generation_diff: null, manifest: { document: {} },
  };
  return { ok: true, status: 200, json: async () => payload };
};

async function execute(fileName) {
  if (current) {
    const source = fs.readFileSync(path.join(webRoot, fileName), "utf8");
    vm.runInThisContext(`{${source}\n}`, { filename: fileName });
  } else {
    const original = path.join(webRoot, "assets", fileName);
    const temporary = path.join(webRoot, "assets", `.parity-${fileName}.mjs`);
    const source = fs.readFileSync(original, "utf8").replace(
      /import\{n as e,t\}from".\/style-[^"]+\.js";/,
      "const e=async (u,o)=>{const r=await fetch(u,{method:'POST',body:JSON.stringify(o)});return r.json()};const t=(w,input)=>({workspace_id:w,input});"
    );
    fs.writeFileSync(temporary, source);
    try { await import(`${pathToFileURL(temporary).href}?v=${Date.now()}`); }
    finally { fs.unlinkSync(temporary); }
  }
  await new Promise(resolve => setTimeout(resolve, 10));
}

function listener(id, event) {
  return (elements.get(id)?.listeners[event]?.length || 0) > 0;
}
function called(route) { return calls.some(call => call.url.split("?")[0] === route); }

(async () => {
  const phase = value => { if (process.env.PARITY_DEBUG) console.error(value); };
  loadHtml(path.join(webRoot, "index.html"));
  phase("inventory-html");
  await execute(current ? "app.js" : "inventory-Cram1__V.js");
  phase("inventory-executed");
  const search = elements.get("search");
  search.value = "not-present";
  await search.dispatch("input");
  const filtering = current
    ? elements.get("result-count").textContent.startsWith("0")
    : elements.get("summary").textContent.startsWith("0");
  if (current) {
    const generate = dynamic.get("generate-one-current")?.[0];
    if (generate) void generate.dispatch("click");
    phase("current-one");
    elements.get("generate-all").disabled = false;
    await elements.get("generate-all").dispatch("click");
    phase("current-generate-all");
    await elements.get("classify").dispatch("click");
    elements.get("base-directory").value = "C:\\fixture";
    elements.get("data-directory").value = "C:\\fixture\\Data";
    elements.get("manifest-directory").value = "C:\\fixture\\Manifests";
    await elements.get("directory-form").dispatch("submit");
    phase("current-classify");
    for (const id of ["learn-patterns", "clear-results", "clear-learning"]) {
      elements.get(id).disabled = false;
      await elements.get(id).dispatch("click");
      phase(`current-${id}`);
    }
  } else {
    const generate = dynamic.get("generate-one")?.[0];
    if (generate) void generate.dispatch("click");
    elements.get("workspace-path").value = "C:\\fixture";
    elements.get("generated-output-subpath").value = "generated";
    elements.get("export-output-subpath").value = "review";
    elements.get("schema-catalog-id").value = "catalog";
    await elements.get("register").dispatch("click");
    await new Promise(resolve => setImmediate(resolve));
    await elements.get("cancel").dispatch("click");
    for (const id of ["learn", "generate-all", "reset", "clear-learning"]) await elements.get(id).dispatch("click");
  }
  phase("actions-executed");
  const details = dynamic.get(current ? "file-row" : "row-toggle")?.[0];
  if (details) void details.dispatch("click");
  const progress = elements.get("progress-panel")?.hidden === false
    || elements.get("job-progress")?.hidden === false;
  const detailBehavior = Boolean(details?.listeners.click?.length);
  await new Promise(resolve => setImmediate(resolve));

  elements.clear(); dynamic.clear();
  loadHtml(path.join(webRoot, "manifest.html"));
  phase("manifest-html");
  global.window.location.search = current ? "?file=a&source=s&manifest=m.json" : "?id=m";
  global.location = global.window.location;
  await execute(current ? "manifest.js" : "manifest-COvLs7mi.js");
  phase("manifest-executed");
  await elements.get("copy").dispatch("click");
  const manifestCopy = listener("copy", "click") && clipboardWrites > 0;
  const provenance = Boolean(elements.get("provenance") && elements.get("candidate-trust"));

  const routes = current ? {
    classify: "/api/classify", learn: "/api/learn", one: "/api/generate-manifest",
    all: "/api/generate-all", reset: "/api/reset", clear: "/api/clear-learning",
  } : {
    classify: "/api/v1/jobs", learn: "/api/v1/learning/learn", one: "/api/v1/generation/one",
    all: "/api/v1/generation/all", reset: "/api/v1/inventories/reset",
    clear: "/api/v1/learning/models",
  };
  console.log(JSON.stringify({
    "ui.classification": called(routes.classify),
    "ui.filtering": filtering,
    "ui.details_and_evidence": detailBehavior,
    "ui.manifest_view_and_copy": manifestCopy,
    "ui.learning": called(routes.learn),
    "ui.single_generation": called(routes.one),
    "ui.bulk_generation": called(routes.all),
    "ui.progress": progress,
    "ui.clear_inventory": called(routes.reset),
    "ui.clear_learning": called(routes.clear),
    "ui.cancellation": current ? undefined : called("/api/v1/jobs/cancel"),
    "ui.provenance_and_trust": current ? undefined : provenance,
    "osdu_ingestion": calls.some(call => /(?:^|[/_-])ingest(?:ion)?(?:$|[/_-])/.test(call.url)),
  }));
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
