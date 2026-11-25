# OSDU exploration agents on Microsoft Foundry

Each specialist is a separately callable Microsoft Agent Framework agent, with
its own `main.py`, `agent.py`, `instructions.txt`, `requirements.txt`, and
`.agentignore`. There is no shared `OSDU_STAGE` switch, `run_stage` wrapper, or
global stage counter. Existing `src\agentic_osdu\agents` contains different,
domain-level manifest workflows and is not the hosted-agent directory.

## Repository boundaries

```text
src\agentic_osdu\             Deterministic domain tools, parsers, policies, state
mcp_servers\
  workspace_discovery\       Workspace, Data-only discovery, bounded detection
  extraction\                Evidence-selected extensible parser dispatch
  classification\            Classification from authoritative observations
  inventory_review\          Failure accounting, snapshots, paginated review
  shared\                    Operation use cases, concurrency, durable receipts
  host\                      Entra-authenticated MCP composition and Dockerfile
agents\
  approve-workspace\         Standalone workspace specialist
  discover-formats\          Standalone discovery/detection specialist
  extract-metadata\          Standalone extraction specialist
  classify-data\             Standalone classification specialist
  account-failures\          Standalone failure-accounting specialist
  persist-inventory\         Standalone inventory specialist
  review-inventory\          Standalone review specialist
  shared\osdu_agent_host\    Hosting, credentials, MCP transport plumbing only
  invoke.py                  Independent invocation CLI
swarms\exploration\           Supervisor, instructions, hosted entry point, runner
deployment\                  Foundry manifest and infrastructure templates
scripts\                     Explicit packaging, deployment, access configuration
```

The specialist decides how to address its caller's task using its own tools.
The supervisor selects specialists and delegates independent file tasks
concurrently; it does not own parsing, evidence, identifiers, or inventory data.
The MCP server owns those deterministic operations and validates capabilities.
See [MCP servers](../mcp_servers/README.md) and [swarm execution](../swarms/README.md).

| Agent / role ID | MCP endpoint suffix | Capabilities |
| --- | --- | --- |
| `approve-workspace` / 1 | `workspace-discovery/` | `register_source_workspace` |
| `discover-formats` / 2 | `workspace-discovery/` | `discover_source_files`, `inspect_file_format` |
| `extract-metadata` / 3 | `extraction/` | `extract_detected_metadata` |
| `classify-data` / 4 | `classification/` | `classify_observed_file` |
| `account-failures` / 5 | `inventory-review/` | `account_file_failures` |
| `persist-inventory` / 6 | `inventory-review/` | `persist_observed_inventory` |
| `review-inventory` / 7 | `inventory-review/` | `review_persisted_inventory` |

All endpoints are beneath `https://<backend-host>/mcp/`. The role IDs are explicit
identity permissions, **not execution order**. An extraction needs a real
detected file, not completion of another file's work or an entire pipeline.
A review needs an existing inventory, not new discovery.

## Deploy the MCP host and selected agents

Prerequisites: authenticated `az`/`azd` with hosted-agent/project extensions,
an existing Foundry project and chat model, appropriate region/quota and inference
permissions, an Entra API application configured for v2 access tokens, and approval
for infrastructure spend and source-data residency. The backend audience is that
API application's client GUID, **not** a management API or project managed identity.
Its scope is the application's URI plus `/.default`.

The infrastructure provisions Azure Container Apps, a read-only Azure Files mount,
private PostgreSQL, ACR with admin access disabled, networking, and logs. Hosted
agents, storage, infrastructure and model invocations are billable. Source metadata
can enter model context; obtain the relevant data-residency approval first.

From the repository root, replace placeholders with approved values:

```powershell
$deployment = @{
    SubscriptionId        = '<subscription-guid>'
    TenantId              = '<tenant-guid>'
    ResourceGroupName     = 'rg-osdu-exploration'
    Location              = '<backend-region>'
    NamePrefix            = 'osduexplore'
    EnvironmentName       = 'osdu-dev'
    FoundryProjectId      = '/subscriptions/<guid>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>'
    FoundryProjectEndpoint = 'https://<account>.services.ai.azure.com/api/projects/<project>'
    FoundryLocation       = '<foundry-region>'
    ModelDeploymentName   = '<existing-model-deployment>'
    ApiAudience           = '<backend-api-application-client-guid>'
    BackendScope          = 'api://<backend-api-application-client-guid>/.default'
}

# Offline plan: no provisioning, source upload, or model calls.
.\scripts\deploy_foundry.ps1 @deployment

# Select independent services rather than deploying every agent.
.\scripts\deploy_foundry.ps1 @deployment -ServiceNames extract-metadata,classify-data

# Only after approving the plan and costs:
$password = Read-Host 'PostgreSQL password' -AsSecureString
.\scripts\deploy_foundry.ps1 @deployment -Deploy -PostgresPassword $password
```

`-WhatIf` prevents cloud changes. The default selection is the seven specialists;
the supervisor is opt-in. Deployment checks the HTTPS `/healthz` endpoint before
deploying agents, builds `mcp_servers\host\Dockerfile`, and packages selected source
directories. It does not create a model, upload sources, or invoke agents.

`deployment\azure.yaml` declares a different `project` directory for every
specialist. `scripts\package_foundry.ps1` copies the shared helper into each
selected source before remote build and marks generated copies for safe cleanup.
It never overwrites an existing package directory. Generated packages are ignored
by git and removed by the deployment script's `finally` cleanup.

For a manual `azd` deployment, package the selected service first, run the normal
deployment from `deployment`, then clean it explicitly:

