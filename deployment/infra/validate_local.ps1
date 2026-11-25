#requires -Version 7.0
<#
.SYNOPSIS
Offline deployment and packaging regression checks. Azure commands are mocked, never executed.
.DESCRIPTION
Parses the two deployment scripts, checks plan/WhatIf safety and identity input
validation, and exercises the deployment ordering and secret-file cleanup using
local fixtures. Run az bicep build --file <template> --stdout separately to
compile the templates with an already-installed compiler.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$deploySource = Join-Path $repoRoot 'scripts\deploy_foundry.ps1'
$accessSource = Join-Path $repoRoot 'scripts\configure_foundry_access.ps1'
$packageSource = Join-Path $repoRoot 'scripts\package_foundry.ps1'
foreach ($file in @($deploySource, $accessSource, $packageSource, $PSCommandPath)) {
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell parse errors in ${file}: $errors" }
}

$fixtureName = '.local-validation-' + [guid]::NewGuid().ToString('N')
$fixture = Join-Path $PSScriptRoot $fixtureName
$state = @{
    Calls = [System.Collections.Generic.List[object]]::new()
    FailBuild = $false
    Tenant = '11111111-1111-1111-1111-111111111111'
    ParametersChecked = 0
    HealthFailuresLeft = 0
    HealthAlwaysFails = $false
    FailDeploy = $false
    ExpectedReaders = 0
    ExpectedSpecialists = 0
}
function Assert-True([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw "FAIL: $Message" }
}
function Assert-Throws([scriptblock] $Action, [string] $Pattern) {
    try { & $Action; throw 'Expected an exception.' }
    catch {
        if ($_.Exception.Message -notmatch $Pattern) { throw }
    }
}
$appTemplate = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'app.bicep') -Raw
Assert-True ($appTemplate -match "command:\s*\['uvicorn'\]\s*args:\s*\['mcp_servers\.host\.app:create_app',\s*'--factory',\s*'--host',\s*'0\.0\.0\.0',\s*'--port',\s*'8080',\s*'--workers',\s*'1'\]") 'Container App must run the MCP host application factory with uvicorn and one worker.'
function az {
    $a = @($args)
    $state.Calls.Add(@{ Tool = 'az'; Arguments = $a })
    $global:LASTEXITCODE = 0
    $line = $a -join ' '
    if ($line -match '^account show') {
        return (@{ tenantId = $state.Tenant } | ConvertTo-Json -Compress)
    }
    if ($line -match '^resource show') {
        if ($line -match '/projects/project') { return '{"location":"eastus"}' }
        return '{"properties":{"endpoints":{"Azure AI":"https://account.services.ai.azure.com/"}}}'
    }
    if ($line -match '^deployment group create') {
        $parameterIndex = [array]::IndexOf($a, '--parameters') + 1
        $path = $a[$parameterIndex].Substring(1)
        $values = (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -AsHashtable).parameters
        Assert-True ($path.StartsWith($fixture)) 'Secret file must stay within the local fixture.'
        Assert-True ((Get-Acl -LiteralPath (Split-Path $path -Parent)).AreAccessRulesProtected) 'Secret directory must disable inherited access.'
        if ($values.ContainsKey('databaseUrl')) {
            Assert-True ($values.databaseUrl.value -match '^postgresql\+psycopg://.*\?sslmode=require$') 'Database URL must require TLS.'
            Assert-True ($values.agentPrincipals.value.Count -eq $state.ExpectedSpecialists) 'Specialist authorization must preserve the supplied subset and default to deny-all.'
            Assert-True ($values.readerPrincipals.value.Count -eq $state.ExpectedReaders) 'Reader allowlist must contain only supplied identities.'
        }
        else {
            Assert-True ($values.postgresPassword.value -eq 'Local-Test-Password-Only!42') 'SecureString must reach only the ARM parameter file.'
        }
        $state.ParametersChecked++
        return
    }
    if ($line -match '^deployment group show') {
        if ($line -match 'test-bootstrap') {
            return '{"registryLoginServer":{"value":"test.azurecr.io"},"postgresHost":{"value":"private.postgres.database.azure.com"}}'
        }
        return '{"backendUrl":{"value":"https://test-backend.example.net"},"backendResourceId":{"value":"/subscriptions/test/containerApps/test-backend"}}'
    }
    if ($line -match '^acr build' -and $state.FailBuild) { $global:LASTEXITCODE = 23 }
}
function azd {
    $a = @($args)
    $state.Calls.Add(@{ Tool = 'azd'; Arguments = $a })
    $global:LASTEXITCODE = 0
    if ($a[0] -eq 'env' -and $a[1] -eq 'new') {
        $path = Join-Path $fixture "deployment\.azure\$($a[2])"
        $null = New-Item -ItemType Directory -Path $path -Force
        Set-Content -LiteralPath (Join-Path $path '.env') -Value '# local fixture'
    }
    if ($a[0] -eq 'deploy') {
        Assert-True ((Get-Location).Path -eq (Join-Path $fixture 'deployment')) 'azd must execute from deployment root.'
        $relative = if ($a[1] -eq 'exploration-supervisor') { 'swarms\exploration' } else { "agents\$($a[1])" }
        Assert-True (Test-Path -LiteralPath (Join-Path $fixture "$relative\osdu_agent_host\helper.py")) 'Selected service must be self-contained before azd deploy.'
        if ($state.FailDeploy) { $global:LASTEXITCODE = 29 }
    }
}
function Invoke-WebRequest {
    param([uri] $Uri, [string] $Method, [switch] $UseBasicParsing,
        [int] $TimeoutSec, [int] $MaximumRedirection, [switch] $SkipHttpErrorCheck,
        [string] $ErrorAction)
    $state.Calls.Add(@{ Tool = 'https'; Arguments = @($Uri.AbsoluteUri, $Method, $TimeoutSec, $MaximumRedirection) })
    Assert-True ($Uri.AbsoluteUri -eq 'https://test-backend.example.net/healthz') 'Readiness must request only the backend HTTPS health endpoint.'
    Assert-True ($Method -eq 'Get' -and $MaximumRedirection -eq 0 -and $TimeoutSec -le 10) 'Health checks must reject redirects and bound request duration.'
    if ($state.HealthAlwaysFails -or $state.HealthFailuresLeft -gt 0) {
        $state.HealthFailuresLeft--
        return [pscustomobject]@{ StatusCode = 503 }
    }
    return [pscustomobject]@{ StatusCode = 200 }
}

