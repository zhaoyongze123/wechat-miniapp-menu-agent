# Velopack 隔离验收：安装或覆盖升级、启动、版本与 commit、设备身份、数据库、数据保留与卸载。
# 覆盖升级通过 -PreviousSetup 指定已发布基线；不验证增量（增量由 vpk 生成，另行验证）。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Setup,
    [Parameter(Mandatory)][string]$Version,
    # 可选覆盖升级：先安装该已发布基线的 Setup，再用 $Setup 在同一安装目录覆盖升级。
    [string]$PreviousSetup = '',
    [string]$PreviousVersion = '',
    # 可选：校验 /api/version 的 commit 是否为产物真实构建 commit。
    [string]$Commit = '',
    [int]$Port = 8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path $Setup).Path
if ($PreviousSetup) { $PreviousSetup = (Resolve-Path $PreviousSetup).Path }
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
    param([string]$Expected = '', [string]$ExpectedCommit = '')
    $url = "http://127.0.0.1:$Port/healthz"
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $health = Invoke-RestMethod $url -UseBasicParsing -TimeoutSec 2
            if ($health.ok -and $health.role -eq 'worker') {
                $info = Invoke-RestMethod "http://127.0.0.1:$Port/api/version" -UseBasicParsing -TimeoutSec 2
                if ((!$Expected -or $info.version -eq $Expected) -and (!$ExpectedCommit -or $info.commit -eq $ExpectedCommit)) {
                    return $info
                }
            }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw "Worker 未在端口 $Port 上以版本 $Expected（commit $ExpectedCommit）健康启动。"
}

function Get-DeviceId {
    return (Invoke-RestMethod "http://127.0.0.1:$Port/api/manual/state" -UseBasicParsing -TimeoutSec 5).device_id
}

function Assert-Layout {
    foreach ($path in @(
        (Join-Path $installRoot 'Update.exe'),
        (Join-Path $installRoot 'current\MenuCapture.exe'),
        (Join-Path $installRoot 'current\python\python.exe'),
        (Join-Path $installRoot 'current\app\menu_capture\api\server.py'),
        (Join-Path $installRoot 'current\sq.version')
    )) {
        if (-not (Test-Path -LiteralPath $path)) { throw "安装布局缺少：$path" }
    }
    return (Get-Content -LiteralPath (Join-Path $installRoot 'current\sq.version') -Raw).Trim()
}

function Assert-Database {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "缺少数据库：$Path" }
    # 使用安装包自带运行时只读校验，避免依赖本机 sqlite3。
    $python = Join-Path $installRoot 'current\python\python.exe'
    $result = & $python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("pragma integrity_check").fetchone()[0]); print(len(c.execute("select name from sqlite_master where type=''table''").fetchall()))' $Path
    if ($LASTEXITCODE -ne 0) { throw '数据库校验脚本执行失败。' }
    $lines = @($result)
    if ($lines[0] -ne 'ok') { throw "数据库完整性检查失败：$($lines[0])" }
    if ([int]$lines[1] -le 0) { throw '数据库中没有任何表。' }
}

