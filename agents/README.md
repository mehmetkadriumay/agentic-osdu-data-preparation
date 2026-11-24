# Seven OSDU exploration agents on Microsoft Foundry

This folder hosts the README's seven exploration steps as **seven independent
Microsoft Agent Framework agents**, each with its own Foundry deployment and
Entra agent identity. It does not replace the existing `src\agentic_osdu\agents`
roles, which also cover manifest workflows.

The shared backend uses Azure Container Apps, an **Azure Files share mounted
read-only**, and PostgreSQL Flexible Server. No laptop filesystem is reachable
from Foundry: upload an approved copy of the source data first.

## Architecture and responsibilities

```text
run_pipeline.py -- caller identity, checks real backend receipts
  -> seven Foundry Responses endpoints, in order
       -> one stage-specific Agent Framework Python tool per agent
            -> authenticated HTTPS stage adapter on Container Apps
                 -> existing typed deterministic tool registry and data-type handlers
                 -> /mnt/source/Data on read-only Azure Files
                 -> PostgreSQL inventory, run ledger, evidence, and provenance
```

| Step | Foundry service | Deterministic capabilities |
| --- | --- | --- |
| 1 | `approve-workspace` | `register_approved_workspace` |
| 2 | `discover-formats` | Discovery, bounded sampling, format detection |
| 3 | `extract-metadata` | Registered format-specific extractors |
| 4 | `classify-data` | `classify_subsurface_data` |
| 5 | `account-failures` | Read/account for the run's actual file outcomes; no new parsing |
| 6 | `persist-inventory` | Persist an authoritative inventory mutation with optimistic concurrency |
| 7 | `review-inventory` | Paginate the inventory review and join persisted metadata |

`azure.yaml` deploys the same small host with seven different `OSDU_STAGE` values.
`osdu_foundry\stages.py` defines their responsibilities and backend capabilities.
Each model can call only its fixed `run_stage` function. The backend independently
binds the authenticated principal to that stage, enforces ordering and immutable
run configuration, and records verified stage principal IDs.

**Important transport distinction:** the cloud endpoint is a restricted staged
HTTP adapter over the **same registered implementations used by MCP**, not the
local unauthenticated API or a public copy of all 30 MCP tools. Agents cannot
choose arbitrary tool names, URLs, paths, Python code, or inventory payloads.
The local stdio/loopback MCP transports retain their existing behavior.

The backend owns identifiers, extraction references, evidence, and persistence.
Models do not copy file lists or construct metadata references between stages.
The launcher verifies the backend receipt after each model response rather than
trusting an agent's claim of success. Full raw samples are not sent to the model
or retained in the run ledger.

## Prerequisites

1. Use PowerShell 7, Python 3.13 for the hosted agents, Azure CLI, and Azure
   Developer CLI >=1.36 with the `azure.ai.agents` and `azure.ai.projects`
   extensions. Authenticate interactively yourself before running deployment.
   The scripts never log you in or silently select the active subscription.
2. Provide an **existing Foundry project and chat model deployment** in your
   approved region. The deployment script creates neither. Hosted/code deployment
   availability, subscription quota, service terms, and preview-package approval
   must be checked for that region. The hosting package is explicitly pinned to a
   preview release; this is a deployment starter, not a production certification.
3. Have permission to create the backend resources and assign its ACR pull role,
   plus Foundry Project Manager permissions for agent deployment. Confirm each
   hosted agent identity can use the selected model under your project's
   documented inference permissions. Project membership alone is not a substitute
   for verifying the identity used at runtime.
4. Create an Entra application/service principal for the backend API in the same
   tenant. Expose a dedicated API scope/resource URI and set
   `api.requestedAccessTokenVersion` to **2**. Supply the application's client ID
   as `ApiAudience` and its token scope, for example
   `api://<application-client-id>/.default`, as `BackendScope`. Configure required
   consent/application permissions with your tenant administrator. Management API
   tokens such as `https://management.azure.com/` are not backend API credentials.
5. Know the operator/coordinator's Entra **object ID**. The runner needs explicit
   read access to backend receipts and permission to invoke all seven Foundry
   endpoints. Agent stage permissions use each hosted agent's **service-principal
   object ID**, not a client/application ID and not the Foundry project identity.
6. Approve upload/data residency and Azure spend. Seven hosted agents, an always-on
   backend, PostgreSQL, ACR, Azure Files, networking, and logging are billable.
   Source metadata is stored in PostgreSQL and the local report; model-call
   inputs contain run configuration, not raw source bytes.

