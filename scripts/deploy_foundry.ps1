#requires -Version 7.0
<#
.SYNOPSIS
Plans, or explicitly deploys, the private-data runtime and seven hosted Foundry agents.
.DESCRIPTION
The default is an offline plan, not an ARM what-if. -Deploy permits billable changes;
-WhatIf still prevents all Azure calls. No login, model creation, source upload,
remote agent invocation, or resource deletion is performed.

Prerequisites: authenticated az and azd (agents/projects extensions), an existing
Foundry project/model, and an Entra API app with requestedAccessTokenVersion=2.
ApiAudience is that app's client GUID, not the Foundry project identity. BackendScope
is its application URI plus /.default. The application validates tokens itself.
Provide distinct, operator-verified service principal object IDs for the selected
capability roles (any subset of 1..7) after
deployment with configure_foundry_access.ps1; bootstrap defaults to deny-all.

PostgresPassword is a SecureString. Deployment parameter files are created beneath
deployment\infra in a current-user-only directory and removed in finally. Never enable
native CLI debug logging or transcripts when handling deployment secrets.
Database credentials are never written to azd environments or printed.

UploadIpAddress optionally permits one public IPv4 address for manual source uploads
to the source share's Data directory; otherwise only the ACA subnet can access it.
No data is uploaded. Removing an earlier upload IP requires redeployment with ''.

The backend image must run as nonroot UID 10001 (mcp_servers\host\Dockerfile),
with uvicorn mcp_servers.host.app:create_app as its application factory.
It is forced to one worker and one replica because caches are not distributed.
An unauthenticated HTTPS GET /healthz must return 200 within the bounded health
timeout before agent deployment. Redirects are rejected; tokens and response bodies
are never logged. Each azd deploy must succeed; no separate agent-state query or
model invocation is performed. Existing Foundry model inference permissions are
an operator/platform prerequisite; this script does not grant those permissions.
Rerunning creates a new backend image and immutable versions of selected services.
ServiceNames defaults to the seven independent capability agents (role IDs 1..7,
not a sequential counter). exploration-supervisor is opt-in: supply a JSON object
of all seven service names to existing HTTPS Responses endpoint strings using
AgentEndpointsPath. SupervisorPrincipalId, when supplied, must already be
allowlisted in ReaderPrincipalsPath, never in AgentPrincipalsPath. Bootstrap may
omit it and readers: receipt reads remain denied until the actual supervisor
principal is verified and separately configured. No endpoints or IDs are guessed.
The authored deployment\azure.yaml adopts the existing project using environment
settings; agent init is not run and the manifest is never overwritten.
Selected shared helpers are packaged only after approval and cleaned in finally.
Supervisor persistence is disabled by default. -PermitInventoryWrites explicitly
sets its immutable operator policy to allow persist-inventory delegation; this
switch is valid only when exploration-supervisor is among the selected services.
.EXAMPLE
$password = Read-Host 'PostgreSQL password' -AsSecureString
.\scripts\deploy_foundry.ps1 -SubscriptionId '<guid>' -TenantId '<guid>' `
  -ResourceGroupName 'rg-osdu' -Location 'eastus' -NamePrefix 'osdu' `
  -EnvironmentName 'osdu-dev' -FoundryProjectId '<project ARM ID>' `
  -FoundryProjectEndpoint 'https://ACCOUNT.services.ai.azure.com/api/projects/PROJECT' `
  -FoundryLocation 'eastus' -ModelDeploymentName 'existing-model' `
  -ApiAudience '<API app client GUID>' -BackendScope 'api://<API app client GUID>/.default'
Add -Deploy -PostgresPassword $password to execute after reviewing the plan.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)][guid] $SubscriptionId,
    [Parameter(Mandatory)][guid] $TenantId,
    [Parameter(Mandatory)][ValidatePattern('^[\w.()-]{1,90}$')][string] $ResourceGroupName,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string] $Location,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9]{2,11}$')][string] $NamePrefix,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9-]+$')][string] $EnvironmentName,
    [Parameter(Mandatory)][string] $FoundryProjectId,
    [Parameter(Mandatory)][uri] $FoundryProjectEndpoint,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string] $FoundryLocation,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $ModelDeploymentName,
    [Parameter(Mandatory)][guid] $ApiAudience,
    [Parameter(Mandatory)][ValidatePattern('^api://[^ ]+/\.default$')][string] $BackendScope,
    [securestring] $PostgresPassword,
    [ValidatePattern('^[a-z][a-z0-9_]{0,30}$')][string] $PostgresAdministrator = 'osduadmin',
    [ValidatePattern('^[a-z][a-z0-9_]{0,30}$')][string] $DatabaseName = 'osdu',
    [string] $RegistryName,
    [string] $StorageAccountName,
    [string] $PostgresServerName,
    [string] $ContainerAppName,
    [string] $ContainerEnvironmentName,
    [string] $IdentityName,
    [string] $WorkspaceName,
    [string] $VirtualNetworkName,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')][string] $FileShareName = 'source',
    [string] $UploadIpAddress = '',
    [ValidateRange(1, 102400)][int] $ShareQuotaGiB = 100,
    [ValidateRange(1, 1000000)][int] $MaxFiles = 100,
    [ValidateRange(1, 1800)][int] $BackendHealthTimeoutSeconds = 300,
    [ValidateRange(1, 60)][int] $BackendHealthPollSeconds = 5,
    [string] $AgentPrincipalsPath,
    [string] $ReaderPrincipalsPath,
    [ValidateNotNullOrEmpty()]
    [ValidateSet('approve-workspace', 'discover-formats', 'extract-metadata', 'classify-data',
        'account-failures', 'persist-inventory', 'review-inventory', 'exploration-supervisor')]
    [string[]] $ServiceNames = @('approve-workspace', 'discover-formats', 'extract-metadata',
        'classify-data', 'account-failures', 'persist-inventory', 'review-inventory'),
    [string] $AgentEndpointsPath,
    [ValidateRange(1, 7)][int] $SwarmConcurrency = 3,
    [guid] $SupervisorPrincipalId,
    [switch] $PermitInventoryWrites,
    [switch] $Deploy
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Invoke-Native {
    param([string] $Tool, [string[]] $Arguments, [switch] $Json)
    # Do not echo arguments or native error text: ARM parameters may contain secrets.
    $result = & $Tool @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$Tool failed (exit $LASTEXITCODE). Inspect sanitized Azure operation status; do not enable debug logging." }
    if ($Json) { return (($result | ForEach-Object { "$_" }) -join "`n" | ConvertFrom-Json -AsHashtable) }
}

