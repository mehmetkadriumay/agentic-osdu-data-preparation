param(
    [Parameter(Mandatory = $true)]
    [string]$CurrentWheel,
    [string]$PreviousRef = "51702e705968dc63b3b2bc160ed66ee182bf9bfc",
    [switch]$OfflineReuseInstalledDependencies
)

$ErrorActionPreference = "Stop"
$root = Join-Path ([System.IO.Path]::GetTempPath()) "agentic-osdu-package-rollback-$([guid]::NewGuid())"
$previousRoot = Join-Path $root "previous"
$previousDist = Join-Path $root "previous-dist"
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$cli = Join-Path $venv "Scripts\agentic-osdu.exe"
$worktreeAdded = $false
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$process = $null
try {
    New-Item -ItemType Directory -Path $root | Out-Null
    git worktree add --detach $previousRoot $PreviousRef
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the prior-release worktree" }
    $worktreeAdded = $true
    if ($OfflineReuseInstalledDependencies) {
        uv build --project $previousRoot --out-dir $previousDist --no-build-isolation
    } else {
        uv build --project $previousRoot --out-dir $previousDist
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to build the prior package" }
    $previousWheel = (Get-ChildItem (Join-Path $previousDist "*.whl")).FullName
    if ($OfflineReuseInstalledDependencies) {
        uv venv --python 3.12 $venv
    } else {
        uv venv --python 3.12 $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the rollback environment" }
    if ($OfflineReuseInstalledDependencies) {
        uv pip install --python $python --no-deps $CurrentWheel
        if ($LASTEXITCODE -ne 0) { throw "Unable to install the current package" }
        uv pip install --python $python --reinstall --no-deps $previousWheel
    } else {
        uv pip install --python $python $CurrentWheel
        if ($LASTEXITCODE -ne 0) { throw "Unable to install the current package" }
        uv pip install --python $python --reinstall $previousWheel
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to reinstall the prior package" }
    if ($OfflineReuseInstalledDependencies) {
        $sourceSite = (
            Resolve-Path (Join-Path $PSScriptRoot "..\.venv\Lib\site-packages")
        ).Path
        $targetSite = Join-Path $venv "Lib\site-packages"
        Get-ChildItem -LiteralPath $sourceSite |
            Where-Object { $_.Name -notmatch "agentic[_-]osdu" } |
            Copy-Item -Destination $targetSite -Recurse -Force
    }
    $installed = & $python -c "from importlib.metadata import version; print(version('agentic-osdu-data-preparation'))"
    if ($installed.Trim() -ne "0.1.0") { throw "Package rollback did not restore version 0.1.0" }
    Write-Host "agentic-osdu --help"
    & $cli --help
    if ($LASTEXITCODE -notin 0, 2) { throw "The rolled-back CLI help probe failed" }
    $process = Start-Process -FilePath $cli -ArgumentList @(
        "web-serve", "--host", "127.0.0.1", "--port", "$port"
    ) -PassThru -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            if ($process.HasExited) { throw "Rolled-back loopback UI exited before readiness" }
        }
    }
    if (-not $ready) { throw "Rolled-back loopback UI did not become ready" }
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit()
    }
    if ($worktreeAdded) {
        git worktree remove --force $previousRoot
    }
    if (Test-Path $root) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