function Stop-TestWorker {
    $launcher = Join-Path $installRoot 'current\MenuCapture.exe'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { return }
    $stop = Start-Process -FilePath $launcher -ArgumentList '--stop' -Wait -PassThru -WindowStyle Hidden
    if ($stop.ExitCode -ne 0) { throw "Launcher refused a verified graceful stop (exit $($stop.ExitCode))." }
    $prefix = "$installRoot\current\"
    for ($i = 0; $i -lt 120; $i++) {
        $alive = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
        $listening = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
        if ($alive.Count -eq 0 -and $listening.Count -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw 'Launcher did not prove that its Worker process tree and port had stopped; refusing force termination.'
}

try {
    if ($PreviousSetup) {
        # 覆盖升级验收：先安装已发布基线并运行一次，形成业务数据、数据库与设备身份对照。
        Write-Host "安装基线 Velopack 包：$PreviousSetup"
        $prevInstall = Start-Process -FilePath $PreviousSetup -ArgumentList '--silent', '--installto', $installRoot -Wait -PassThru
        if ($prevInstall.ExitCode -ne 0) { throw "基线 Setup.exe 退出码 $($prevInstall.ExitCode)。" }
        $prevSq = Assert-Layout
        if ($PreviousVersion -and $prevSq -notmatch [regex]::Escape($PreviousVersion)) {
            throw "基线 sq.version=$prevSq，期望 $PreviousVersion。"
        }
        Start-Process -FilePath (Join-Path $installRoot 'current\MenuCapture.exe') -WindowStyle Hidden
        $prevInfo = Invoke-Health -Expected $PreviousVersion
        $deviceBefore = Get-DeviceId
        Assert-Database -Path (Join-Path $homePath 'data\tasks.sqlite3')
        Write-Host "基线版本 $($prevInfo.version) 运行通过，设备身份 $deviceBefore。"
        Stop-TestWorker
    }

    Write-Host "安装 Velopack 包：$Setup"
    $install = Start-Process -FilePath $Setup -ArgumentList '--silent', '--installto', $installRoot -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "Setup.exe 退出码 $($install.ExitCode)。" }

    $installed = Assert-Layout
    if ($installed -notmatch [regex]::Escape($Version)) { throw "sq.version=$installed，期望 $Version。" }

    # Velopack Setup --silent 下显式启动 current\MenuCapture.exe（或根 stub）。
    $launcherExe = Join-Path $installRoot 'current\MenuCapture.exe'
    Start-Process -FilePath $launcherExe -WindowStyle Hidden
    $info = Invoke-Health -Expected $Version -ExpectedCommit $Commit
    $worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$installRoot\current\", [StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
    if (-not $worker) { throw 'Worker 不是从 current 目录启动。' }
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
    if (-not (Test-Path -LiteralPath $startup)) { throw '首次运行未创建登录启动快捷方式。' }
    Write-Host "版本 $($info.version)（commit $($info.commit)）启动通过，Worker PID $($worker.ProcessId)。"

    if ($PreviousSetup) {
        $deviceAfter = Get-DeviceId
        if ($deviceAfter -ne $deviceBefore) { throw "覆盖升级改变了设备身份：$deviceBefore -> $deviceAfter" }
        Assert-Database -Path (Join-Path $homePath 'data\tasks.sqlite3')
        Write-Host '覆盖升级后设备身份与数据库校验通过。'
    }

    Stop-TestWorker
    # 再次启动验证单实例与重启恢复；不重复安装。
    Start-Process -FilePath $launcherExe -WindowStyle Hidden
    Invoke-Health -Expected $Version -ExpectedCommit $Commit | Out-Null
    Write-Host '重启恢复通过。'

    if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne 'preserve') { throw '安装/重启改动了业务数据。' }
    foreach ($entry in $seedHashes.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Key)) { throw "安装/重启删除了 $($entry.Key)。" }
        if ((Get-FileHash -LiteralPath $entry.Key -Algorithm SHA256).Hash -ne $entry.Value) {
            throw "安装/重启改动了 $($entry.Key)。"
        }
    }
    if ($PreviousSetup) {
        Write-Host 'Velopack 覆盖升级、启动、版本与 commit、设备身份、数据库、重启恢复与数据保留验收通过。'
    } else {
        Write-Host "Velopack 安装、启动、版本核验、重启恢复与数据保留验收通过。"
    }
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
    $stopped = $false
    try {
        Stop-TestWorker
        $stopped = $true
    } catch {
        Write-Host "Test instance retained because graceful stop was not verified: $_"
    }
    if ($stopped) {
        $updater = Join-Path $installRoot 'Update.exe'
        if (Test-Path -LiteralPath $updater) {
            Start-Process -FilePath $updater -ArgumentList 'uninstall', '-s' -Wait -ErrorAction SilentlyContinue
        }
    }
    $env:LOCALAPPDATA = $originalLocalData
    $env:MENU_CAPTURE_HOME = $originalHome
    $env:MENU_CAPTURE_PORT = $originalPort
}
