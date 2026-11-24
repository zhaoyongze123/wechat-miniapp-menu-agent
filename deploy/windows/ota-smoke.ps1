# 通过应用内更新接口验收整条 OTA 链路：下载状态 → 安装任务文件 → 独立执行器 →
# 静默安装 → 健康切换 → 回滚。只使用隔离目录，结束时卸载测试实例。
# 与 smoke.ps1 的区别：这里不手动运行安装包，安装动作由采集端自身的更新执行器完成。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$PreviousInstaller,
    [Parameter(Mandatory)][string]$PreviousVersion,
    [int]$Port = 8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PreviousVersion -eq $Version) { throw 'OTA validation requires distinct previous and candidate versions.' }
$Installer = (Resolve-Path $Installer).Path
$PreviousInstaller = (Resolve-Path $PreviousInstaller).Path
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$existingStartup = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
if ((Get-ScheduledTask -TaskName "MenuCapture Worker - $sid" -ErrorAction SilentlyContinue) -or
    (Test-Path -LiteralPath $existingStartup) -or
    (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    throw '已有采集服务或登录启动项。请在独立 Windows 账号或临时运行环境执行安装验收。'
}
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $tempRoot ('mco-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$install = Join-Path $testRoot 'Programs\MenuCapture'
$localData = Join-Path $testRoot 'LocalAppData'
$homePath = Join-Path $localData 'MenuCapture'
$dataDir = Join-Path $homePath 'data'
$launcher = Join-Path $install 'Launch.ps1'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$assetName = "MenuCapture-Setup-$Version-win-x64.exe"
$downloadDir = Join-Path $dataDir "updates\$Version"
$base = "http://127.0.0.1:$Port"
$originalLocalData = $env:LOCALAPPDATA
$originalHome = $env:MENU_CAPTURE_HOME
$originalPort = $env:MENU_CAPTURE_PORT

function Find-TestWorker {
    $exePrefix = (Join-Path $install 'releases') + '\'
    return Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($exePrefix, [StringComparison]::OrdinalIgnoreCase) }
}

function Stop-TestSupervisor {
    $target = $launcher
    Set-Content -LiteralPath (Join-Path $homePath 'stop-supervisor') -Value 'ota smoke cleanup'
    for ($attempt = 0; $attempt -lt 8 -and (Find-TestWorker); $attempt++) { Start-Sleep -Seconds 1 }
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($target) -and $_.CommandLine.Contains('Supervise') } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Find-TestWorker | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Assert-WorkerHealthy {
    param([string]$ExpectedVersion)
    $expectedExe = Join-Path $install "releases\$ExpectedVersion\python\python.exe"
    for ($i = 0; $i -lt 90; $i++) {
        $process = Find-TestWorker | Where-Object {
            $_.ExecutablePath.Equals($expectedExe, [StringComparison]::OrdinalIgnoreCase)
        } | Select-Object -First 1
        if ($process) {
            try {
                $health = Invoke-RestMethod "$base/healthz" -UseBasicParsing -TimeoutSec 2
                $info = Invoke-RestMethod "$base/api/version" -UseBasicParsing -TimeoutSec 2
                if ($health.ok -and $health.role -eq 'worker' -and $info.version -eq $ExpectedVersion) { return $process }
            } catch {}
        }
        Start-Sleep -Seconds 1
    }
    throw "Worker $ExpectedVersion did not become healthy on port $Port."
}

function Install-TestWorker {
    param([string]$Path, [string]$Label)
    $installLog = Join-Path $testRoot "install-$Label.log"
    $process = Start-Process -FilePath $Path -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=""$install""", "/LOG=""$installLog"""
    )
    if ($process.ExitCode -ne 0) { throw "Installer $Label exit code $($process.ExitCode); see $installLog." }
}

function Assert-VersionFile {
    param([string]$Expected)
    $file = Join-Path $install 'current-version'
    if (-not (Test-Path -LiteralPath $file)) { throw 'current-version is missing.' }
    $actual = (Get-Content -LiteralPath $file -Raw).Trim()
    if ($actual -ne $Expected) { throw "current-version=$actual, expected $Expected." }
}

function Wait-OtaTerminal {
    param([int]$TimeoutSeconds = 900)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # 安装过程中采集端会被守护进程重启，短暂连接失败不代表失败，继续轮询。
        try {
            $state = Invoke-RestMethod "$base/api/updates/install" -UseBasicParsing -TimeoutSec 5
        } catch {
            Start-Sleep -Seconds 3
            continue
        }
        Write-Host ("OTA status: {0} - {1}" -f $state.status, $state.message)
        if ($state.status -in @('completed', 'failed', 'rolled_back')) { return $state }
        Start-Sleep -Seconds 3
    }
    throw 'OTA install did not reach a terminal state.'
}