function Wait-BackendReady {
    param([uri] $BackendUrl)
    if ($BackendUrl.Scheme -ne 'https' -or $BackendUrl.UserInfo -or
        $BackendUrl.Query -or $BackendUrl.Fragment -or $BackendUrl.AbsolutePath -ne '/') {
        throw 'Backend deployment must output an HTTPS origin without credentials, query, fragment, or path.'
    }
    $healthUri = [uri]::new($BackendUrl, '/healthz')
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($clock.Elapsed.TotalSeconds -lt $BackendHealthTimeoutSeconds) {
            $remaining = $BackendHealthTimeoutSeconds - $clock.Elapsed.TotalSeconds
            $requestTimeout = [int][Math]::Max(1, [Math]::Min(10, [Math]::Ceiling($remaining)))
            try {
                $response = Invoke-WebRequest -Uri $healthUri -Method Get -UseBasicParsing `
                    -TimeoutSec $requestTimeout -MaximumRedirection 0 -SkipHttpErrorCheck -ErrorAction Stop
                if ([int]$response.StatusCode -eq 200) {
                    Write-Host 'Backend HTTPS /healthz readiness verified.'
                    return
                }
            }
            catch {
                # Do not log exceptions, response bodies, or headers from public ingress.
            }
            $remaining = $BackendHealthTimeoutSeconds - $clock.Elapsed.TotalSeconds
            if ($remaining -gt 0) {
                Start-Sleep -Milliseconds ([int][Math]::Ceiling(1000 * [Math]::Min($BackendHealthPollSeconds, $remaining)))
            }
        }
    }
    finally { $clock.Stop() }
    throw "Backend HTTPS /healthz did not return 200 within $BackendHealthTimeoutSeconds seconds; no agents were deployed."
}

function Read-Principals {
    param([string] $Path, [switch] $Readers)
    if (-not $Path) {
        if ($Readers) { return ,@() }
        return @{}
    }
    $parsed = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable -NoEnumerate
    if ($Readers) {
        if ($parsed -isnot [array]) { throw 'ReaderPrincipalsPath must contain a JSON array.' }
        foreach ($id in $parsed) {
            $guid = [guid]::Empty
            if ($id -isnot [string] -or -not [guid]::TryParse($id, [ref]$guid) -or $guid -eq [guid]::Empty) {
                throw 'Each reader principal must be a nonempty object-ID GUID string.'
            }
        }
        return ,$parsed
    }
    if ($parsed -isnot [System.Collections.IDictionary]) { throw 'AgentPrincipalsPath must contain a JSON object.' }
    $roles = [System.Collections.Generic.HashSet[int]]::new()
    $ids = [System.Collections.Generic.HashSet[guid]]::new()
    foreach ($id in $parsed.Keys) {
        $guid = [guid]::Empty
        $role = $parsed[$id]
        if (-not [guid]::TryParse($id, [ref]$guid) -or $guid -eq [guid]::Empty -or -not $ids.Add($guid) -or
            $role -isnot [long] -or $role -lt 1 -or $role -gt 7 -or -not $roles.Add([int]$role)) {
            throw 'Agent mapping requires distinct object-ID GUID keys and distinct integer capability role IDs 1..7.'
        }
    }
    return $parsed
}

if ($SubscriptionId -eq [guid]::Empty -or $TenantId -eq [guid]::Empty -or $ApiAudience -eq [guid]::Empty) {
    throw 'Subscription, tenant, and API audience cannot be empty GUIDs.'
}
$projectPattern = '^/subscriptions/(?<sub>[0-9a-fA-F-]{36})/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/(?<account>[^/]+)/projects/(?<project>[^/]+)$'
if ($FoundryProjectId -notmatch $projectPattern) { throw 'FoundryProjectId must be an existing Microsoft.CognitiveServices/accounts/projects ARM resource ID.' }
$projectSubscription = $Matches.sub
$projectAccount = $Matches.account
$projectName = $Matches.project
if ($FoundryProjectEndpoint.Scheme -ne 'https' -or $FoundryProjectEndpoint.Query -or
    $FoundryProjectEndpoint.Fragment -or $FoundryProjectEndpoint.UserInfo -or
    $FoundryProjectEndpoint.AbsolutePath.TrimEnd('/') -cne "/api/projects/$projectName") {
    throw 'FoundryProjectEndpoint must be an HTTPS project endpoint matching the project ARM ID.'
}
if ($UploadIpAddress) {
    $ip = $null
    if (-not [ipaddress]::TryParse($UploadIpAddress, [ref]$ip) -or
        $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $UploadIpAddress -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -or
        $UploadIpAddress -match '^(0|10|127)\.|^169\.254\.|^192\.168\.|^172\.(1[6-9]|2\d|3[01])\.') {
        throw 'UploadIpAddress must be one public IPv4 address, not a CIDR or private address.'
    }
}
$agents = Read-Principals -Path $AgentPrincipalsPath
$readers = Read-Principals -Path $ReaderPrincipalsPath -Readers
foreach ($reader in $readers) {
    if (@($agents.Keys | Where-Object { [guid]$_ -eq [guid]$reader }).Count) {
        throw 'Reader principals must not have specialist tool permissions.'
    }
}
$services = @($ServiceNames | Select-Object -Unique)
if ($PermitInventoryWrites -and 'exploration-supervisor' -notin $services) {
    throw '-PermitInventoryWrites applies only to selected exploration-supervisor deployments.'
}
$endpointJson = $null
if ('exploration-supervisor' -in $services) {
    if (-not $AgentEndpointsPath) { throw 'Supervisor deployment requires AgentEndpointsPath with existing specialist endpoints.' }
    $endpoints = Get-Content -LiteralPath $AgentEndpointsPath -Raw | ConvertFrom-Json -AsHashtable -NoEnumerate
    $specialists = @('approve-workspace', 'discover-formats', 'extract-metadata', 'classify-data',
        'account-failures', 'persist-inventory', 'review-inventory')
    if ($endpoints -isnot [System.Collections.IDictionary] -or $endpoints.Count -ne 7) {
        throw 'AgentEndpointsPath must map exactly the seven specialist service names to HTTPS endpoint strings.'
    }
    foreach ($service in $specialists) {
        $uri = $null
        if (-not $endpoints.Contains($service) -or $endpoints[$service] -isnot [string] -or
            -not [uri]::TryCreate($endpoints[$service], [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
            throw 'AgentEndpointsPath must map exactly the seven specialist service names to HTTPS endpoint strings without credentials, query, or fragment.'
        }
    }
    if ($PSBoundParameters.ContainsKey('SupervisorPrincipalId') -and
        ($SupervisorPrincipalId -eq [guid]::Empty -or
        -not @($readers | Where-Object { [guid]$_ -eq $SupervisorPrincipalId }).Count -or
        @($agents.Keys | Where-Object { [guid]$_ -eq $SupervisorPrincipalId }).Count)) {
        throw 'SupervisorPrincipalId must be explicitly allowlisted in ReaderPrincipalsPath with no specialist tool permissions.'
    }
    $endpointJson = ConvertTo-Json -InputObject $endpoints -Compress
}
$repoRoot = Split-Path $PSScriptRoot -Parent
$deploymentRoot = Join-Path $repoRoot 'deployment'
$infraRoot = Join-Path $deploymentRoot 'infra'
$packageScript = Join-Path $PSScriptRoot 'package_foundry.ps1'
foreach ($file in @('deployment\azure.yaml', 'mcp_servers\host\Dockerfile',
    'deployment\infra\bootstrap.bicep', 'deployment\infra\app.bicep', 'scripts\package_foundry.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $file) -PathType Leaf)) { throw "Missing prerequisite $file." }
}
foreach ($tool in @('az', 'azd')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Missing prerequisite: $tool must already be installed and authenticated (no automatic installation or login)." }
}

$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $suffix = [BitConverter]::ToString($hasher.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes("$SubscriptionId/$ResourceGroupName/$NamePrefix"))).Replace('-', '').Substring(0, 8).ToLowerInvariant()
}
finally { $hasher.Dispose() }
if (-not $RegistryName) { $RegistryName = "${NamePrefix}${suffix}acr" }
if (-not $StorageAccountName) { $StorageAccountName = "${NamePrefix}${suffix}st" }
if (-not $PostgresServerName) { $PostgresServerName = "$NamePrefix-$suffix-pg" }
if (-not $ContainerAppName) { $ContainerAppName = "$NamePrefix-backend" }
if (-not $ContainerEnvironmentName) { $ContainerEnvironmentName = "$NamePrefix-env" }
if (-not $IdentityName) { $IdentityName = "$NamePrefix-pull" }
if (-not $WorkspaceName) { $WorkspaceName = "$NamePrefix-logs" }
if (-not $VirtualNetworkName) { $VirtualNetworkName = "$NamePrefix-vnet" }
if ($RegistryName -cnotmatch '^[a-zA-Z0-9]{5,50}$' -or $StorageAccountName -cnotmatch '^[a-z0-9]{3,24}$' -or
    $PostgresServerName -cnotmatch '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$' -or
    $ContainerAppName -cnotmatch '^[a-z][a-z0-9-]{0,30}[a-z0-9]$' -or $ContainerAppName.Contains('--')) {
    throw 'Invalid registry, storage, PostgreSQL, or Container App name.'
}
foreach ($name in @($ContainerEnvironmentName, $IdentityName, $WorkspaceName, $VirtualNetworkName)) {
    if ($name -cnotmatch '^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}[a-zA-Z0-9]$') {
        throw 'Environment, identity, workspace and virtual network names must be 2..64 alphanumeric/hyphen characters.'
    }
}
Write-Host "PLAN: subscription=$SubscriptionId tenant=$TenantId resourceGroup=$ResourceGroupName location=$Location"
Write-Host "Create/update: ACR=$RegistryName (Basic, admin disabled), Azure Files=$StorageAccountName/$FileShareName ($ShareQuotaGiB GiB, read-only mount)"
Write-Host "Create/update: PostgreSQL=$PostgresServerName (private, B1ms/32GiB), VNet=$VirtualNetworkName, logs=$WorkspaceName, UAMI=$IdentityName"
Write-Host "Create/update: ACA environment=$ContainerEnvironmentName app=$ContainerAppName (external HTTPS, one replica/worker)"
Write-Host "Build: repository root via mcp_servers\host\Dockerfile; no source-data upload."
Write-Host "Readiness gate: HTTPS GET /healthz must return 200 within $BackendHealthTimeoutSeconds seconds before deploying agents."
Write-Host "Adopt existing project=$FoundryProjectId endpoint=$FoundryProjectEndpoint region=$FoundryLocation model=$ModelDeploymentName"
Write-Host "Deploy independently: $($services -join ', '). Agent authorization entries=$($agents.Count); read-only reader entries=$($readers.Count)."
Write-Host 'COST: selected hosted services, always-on ACA, PostgreSQL, ACR, Azure Files, networking and log ingestion are billable; no model is created or invoked.'
Write-Host "Upload IP exception: $(if ($UploadIpAddress) { $UploadIpAddress } else { 'none (manual upload requires an approved network path)' })."
if (-not $Deploy) { Write-Host 'Plan only. No Azure calls or filesystem changes performed. Use -Deploy to execute; -WhatIf still prevents Azure calls.'; return }
if (-not $PostgresPassword -or $PostgresPassword.Length -lt 8 -or $PostgresPassword.Length -gt 128) {
    throw '-Deploy requires -PostgresPassword (SecureString, 8..128 characters).'
}
if (-not $IsWindows) { throw 'Secure deployment parameter ACL handling requires Windows PowerShell 7 on Windows.' }
if (-not $PSCmdlet.ShouldProcess("$SubscriptionId/$ResourceGroupName and $FoundryProjectId", 'Create billable backend infrastructure, build image, deploy selected hosted services')) { return }

foreach ($service in $services) {
    $relative = if ($service -eq 'exploration-supervisor') { 'swarms\exploration' } else { "agents\$service" }
    foreach ($file in @('main.py', 'requirements.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "$relative\$file") -PathType Leaf)) {
            throw "Missing selected service prerequisite: $relative\$file"
        }
    }
}
& $packageScript -ServiceNames $services -Confirm:$false
try {
# Fail before any cloud mutation if the installed CLI lacks required command groups
# or the already-installed Bicep compiler cannot compile the templates.
Invoke-Native azd @('ai', 'project', 'show', '--help')
Invoke-Native az @('bicep', 'version', '--only-show-errors')
foreach ($template in @('bootstrap.bicep', 'app.bicep')) {
    Invoke-Native az @('bicep', 'build', '--file', (Join-Path $infraRoot $template), '--stdout', '--only-show-errors')
}

# Every cloud operation below targets an explicit subscription/project.
foreach ($sub in @("$SubscriptionId", $projectSubscription) | Select-Object -Unique) {
    $account = Invoke-Native az @('account', 'show', '--subscription', $sub, '--output', 'json', '--only-show-errors') -Json
    if ($account.tenantId -ne "$TenantId") { throw "Subscription $sub is not in the supplied tenant; no deployment performed." }
}
$project = Invoke-Native az @('resource', 'show', '--ids', $FoundryProjectId, '--subscription', $projectSubscription, '--output', 'json', '--only-show-errors') -Json
if ($project.location -ne $FoundryLocation) { throw 'FoundryLocation does not match the existing project.' }
$foundryAccountId = $FoundryProjectId.Substring(0, $FoundryProjectId.LastIndexOf('/projects/'))
$foundryAccount = Invoke-Native az @('resource', 'show', '--ids', $foundryAccountId, '--subscription', $projectSubscription, '--output', 'json', '--only-show-errors') -Json
$endpointHost = $FoundryProjectEndpoint.DnsSafeHost
$accountEndpoints = $foundryAccount.properties.endpoints.Values | ForEach-Object { ([uri]$_).DnsSafeHost }
if ($endpointHost -notin $accountEndpoints) { throw 'Project endpoint hostname is not an endpoint of the supplied Foundry account.' }
Invoke-Native az @('cognitiveservices', 'account', 'deployment', 'show', '--name', $projectAccount, '--resource-group', ($FoundryProjectId -split '/')[4], '--subscription', $projectSubscription, '--deployment-name', $ModelDeploymentName, '--output', 'none', '--only-show-errors')

$privateDir = Join-Path $infraRoot ('.deployment-private-' + [guid]::NewGuid().ToString('N'))
$parameterFile = Join-Path $privateDir 'parameters.json'
$plainPassword = $null
$databaseUrl = $null
try {
    $null = New-Item -ItemType Directory -Path $privateDir
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($sid)
    $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $privateDir -AclObject $acl
    $plainPassword = [System.Net.NetworkCredential]::new('', $PostgresPassword).Password
    $characterClasses = @('[A-Z]', '[a-z]', '[0-9]', '[^A-Za-z0-9]') |
        Where-Object { $plainPassword -cmatch $_ }
    if (@($characterClasses).Count -lt 3 -or $plainPassword.IndexOf($PostgresAdministrator, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw 'PostgreSQL password must contain at least three character classes and must not contain the administrator name.'
    }
    $parameters = @{
        location = $Location; registryName = $RegistryName; storageAccountName = $StorageAccountName
        fileShareName = $FileShareName; postgresServerName = $PostgresServerName
        databaseName = $DatabaseName; postgresAdministrator = $PostgresAdministrator
        postgresPassword = $plainPassword; environmentName = $ContainerEnvironmentName
        identityName = $IdentityName; workspaceName = $WorkspaceName; virtualNetworkName = $VirtualNetworkName
        uploadIpAddress = $UploadIpAddress; shareQuotaGiB = $ShareQuotaGiB
    }
    $armParameters = @{}
    foreach ($key in $parameters.Keys) { $armParameters[$key] = @{ value = $parameters[$key] } }
    @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'; parameters = $armParameters } |
        ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $parameterFile -Encoding utf8NoBOM
    Invoke-Native az @('group', 'create', '--subscription', "$SubscriptionId", '--name', $ResourceGroupName, '--location', $Location, '--output', 'none', '--only-show-errors')
    Invoke-Native az @('deployment', 'group', 'create', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName, '--name', "$NamePrefix-bootstrap", '--template-file', (Join-Path $infraRoot 'bootstrap.bicep'), '--parameters', "@$parameterFile", '--output', 'none', '--only-show-errors')
    $outputs = Invoke-Native az @('deployment', 'group', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName, '--name', "$NamePrefix-bootstrap", '--query', 'properties.outputs', '--output', 'json', '--only-show-errors') -Json
    $tag = 'backend:' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    Invoke-Native az @('acr', 'build', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName, '--registry', $RegistryName, '--image', $tag, '--file', 'mcp_servers\host\Dockerfile', '--no-logs', '--output', 'none', '--only-show-errors', $repoRoot)
    $image = "$($outputs.registryLoginServer.value)/$tag"
    $databaseUrl = 'postgresql+psycopg://{0}:{1}@{2}:5432/{3}?sslmode=require' -f
        [uri]::EscapeDataString($PostgresAdministrator), [uri]::EscapeDataString($plainPassword),
        $outputs.postgresHost.value, [uri]::EscapeDataString($DatabaseName)
    $parameters = @{
        location = $Location; appName = $ContainerAppName; environmentName = $ContainerEnvironmentName
        identityName = $IdentityName; registryName = $RegistryName; image = $image; databaseUrl = $databaseUrl
        tenantId = "$TenantId"; apiAudience = "$ApiAudience"; agentPrincipals = $agents
        readerPrincipals = $readers; maxFiles = $MaxFiles
    }
    $armParameters = @{}
    foreach ($key in $parameters.Keys) { $armParameters[$key] = @{ value = $parameters[$key] } }
    @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'; parameters = $armParameters } |
        ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $parameterFile -Encoding utf8NoBOM
    Invoke-Native az @('deployment', 'group', 'create', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName, '--name', "$NamePrefix-app", '--template-file', (Join-Path $infraRoot 'app.bicep'), '--parameters', "@$parameterFile", '--output', 'none', '--only-show-errors')
    $appOutputs = Invoke-Native az @('deployment', 'group', 'show', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName, '--name', "$NamePrefix-app", '--query', 'properties.outputs', '--output', 'json', '--only-show-errors') -Json
}
finally {
    $plainPassword = $null
    $databaseUrl = $null
    $parameters = $null
    $armParameters = $null
    if (Test-Path -LiteralPath $parameterFile) { Remove-Item -LiteralPath $parameterFile -Force }
    if (Test-Path -LiteralPath $privateDir) { Remove-Item -LiteralPath $privateDir -Force }
}

Wait-BackendReady -BackendUrl ([uri]$appOutputs.backendUrl.value)

Push-Location $deploymentRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $deploymentRoot ".azure\$EnvironmentName\.env"))) {
        Invoke-Native azd @('env', 'new', $EnvironmentName, '--subscription', $projectSubscription, '--location', $FoundryLocation, '--no-prompt')
    }
    $envValues = [ordered]@{
        AZURE_SUBSCRIPTION_ID = $projectSubscription
        AZURE_TENANT_ID = "$TenantId"
        AZURE_LOCATION = $FoundryLocation
        AZURE_AI_PROJECT_ID = $FoundryProjectId
        AZURE_AI_PROJECT_ENDPOINT = $FoundryProjectEndpoint.AbsoluteUri.TrimEnd('/')
        AZURE_AI_MODEL_DEPLOYMENT_NAME = $ModelDeploymentName
        OSDU_BACKEND_URL = $appOutputs.backendUrl.value
        OSDU_BACKEND_SCOPE = $BackendScope
    }
    if ($endpointJson) {
        $envValues.OSDU_AGENT_ENDPOINTS_JSON = $endpointJson
        $envValues.OSDU_SWARM_CONCURRENCY = "$SwarmConcurrency"
        $envValues.OSDU_ALLOW_INVENTORY_WRITES = if ($PermitInventoryWrites) { 'true' } else { 'false' }
    }
    foreach ($entry in $envValues.GetEnumerator()) {
        Invoke-Native azd @('env', 'set', $entry.Key, $entry.Value, '-e', $EnvironmentName, '--no-prompt')
    }
    foreach ($service in $services) {
        Write-Host "Deploying hosted Foundry service: $service"
        Invoke-Native azd @('deploy', $service, '-e', $EnvironmentName, '--no-prompt')
    }
}
finally { Pop-Location }
}
finally { & $packageScript -ServiceNames $services -Clean -Confirm:$false }

Write-Host "Backend URL: $($appOutputs.backendUrl.value)"
Write-Host "Backend resource: $($appOutputs.backendResourceId.value)"
Write-Host "Source share: $StorageAccountName/$FileShareName; place files below Data manually."
Write-Host "Foundry environment: $EnvironmentName; selected service-specific names, versions and endpoints are recorded by azd in deployment\.azure\$EnvironmentName."
Write-Host 'Next: obtain and verify each agent service principal object ID, then run configure_foundry_access.ps1. Do NOT substitute the project identity.'
Write-Host 'No model invocation or source upload was performed. GET /runs requires an explicitly allowlisted reader identity.'
