# Velopack 隔离验收：安装、启动、版本/健康核验、数据保留与卸载。
# 不验证增量（增量由 vpk 生成，需两个正式包，另行在专用 Windows 环境执行）。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Setup,
    [Parameter(Mandatory)][string]$Version,
    [int]$Port = 8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path $Setup).Path
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$existingStartup = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
if ((Test-Path -LiteralPath $existingStartup) -or
    (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    throw '已有登录启动项或端口监听。请在独立 Windows 账号或临时运行环境执行安装验收。'
}

$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $tempRoot ('mcv-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$localData = Join-Path $testRoot 'LocalAppData'
$homePath = Join-Path $localData 'MenuCapture'
$installRoot = Join-Path $localData 'Yipu.MenuCapture'
$originalLocalData = $env:LOCALAPPDATA
$originalHome = $env:MENU_CAPTURE_HOME
$originalPort = $env:MENU_CAPTURE_PORT
$null = New-Item -ItemType Directory -Path (Join-Path $homePath 'data') -Force
$marker = Join-Path $homePath 'data\offline-sentinel'
Set-Content -LiteralPath $marker -Value 'preserve'
$env:LOCALAPPDATA = $localData
$env:MENU_CAPTURE_HOME = $homePath
$env:MENU_CAPTURE_PORT = "$Port"
$seedHashes = @{}
foreach ($file in Get-ChildItem -LiteralPath $homePath -Recurse -File -Force) {
    $seedHashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
}

function Invoke-Health {
    param([string]$Expected)
    $url = "http://127.0.0.1:$Port/healthz"
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $health = Invoke-RestMethod $url -UseBasicParsing -TimeoutSec 2
            if ($health.ok -and $health.role -eq 'worker') {
                $info = Invoke-RestMethod "http://127.0.0.1:$Port/api/version" -UseBasicParsing -TimeoutSec 2
                if ($info.version -eq $Expected) { return }
            }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw "Worker 未在端口 $Port 上以版本 $Expected 健康启动。"
}

function Stop-TestWorker {
    $tokenPath = Join-Path $homePath 'data\launcher-token'
    if (Test-Path -LiteralPath $tokenPath) {
        $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
        try {
            Invoke-RestMethod "http://127.0.0.1:$Port/api/internal/shutdown" -Method Post `
                -Headers @{ 'X-Coordinator-Token' = $token } -UseBasicParsing -TimeoutSec 5 | Out-Null
        } catch {}
    }
    $prefix = "$installRoot\current\"
    for ($i = 0; $i -lt 60; $i++) {
        $alive = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
        if (-not $alive) { break }
        Start-Sleep -Seconds 1
    }
    Get-CimInstance Win32_Process -Filter "Name = 'MenuCapture.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

try {
    Write-Host "安装 Velopack 包：$Setup"
    $install = Start-Process -FilePath $Setup -ArgumentList '--silent' -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "Setup.exe 退出码 $($install.ExitCode)。" }

    foreach ($path in @(
        (Join-Path $installRoot 'Update.exe'),
        (Join-Path $installRoot 'MenuCapture.exe'),
        (Join-Path $installRoot 'current\MenuCapture.exe'),
        (Join-Path $installRoot 'current\python\python.exe'),
        (Join-Path $installRoot 'current\app\menu_capture\api\server.py'),
        (Join-Path $installRoot 'current\sq.version')
    )) {
        if (-not (Test-Path -LiteralPath $path)) { throw "安装布局缺少：$path" }
    }
    $installed = (Get-Content -LiteralPath (Join-Path $installRoot 'current\sq.version') -Raw).Trim()
    if ($installed -notmatch [regex]::Escape($Version)) { throw "sq.version=$installed，期望 $Version。" }

    # Velopack Setup 默认会拉起应用；--silent 下显式启动稳定 stub，验证快捷方式/登录入口所用路径。
    Start-Process -FilePath (Join-Path $installRoot 'MenuCapture.exe') -WindowStyle Hidden
    Invoke-Health -Expected $Version
    $worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$installRoot\current\", [StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
    if (-not $worker) { throw 'Worker 不是从 current 目录启动。' }
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
    if (-not (Test-Path -LiteralPath $startup)) { throw '首次运行未创建登录启动快捷方式。' }
    Write-Host "版本 $Version 启动通过，Worker PID $($worker.ProcessId)。"

    Stop-TestWorker
    # 再次启动验证单实例与重启恢复；不重复安装。
    Start-Process -FilePath (Join-Path $installRoot 'MenuCapture.exe') -WindowStyle Hidden
    Invoke-Health -Expected $Version
    Write-Host '重启恢复通过。'

    if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne 'preserve') { throw '安装/重启改动了业务数据。' }
    foreach ($entry in $seedHashes.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Key)) { throw "安装/重启删除了 $($entry.Key)。" }
        if ((Get-FileHash -LiteralPath $entry.Key -Algorithm SHA256).Hash -ne $entry.Value) {
            throw "安装/重启改动了 $($entry.Key)。"
        }
    }
    Write-Host "Velopack 安装、启动、版本核验、重启恢复与数据保留验收通过。"
} catch {
    $keep = Join-Path $tempRoot ('menu-capture-smoke-logs-' + (Get-Date -Format 'yyyyMMddHHmmss'))
    $null = New-Item -ItemType Directory -Path $keep -Force
    foreach ($path in @(
        (Join-Path $homePath 'logs\launcher.log'),
        (Join-Path $homePath 'logs\service.stderr.log')
    )) {
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $keep -Force }
    }
    Write-Host "日志保留在 $keep"
    Get-ChildItem -LiteralPath $keep -File | ForEach-Object { Get-Content -LiteralPath $_.FullName -Tail 200 }
    throw
} finally {
    Stop-TestWorker
    $updater = Join-Path $installRoot 'Update.exe'
    if (Test-Path -LiteralPath $updater) {
        Start-Process -FilePath $updater -ArgumentList 'uninstall', '-s' -Wait -ErrorAction SilentlyContinue
    }
    $env:LOCALAPPDATA = $originalLocalData
    $env:MENU_CAPTURE_HOME = $originalHome
    $env:MENU_CAPTURE_PORT = $originalPort
}
