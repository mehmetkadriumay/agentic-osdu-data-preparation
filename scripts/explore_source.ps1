param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$WorkspaceRoot,
    [switch]$PrintPrompt
)

$ErrorActionPreference = "Stop"

if ($WorkspaceRoot -match "[\r\n]") {
    throw "WorkspaceRoot must be a single filesystem path."
}
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw "WorkspaceRoot must be an existing directory."
}
$workspace = Resolve-Path -LiteralPath $WorkspaceRoot
if ($workspace.Provider.Name -ne "FileSystem") {
    throw "WorkspaceRoot must be a filesystem directory."
}
$source = Join-Path $workspace.ProviderPath "Data"
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "The workspace must contain an existing Data directory."
}

$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).ProviderPath
$readme = Get-Content -LiteralPath (Join-Path $repository "README.md") -Raw -Encoding UTF8
$pattern = '(?ms)^```text\r?\n(?<prompt>Use the agentic-osdu-data-preparation MCP server to explore and extract\r?\nmetadata from C:\\OSDU\\NewSurvey\\Data\..*?)\r?\n```'
$match = [regex]::Match($readme, $pattern)
if (-not $match.Success) {
    throw "The README exploration prompt was not found."
}
$prompt = $match.Groups["prompt"].Value.Replace(
    'C:\OSDU\NewSurvey\Data', $source
).Replace(
    'C:\OSDU\NewSurvey', $workspace.ProviderPath
)

if ($PrintPrompt) {
    Write-Output $prompt
    return
}

Push-Location -LiteralPath $repository
try {
    & uv run --frozen copilot --interactive $prompt
    if ($LASTEXITCODE -ne 0) {
        throw "Copilot exploration exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