```powershell
.\scripts\package_foundry.ps1 -ServiceNames extract-metadata
# Use your configured deployment\azure.yaml azd environment.
.\scripts\package_foundry.ps1 -ServiceNames extract-metadata -Clean
```

Use separate Foundry projects for isolated environments. Redeploying a service
into the same project creates a new version of that agent.

## Configure actual agent identities and sources

Bootstrap denies specialist execution and receipt reads until identities are
explicitly configured. Obtain the **actual hosted-agent service-principal object
IDs** through the documented Foundry/Entra identity readback. Do not guess JSON
property paths or substitute application/client IDs or the project identity.

Create ignored operator files with the following shapes:

```json
{
  "<approve-workspace-object-id>": 1,
  "<discover-formats-object-id>": 2,
  "<extract-metadata-object-id>": 3,
  "<classify-data-object-id>": 4,
  "<account-failures-object-id>": 5,
  "<persist-inventory-object-id>": 6,
  "<review-inventory-object-id>": 7
}
```

`reader-principals.local.json` is an array of actual operator/coordinator object
IDs. A hosted supervisor must also be an explicitly authorized reader and have
permission to invoke specialist Responses endpoints. It receives no specialist
MCP role and cannot call their deterministic operations directly.
For selected standalone services, supply only their verified role/identity subset;
unrelated agents do not need to be deployed. Empty mappings remain deny-all.

```powershell
.\scripts\configure_foundry_access.ps1 `
  -SubscriptionId $deployment.SubscriptionId -TenantId $deployment.TenantId `
  -ResourceGroupName $deployment.ResourceGroupName -ContainerAppName '<actual-app-name>' `
  -AgentPrincipalsPath .\agent-principals.local.json `
  -ReaderPrincipalsPath .\reader-principals.local.json
# Add -Deploy after reviewing the offline plan.
```

Access updates restart the backend revision. Complete/persist active work first:
unpersisted filesystem capabilities cannot survive a restart.

Upload an approved source copy into the Azure Files share's `Data` directory
through a separately approved network path. Upload is deliberately not automated.
The cloud workspace is `/mnt/source`, not `C:\OSDU\NewSurvey` or the Copilot
repository workspace. Sources remain read-only; do not change them during
observation/extraction, and do not upload links as substitutes for files.

## Invoke one agent independently

The independent invocation CLI needs only Azure identity and HTTP dependencies:

```powershell
uv venv .\agents\.venv --python 3.13
uv pip install --python .\agents\.venv\Scripts\python.exe `
  -r .\agents\invocation-requirements.txt

# Use a real file_id returned by discovery/detection on this MCP host.
@{ file_id = '<observed-file-UUID>' } |
  ConvertTo-Json | Set-Content .\extract-task.local.json

$invoke = @(
  '.\agents\invoke.py',
  '--agent', 'extract-metadata',
  '--operation', 'extract_detected_metadata',
  '--arguments', '.\extract-task.local.json',
  '--endpoint', 'https://<actual-full-extraction-agent-Responses-endpoint>',
  '--backend-url', 'https://<actual-MCP-backend-host>',
  '--backend-scope', $deployment.BackendScope,
  '--output', '.\extraction-result.local.json'
)
& .\agents\.venv\Scripts\python.exe @invoke             # Offline plan.
& .\agents\.venv\Scripts\python.exe @invoke --execute   # Explicit model call.
```

The caller needs reader access to verify `/operations/{request_id}`. The CLI
does not trust the model's completion claim. It verifies the operation, arguments,
request ID and terminal status against the backend receipt.

For workspace registration use operation `register_source_workspace` with `{}`.
For discovery use `discover_source_files` with `{"max_files":100}`. For review use
`review_persisted_inventory` with an actual inventory ID and page bounds.
Persistence additionally requires `--allow-inventory-writes`; a null inventory
ID requests a server-allocated new ID. Existing inventories require an observed
`expected_state_version`, not an invented value.

## Run one specialist locally

Install that specialist's own requirements into a dedicated environment and
configure `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_MODEL_DEPLOYMENT_NAME`,
`OSDU_BACKEND_URL` and `OSDU_BACKEND_SCOPE`. Then run its entry point:

```powershell
uv pip install --python .\agents\.venv\Scripts\python.exe `
  -r .\agents\extract-metadata\requirements.txt
& .\agents\.venv\Scripts\python.exe .\agents\extract-metadata\main.py
```

This starts a local Responses host (port 8088); it still uses the configured
Foundry model and authenticated MCP backend. Local testing is not offline model
execution. Each agent can be run this way without starting the supervisor.

## Verification and migration

```powershell
python -m unittest discover -s agents\tests -v
.\deployment\infra\validate_local.ps1
uv run ruff check .
uv run ruff format --check .
```

The real MCP/core integration test runs when root dependencies are installed.
Full SDK hosting, cloud identity and deployed invocation must also be checked in
your approved environment; offline checks are not proof of a live deployment.

The old `/stages/*` API, shared agent entry point, and `run_pipeline.py` are removed.
Existing inventory data is retained. Old run-ledger records are historical, not
resumable swarm sessions. New invocations use independent operation receipts.
No source-writing, manifest generation, OSDU ingestion or human approval
capabilities are exposed by these MCP servers.

Official references:
[Agent Framework hosting](https://learn.microsoft.com/azure/foundry/how-to/develop/framework-hosted-agents),
[Foundry manifest](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference),
[hosted identity](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-token-service),
[MCP client lifecycle](https://py.sdk.modelcontextprotocol.io/client/transports/).
