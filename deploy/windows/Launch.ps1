# Windows 每用户启动器。守护进程只管理自己启动的采集进程，配置和离线结果保存在安装目录外。
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Open', 'Supervise', 'Register', 'Unregister')]
    [string]$Action,
    [ValidatePattern('^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$')]
    [string]$Version
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$taskName = "MenuCapture Worker - $sid"
# 数据目录优先取 MENU_CAPTURE_HOME，便于把配置和结果放在非系统盘；默认仍使用用户本地应用数据目录。
$homePath = if ($env:MENU_CAPTURE_HOME) { $env:MENU_CAPTURE_HOME } else { Join-Path $env:LOCALAPPDATA 'MenuCapture' }
$logPath = Join-Path $homePath 'logs'
$releaseFile = Join-Path $PSScriptRoot 'current-version'
$stopFile = Join-Path $homePath 'stop-supervisor'
$launchScript = Join-Path $PSScriptRoot 'Launch.ps1'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) '菜单采集后台.lnk'
$workbench = 'http://127.0.0.1:8765'
$basePath = $env:PATH
$watchArgs = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$launchScript"" -Action Supervise"
$null = New-Item -ItemType Directory -Path $homePath, $logPath -Force

function Write-LauncherLog {
    param([string]$Message)
    $path = Join-Path $logPath 'launcher.log'
    if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 1048576) {
        Move-Item -LiteralPath $path -Destination "$path.previous" -Force
    }
    Add-Content -LiteralPath $path -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

function Read-WorkerJson {
    param([string]$Path)
    try {
        return Invoke-RestMethod -Uri "$workbench$Path" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
    } catch {
        return $null
    }
}

function Read-Release {
    $value = [IO.File]::ReadAllText($releaseFile).Trim()
    if ($value -notmatch '^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$') {
        throw '安装版本记录无效，请重新运行安装包。'
    }
    return $value
}

