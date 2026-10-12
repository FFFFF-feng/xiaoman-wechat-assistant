[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$LocalRoot, [switch]$Stop)
$ErrorActionPreference = 'Stop'
$localPath = (Resolve-Path -LiteralPath $LocalRoot).Path
$nodePath = Join-Path $localPath 'runtime\node-v24.21.0-win-x64\node.exe'
$projectPath = Split-Path -Parent $PSScriptRoot
$workerPath = Join-Path $projectPath 'integrations\openclaw-xiaoman\enrollment-worker.mjs'
$privatePath = Join-Path $localPath 'state\xiaoman'
$pidPath = Join-Path $privatePath 'enrollment-process.json'
if (!(Test-Path -LiteralPath (Join-Path $privatePath '.xiaoman-bridge'))) { throw 'Owned bridge runtime required.' }
if (!(Test-Path -LiteralPath $nodePath)) { throw 'Isolated Node installation missing.' }
$workerProcess = $null
if (Test-Path -LiteralPath $pidPath) {
    $record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $workerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
    if ($workerProcess -and ($workerProcess.ExecutablePath -ne $nodePath -or !$workerProcess.CommandLine.Contains($workerPath))) {
        throw 'Enrollment worker ownership changed. Nothing stopped or replaced.'
    }
}
if ($Stop) {
    if ($workerProcess) {
        # Target identity is verified above. Only this worker's native-login descendants are stopped.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($workerProcess.ProcessId)"
        if (!$current -or $current.CreationDate -ne $workerProcess.CreationDate -or $current.ExecutablePath -ne $nodePath) {
            throw 'Enrollment worker identity changed. Nothing stopped.'
        }
        $result = & taskkill.exe /PID $current.ProcessId /T /F 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop verified enrollment worker.' }
        Write-Host 'Stopped only the verified enrollment worker and its login children. Existing chat/data preserved.'
    }
    return
}
if ($workerProcess) { Write-Host 'Enrollment worker already running; no duplicate started.'; return }
# Recover a worker that wrote no PID file yet, or detect an ambiguous launcher race.
$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" | Where-Object {
    $_.ExecutablePath -eq $nodePath -and $_.CommandLine -and $_.CommandLine.Contains($workerPath)
})
if ($existing.Count -gt 0) {
    if ($existing.Count -ne 1) { throw 'Multiple enrollment workers exist; do not start another.' }
    Write-Host 'Enrollment worker is starting; no duplicate started.'
    return
}
$savedRoot = $env:XIAOMAN_LOCAL_ROOT
try {
    $env:XIAOMAN_LOCAL_ROOT = $localPath
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $process = Start-Process -FilePath $nodePath -ArgumentList @('"' + $workerPath + '"') -WorkingDirectory $localPath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $privatePath "enrollment-$stamp.out.log") -RedirectStandardError (Join-Path $privatePath "enrollment-$stamp.err.log")
    for ($attempt=0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) { throw 'Enrollment worker stopped. Inspect its private logs.' }
        if (Test-Path -LiteralPath $pidPath) {
            $record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
            if ($record.pid -eq $process.Id) { Write-Host 'Invitation enrollment worker started. Public API cannot choose user identity.'; return }
        }
    }
    throw 'Enrollment worker did not become ready.'
} finally { $env:XIAOMAN_LOCAL_ROOT = $savedRoot }
