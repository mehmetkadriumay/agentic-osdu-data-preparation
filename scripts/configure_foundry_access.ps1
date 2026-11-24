#requires -Version 7.0
<#
.SYNOPSIS
Updates ONLY the backend agent principal-stage allowlist after Foundry deployment.
.DESCRIPTION
Default is an offline plan. -Deploy and ShouldProcess approval are required to
update the single OSDU_AGENT_PRINCIPALS_JSON environment variable. Existing image,
database secret, reader principals, volumes and other settings are preserved.

AgentPrincipalsPath contains a JSON object with seven operator-verified service
principal OBJECT IDs mapped to distinct integer stages 1..7. Never use client IDs
or the Foundry project identity. {} is allowed only with -AllowDenyAll, to revoke
all agent access. No identities are guessed, queried from undocumented fields,
or assigned project-wide roles.
.EXAMPLE
.\scripts\configure_foundry_access.ps1 -SubscriptionId '<guid>' -TenantId '<guid>' `
  -ResourceGroupName 'rg-osdu' -ContainerAppName 'osdu-backend' `
  -AgentPrincipalsPath '.\verified-agent-principals.json'
Add -Deploy to apply after reviewing the plan.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)][guid] $SubscriptionId,
    [Parameter(Mandatory)][guid] $TenantId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $ResourceGroupName,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $ContainerAppName,
    [Parameter(Mandatory)][string] $AgentPrincipalsPath,
    [switch] $AllowDenyAll,
    [switch] $Deploy
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if ($SubscriptionId -eq [guid]::Empty -or $TenantId -eq [guid]::Empty) { throw 'Subscription and tenant must be nonempty GUIDs.' }
$mapping = Get-Content -LiteralPath $AgentPrincipalsPath -Raw | ConvertFrom-Json -AsHashtable -NoEnumerate
if ($mapping -isnot [System.Collections.IDictionary]) { throw 'AgentPrincipalsPath must contain a JSON object.' }
$stages = [System.Collections.Generic.HashSet[int]]::new()
$ids = [System.Collections.Generic.HashSet[guid]]::new()
foreach ($id in $mapping.Keys) {
    $guid = [guid]::Empty
    $stage = $mapping[$id]
    if (-not [guid]::TryParse($id, [ref]$guid) -or $guid -eq [guid]::Empty -or -not $ids.Add($guid) -or
        $stage -isnot [long] -or $stage -lt 1 -or $stage -gt 7 -or -not $stages.Add([int]$stage)) {
        throw 'Mapping requires seven distinct object-ID GUID keys and distinct integer stages 1..7.'
    }
}
if ($mapping.Count -ne 7 -and -not ($mapping.Count -eq 0 -and $AllowDenyAll)) {
    throw 'Provide all seven verified identities, or {} with -AllowDenyAll to revoke all agent access.'
}
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI must already be installed and authenticated.' }
$json = ConvertTo-Json -InputObject $mapping -Compress
Write-Host "PLAN: subscription=$SubscriptionId tenant=$TenantId resourceGroup=$ResourceGroupName app=$ContainerAppName"
Write-Host "Update ONLY OSDU_AGENT_PRINCIPALS_JSON ($($mapping.Count) verified principal-stage entries). Existing reader access is unchanged."
if (-not $Deploy) { Write-Host 'Plan only. No Azure calls or filesystem changes performed.'; return }
if (-not $PSCmdlet.ShouldProcess("$SubscriptionId/$ResourceGroupName/$ContainerAppName", 'Update backend agent authorization allowlist')) { return }
$accountText = & az account show --subscription "$SubscriptionId" --output json --only-show-errors 2>&1
if ($LASTEXITCODE -ne 0) { throw "az account show failed (exit $LASTEXITCODE)." }
$account = ($accountText -join "`n") | ConvertFrom-Json
if ($account.tenantId -ne "$TenantId") { throw 'Subscription tenant does not match TenantId; no update performed.' }
# Azure CLI's Windows .cmd shim uses legacy native quoting, which otherwise strips
# JSON quotes. Standard argument passing (or a real executable) retains them.
$argumentPassing = Get-Variable PSNativeCommandArgumentPassing -ValueOnly -ErrorAction SilentlyContinue
if ($IsWindows -and ($argumentPassing -eq 'Legacy' -or
    ((Get-Command az).Source -match '\.(cmd|bat)$' -and $argumentPassing -ne 'Standard') -or -not $argumentPassing)) {
    $json = $json.Replace('"', '\"')
}
# --set-env-vars merges this entry; --replace-env-vars would remove other settings.
$null = & az containerapp update --subscription "$SubscriptionId" --resource-group $ResourceGroupName --name $ContainerAppName `
    --set-env-vars "OSDU_AGENT_PRINCIPALS_JSON=$json" --output none --only-show-errors 2>&1
if ($LASTEXITCODE -ne 0) { throw "az containerapp update failed (exit $LASTEXITCODE)." }
Write-Host "Updated agent allowlist for $ContainerAppName. No model invocation performed."
