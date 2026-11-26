<#
.SYNOPSIS
独立于 Velopack current 目录运行的恢复看门狗。

.DESCRIPTION
本脚本不解包、不切换版本，也不执行自制回滚。若事务协调器经 PID 与 OS 创建时间确认已退出，
且事务（包括 stopping 阶段）超过约定的无进展时限，则在安装事务锁内把状态推进到 recovery_required。
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ControlFile)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-JsonAtomic {
    param([string]$Path, [object]$Payload)
    $parent = Split-Path -Parent $Path
    $null = New-Item -ItemType Directory -Path $parent -Force
    $temp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $json = ConvertTo-Json -InputObject $Payload -Depth 32
    [IO.File]::WriteAllText($temp, $json, [Text.UTF8Encoding]::new($false))
    try {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $backup = "$Path.$([guid]::NewGuid().ToString('N')).bak"
            [IO.File]::Replace($temp, $Path, $backup, $true)
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        } else {
            [IO.File]::Move($temp, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Write-WatchdogLog {
    param([string]$Path, [string]$Message)
    $line = "{0:o} {1}{2}" -f [DateTime]::UtcNow, $Message, [Environment]::NewLine
    [IO.File]::AppendAllText($Path, $line, [Text.UTF8Encoding]::new($false))
}

function Get-ProcessIdentityState {
    param([int]$Pid, [double]$StartedAt, [string]$ExpectedExecutable)
    if ($Pid -le 0 -or $StartedAt -le 0) { return 'unknown' }
    try {
        $process = Get-Process -Id $Pid -ErrorAction Stop
        $actualStart = [DateTimeOffset]$process.StartTime.ToUniversalTime()
        $actualEpoch = $actualStart.ToUnixTimeMilliseconds() / 1000.0
        if ([Math]::Abs($actualEpoch - $StartedAt) -gt 0.25) { return 'dead' }
        if ($ExpectedExecutable) {
            $actualPath = $process.Path
            if (-not $actualPath) { return 'unknown' }
            if (-not [string]::Equals($actualPath, $ExpectedExecutable,
                    [StringComparison]::OrdinalIgnoreCase)) { return 'dead' }
        }
        return 'alive'
    } catch [ArgumentException] {
        return 'dead'
    } catch {
        # 权限、WMI 或进程查询异常均为 unknown，绝不据此接管或宣告退出。
        return 'unknown'
    }
}

function Set-RecoveryRequired {
    param([object]$Control, [string]$JournalPath, [string]$LockPath, [string]$LogPath,
          [int]$WatchdogPid)
    $stream = $null
    $locked = $false
    try {
        $stream = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        if ($stream.Length -eq 0) { $stream.WriteByte(0); $stream.Flush() }
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        while (-not $locked -and [DateTime]::UtcNow -lt $deadline) {
            try { $stream.Lock(0, 1); $locked = $true }
            catch [IO.IOException] { Start-Sleep -Milliseconds 50 }
        }
        if (-not $locked) { return }

        $journal = Get-Content -LiteralPath $JournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($journal.transaction_id -ne $Control.transaction_id
                -or $journal.target_version -ne $Control.target_version
                -or $journal.target_commit -ne $Control.target_commit
                -or $journal.previous_version -ne $Control.previous_version
                -or $journal.previous_commit -ne $Control.previous_commit
                -or $journal.status -notin @('applying', 'verifying', 'rolling_back')) {
            return
        }
        $coordinator = $journal.coordinator
        if ($null -eq $coordinator -or $null -eq $coordinator.pid -or
                $null -eq $coordinator.process_started_at) { return }
        $coordinatorPid = [int]$coordinator.pid
        $coordinatorStarted = [double]$coordinator.process_started_at
        $state = Get-ProcessIdentityState -Pid $coordinatorPid -StartedAt $coordinatorStarted
        if ($state -ne 'dead') { return }

        $updated = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse([string]$journal.updated_at, [ref]$updated)) { return }
        $age = ([DateTimeOffset]::UtcNow - $updated.ToUniversalTime()).TotalSeconds
        if ($age -lt [double]$Control.stale_after_seconds) { return }

        $journal.status = 'recovery_required'
        $journal.error = '外部看门狗确认安装协调器已退出且事务长时间无进展'
        $journal.message = '保留恢复闸门；看门狗不会自行切换版本或解包恢复包'
        $journal.finished_at = ''
        $journal.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
        if ($null -eq $journal.evidence) { $journal.evidence = [pscustomobject]@{} }
        $journal.evidence | Add-Member -NotePropertyName watchdog_detected_at `
            -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString('o')) -Force
        $journal.evidence | Add-Member -NotePropertyName watchdog_owner_pid `
            -NotePropertyValue $WatchdogPid -Force
        $journal.evidence | Add-Member -NotePropertyName failed_coordinator_pid `
            -NotePropertyValue $coordinatorPid -Force
        $journal.evidence | Add-Member -NotePropertyName failed_coordinator_started_at `
            -NotePropertyValue $coordinatorStarted -Force
        $journal.evidence | Add-Member -NotePropertyName watchdog_observed_state `
            -NotePropertyValue $state -Force
        Write-JsonAtomic -Path $JournalPath -Payload $journal
        Write-WatchdogLog -Path $LogPath -Message '事务已进入 recovery_required；等待人工恢复。'
    } catch {
        Write-WatchdogLog -Path $LogPath -Message "无法安全写入恢复阻断状态：$_"
    } finally {
        if ($locked -and $null -ne $stream) { try { $stream.Unlock(0, 1) } catch {} }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

$control = Get-Content -LiteralPath (Resolve-Path -LiteralPath $ControlFile) -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($field in @('transaction_id', 'owner_token', 'target_version', 'target_commit',
        'previous_version', 'previous_commit', 'install_root', 'data_root', 'launcher_pid',
        'stale_after_seconds', 'watchdog_nonce')) {
    if ($null -eq $control.$field -or [string]::IsNullOrWhiteSpace([string]$control.$field)) {
        throw "恢复协调器控制文件缺少 $field。"
    }
}
$recoveryRoot = Split-Path -Parent (Resolve-Path -LiteralPath $ControlFile).Path
$logPath = Join-Path $recoveryRoot 'watchdog.log'
$readyPath = Join-Path $recoveryRoot 'watchdog-ready.json'
$journalPath = Join-Path $control.data_root 'data\update-install.json'
$lockPath = "$journalPath.lock"
$selfPid = $PID
$selfProcess = Get-Process -Id $selfPid -ErrorAction Stop
$selfStarted = ([DateTimeOffset]$selfProcess.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
Write-JsonAtomic -Path $readyPath -Payload @{
    transaction_id = [string]$control.transaction_id
    owner_token = [string]$control.owner_token
    watchdog_nonce = [string]$control.watchdog_nonce
    pid = $selfPid
    process_started_at = $selfStarted
    ready_at = [DateTimeOffset]::UtcNow.ToString('o')
}
Write-WatchdogLog -Path $logPath -Message "协调器就绪，PID $selfPid。"

while ($true) {
    try {
        $journal = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($journal.transaction_id -ne $control.transaction_id) { break }
        if ($journal.status -in @('completed', 'failed', 'rolled_back', 'recovery_required')) { break }
        if ($journal.status -in @('stopping', 'applying', 'verifying', 'rolling_back')) {
            Set-RecoveryRequired -Control $control -JournalPath $journalPath -LockPath $lockPath `
                -LogPath $logPath -WatchdogPid $selfPid
        }
    } catch {
        Write-WatchdogLog -Path $logPath -Message "监控事务失败，保持恢复闸门：$_"
    }
    Start-Sleep -Seconds 5
}
