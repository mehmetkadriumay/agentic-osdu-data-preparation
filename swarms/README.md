# Supervisor-led OSDU exploration swarm

`exploration` is a separately hosted Microsoft Agent Framework supervisor.
Its model dynamically chooses specialists and hands tasks to their independently
deployed Foundry Responses endpoints. `delegate_tasks` executes up to 32 tasks
per batch with configurable concurrency (1-16, default 4). It verifies each
actual MCP operation receipt instead of trusting specialist completion claims.

This is not the removed sequential `run_pipeline.py` under a new name. A supervisor
may handle just extraction, just review, or an end-to-end task. It can mix
inspection/extraction/classification work for **different files** in a batch.
Same-file dependent tasks cannot be submitted in the same batch. Registration,
discovery, failure accounting and commits use separate barriers. The MCP host
also enforces per-file synchronization and a single-writer commit barrier.
Unsupported files and uncertain outcomes remain explicit results.

## Run the supervisor locally over hosted specialists

Use actual full Responses endpoints obtained from the deployed services, including
their documented path/query requirements. Do not construct endpoints by guessing
from agent names:

```json
{
  "approve-workspace": "https://<actual-workspace-agent-Responses-endpoint>",
  "discover-formats": "https://<actual-discovery-agent-Responses-endpoint>",
  "extract-metadata": "https://<actual-extraction-agent-Responses-endpoint>",
  "classify-data": "https://<actual-classification-agent-Responses-endpoint>",
  "account-failures": "https://<actual-accounting-agent-Responses-endpoint>",
  "persist-inventory": "https://<actual-inventory-agent-Responses-endpoint>",
  "review-inventory": "https://<actual-review-agent-Responses-endpoint>"
}
```

Save the actual mapping as ignored `agent-endpoints.local.json`. The operator
needs Foundry invocation permission and explicit backend reader access.

```powershell
uv venv .\swarms\.venv --python 3.13
uv pip install --python .\swarms\.venv\Scripts\python.exe `
  -r .\swarms\exploration\requirements.txt

$swarm = @(
  '.\swarms\exploration\run.py',
  '--endpoints', '.\agent-endpoints.local.json',
  '--backend-url', 'https://<actual-backend-host>',
  '--backend-scope', 'api://<actual-backend-api-client-guid>/.default',
  '--project-endpoint', 'https://<account>.services.ai.azure.com/api/projects/<project>',
  '--model', '<existing-chat-model-deployment>',
  '--concurrency', '4',
  '--timeout', '1800',
  '--output', '.\swarm-review.local.json',
  '--request', 'Explore the approved Data source. Inspect independent files concurrently, extract supported formats, classify from observations, retain all failures, persist one new inventory, and review every page. Do not modify sources, generate manifests, or ingest into OSDU.'
)

# Offline plan: no tokens, inference, inventory changes or report files.
& .\swarms\.venv\Scripts\python.exe @swarm

# Explicitly authorize inference and inventory persistence.
& .\swarms\.venv\Scripts\python.exe @swarm --execute --allow-inventory-writes
```

Without `--allow-inventory-writes`, the coordinator rejects persistence even if
the model requests it or puts an approval flag into task arguments. This is
immutable caller/operator policy, not permission the model can grant itself.
For existing inventories, supply the actual ID and observed version in the task.
The supervisor cannot guess a version to overwrite another run.

The report separates `supervisor_summary` (model-generated, untrusted) from
authoritative receipts and delegation outcomes. A timeout/failure retains available
receipts in a blocked report. Unknown outcomes require investigation, especially
before retrying an inventory commit. A model returning text is not proof that
the requested end-to-end work was completed.

## Host the supervisor on Foundry

First deploy/configure the seven specialists as described in
[agents](../agents/README.md). Then select the separate supervisor service:

```powershell
.\scripts\deploy_foundry.ps1 @deployment `
  -ServiceNames exploration-supervisor `
  -AgentEndpointsPath .\agent-endpoints.local.json `
  -SwarmConcurrency 4

# After approving costs and persistence policy:
.\scripts\deploy_foundry.ps1 @deployment -Deploy -PostgresPassword $password `
  -ServiceNames exploration-supervisor `
  -AgentEndpointsPath .\agent-endpoints.local.json `
  -SwarmConcurrency 4 -PermitInventoryWrites
```

Bootstrap can deploy with no supervisor reader grant. After obtaining its actual
agent identity object ID, add it to `reader-principals.local.json`, configure
backend access, and verify its Foundry specialist invocation permissions.
Never map it to a specialist MCP role. For an already verified supervisor,
the deployment script accepts `-SupervisorPrincipalId` with its reader mapping.
Do not invoke during backend revision/access updates.

The hosted service has its own directory/entry point and uses
`OSDU_AGENT_ENDPOINTS_JSON`, `OSDU_SWARM_CONCURRENCY`, and
`OSDU_ALLOW_INVENTORY_WRITES` plus ordinary Foundry/backend settings.
Persistence is disabled by default; `-PermitInventoryWrites` explicitly enables
that operator policy. Independently called specialists do not require the
supervisor to be running.

## Current boundary

The swarm distributes **agent reasoning and delegation**, while deterministic
file access stays in one protected MCP host. It does not provide distributed
parser workers or durable resumable supervisor sessions. Persist active work
before backend restarts; review of persisted inventories remains independent.
No source-writing, manifest generation, OSDU ingestion, or human-approval tools
are available.
