#requires -Version 7.0
<#
.SYNOPSIS
Explicitly packages selected hosted services offline, or cleans owned packages.
.DESCRIPTION
Copies agents\shared\osdu_agent_host into each selected service root, excluding
Python caches. Existing package directories are never overwritten. -Clean removes
only selected osdu_agent_host directories carrying this script's ownership marker.
No dependency installation, source upload, or Azure calls are performed.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()]
    [ValidateSet('approve-workspace', 'discover-formats', 'extract-metadata', 'classify-data',
        'account-failures', 'persist-inventory', 'review-inventory', 'exploration-supervisor')]
    [string[]] $ServiceNames = @('approve-workspace', 'discover-formats', 'extract-metadata',
        'classify-data', 'account-failures', 'persist-inventory', 'review-inventory'),
    [switch] $Clean
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$source = Join-Path $repoRoot 'agents\shared\osdu_agent_host'
$markerName = '.package-foundry-owned'
$markerValue = 'package_foundry.ps1:osdu_agent_host:v1'
$targets = @(
    foreach ($service in $ServiceNames | Select-Object -Unique) {
        $relative = if ($service -eq 'exploration-supervisor') { 'swarms\exploration' } else { "agents\$service" }
        $root = Join-Path $repoRoot $relative
        [pscustomobject]@{ Root = $root; Package = Join-Path $root 'osdu_agent_host' }
    }
)
function Test-OwnedPackage([string] $Path) {
    $marker = Join-Path $Path $markerName
    return ((Test-Path -LiteralPath $marker -PathType Leaf) -and
        (Get-Content -LiteralPath $marker -Raw).Trim() -ceq $markerValue)
}
function Assert-NoLinks([string] $Path) {
    foreach ($item in @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing package path containing a filesystem link: $($item.FullName)"
        }
    }
}
foreach ($target in $targets) {
    $ancestor = $target.Root
    while ($ancestor -and $ancestor -ne $repoRoot) {
        if ((Test-Path -LiteralPath $ancestor) -and
            ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing linked service ancestor: $ancestor"
        }
        $ancestor = Split-Path $ancestor -Parent
    }
    if (Test-Path -LiteralPath $target.Root) {
        if ((Get-Item -LiteralPath $target.Root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing linked service root: $($target.Root)"
        }
    }
    if (Test-Path -LiteralPath $target.Package) {
        if (-not $Clean) { throw "Package directory already exists; refusing overwrite: $($target.Package)" }
        Assert-NoLinks $target.Package
        if (-not (Test-OwnedPackage $target.Package)) { throw "Refusing cleanup without ownership marker: $($target.Package)" }
    }
    elseif (-not $Clean -and -not (Test-Path -LiteralPath $target.Root -PathType Container)) {
        throw "Missing service root: $($target.Root)"
    }
}
if ($Clean) {
    foreach ($target in $targets) {
        if ((Test-Path -LiteralPath $target.Package) -and $PSCmdlet.ShouldProcess($target.Package, 'Remove generated owned Python package')) {
            Remove-Item -LiteralPath $target.Package -Recurse -Force
        }
    }
    return
}
if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "Missing shared helper: $source" }
Assert-NoLinks $source
if (Test-Path -LiteralPath (Join-Path $source $markerName)) { throw 'Shared source must not contain a generated ownership marker.' }
$created = [System.Collections.Generic.List[string]]::new()
try {
    foreach ($target in $targets) {
        if (-not $PSCmdlet.ShouldProcess($target.Package, 'Copy shared Python helper for remote build')) { continue }
        $null = New-Item -ItemType Directory -Path $target.Package
        $created.Add($target.Package)
        Set-Content -LiteralPath (Join-Path $target.Package $markerName) -Value $markerValue -Encoding utf8NoBOM
        foreach ($item in Get-ChildItem -LiteralPath $source -Recurse -Force) {
            $relative = [IO.Path]::GetRelativePath($source, $item.FullName)
            if ($relative -match '(^|[\\/])__pycache__([\\/]|$)' -or $item.Extension -in @('.pyc', '.pyo')) { continue }
            $destination = Join-Path $target.Package $relative
            if ($item.PSIsContainer) { $null = New-Item -ItemType Directory -Path $destination -Force }
            else {
                $null = New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force
                Copy-Item -LiteralPath $item.FullName -Destination $destination
            }
        }
    }
}
catch {
    foreach ($path in $created) {
        if (Test-OwnedPackage $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    throw
}
