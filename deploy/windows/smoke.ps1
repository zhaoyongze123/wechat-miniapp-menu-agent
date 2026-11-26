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
# 新建账号首次经 SSH 登录时 shell 文件夹可能尚未初始化，退回 APPDATA 标准路径。
$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { $startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
# 新建账号的启动目录可能尚未生成；启动器只在目录存在时创建快捷方式，这里先补齐。
$null = New-Item -ItemType Directory -Path $startupDir -Force
$startupLink = Join-Path $startupDir '菜单采集后台.lnk'
# 普通账号下 CIM 网络查询可能被拒绝，改用 .NET 套接字探测端口占用。
function Test-PortListening {
    param([int]$TcpPort)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        return $client.ConnectAsync('127.0.0.1', $TcpPort).Wait(500) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-WorkerProcesses {
    param([string]$Prefix)
    return @(Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
}

if ((Test-Path -LiteralPath $startupLink) -or (Test-PortListening -Port $Port)) {
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
    # 使用安装包自带运行时只读校验，避免依赖本机 sqlite3；脚本落盘以防 PowerShell 5.1 丢失内嵌引号。
    $python = Join-Path $installRoot 'current\python\python.exe'
    $probe = Join-Path $testRoot 'db-probe.py'
    if (-not (Test-Path -LiteralPath $probe)) {
        Set-Content -LiteralPath $probe -Encoding ASCII -Value @'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(con.execute("pragma integrity_check").fetchone()[0])
print(len(con.execute("select name from sqlite_master where type='table'").fetchall()))
'@
    }
    $result = & $python $probe $Path
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
        $alive = @(Get-WorkerProcesses -Prefix $prefix)
        if ($alive.Count -eq 0 -and -not (Test-PortListening -Port $Port)) { return }
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
    $worker = Get-WorkerProcesses -Prefix "$installRoot\current\" | Select-Object -First 1
    if (-not $worker) { throw 'Worker 不是从 current 目录启动。' }
    if (-not (Test-Path -LiteralPath $startupLink)) { throw '首次运行未创建登录启动快捷方式。' }
    Write-Host "版本 $($info.version)（commit $($info.commit)）启动通过，Worker PID $($worker.Id)。"

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