function Start-Worker {
    param([string]$ActiveVersion)
    $release = Join-Path $PSScriptRoot "releases\$ActiveVersion"
    $exe = Join-Path $release 'python\python.exe'
    $app = Join-Path $release 'app'
    $env:MENU_CAPTURE_ROLE = 'worker'
    $env:MENU_CAPTURE_HOME = $homePath
    $env:MENU_CAPTURE_BIND = '127.0.0.1'
    $env:MENU_CAPTURE_PORT = '8765'
    $env:TESSERACT_CMD = Join-Path $release 'tesseract\tesseract.exe'
    $env:TESSDATA_PREFIX = Join-Path $release 'tesseract\tessdata'
    $env:PATH = "$(Join-Path $release 'node');$(Join-Path $release 'python');$basePath"
    $stdout = Join-Path $logPath 'service.stdout.log'
    $stderr = Join-Path $logPath 'service.stderr.log'
    foreach ($file in $stdout, $stderr) {
        if (Test-Path -LiteralPath $file) { Move-Item -LiteralPath $file -Destination "$file.previous" -Force }
    }
    $worker = Start-Process -FilePath $exe -ArgumentList '-X utf8 -E -s -B -m menu_capture.server' -WorkingDirectory $app `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Write-LauncherLog "采集端 $ActiveVersion 已启动，PID $($worker.Id)。"
    return $worker
}

function Test-WorkerIdle {
    $runner = Read-WorkerJson '/api/status'
    $annotation = Read-WorkerJson '/api/annotation/state'
    if ($null -eq $runner -or $null -eq $runner.PSObject.Properties['running']) { return $false }
    $annotationActive = $null -ne $annotation -and
        $null -ne $annotation.PSObject.Properties['active'] -and $annotation.active
    return -not ($runner.running -or $annotationActive)
}

function Test-WorkerReady {
    $health = Read-WorkerJson '/healthz'
    return $null -ne $health -and $null -ne $health.PSObject.Properties['role'] -and $health.role -eq 'worker'
}

switch ($Action) {
    'Register' {
        if (-not $Version) { throw '未指定安装版本。' }
        $temp = "$releaseFile.$PID.tmp"
        [IO.File]::WriteAllText($temp, $Version, [Text.Encoding]::ASCII)
        # PowerShell 会把 Replace 的 $null 备份参数转成空字符串，升级时必然报“路径形式不合法”；
        # 这里用 Move-Item -Force 覆盖旧的版本记录。
        Move-Item -LiteralPath $temp -Destination $releaseFile -Force
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
        try {
            $taskAction = New-ScheduledTaskAction -Execute $powershell -Argument $watchArgs -WorkingDirectory $PSScriptRoot
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
            $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
            Remove-Item -LiteralPath $startupLink -Force -ErrorAction SilentlyContinue
        } catch {
            # 受管理的标准账户可能没有注册任务的权限，同一会话的启动快捷方式仍然能自动拉起守护进程。
            Write-LauncherLog "$($_.Exception.Message) 使用当前用户启动文件夹。"
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut($startupLink)
            $link.TargetPath = $powershell
            $link.Arguments = $watchArgs
            $link.WorkingDirectory = $PSScriptRoot
            $link.Save()
        }
    }
    'Open' {
        Start-Process -FilePath $powershell -ArgumentList $watchArgs -WindowStyle Hidden
        for ($i = 0; $i -lt 25; $i++) {
            if (Test-WorkerReady) {
                Start-Process "$workbench/"
                exit 0
            }
            Start-Sleep -Seconds 1
        }
        Add-Type -AssemblyName PresentationFramework
        $null = [Windows.MessageBox]::Show("本机采集端未就绪。请确认 8765 端口可用，并查看 $logPath。", '菜单采集')
        exit 1
    }
    'Supervise' {
        $mutex = [Threading.Mutex]::new($false, "Local\MenuCapture-$sid")
        try {
            try {
                $entered = $mutex.WaitOne(0)
            } catch [Threading.AbandonedMutexException] {
                $entered = $true
            }
            if (-not $entered) { return }
            while (-not (Test-Path -LiteralPath $stopFile)) {
                try {
                    $active = Read-Release
                    $worker = Start-Worker $active
                    try {
                        while (-not $worker.WaitForExit(2000)) {
                            $wantStop = Test-Path -LiteralPath $stopFile
                            if ($wantStop -or ((Read-Release) -ne $active -and (Test-WorkerIdle))) {
                                # 只终止由本守护进程创建的进程。升级前等当前采集或标注结束。
                                if (-not $worker.HasExited) { $worker.Kill() }
                                $worker.WaitForExit()
                                break
                            }
                        }
                        Write-LauncherLog "采集端 $active 已停止，退出码 $($worker.ExitCode)。"
                    } finally {
                        if (-not $worker.HasExited) { $worker.Kill() }
                        $worker.Dispose()
                    }
                } catch {
                    Write-LauncherLog $_.Exception.Message
                }
                if (-not (Test-Path -LiteralPath $stopFile)) { Start-Sleep -Seconds 5 }
            }
        } finally {
            if (Get-Variable entered -ErrorAction SilentlyContinue) {
                if ($entered) { $mutex.ReleaseMutex() }
            }
            $mutex.Dispose()
        }
    }
    'Unregister' {
        if ((Test-WorkerReady) -and -not (Test-WorkerIdle)) {
            Write-LauncherLog '仍有采集或标注在执行，取消卸载。'
            exit 2
        }
        [IO.File]::WriteAllText($stopFile, 'stop')
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $startupLink -Force -ErrorAction SilentlyContinue
        # 给守护进程一个检测停止标记的周期，以便安装目录里的运行文件能够卸载。
        $mutex = [Threading.Mutex]::new($false, "Local\MenuCapture-$sid")
        try {
            try {
                if (-not $mutex.WaitOne(8000)) { exit 3 }
            } catch [Threading.AbandonedMutexException] {}
            $mutex.ReleaseMutex()
        } finally {
            $mutex.Dispose()
        }
    }
}
