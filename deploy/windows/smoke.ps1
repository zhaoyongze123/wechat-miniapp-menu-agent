# 在隔离目录中验收安装、升级、进程恢复和数据保留；测试完成后卸载测试实例。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [string]$Version,
    [string]$PreviousInstaller,
    [string]$PreviousVersion,
    [string]$DataSeed,
    [int]$Port = 8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Installer = (Resolve-Path $Installer).Path
if ($PreviousInstaller) {
    $PreviousInstaller = (Resolve-Path $PreviousInstaller).Path
    if (-not $PreviousVersion -or -not $Version -or $PreviousVersion -eq $Version) {
        throw 'Upgrade validation requires distinct previous and candidate version numbers.'
    }
    if ($Port -ne 8765) { throw 'Pre-React installers require port 8765. Use a disposable Windows runner.' }
}
$firstInstaller = if ($PreviousInstaller) { $PreviousInstaller } else { $Installer }
$firstVersion = if ($PreviousInstaller) { $PreviousVersion } else { $Version }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$existingStartup = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
if ((Get-ScheduledTask -TaskName "MenuCapture Worker - $sid" -ErrorAction SilentlyContinue) -or
    (Test-Path -LiteralPath $existingStartup) -or
    (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    throw '已有采集服务或登录启动项。请在独立 Windows 账号或临时运行环境执行安装验收。'
}
# 安装器同样受 260 字符路径上限约束；测试目录用短前缀，避免 releases\...\node_modules 深层文件装不进去。
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $tempRoot ('mcs-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$install = Join-Path $testRoot 'Programs\MenuCapture'
$localData = Join-Path $testRoot 'LocalAppData'
$homePath = Join-Path $localData 'MenuCapture'
$launcher = Join-Path $install 'Launch.ps1'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$originalLocalData = $env:LOCALAPPDATA
$originalHome = $env:MENU_CAPTURE_HOME
$originalPort = $env:MENU_CAPTURE_PORT
$null = New-Item -ItemType Directory -Path (Join-Path $homePath 'data') -Force
$marker = Join-Path $homePath 'data\offline-sentinel'
Set-Content -LiteralPath $marker -Value 'preserve'
if ($DataSeed) {
    # 用真实历史数据副本做升级验收，确保升级前后都能读到既有任务与待上传记录。
    Copy-Item -Path (Join-Path $DataSeed '*') -Destination $homePath -Recurse -Force
}
# 新旧安装器解析数据目录的方式不同：LOCALAPPDATA 与 MENU_CAPTURE_HOME 指向同一位置。
$env:LOCALAPPDATA = $localData
$env:MENU_CAPTURE_HOME = $homePath
$env:MENU_CAPTURE_PORT = "$Port"
$seedHashes = @{}
foreach ($file in Get-ChildItem -LiteralPath $homePath -Recurse -File -Force | Where-Object { $_.FullName -notlike '*\logs\*' }) {
    $seedHashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
}

function Find-TestWorker {
    $exePrefix = (Join-Path $install 'releases') + '\'
    return Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($exePrefix, [StringComparison]::OrdinalIgnoreCase) }
}

function Assert-WorkerHealthy {
    param([int]$PreviousProcessId = 0, [string]$ExpectedVersion)
    $url = "http://127.0.0.1:$Port/healthz"
    $expectedExe = if ($ExpectedVersion) { Join-Path $install "releases\$ExpectedVersion\python\python.exe" } else { '' }
    for ($i = 0; $i -lt 60; $i++) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess)
        $process = Find-TestWorker | Where-Object {
            $_.ProcessId -ne $PreviousProcessId -and $_.ProcessId -in $listeners -and
            (-not $expectedExe -or $_.ExecutablePath.Equals($expectedExe, [StringComparison]::OrdinalIgnoreCase))
        } | Select-Object -First 1
        if ($process) {
            try {
                $health = Invoke-RestMethod $url -UseBasicParsing -TimeoutSec 2
                if ($health.ok -and $health.role -eq 'worker') { return $process }
            } catch {}
        }
        Start-Sleep -Seconds 1
    }
    throw "Installed worker $ExpectedVersion did not become healthy on port $Port."
}

function Start-TestSupervisor {
    $watchArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""$launcher"" -Action Supervise"
    Start-Process -FilePath $powershell -ArgumentList $watchArgs -WindowStyle Hidden
}

