param([int]$Port = 18080)
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '请先在项目目录建立 .venv 并安装 requirements.txt。'
}
if ($Port -lt 1024 -or $Port -gt 65535) { throw '请使用1024至65535的端口。' }
# 不依赖可能无权调用的系统网络查询；独占绑定探测后立即释放。
$taskProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$taskProbe.Server.ExclusiveAddressUse = $true
try {
    $taskProbe.Start()
} catch {
    throw "端口 $Port 不可用或已被占用，请先查看是否小满已经运行，不要重复启动或终止其他项目。"
} finally {
    $taskProbe.Stop()
}
# 只清除此启动进程继承的代理；不修改 Windows 全局代理。
foreach ($taskProxy in @('ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy')) {
    Remove-Item -LiteralPath ('Env:' + $taskProxy) -ErrorAction SilentlyContinue
}
Push-Location $taskProject
try {
    Write-Host "小满本地地址：http://127.0.0.1:$Port （Ctrl+C停止；不监听公网）"
    & $taskPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1
} finally {
    Pop-Location
}
