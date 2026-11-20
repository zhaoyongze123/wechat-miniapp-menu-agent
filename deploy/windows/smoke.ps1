# 仅在隔离的 Windows 构建机上验收安装、进程恢复和数据保留。测试完成后卸载测试实例。
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Installer)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Installer = (Resolve-Path $Installer).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('menu-capture-smoke-' + [guid]::NewGuid().ToString('N'))
$install = Join-Path $testRoot 'Programs\MenuCapture'
$localData = Join-Path $testRoot 'LocalAppData'
$homePath = Join-Path $localData 'MenuCapture'
$log = Join-Path $testRoot 'install.log'
$launcher = Join-Path $install 'Launch.ps1'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$originalLocalData = $env:LOCALAPPDATA
$null = New-Item -ItemType Directory -Path (Join-Path $homePath 'data') -Force
$marker = Join-Path $homePath 'data\offline-sentinel'
Set-Content -LiteralPath $marker -Value 'preserve'
$env:LOCALAPPDATA = $localData

function Find-TestWorker {
    $exePrefix = (Join-Path $install 'releases') + '\'
    return Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($exePrefix, [StringComparison]::OrdinalIgnoreCase) }
}

function Assert-WorkerHealthy {
    param([int]$PreviousProcessId = 0)
    for ($i = 0; $i -lt 30; $i++) {
        $process = Find-TestWorker | Where-Object ProcessId -NE $PreviousProcessId
        if ($process) {
            try {
                $health = Invoke-RestMethod 'http://127.0.0.1:8765/healthz' -UseBasicParsing -TimeoutSec 2
                if ($health.ok -and $health.role -eq 'worker') { return $process }
            } catch {}
        }
        Start-Sleep -Seconds 1
    }
    throw 'Installed worker did not become healthy.'
}

function Install-TestWorker {
    param([int]$Index)
    # 每次安装单独一份日志：重装时安装器会先卸载旧版本，两份日志混在一起会掩盖真正的失败原因。
    $installLog = Join-Path $testRoot "install-$Index.log"
    $process = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=""$install""", "/LOG=""$installLog"""
    )
    Write-Host "Install #${Index} exit code: $($process.ExitCode)"
    if ($process.ExitCode -ne 0) {
        if (Test-Path -LiteralPath $installLog) {
            Write-Host "Installer log head ($installLog):"
            Get-Content -LiteralPath $installLog -TotalCount 120
            Write-Host "Installer log tail ($installLog):"
            Get-Content -LiteralPath $installLog -Tail 60
        }
        throw "Installer #$Index exit code $($process.ExitCode)."
    }
}

try {
    Install-TestWorker -Index 1
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $task = Get-ScheduledTask -TaskName "MenuCapture Worker - $sid" -ErrorAction SilentlyContinue
    $fallback = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
    if (-not $task -and -not (Test-Path -LiteralPath $fallback)) { throw 'Login startup entry is missing.' }

    $watchArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""$launcher"" -Action Supervise"
    Start-Process -FilePath $powershell -ArgumentList $watchArgs -WindowStyle Hidden
    $first = Assert-WorkerHealthy
    Stop-Process -Id $first.ProcessId -Force
    $second = Assert-WorkerHealthy -PreviousProcessId $first.ProcessId
    Install-TestWorker -Index 2
    if ((Get-Content -LiteralPath $marker).Trim() -ne 'preserve') { throw 'Reinstall removed local state.' }
    Write-Host "Installer boots the worker, supervisor restarted $($first.ProcessId) as $($second.ProcessId), and reinstall preserved data."
} catch {
    foreach ($path in @($log, (Join-Path $homePath 'logs\launcher.log'), (Join-Path $homePath 'logs\service.stderr.log'))) {
        if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Tail 200 }
    }
    throw
} finally {
    $uninstaller = Join-Path $install 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait
    }
    $env:LOCALAPPDATA = $originalLocalData
}