try {
    $null = New-Item -ItemType Directory -Path $dataDir, $downloadDir -Force
    $marker = Join-Path $dataDir 'ota-sentinel'
    Set-Content -LiteralPath $marker -Value 'preserve'

    # 1) 预置“下载完成”状态与发布清单：真实环境由下载器与 GitHub 检查写入同样的文件。
    $artifact = Join-Path $downloadDir $assetName
    Copy-Item -LiteralPath $Installer -Destination $artifact -Force
    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $artifact).Length
    $downloadState = [ordered]@{
        active = $false; status = 'completed'; version = $Version
        asset_kind = 'windows_installer'; asset_name = $assetName; arch = 'x64'
        sha256 = $hash; etag = ''; last_modified = ''
        downloaded = $size; total = $size
        started_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        finished_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        error = ''; path = $artifact
    }
    # Python 侧按 UTF-8 读取 JSON；必须写无 BOM 文件，否则 Windows PowerShell 的 utf8 编码会让解析失败。
    $utf8 = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText((Join-Path $dataDir 'update-download.json'), ($downloadState | ConvertTo-Json), $utf8)
    $cache = [ordered]@{
        # checked_at 在服务端按 epoch 秒参与缓存有效期计算，不能写 ISO 字符串。
        checked_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        backoff_until = 0
        channel = 'stable'
        latest = [ordered]@{
            version = $Version; eligible = $true; channel = 'stable'
            assets = @([ordered]@{ kind = 'windows_installer'; name = $assetName; arch = 'x64'; sha256 = $hash; size = $size })
        }
    }
    [IO.File]::WriteAllText((Join-Path $dataDir 'update-cache.json'), ($cache | ConvertTo-Json -Depth 6), $utf8)

    # 2) 安装上一版本并启动，作为 OTA 的真实起点。
    $env:LOCALAPPDATA = $localData
    $env:MENU_CAPTURE_HOME = $homePath
    $env:MENU_CAPTURE_PORT = "$Port"
    $env:MENU_CAPTURE_ALLOW_SELF_UPDATE = '1'
    # 当前发布物未做代码签名，隔离验收显式放行；签名无效仍会被执行器拒绝。
    $env:MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE = '1'
    Install-TestWorker -Path $PreviousInstaller -Label 'previous'
    Assert-VersionFile $PreviousVersion
    $watchArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""$launcher"" -Action Supervise"
    Start-Process -FilePath $powershell -ArgumentList $watchArgs -WindowStyle Hidden
    $first = Assert-WorkerHealthy -ExpectedVersion $PreviousVersion

    # 3) 通过应用内更新接口触发安装，不再手动执行安装包。
    $status = Invoke-RestMethod "$base/api/updates/install" -UseBasicParsing -TimeoutSec 5
    if (-not $status.supported) { throw 'Install executor is not enabled on this worker.' }
    $token = (Invoke-RestMethod "$base/api/session" -UseBasicParsing -TimeoutSec 5).token
    $body = @{ version = $Version; confirm = $Version } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Post -Uri "$base/api/updates/install" -Headers @{ 'X-Capture-Token' = $token } `
        -Body $body -ContentType 'application/json' -TimeoutSec 15
    $terminal = Wait-OtaTerminal
    if ($terminal.status -ne 'completed') { throw "OTA install ended as $($terminal.status): $($terminal.message)" }

    # 4) 独立执行器完成后，守护进程应已切换到目标版本。
    Assert-VersionFile $Version
    $second = Assert-WorkerHealthy -ExpectedVersion $Version
    Write-Host "OTA switched worker $($first.ProcessId) to $($second.ProcessId) on $Version."

    # 5) 回滚仍复用现有 rollback.ps1，确认切换链路可逆。
    $rollback = Join-Path $install 'rollback.ps1'
    if (-not (Test-Path -LiteralPath $rollback)) { throw 'Installed rollback.ps1 is missing.' }
    & $rollback -Version $PreviousVersion -TimeoutSeconds 120
    if ($LASTEXITCODE -ne 0) { throw "Rollback to $PreviousVersion failed." }
    $third = Assert-WorkerHealthy -ExpectedVersion $PreviousVersion
    Assert-VersionFile $PreviousVersion
    Write-Host "Rollback switched worker $($second.ProcessId) to $($third.ProcessId) on $PreviousVersion."

    if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne 'preserve') { throw 'OTA install removed local state.' }
    $journal = Get-Content -LiteralPath (Join-Path $dataDir 'update-install.json') -Raw | ConvertFrom-Json
    if ($journal.status -ne 'completed') { throw "Install journal ended as $($journal.status)." }
    Write-Host 'OTA API install, executor switch and rollback passed.'
} catch {
    $keep = Join-Path $tempRoot ('menu-capture-ota-logs-' + (Get-Date -Format 'yyyyMMddHHmmss'))
    $null = New-Item -ItemType Directory -Path $keep -Force
    foreach ($path in @(
        (Join-Path $testRoot 'install-previous.log'),
        (Join-Path $dataDir 'update-install.json'),
        (Join-Path $dataDir 'update-install.log'),
        (Join-Path $homePath 'logs\launcher.log'),
        (Join-Path $homePath 'logs\service.stderr.log')
    )) {
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $keep -Force }
    }
    # Inno 自身失败时可能来不及写 /LOG，但 setup.iss 开启了 SetupLogging，
    # 默认日志会落在 %TEMP%\Setup Log*.txt；失败时一并收集，避免只能看到“退出码 1”。
    Get-ChildItem -LiteralPath $env:TEMP -Filter 'Setup Log*.txt' -File -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $keep -Force }
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
    $env:MENU_CAPTURE_ALLOW_SELF_UPDATE = $null
    $env:MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE = $null
}
