# 按发布版本回滚：切换 current-version 到已安装的 releases/<版本>，由守护进程在空闲时完成切换。
# 数据、配置、设备身份与历史记录都在安装目录之外，回滚不会改动它们。
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$')]
    [string]$Version,
    [int]$TimeoutSeconds = 180
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$homePath = if ($env:MENU_CAPTURE_HOME) { $env:MENU_CAPTURE_HOME } else { Join-Path $env:LOCALAPPDATA 'MenuCapture' }
$logPath = Join-Path $homePath 'logs'
$null = New-Item -ItemType Directory -Path $logPath -Force
$rollbackLog = Join-Path $logPath 'rollback.log'

function Write-RollbackLog {
    param([string]$Message)
    Add-Content -LiteralPath $rollbackLog -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

$release = Join-Path $PSScriptRoot "releases\$Version"
$required = @(
    (Join-Path $release 'python\python.exe'),
    (Join-Path $release 'app\menu_capture\server.py')
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "版本 $Version 未完整安装，缺少: $path"
    }
}

$manifestPath = Join-Path $release 'release-manifest.json'
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Write-Host "目标版本 $Version，源码提交 $($manifest.git_commit)，前端入口 $($manifest.frontend_entry)。"
}

Write-RollbackLog "请求回滚到 $Version。"
# 复用安装器的版本登记逻辑：写入 current-version 并确保守护进程已注册；
# 守护进程检测到版本变化后，会在当前采集或标注结束后切换到目标版本。
& (Join-Path $PSScriptRoot 'Launch.ps1') -Action Register -Version $Version
Write-RollbackLog "已写入回滚版本 $Version，等待守护进程切换。"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$pendingNotified = $false
while ((Get-Date) -lt $deadline) {
    try {
        # 迁移前版本没有 /api/version 或 React manifest，通过进程路径确认具体回滚版本。
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/healthz' -TimeoutSec 2 -UseBasicParsing
        $python = Join-Path $release 'python\python.exe'
        $worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.Equals($python, [StringComparison]::OrdinalIgnoreCase) }
        if ($health.ok -and $health.role -eq 'worker' -and $worker) {
            Write-RollbackLog "回滚完成，运行版本 $Version。"
            Write-Host "已回滚到 $Version，目标版本采集服务已通过健康检查。"
            exit 0
        }
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 2 -UseBasicParsing
        if (-not $pendingNotified -and $status.running) {
            Write-Host '当前有采集任务在执行，守护进程会在任务结束后切换，请稍候。'
            $pendingNotified = $true
        }
    } catch {
        # 切换过程中端口短暂不可用属于预期，继续等待。
    }
    Start-Sleep -Seconds 3
}

$message = "回滚版本已登记为 $Version，但服务在 $TimeoutSeconds 秒内未完成切换（可能仍有采集或标注在执行）。"
Write-RollbackLog $message
Write-Warning $message
exit 1
