param(
    [Parameter(Mandatory = $true)]
    [string]$Wheel,
    [switch]$OfflineReuseInstalledDependencies
)

$ErrorActionPreference = "Stop"
$root = Join-Path ([System.IO.Path]::GetTempPath()) "agentic-osdu-clean-$([guid]::NewGuid())"
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$cli = Join-Path $venv "Scripts\agentic-osdu.exe"
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$process = $null
try {
    if ($OfflineReuseInstalledDependencies) {
        uv venv --python 3.12 $venv
    } else {
        uv venv --python 3.12 $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the clean verification environment" }
    if ($OfflineReuseInstalledDependencies) {
        uv pip install --python $python --no-deps $Wheel
    } else {
        uv pip install --python $python $Wheel
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the release wheel" }
    if ($OfflineReuseInstalledDependencies) {
        $sourceSite = (
            Resolve-Path (Join-Path $PSScriptRoot "..\.venv\Lib\site-packages")
        ).Path
        $targetSite = Join-Path $venv "Lib\site-packages"
        Get-ChildItem -LiteralPath $sourceSite |
            Where-Object { $_.Name -notmatch "agentic[_-]osdu" } |
            Copy-Item -Destination $targetSite -Recurse -Force
    }
    Write-Host "agentic-osdu --help"
    & $cli --help
    if ($LASTEXITCODE -ne 0) { throw "The installed CLI help probe failed" }
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
            if ($process.HasExited) { throw "Loopback UI exited before becoming ready" }
        }
    }
    if (-not $ready) { throw "Loopback UI did not become ready" }
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit()
    }
    if (Test-Path $root) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