Official references:
[framework hosting](https://learn.microsoft.com/azure/foundry/how-to/develop/framework-hosted-agents),
[azure.yaml](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference),
[hosted identity/token service](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-token-service),
and [hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions).

## 1. Review the infrastructure plan

Run from the repository root. Replace the placeholders with approved values.
An existing Foundry project may be in a different resource group from the new
backend. `FoundryLocation` must match the existing project.

```powershell
# Keep operator-specific files out of git; *.local.json is ignored.
@('<operator-object-id>') | ConvertTo-Json -AsArray |
    Set-Content .\readers.local.json

$deployment = @{
    SubscriptionId        = '<subscription-guid>'
    TenantId              = '<tenant-guid>'
    ResourceGroupName     = 'rg-osdu-exploration'
    Location              = '<backend-region>'
    NamePrefix            = 'osduexplore'
    EnvironmentName       = 'osdu-dev'
    FoundryProjectId      = '/subscriptions/<subscription-guid>/resourceGroups/<foundry-rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>'
    FoundryProjectEndpoint = 'https://<account>.services.ai.azure.com/api/projects/<project>'
    FoundryLocation       = '<foundry-region>'
    ModelDeploymentName   = '<existing-chat-model-deployment>'
    ApiAudience           = '<backend-api-application-client-id>'
    BackendScope          = 'api://<backend-api-application-client-id>/.default'
    ReaderPrincipalsPath  = '.\readers.local.json'
    MaxFiles              = 100
}

# Offline plan: no Azure calls, provisioning, builds, or model invocations.
.\scripts\deploy_foundry.ps1 @deployment

# Also exercise the explicit execution path without applying it.
.\scripts\deploy_foundry.ps1 @deployment -Deploy -WhatIf
```

The templates create a VNet, delegated PostgreSQL subnet/private DNS, Container
Apps environment, Log Analytics, managed identity, ACR with admin disabled, and
an Azure Files share. PostgreSQL is private. Storage network rules permit the
backend subnet and, optionally, one approved upload IP using `-UploadIpAddress`.
An omitted upload IP means uploads require another approved network path.

The backend exposes HTTPS with token validation and exact principal allowlists;
this is not an end-to-end private Foundry network topology. A private Foundry
project needs an approved operator connectivity path and outbound access to the
backend. Designing private hosted-agent networking is a separate infrastructure
decision; it cannot be enabled arbitrarily on an existing Foundry account.

Use separate Foundry projects for separate environments. The seven agent names
in `azure.yaml` are fixed; redeploying them into the same project creates new
versions of those agents rather than an isolated environment.

## 2. Provision the backend and deploy the seven agents

```powershell
# Supply the same approved password for a repeat infrastructure deployment.
# Do not put a literal password in command history, files, or source control.
$password = Read-Host 'PostgreSQL administrator password' -AsSecureString
.\scripts\deploy_foundry.ps1 @deployment -PostgresPassword $password -Deploy
```

The script checks tenant/project/model configuration, provisions the backend
foundation, builds `agents\backend\Dockerfile` remotely in ACR, creates the
Container App, adopts the existing Foundry project, and deploys each agent
independently with `azd`. It reports the backend URL and resource/share names.
It does **not** upload source data or make model calls.

PostgreSQL's connection URL is a Container Apps secret reference. Temporary
secure deployment parameters are removed on success/failure; infrastructure
outputs do not include passwords or storage keys. Do not enable verbose CLI
debug tracing when dealing with deployment secrets. Protect administrator
credentials and use a separately provisioned least-privilege database role for
production; the starter backend currently uses the supplied database account.

Bootstrap deploys an **empty agent allowlist** unless a verified mapping is
supplied. All stage calls fail closed until the next section is completed.
The endpoint can be healthy while no agent has permission to use it.

## 3. Authorize the actual hosted agent identities

Inspect the deployed services in the Foundry portal and Entra Agent ID, or use
the current Azure SDK/CLI's documented identity readback. For example:

```powershell
Push-Location .\agents
azd ai agent show approve-workspace --output json
azd ai agent show extract-metadata --output json
Pop-Location
```

Verify each service's **runtime principal object ID**. No identity JSON property
path is guessed by the automation. Create `agent-principals.local.json` with
seven distinct verified principal IDs:

```json
{
  "<approve-workspace-principal-object-id>": 1,
  "<discover-formats-principal-object-id>": 2,
  "<extract-metadata-principal-object-id>": 3,
  "<classify-data-principal-object-id>": 4,
  "<account-failures-principal-object-id>": 5,
  "<persist-inventory-principal-object-id>": 6,
  "<review-inventory-principal-object-id>": 7
}
```

Grant any required backend API permission/consent to those identities through
your tenant's approved process, then update only the backend allowlist:

```powershell
$access = @{
    SubscriptionId     = $deployment.SubscriptionId
    TenantId           = $deployment.TenantId
    ResourceGroupName  = $deployment.ResourceGroupName
    ContainerAppName   = '<backend-app-name-reported-by-deployment>'
    AgentPrincipalsPath = '.\agent-principals.local.json'
}
.\scripts\configure_foundry_access.ps1 @access
.\scripts\configure_foundry_access.ps1 @access -Deploy
```

The backend validates RS256 signatures, expiry, issuer, tenant, audience, and the
explicit principal-stage mapping. No anonymous stage access, project-identity
fallback, shared bearer token, or human-review impersonation is available.
This configuration update restarts the backend: do it **before starting a run**.
Reverify identities/permissions after agent redeployment or identity rotation.

## 4. Upload an approved source copy

Using Azure Storage Explorer or your approved upload process, place this layout
in the reported `source` share:

```text
source share
  Data
    seismic.sgy
    well.las
    navigation.p190
    measurements.csv
    unknown.vendor
```

The cloud approved workspace is `/mnt/source` and discovery is restricted to
`Data`. `C:\OSDU\NewSurvey` remains your local folder; it is **not** a Foundry
workspace and cannot be passed to the cloud agents. Upload is a separate,
explicit data-movement operation. Do not alter uploaded files while a run is
active, and do not upload links/junctions as substitutes for source files.
The backend verifies the mounted filesystem is actually read-only at startup.
The hosted agent identities receive no storage-account write key or source
write permission.

## 5. Capture endpoints and run the complete workflow

Retrieve each service's full **Responses protocol endpoint**, preserving its
query string. Do not use the project endpoint in place of an agent endpoint.

```powershell
Push-Location .\agents
azd ai agent endpoint show approve-workspace --output json
azd ai agent endpoint show discover-formats --output json
# Repeat for the other five service names in the table.
Pop-Location
```

Save these values in `agent-endpoints.local.json`:

```json
{
  "approve-workspace": "https://<full-responses-endpoint-for-stage-1>",
  "discover-formats": "https://<full-responses-endpoint-for-stage-2>",
  "extract-metadata": "https://<full-responses-endpoint-for-stage-3>",
  "classify-data": "https://<full-responses-endpoint-for-stage-4>",
  "account-failures": "https://<full-responses-endpoint-for-stage-5>",
  "persist-inventory": "https://<full-responses-endpoint-for-stage-6>",
  "review-inventory": "https://<full-responses-endpoint-for-stage-7>"
}
```

Install agent/runner dependencies in a **separate environment**, not the local
core's locked environment:

```powershell
uv venv .\agents\.venv --python 3.13
uv pip install --python .\agents\.venv\Scripts\python.exe -r .\agents\requirements.txt

$pipeline = @(
    '.\agents\run_pipeline.py',
    '--endpoints', '.\agent-endpoints.local.json',
    '--backend-url', 'https://<backend-hostname>',
    '--backend-scope', $deployment.BackendScope,
    '--max-files', '100',
    '--output', '.\reports\survey-inventory.local.json'
)

# Plan only; no tokens, model calls, inventory changes, or report writes.
& .\agents\.venv\Scripts\python.exe @pipeline

# Explicitly authorize the seven model invocations and PostgreSQL inventory write.
& .\agents\.venv\Scripts\python.exe @pipeline --execute
```

Keep the report outside the source directory. The launcher prints the `run_id`
and client-allocated `inventory_id` before invocation. A new inventory starts
with expected version zero, per the existing persistence contract; all observed
identifiers, fingerprints, extraction references, and persisted versions come
from deterministic tools.

For a later run of the **same source**, add `--inventory-id <previous-inventory-id>`;
the launcher reads its current version from the backend. Stable file identities
cannot be reassigned to an unrelated inventory. Changed files at the same path
replace their prior inventory entries under the expected-version guard, without
changing source files. Other existing inventory entries are not silently removed.

To continue an interrupted launcher while the backend instance is unchanged,
use `--run-id <original-run-id> --inventory-id <original-inventory-id>` and a new
output filename. The launcher reads the original immutable run configuration and
skips already-confirmed stages. It does not trust or parse the LLM's completion
text as a handoff.

The final JSON contains every review page, actual metadata payloads and extraction
references, classification/confidence/trust, evidence, provenance, and per-file
warnings. Unsupported formats and parser failures stay visible. A `completed`
pipeline is **not** schema validation, human approval, or OSDU ingestion; all
review items explicitly require human review.

## Bounds, restart behavior, and extension points

- Discovery is capped by the operator's `OSDU_MAX_FILES` (100 by default), with
  a 1 TiB aggregate source-size ceiling. Exceeding discovery limits stops the run,
  rather than silently increasing them. Approve budget changes separately.
- Detection reads one 65,536-byte prefix per file. Extension-only evidence and
  near-tied candidates (confidence gap below 0.05) are retained for review, not
  guessed/extracted. Some valid extensionless or weak-signature formats therefore
  require improved deterministic detectors, not prompt overrides.
- Registered extractors retain their own bounded parsing options. The existing
  extraction contract also computes a complete SHA-256, which can read the entire
  source file even though sampling/parsing are bounded. Plan I/O accordingly.
- Extraction/classification failures are handled independently per file. Database,
  authorization, workspace, ordering, and backend-lifecycle failures stop the run.
  Run state and failure evidence are durable in PostgreSQL. The inventory mutation
  is retained with the run as well as being applied through `persist_inventory`,
  preserving evidence/provenance that the core review projection does not show.
- Classification currently uses the registry's controlled filename/format rules;
  extraction references are linked accurately, but parsed metadata does not yet
  override those rules. Unknown/conflicting values require review, and a suggested
  OSDU kind is not proof of schema validity.
- Keep the backend at **one replica, one worker, one unfinished run**. Parser
  capabilities and observed references are process-local. PostgreSQL does not make
  them distributed. Do not deploy/update access or change mounts during a run.
  After a backend restart before stage 6 completes, start a new run and read the
  actual inventory version; old capabilities are never silently reused. A
  persisted run can still complete stage 7 after restart, because review reads
  PostgreSQL rather than reopening source files.
- Inventory commit and run-receipt updates are separate database transactions.
  On an interrupted commit, inspect the actual inventory version before starting
  a new run; do not assume zero or blindly retry a blocked run.
- Add formats through the existing
  [data-type extension architecture](../docs/extending-data-types.md). Detection,
  extraction routing, and classification reuse the injected registry. A genuinely
  new registered tool ID additionally needs a deliberate stage-capability change.
  No dynamic code loading or package installation is exposed to agents.

## Local checks and operations

```powershell
# Dependency-free orchestration/config tests; JWT tests require backend deps.
# The real-tool integration test runs when the root project is installed.
python -m unittest discover -s agents\tests -v

# Install optional backend dependencies and the core into the separate agent
# environment to include real registry/persistence and JWT integration.
uv pip install --python .\agents\.venv\Scripts\python.exe . -r .\agents\backend\requirements.txt
& .\agents\.venv\Scripts\python.exe -m unittest discover -s agents\tests -v

# Existing project integration, formatting, and typing gates.
uv sync --frozen
uv run pytest --no-cov tests\integration\test_data_type_extensions.py tests\unit\test_database_backends.py agents\tests
uv run ruff check .
uv run ruff format --check .
uv run mypy

# Bicep/PowerShell checks and mocked cloud automation: no provisioning.
.\agents\infra\validate_local.ps1
```

Use backend `/healthz` for process readiness; it deliberately exposes no source
details and does not prove any agent is authorized. A useful smoke run is a
small CSV plus one unsupported file, using the launcher only after approving
model spend. Check the persisted metadata, unsupported-file warning, inventory
version, and unchanged source bytes, not just an agent's text response.

Monitor latency/failures without logging raw samples, metadata, or bearer tokens.
Set PostgreSQL backup/retention, storage lifecycle, least-privilege database
credentials, ingress rate limits/WAF controls, package locking/image scanning,
cost alerts, and a tested recovery
procedure before production use. Initial tables are created by the runtime;
existing database schema upgrades require a reviewed migration, not
`create_all` as a migration strategy.

Cleanup is deliberately **not automatic**: first stop invocations, retain/export
the inventory and source copy as required, then remove the seven hosted agent
deployments and explicitly delete the dedicated backend resource group only
after confirming its contents. The existing Foundry project/model may be shared
and are not owned by these backend templates.
