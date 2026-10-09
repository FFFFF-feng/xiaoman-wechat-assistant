param([int]$Port = 18082)
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskProject '.venv\Scripts\python.exe'
$taskConfig = Join-Path $taskProject '.env.official.local'
if (-not (Test-Path -LiteralPath $taskConfig)) { throw '请先建立 .env.official.local 并填写正式号配置。' }
if ($Port -lt 1024 -or $Port -gt 65535) { throw '端口无效。' }
$taskProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$taskProbe.Server.ExclusiveAddressUse = $true
try { $taskProbe.Start() } catch { throw "端口 $Port 不可用，请先检查是否已启动正式号联调实例。" } finally { $taskProbe.Stop() }
$env:XIAOMAN_ENV_FILE = $taskConfig
$env:PYTHONIOENCODING = 'utf-8'
foreach ($taskProxy in @('ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy')) {
    Remove-Item -LiteralPath ('Env:' + $taskProxy) -ErrorAction SilentlyContinue
}
Push-Location $taskProject
try {
    & $taskPython -m app.wechat_preflight
    if ($LASTEXITCODE -ne 0) { throw '正式号配置检查未通过，先补全本地配置；没有启动正式号服务。' }
    Write-Host "正式号联调服务：http://127.0.0.1:$Port；临时公网入口只连接白名单网关，不连接此端口。Ctrl+C停止。"
    & $taskPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1 --no-access-log
} finally { Pop-Location }
