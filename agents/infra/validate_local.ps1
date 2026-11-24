#requires -Version 7.0
<#
.SYNOPSIS
Offline deployment regression checks. Azure commands are mocked, never executed.
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
foreach ($file in @($deploySource, $accessSource, $PSCommandPath)) {
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell parse errors in ${file}: $errors" }
}

$fixture = Join-Path $PSScriptRoot ('.local-validation-' + [guid]::NewGuid().ToString('N'))
$state = @{
    Calls = [System.Collections.Generic.List[object]]::new()
    FailBuild = $false
    Tenant = '11111111-1111-1111-1111-111111111111'
    ParametersChecked = 0
    HealthFailuresLeft = 0
    HealthAlwaysFails = $false
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
            Assert-True ($values.agentPrincipals.value.Count -eq 0) 'Bootstrap authorization must default to deny-all.'
            Assert-True ($values.readerPrincipals.value.Count -eq 0) 'Reader allowlist must default empty.'
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
    $state.Calls.Add(@{ Tool = 'azd'; Arguments = @($args) })
    $global:LASTEXITCODE = 0
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
    $null = New-Item -ItemType Directory -Path (Join-Path $fixture 'scripts'), (Join-Path $fixture 'agents\backend'), (Join-Path $fixture 'agents\infra')
    Copy-Item -LiteralPath $deploySource -Destination (Join-Path $fixture 'scripts\deploy_foundry.ps1')
    Copy-Item -LiteralPath $accessSource -Destination (Join-Path $fixture 'scripts\configure_foundry_access.ps1')
    foreach ($file in @('azure.yaml', 'main.py', 'requirements.txt', 'backend\Dockerfile', 'infra\bootstrap.bicep', 'infra\app.bicep')) {
        Set-Content -LiteralPath (Join-Path $fixture "agents\$file") -Value '# Offline validation fixture only.'
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

    $mappingPath = Join-Path $fixture 'mapping.json'
    $mapping = @{}
    foreach ($stage in 1..7) { $mapping["aaaaaaaa-aaaa-aaaa-aaaa-$($stage.ToString('000000000000'))"] = $stage }
    $mapping | ConvertTo-Json | Set-Content -LiteralPath $mappingPath
    $accessParameters = @{
        SubscriptionId = $parameters.SubscriptionId; TenantId = $parameters.TenantId
        ResourceGroupName = $parameters.ResourceGroupName; ContainerAppName = 'test-backend'
        AgentPrincipalsPath = $mappingPath
    }
    & $accessScript @accessParameters
    & $accessScript @accessParameters -Deploy -WhatIf
    Assert-True ($state.Calls.Count -eq 0) 'Access plan and WhatIf must stay offline.'
    '{"aaaaaaaa-aaaa-aaaa-aaaa-000000000001":1}' | Set-Content -LiteralPath $mappingPath
    Assert-Throws { & $accessScript @accessParameters } 'all seven'
    '{}' | Set-Content -LiteralPath $mappingPath
    Assert-Throws { & $accessScript @accessParameters } 'all seven'
    & $accessScript @accessParameters -AllowDenyAll
    $mapping['aaaaaaaa-aaaa-aaaa-aaaa-000000000007'] = '7'
    $mapping | ConvertTo-Json | Set-Content -LiteralPath $mappingPath
    Assert-Throws { & $accessScript @accessParameters } 'distinct integer'
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
            Assert-True (($call.Arguments -join ' ') -notmatch 'Password|postgresql|FOUNDRY_|AGENT_') 'azd must never receive database secrets or reserved runtime variables.'
        }
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'agents\infra') -Directory -Force).Count -eq 0) 'Secret directory must be removed after success.'

        $state.Calls.Clear()
        $state.FailBuild = $true
        Assert-Throws { & $deployScript @parameters -Deploy -PostgresPassword $password -Confirm:$false } 'exit 23'
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'agents\infra') -Directory -Force).Count -eq 0) 'Secret directory must be removed after a native failure.'
        Assert-True (@($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' }).Count -eq 0) 'Build failure must prevent agent deployments.'

        $state.Calls.Clear()
        $state.FailBuild = $false
        $state.HealthAlwaysFails = $true
        Assert-Throws {
            & $deployScript @parameters -Deploy -PostgresPassword $password -BackendHealthTimeoutSeconds 1 -BackendHealthPollSeconds 1 -Confirm:$false
        } 'did not return 200 within 1 seconds'
        Assert-True (@($state.Calls | Where-Object Tool -eq 'https').Count -ge 1) 'Unhealthy backend must be checked before aborting.'
        Assert-True (@($state.Calls | Where-Object { $_.Tool -eq 'azd' -and $_.Arguments[0] -eq 'deploy' }).Count -eq 0) 'Unhealthy backend must prevent agent deployment.'
        Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $fixture 'agents\infra') -Directory -Force).Count -eq 0) 'Health-check failure must not retain secret parameter files.'
        $state.HealthAlwaysFails = $false
    }

    $state.Calls.Clear()
    & $accessScript @accessParameters -Deploy -Confirm:$false
    $updates = @($state.Calls | Where-Object { ($_.Arguments -join ' ') -match '^containerapp update' })
    Assert-True ($updates.Count -eq 1) 'Access configuration must issue exactly one Container App update.'
    Assert-True (($updates[0].Arguments -join ' ') -match '--set-env-vars OSDU_AGENT_PRINCIPALS_JSON=') 'Access update must merge only the agent mapping.'
    $state.Calls.Clear()
    $state.Tenant = '99999999-9999-9999-9999-999999999999'
    Assert-Throws { & $accessScript @accessParameters -Deploy -Confirm:$false } 'tenant does not match'
    Assert-True ($state.Calls.Count -eq 1) 'Wrong tenant must stop before mutation.'
    Write-Host 'PASS: AST parsing; offline plans/WhatIf; allowlist validation; mocked seven-agent deployment; HTTPS readiness retry and timeout gate; secure parameter cleanup on success/failure; mapping-only update; tenant mismatch guard.'
}
finally {
    # Only this invocation's uniquely named fixture subtree is removed.
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