try {
    $specialists = @('approve-workspace', 'discover-formats', 'extract-metadata', 'classify-data',
        'account-failures', 'persist-inventory', 'review-inventory')
    $null = New-Item -ItemType Directory -Path (Join-Path $fixture 'scripts'),
        (Join-Path $fixture 'mcp_servers\host'), (Join-Path $fixture 'deployment\infra'),
        (Join-Path $fixture 'agents\shared\osdu_agent_host\__pycache__')
    $fixture = (Resolve-Path -LiteralPath $fixture).Path
    Copy-Item -LiteralPath $deploySource -Destination (Join-Path $fixture 'scripts\deploy_foundry.ps1')
    Copy-Item -LiteralPath $accessSource -Destination (Join-Path $fixture 'scripts\configure_foundry_access.ps1')
    Copy-Item -LiteralPath $packageSource -Destination (Join-Path $fixture 'scripts\package_foundry.ps1')
    Copy-Item -LiteralPath (Join-Path $repoRoot 'deployment\azure.yaml') -Destination (Join-Path $fixture 'deployment\azure.yaml')
    $manifest = Get-Content -LiteralPath (Join-Path $fixture 'deployment\azure.yaml') -Raw
    Assert-True ($manifest -notmatch 'OSDU_STAGE') 'Capabilities must not be selected by sequential stage.'
    foreach ($service in $specialists) {
        Assert-True ($manifest.Contains("project: ../agents/$service")) "Manifest must use independent project root for $service."
    }
    Assert-True ($manifest.Contains('project: ../swarms/exploration') -and
        $manifest.Contains('OSDU_AGENT_ENDPOINTS_JSON: ${OSDU_AGENT_ENDPOINTS_JSON}') -and
        $manifest.Contains('OSDU_SWARM_CONCURRENCY: ${OSDU_SWARM_CONCURRENCY}') -and
        $manifest.Contains('OSDU_ALLOW_INVENTORY_WRITES: ${OSDU_ALLOW_INVENTORY_WRITES}')) 'Supervisor manifest must use supplied endpoints, concurrency and inventory write policy.'
    foreach ($file in @('mcp_servers\host\Dockerfile', 'deployment\infra\bootstrap.bicep', 'deployment\infra\app.bicep')) {
        Set-Content -LiteralPath (Join-Path $fixture $file) -Value '# Offline validation fixture only.'
    }
    foreach ($service in @($specialists) + @('exploration-supervisor')) {
        $relative = if ($service -eq 'exploration-supervisor') { 'swarms\exploration' } else { "agents\$service" }
        $root = Join-Path $fixture $relative
        $null = New-Item -ItemType Directory -Path $root -Force
        foreach ($file in @('main.py', 'requirements.txt')) {
            Set-Content -LiteralPath (Join-Path $root $file) -Value '# Offline validation fixture only.'
        }
    }
    Set-Content -LiteralPath (Join-Path $fixture 'agents\shared\osdu_agent_host\helper.py') -Value '# Offline validation helper.'
    Set-Content -LiteralPath (Join-Path $fixture 'agents\shared\osdu_agent_host\__pycache__\helper.pyc') -Value 'cache'
    $packageScript = Join-Path $fixture 'scripts\package_foundry.ps1'
    $generated = Join-Path $fixture 'agents\approve-workspace\osdu_agent_host'
    & $packageScript -ServiceNames approve-workspace -WhatIf
    Assert-True (-not (Test-Path -LiteralPath $generated)) 'Package WhatIf must not copy sources.'
    & $packageScript -ServiceNames approve-workspace
    Assert-True (Test-Path -LiteralPath (Join-Path $generated 'helper.py')) 'Packaging must copy shared helpers.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $generated '__pycache__'))) 'Packaging must skip Python caches.'
    Assert-Throws { & $packageScript -ServiceNames approve-workspace } 'refusing overwrite'
    & $packageScript -ServiceNames approve-workspace -Clean -WhatIf
    Assert-True (Test-Path -LiteralPath $generated) 'Cleanup WhatIf must leave owned package intact.'
    & $packageScript -ServiceNames approve-workspace -Clean
    $null = New-Item -ItemType Directory -Path $generated
    Assert-Throws { & $packageScript -ServiceNames approve-workspace -Clean } 'ownership marker'
    Remove-Item -LiteralPath $generated
    if ($IsWindows) {
        $locked = [IO.File]::Open((Join-Path $fixture 'agents\shared\osdu_agent_host\helper.py'),
            [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        try {
            Assert-Throws { & $packageScript -ServiceNames approve-workspace,review-inventory } 'being used|another process|cannot access|in use'
            Assert-True (-not (Test-Path -LiteralPath $generated) -and
                -not (Test-Path -LiteralPath (Join-Path $fixture 'agents\review-inventory\osdu_agent_host'))) 'Packaging copy failure must roll back packages created by this invocation.'
        }
        finally { $locked.Dispose() }
    }
    $deployScript = Join-Path $fixture 'scripts\deploy_foundry.ps1'
    $accessScript = Join-Path $fixture 'scripts\configure_foundry_access.ps1'
    $parameters = @{
        SubscriptionId = '22222222-2222-2222-2222-222222222222'
        TenantId = $state.Tenant
        ResourceGroupName = 'rg-local-validation'
        Location = 'eastus'
        NamePrefix = 'test'
        EnvironmentName = 'test-env'
        FoundryProjectId = '/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg-foundry/providers/Microsoft.CognitiveServices/accounts/account/projects/project'
        FoundryProjectEndpoint = 'https://account.services.ai.azure.com/api/projects/project'
        FoundryLocation = 'eastus'
        ModelDeploymentName = 'existing-model'
        ApiAudience = '33333333-3333-3333-3333-333333333333'
        BackendScope = 'api://33333333-3333-3333-3333-333333333333/.default'
    }
    & $deployScript @parameters
    Assert-True ($state.Calls.Count -eq 0) 'Default plan must not invoke az or azd.'
    $password = ConvertTo-SecureString 'Local-Test-Password-Only!42' -AsPlainText -Force
    & $deployScript @parameters -Deploy -PostgresPassword $password -WhatIf
    Assert-True ($state.Calls.Count -eq 0) 'WhatIf must not invoke az or azd.'
    Assert-True (-not (Test-Path -LiteralPath $generated)) 'Deploy WhatIf must not package sources.'

    Assert-Throws { & $deployScript @parameters -ServiceNames exploration-supervisor } 'requires AgentEndpointsPath'
    Assert-Throws { & $deployScript @parameters -PermitInventoryWrites } 'only to selected exploration-supervisor'
    $endpointPath = Join-Path $fixture 'endpoints.json'
    $readerPath = Join-Path $fixture 'readers.json'
    $endpoints = @{}
    foreach ($service in $specialists) { $endpoints[$service] = "https://existing.example.net/$service/responses" }
    $endpoints | ConvertTo-Json | Set-Content -LiteralPath $endpointPath
    $supervisorId = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000001'
    & $deployScript @parameters -ServiceNames exploration-supervisor -AgentEndpointsPath $endpointPath
    Assert-Throws {
        & $deployScript @parameters -ServiceNames exploration-supervisor -AgentEndpointsPath $endpointPath -SupervisorPrincipalId $supervisorId
    } 'explicitly allowlisted'
    ConvertTo-Json -InputObject @($supervisorId) | Set-Content -LiteralPath $readerPath
    $supervisorParameters = @{
        ServiceNames = @('exploration-supervisor'); AgentEndpointsPath = $endpointPath
        SupervisorPrincipalId = $supervisorId; ReaderPrincipalsPath = $readerPath; SwarmConcurrency = 2
    }
    & $deployScript @parameters @supervisorParameters
    $endpoints['review-inventory'] = 'http://existing.example.net/responses'
    $endpoints | ConvertTo-Json | Set-Content -LiteralPath $endpointPath
    Assert-Throws { & $deployScript @parameters @supervisorParameters } 'HTTPS endpoint'
    $endpoints['review-inventory'] = 'https://existing.example.net/review-inventory/responses'
    $endpoints | ConvertTo-Json | Set-Content -LiteralPath $endpointPath
    Assert-True ($state.Calls.Count -eq 0) 'Supervisor input validation and plans must stay offline.'

    $mappingPath = Join-Path $fixture 'mapping.json'
    $mapping = @{}
    foreach ($stage in 1..7) { $mapping["aaaaaaaa-aaaa-aaaa-aaaa-$($stage.ToString('000000000000'))"] = $stage }
    $mapping | ConvertTo-Json | Set-Content -LiteralPath $mappingPath
    ConvertTo-Json -InputObject @('aaaaaaaa-aaaa-aaaa-aaaa-000000000001') | Set-Content -LiteralPath $readerPath
    Assert-Throws {
        & $deployScript @parameters -AgentPrincipalsPath $mappingPath -ReaderPrincipalsPath $readerPath
    } 'must not have specialist tool permissions'
    ConvertTo-Json -InputObject @($supervisorId) | Set-Content -LiteralPath $readerPath
    $accessParameters = @{
        SubscriptionId = $parameters.SubscriptionId; TenantId = $parameters.TenantId
        ResourceGroupName = $parameters.ResourceGroupName; ContainerAppName = 'test-backend'
        AgentPrincipalsPath = $mappingPath
    }
    & $accessScript @accessParameters
    & $accessScript @accessParameters -Deploy -WhatIf
    Assert-True ($state.Calls.Count -eq 0) 'Access plan and WhatIf must stay offline.'
    '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":1}' | Set-Content -LiteralPath $mappingPath
    & $accessScript @accessParameters
    & $deployScript @parameters -AgentPrincipalsPath $mappingPath -ServiceNames approve-workspace
    $partialPath = Join-Path $fixture 'partial-mapping.json'
    '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000002":2,"aaaaaaaa-aaaa-aaaa-aaaa-000000000007":7}' |
        Set-Content -LiteralPath $partialPath
    & $accessScript @accessParameters -AgentPrincipalsPath $partialPath
    & $deployScript @parameters -AgentPrincipalsPath $partialPath -ServiceNames discover-formats,review-inventory
    '{}' | Set-Content -LiteralPath $mappingPath
    Assert-Throws { & $accessScript @accessParameters } 'AllowDenyAll'
    & $accessScript @accessParameters -AllowDenyAll
    & $deployScript @parameters -AgentPrincipalsPath $mappingPath
    foreach ($invalid in @(
        '{"invalid-object-id":1}',
        '{"00000000-0000-0000-0000-000000000000":1}',
        '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":0}',
        '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":8}',
        '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":1,"aaaaaaaa-aaaa-aaaa-aaaa-000000000002":1}',
        '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":1,"{aaaaaaaa-aaaa-aaaa-aaaa-000000000001}":7}'
    )) {
        Set-Content -LiteralPath $mappingPath -Value $invalid
        Assert-Throws { & $accessScript @accessParameters } 'distinct object-ID GUID'
        Assert-Throws { & $deployScript @parameters -AgentPrincipalsPath $mappingPath } 'distinct object-ID GUID'
    }
    $mapping['aaaaaaaa-aaaa-aaaa-aaaa-000000000007'] = '7'
    $mapping | ConvertTo-Json | Set-Content -LiteralPath $mappingPath
    Assert-Throws { & $accessScript @accessParameters } 'distinct integer'
    Assert-Throws { & $deployScript @parameters -AgentPrincipalsPath $mappingPath } 'distinct integer'
    $mapping['aaaaaaaa-aaaa-aaaa-aaaa-000000000007'] = 7
    $mapping | ConvertTo-Json | Set-Content -LiteralPath $mappingPath

    if ($IsWindows) {
        $state.HealthFailuresLeft = 1
        & $deployScript @parameters -Deploy -PostgresPassword $password -BackendHealthPollSeconds 1 -Confirm:$false
        Assert-True ($state.ParametersChecked -eq 2) 'Both ARM deployments must use protected parameter files.'
        $calls = @($state.Calls)
        $build = @($calls | Where-Object { $_.Tool -eq 'az' -and ($_.Arguments -join ' ') -match '^acr build' })
        Assert-True ($build.Count -eq 1) 'Exactly one backend image build is expected.'
        $agentDeployments = @($calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' })
        Assert-True ($agentDeployments.Count -eq 7) 'All seven agents must deploy independently.'
        $healthChecks = @($calls | Where-Object Tool -eq 'https')
        Assert-True ($healthChecks.Count -eq 2) 'Readiness must retry a transient unhealthy response.'
        $lastHealthIndex = [array]::LastIndexOf($calls, $healthChecks[-1])
        $firstDeployIndex = [array]::IndexOf($calls, $agentDeployments[0])
        Assert-True ($lastHealthIndex -lt $firstDeployIndex) 'Backend readiness must precede all seven agent deployments.'
        foreach ($call in $calls | Where-Object { $_.Tool -eq 'azd' }) {
            Assert-True (($call.Arguments -join ' ') -notmatch 'Password|postgresql|FOUNDRY_') 'azd must never receive database secrets or reserved runtime variables.'
        }
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'deployment\infra') -Directory -Force).Count -eq 0) 'Secret directory must be removed after success.'
        foreach ($service in $specialists) {
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture "agents\$service\osdu_agent_host"))) 'Generated packages must be removed after successful deployment.'
        }
        Assert-True (($build[0].Arguments -join ' ') -match 'mcp_servers\\host\\Dockerfile') 'ACR must build the root cloud MCP host Dockerfile.'
        Assert-True (@($calls | Where-Object { ($_.Arguments -join ' ') -match '^ai agent init' }).Count -eq 0) 'Authored manifest must not be reinitialized.'
        Assert-True ((Get-Content -LiteralPath (Join-Path $fixture 'deployment\azure.yaml') -Raw) -ceq $manifest) 'Deploy must preserve the authored manifest.'

        $state.Calls.Clear()
        $state.ExpectedSpecialists = 2
        & $deployScript @parameters -AgentPrincipalsPath $partialPath -ServiceNames discover-formats,review-inventory -Deploy -PostgresPassword $password -Confirm:$false
        $selected = @($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' })
        Assert-True ($selected.Count -eq 2 -and $selected[0].Arguments[1] -eq 'discover-formats' -and
            $selected[1].Arguments[1] -eq 'review-inventory') 'Only selected specialist services must deploy.'
        Assert-True (@($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^env new' }).Count -eq 0) 'Existing azd environment must be reused.'
        $state.ExpectedSpecialists = 0

        $state.Calls.Clear()
        & $deployScript @parameters -ServiceNames exploration-supervisor -AgentEndpointsPath $endpointPath -Deploy -PostgresPassword $password -Confirm:$false
        Assert-True (@($state.Calls | Where-Object { $_.Tool -eq 'azd' -and
            ($_.Arguments -join ' ') -match '^deploy exploration-supervisor' }).Count -eq 1) 'Supervisor bootstrap must deploy with receipt readers denied.'
        Assert-True (@($state.Calls | Where-Object { ($_.Arguments -join ' ') -match
            '^env set OSDU_ALLOW_INVENTORY_WRITES false' }).Count -eq 1) 'Supervisor persistence must be denied by default.'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture 'swarms\exploration\osdu_agent_host'))) 'Supervisor bootstrap must clean generated packages.'

        $state.Calls.Clear()
        $state.ExpectedReaders = 1
        & $deployScript @parameters @supervisorParameters -PermitInventoryWrites -Deploy -PostgresPassword $password -Confirm:$false
        $selected = @($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' })
        Assert-True ($selected.Count -eq 1 -and $selected[0].Arguments[1] -eq 'exploration-supervisor') 'Supervisor opt-in must deploy only selected supervisor.'
        $endpointSettings = @($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^env set OSDU_AGENT_ENDPOINTS_JSON' })
        Assert-True ($endpointSettings.Count -eq 1 -and
            (($endpointSettings[0].Arguments[3] | ConvertFrom-Json -AsHashtable).Count -eq 7)) 'Supervisor endpoints must be passed from operator input.'
        Assert-True (@($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^env set OSDU_SWARM_CONCURRENCY 2' }).Count -eq 1) 'Supervisor concurrency must be passed explicitly.'
        Assert-True (@($state.Calls | Where-Object { ($_.Arguments -join ' ') -match
            '^env set OSDU_ALLOW_INVENTORY_WRITES true' }).Count -eq 1) 'Inventory persistence must require the explicit deployment switch.'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture 'swarms\exploration\osdu_agent_host'))) 'Supervisor package must be cleaned.'
        $state.ExpectedReaders = 0

        $state.Calls.Clear()
        $state.FailDeploy = $true
        Assert-Throws { & $deployScript @parameters -ServiceNames approve-workspace -Deploy -PostgresPassword $password -Confirm:$false } 'exit 29'
        Assert-True (-not (Test-Path -LiteralPath $generated)) 'Hosted deploy failure must clean generated packages.'
        $state.FailDeploy = $false

        $state.Calls.Clear()
        $state.FailBuild = $true
        Assert-Throws { & $deployScript @parameters -Deploy -PostgresPassword $password -Confirm:$false } 'exit 23'
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'deployment\infra') -Directory -Force).Count -eq 0) 'Secret directory must be removed after a native failure.'
        Assert-True (-not (Test-Path -LiteralPath $generated)) 'Build failure must clean generated packages.'
        Assert-True (@($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' }).Count -eq 0) 'Build failure must prevent agent deployments.'

        $state.Calls.Clear()
        $state.FailBuild = $false
        $state.HealthAlwaysFails = $true
        Assert-Throws {
            & $deployScript @parameters -Deploy -PostgresPassword $password -BackendHealthTimeoutSeconds 1 -BackendHealthPollSeconds 1 -Confirm:$false
        } 'did not return 200 within 1 seconds'
        Assert-True (@($state.Calls | Where-Object Tool -eq 'https').Count -ge 1) 'Unhealthy backend must be checked before aborting.'
        Assert-True (@($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' }).Count -eq 0) 'Unhealthy backend must prevent agent deployment.'
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'deployment\infra') -Directory -Force).Count -eq 0) 'Health-check failure must not retain secret parameter files.'
        Assert-True (-not (Test-Path -LiteralPath $generated)) 'Health failure must clean generated packages.'
        $state.HealthAlwaysFails = $false
    }

    $state.Calls.Clear()
    $partialAccessParameters = @{} + $accessParameters
    $partialAccessParameters.AgentPrincipalsPath = $partialPath
    & $accessScript @partialAccessParameters -Deploy -Confirm:$false
    $partialUpdates = @($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^containerapp update' })
    Assert-True ($partialUpdates.Count -eq 1) 'A nonempty capability subset must be configurable independently.'
    $mappingArgument = @($partialUpdates[0].Arguments | Where-Object { $_ -like 'OSDU_AGENT_PRINCIPALS_JSON=*' })[0]
    $partialConfigured = $mappingArgument.Substring('OSDU_AGENT_PRINCIPALS_JSON='.Length) | ConvertFrom-Json -AsHashtable
    Assert-True ($partialConfigured.Count -eq 2 -and
        (@($partialConfigured.Values | Sort-Object) -join ',') -eq '2,7') 'Partial configuration must preserve only the selected capability roles.'
    $state.Calls.Clear()
    & $accessScript @accessParameters -Deploy -Confirm:$false
    $updates = @($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^containerapp update' })
    Assert-True ($updates.Count -eq 1) 'Access configuration must issue exactly one Container App update.'
    Assert-True (($updates[0].Arguments -join ' ') -match '--set-env-vars OSDU_AGENT_PRINCIPALS_JSON=') 'Access update must merge only the agent mapping.'
    $state.Calls.Clear()
    $state.Tenant = '99999999-9999-9999-9999-999999999999'
    Assert-Throws { & $accessScript @accessParameters -Deploy -Confirm:$false } 'tenant does not match'
    Assert-True ($state.Calls.Count -eq 1) 'Wrong tenant must stop before mutation.'
    Write-Host 'PASS: AST parsing; new manifest layout; offline plans/WhatIf; packaging ownership and cache exclusion; selected specialist/supervisor deploys; supervisor input gating; environment reuse without init; readiness retry/timeout; secret and package cleanup on success/build/deploy/health failures; mapping-only update; tenant guard.'
}
finally {
    # Only this invocation's uniquely named fixture subtree is removed.
    if (Test-Path -LiteralPath $fixture) {
        $resolved = (Resolve-Path -LiteralPath $fixture).Path
        if ((Split-Path $resolved -Leaf) -cne $fixtureName -or
            (Split-Path $resolved -Parent) -ne (Resolve-Path -LiteralPath $PSScriptRoot).Path) {
            throw 'Refusing cleanup outside this invocation''s resolved named fixture subtree.'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