function Stop-TestSupervisor {
    # 先让测试守护进程正常停止其子进程。超时后也只清理本次临时安装目录的进程。
    $target = $launcher
    Set-Content -LiteralPath (Join-Path $homePath 'stop-supervisor') -Value 'smoke cleanup'
    for ($attempt = 0; $attempt -lt 8 -and (Find-TestWorker); $attempt++) {
        Start-Sleep -Seconds 1
    }
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($target) -and $_.CommandLine.Contains('Supervise') } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Find-TestWorker | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Assert-Version {
    param([string]$Expected, [string]$Label)
    if (-not $Expected) { return }
    $file = Join-Path $install 'current-version'
    if (-not (Test-Path -LiteralPath $file)) { throw "$Label is missing current-version." }
    $actual = (Get-Content -LiteralPath $file -Raw).Trim()
    if ($actual -ne $Expected) { throw "$Label wrote current-version=$actual, expected $Expected." }
}

function Assert-WorkerRelease {
    param([string]$Expected)
    if (-not $Expected) { return }
    $prefix = (Join-Path $install "releases\$Expected") + '\'
    $worker = Find-TestWorker | Where-Object { $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
    if (-not $worker) { throw "Worker did not start from releases\$Expected." }
}

function Assert-ApiVersion {
    param([string]$Expected, [string]$Label, [switch]$AllowLegacy)
    if (-not $Expected) { return }
    $manifest = Join-Path $install "releases\$Expected\release-manifest.json"
    if (-not (Test-Path -LiteralPath $manifest)) {
        if (-not $AllowLegacy) { throw "$Label missing candidate release manifest." }
        # 首次 React 升级前的生产版没有版本 API 和清单，按 Worker 健康状态及可执行文件路径验收。
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 5
        if (-not $health.ok -or $health.role -ne 'worker') { throw "$Label previous Worker health check failed." }
        Assert-WorkerRelease $Expected
        return
    }
    $url = "http://127.0.0.1:$Port/api/version"
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $info = Invoke-RestMethod $url -UseBasicParsing -TimeoutSec 2
            if ($info.version -eq $Expected -and $info.frontend) { return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw "$Label did not report version $Expected with a frontend build via /api/version."
}

function Assert-ReleaseManifest {
    param([string]$Expected)
    if (-not $Expected) { return }
    $path = Join-Path $install "releases\$Expected\release-manifest.json"
    # 迁移前的原生 UI 发布物没有 React 清单，进程路径和 /healthz 会验证其已启动。
    if (-not (Test-Path -LiteralPath $path)) { return }
    $manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ($manifest.version -ne $Expected) { throw "release-manifest version $($manifest.version), expected $Expected." }
    if (-not $manifest.frontend_entry -or -not $manifest.frontend_sha256) { throw "Incomplete release-manifest for $Expected." }
    $asset = Join-Path $install ("releases\$Expected\app\web\react\" + $manifest.frontend_entry.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $asset)) { throw "Frontend entry missing: $asset" }
    $hash = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $manifest.frontend_sha256) { throw "Frontend entry hash mismatch in release-manifest for $Expected." }
}

function Install-TestWorker {
    param([int]$Index, [string]$Path = $Installer)
    # 每次安装单独一份日志：升级时安装器会先卸载旧版本，两份日志混在一起会掩盖真正的失败原因。
    $installLog = Join-Path $testRoot "install-$Index.log"
    $process = Start-Process -FilePath $Path -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=""$install""", "/LOG=""$installLog"""
    )
    Write-Host "Install #${Index} exit code: $($process.ExitCode)"
    if ($process.ExitCode -ne 0) {
        if (Test-Path -LiteralPath $installLog) {
            $entries = Get-Content -LiteralPath $installLog
            Write-Host "Installer log ($installLog): $($entries.Count) lines"
            # 创建/删除目录会占满日志，只保留其余行，失败原因（如 Could not create directory）才不会被淹没。
            $entries | Where-Object { $_ -notmatch 'Creating directory:|Deleting directory:' }
        }
        throw "Installer #$Index exit code $($process.ExitCode)."
    }
    if (Test-Path -LiteralPath $installLog) {
        # 安装器在 /SUPPRESSMSGBOXES 下会吞掉 Register 失败并仍返回 0，这里显式判失败。
        $text = [IO.File]::ReadAllText($installLog, [Text.Encoding]::UTF8)
        if ($text -match '无法设置登录自动启动') {
            throw "Installer #$Index failed to register the worker; see $installLog."
        }
    }
}

try {
    Install-TestWorker -Index 1 -Path $firstInstaller
    Assert-Version $firstVersion 'Install #1'
    $task = Get-ScheduledTask -TaskName "MenuCapture Worker - $sid" -ErrorAction SilentlyContinue
    $fallback = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
    if (-not $task -and -not (Test-Path -LiteralPath $fallback)) { throw 'Login startup entry is missing.' }
    if ($task -and -not $task.Actions[0].Arguments.Contains($launcher)) {
        throw 'Login startup entry does not target the installed worker.'
    }

    Start-TestSupervisor
    $first = Assert-WorkerHealthy -ExpectedVersion $firstVersion
    Assert-WorkerRelease $firstVersion
    Assert-ReleaseManifest $firstVersion
    Assert-ApiVersion $firstVersion 'Install #1' -AllowLegacy:([bool]$PreviousInstaller)
    Stop-Process -Id $first.ProcessId -Force
    $second = Assert-WorkerHealthy -PreviousProcessId $first.ProcessId -ExpectedVersion $firstVersion
    Write-Host "Supervisor restarted worker $($first.ProcessId) as $($second.ProcessId)."

    if ($PreviousInstaller) {
        # 模拟正常原地升级，旧版守护进程保持运行，读取新版本登记并切换。
        Install-TestWorker -Index 2 -Path $Installer
        Assert-Version $Version 'Upgrade'
        $third = Assert-WorkerHealthy -PreviousProcessId $second.ProcessId -ExpectedVersion $Version
        Assert-WorkerRelease $Version
        Assert-ReleaseManifest $Version
        Assert-ApiVersion $Version 'Upgrade'
        Stop-Process -Id $third.ProcessId -Force
        $fourth = Assert-WorkerHealthy -PreviousProcessId $third.ProcessId -ExpectedVersion $Version
        Write-Host "Upgrade restarted worker $($third.ProcessId) as $($fourth.ProcessId)."

        # 回滚验收：版本登记切回安装前版本，守护进程应在空闲时重启旧实例。
        $rollback = Join-Path $install 'rollback.ps1'
        if (-not (Test-Path -LiteralPath $rollback)) { throw 'Installed rollback.ps1 is missing.' }
        & $rollback -Version $PreviousVersion -TimeoutSeconds 120
        if ($LASTEXITCODE -ne 0) { throw "Rollback to $PreviousVersion failed." }
        $fifth = Assert-WorkerHealthy -PreviousProcessId $fourth.ProcessId -ExpectedVersion $PreviousVersion
        Assert-Version $PreviousVersion 'Rollback'
        Assert-WorkerRelease $PreviousVersion
        Assert-ReleaseManifest $PreviousVersion
        Assert-ApiVersion $PreviousVersion 'Rollback' -AllowLegacy
        Write-Host "Rollback switched worker $($fourth.ProcessId) to $($fifth.ProcessId) on version $PreviousVersion."
    } else {
        Install-TestWorker -Index 2
        Assert-Version $Version 'Install #2'
        $null = Assert-WorkerHealthy -ExpectedVersion $Version
        Assert-WorkerRelease $Version
        Assert-ApiVersion $Version 'Install #2'
    }

    if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne 'preserve') { throw 'Reinstall removed local state.' }
    foreach ($entry in $seedHashes.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Key)) { throw "Upgrade removed $($entry.Key)." }
        $hash = (Get-FileHash -LiteralPath $entry.Key -Algorithm SHA256).Hash
        if ($hash -ne $entry.Value) { throw "Upgrade changed $($entry.Key)." }
    }
    if ($PreviousInstaller) {
        Write-Host 'Previous release install, candidate upgrade, restart recovery, rollback and data preservation passed.'
    } else {
        Write-Host 'Candidate install, reinstall, restart recovery and data preservation passed.'
    }
} catch {
    # 失败时把安装日志和运行日志复制到独立目录，便于 CI 产物或人工排查。
    $keep = Join-Path $tempRoot ('menu-capture-smoke-logs-' + (Get-Date -Format 'yyyyMMddHHmmss'))
    $null = New-Item -ItemType Directory -Path $keep -Force
    foreach ($path in @(
        (Join-Path $testRoot 'install-1.log'),
        (Join-Path $testRoot 'install-2.log'),
        (Join-Path $homePath 'logs\launcher.log'),
        (Join-Path $homePath 'logs\service.stderr.log')
    )) {
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $keep -Force }
    }
    Write-Host "Logs kept at $keep"
    Get-ChildItem -LiteralPath $keep -File | ForEach-Object { Get-Content -LiteralPath $_.FullName -Tail 200 }
    throw
} finally {
    Stop-TestSupervisor
    $uninstaller = Join-Path $install 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait
    }
    $env:LOCALAPPDATA = $originalLocalData
    $env:MENU_CAPTURE_HOME = $originalHome
    $env:MENU_CAPTURE_PORT = $originalPort
}
