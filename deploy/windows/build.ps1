# 在 Windows 构建机器上生成自包含安装包。运行安装包不需要本机预装这些构建工具。
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$')]
    [string]$Version = '0.1.0',
    [string]$Output = (Join-Path $PSScriptRoot '..\artifacts'),
    # 可选：把本次构建的 staging bundle 另存一份，供诊断与人工排查。
    # 增量更新已由 Velopack 基于 nupkg 生成，不再复用这里的 bundle。目录必须不存在，防止旧产物被误当本次构建。
    [string]$BundleOutput = '',
    # 一次性全量过渡：跳过历史包下载，并使用 Velopack 原生 --delta None，只产 Full 与 Setup。
    [Alias('full_only')]
    [switch]$FullOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$totalStopwatch = [Diagnostics.Stopwatch]::StartNew()
$channel = if ($Version -match '-') { 'win-x64-preview' } else { 'win-x64-stable' }
Set-Item Env:UV_PYTHON_DOWNLOADS never
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runner = Join-Path $repo 'app'
$runtime = Get-Content (Join-Path $PSScriptRoot 'runtimes.json') -Raw | ConvertFrom-Json
$utf8NoBom = [Text.UTF8Encoding]::new($false)

# 稳定运行环境缓存与启动器缓存的结构版本号；目录布局或清理策略变更时递增以自动失效旧缓存。
$runtimeEnvCacheSchema = 'v1'
$launcherCacheSchema = 'v1'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$([IO.Path]::GetFileName($Program)) failed with exit code $LASTEXITCODE."
    }
}

function Write-StageTiming {
    param(
        [string]$Stage,
        [Diagnostics.Stopwatch]$Stopwatch,
        [string]$Detail = ''
    )
    $elapsed = '{0:N2}s' -f $Stopwatch.Elapsed.TotalSeconds
    if ($Detail) {
        Write-Host "[build.ps1] stage=$Stage $Detail elapsed=$elapsed"
    } else {
        Write-Host "[build.ps1] stage=$Stage elapsed=$elapsed"
    }
}

function Get-StringSha256 {
    param([string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Copy-DirectoryFast {
    param([string]$Source, [string]$Destination)
    $src = $Source.TrimEnd('\')
    $dst = $Destination.TrimEnd('\')
    $null = New-Item -ItemType Directory -Path $dst -Force
    $robocopy = Get-Command 'robocopy.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($robocopy) {
        # robocopy 退出码 0..7 均表示成功，>= 8 表示发生复制失败。
        & $robocopy.Source $src $dst '/E' '/NFL' '/NDL' '/NJH' '/NJS' '/NP' '/MT:8' '/R:2' '/W:1' | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy failed ($src -> $dst) with exit code $LASTEXITCODE."
        }
        return
    }
    Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
}

function Get-MirrorUri {
    param([string]$Uri)
    # raw.githubusercontent.com 在部分网络下直连会被重置；jsDelivr 提供同仓库同提交的镜像，内容仍以哈希校验为准。
    if ($Uri -match '^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$') {
        return "https://cdn.jsdelivr.net/gh/$($Matches[1])/$($Matches[2])@$($Matches[3])/$($Matches[4])"
    }
    return $null
}

function Get-VerifiedFile {
    param([string]$Uri, [string]$Path, [string]$Sha256)
    # 可选的本地下载缓存（MC_RUNTIME_CACHE）：GitHub 直连不稳定时，可按文件名复用已校验的副本。
    $cacheDir = [Environment]::GetEnvironmentVariable('MC_RUNTIME_CACHE')
    $cached = if ($cacheDir) { Join-Path $cacheDir ([IO.Path]::GetFileName($Path)) } else { $null }
    if ($cached -and (Test-Path -LiteralPath $cached)) {
        Copy-Item -LiteralPath $cached -Destination $Path -Force
        if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ieq $Sha256) { return }
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
    # 优先用 curl.exe 流式下载：PowerShell 5.1 的 Invoke-WebRequest 会把整个文件缓存在内存里，
    # 走代理下载大文件时长时间不落盘、无法观察进度，也容易卡死。
    # runner 上可能同时存在 System32 与 Git 自带的 curl.exe，Get-Command 会返回多条，必须取第一条。
    $curlCommand = Get-Command 'curl.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $curl = if ($curlCommand) { $curlCommand.Source } else { $null }
    function Invoke-Download {
        param([string]$DownloadUri, [bool]$UseProxy)
        if ($curl) {
            $curlArgs = @('-fsSL', '--retry', '5', '--retry-all-errors', '--retry-delay', '2', '--connect-timeout', '30', '-C', '-', '-o', $Path)
            if ($UseProxy) { $curlArgs += @('--proxy', $env:HTTPS_PROXY) } else { $curlArgs += @('--noproxy', '*') }
            $curlArgs += $DownloadUri
            Invoke-Checked $curl $curlArgs
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri $DownloadUri -OutFile $Path
        }
    }
    # 代理和直连、原始地址和 CDN 镜像都试一遍：任一来源可用即可，内容仍以 SHA-256 为准。
    $mirror = Get-MirrorUri $Uri
    $attempts = @()
    if ($env:HTTPS_PROXY) { $attempts += @{ Uri = $Uri; Proxy = $true } }
    $attempts += @{ Uri = $Uri; Proxy = $false }
    if ($mirror) {
        if ($env:HTTPS_PROXY) { $attempts += @{ Uri = $mirror; Proxy = $true } }
        $attempts += @{ Uri = $mirror; Proxy = $false }
    }
    $downloaded = $false
    $lastError = $null
    foreach ($attempt in $attempts) {
        try {
            Invoke-Download $attempt.Uri $attempt.Proxy
            $downloaded = $true
            break
        } catch {
            $lastError = $_
            Write-Host "Download failed ($($attempt.Uri), proxy=$($attempt.Proxy)): $($_.Exception.Message)"
            Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $downloaded) { throw "Could not download $Uri. $($lastError.Exception.Message)" }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ine $Sha256) {
        throw "Checksum does not match for $([IO.Path]::GetFileName($Path))."
    }
    if ($cached) {
        New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
        Copy-Item -LiteralPath $Path -Destination $cached -Force
    }
}

function Find-BuildCommand {
    param([string]$Name, [string]$DefaultPath)
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    if (Test-Path -LiteralPath $DefaultPath -PathType Leaf) { return $DefaultPath }
    throw "Missing Windows build tool: $Name"
}

function Get-ReleaseNotes {
    param([string]$Version)
    # 优先摘录 CHANGELOG 中该版本的段落；缺失时使用占位文本，确保 vpk 始终拿到发布说明。
    $changelog = Join-Path $repo 'CHANGELOG.md'
    if (Test-Path -LiteralPath $changelog -PathType Leaf) {
        $lines = @(Get-Content -LiteralPath $changelog -Encoding UTF8)
        $start = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match ('^#{1,6}\s*\[?v?' + [regex]::Escape($Version) + '\]?(\s|$)')) {
                $start = $i
                break
            }
        }
        if ($start -ge 0) {
            $end = $lines.Count
            for ($j = $start + 1; $j -lt $lines.Count; $j++) {
                if ($lines[$j] -match '^#{1,2}\s') {
                    $end = $j
                    break
                }
            }
            if ($end -gt $start + 1) {
                $section = ($lines[($start + 1)..($end - 1)] -join "`n").Trim()
                if ($section) { return $section }
            }
        }
    }
    return "菜单采集工作台 $Version"
}

function Test-RuntimeEnvCache {
    param(
        [string]$CacheDir,
        [string]$ExpectedFingerprint,
        [string]$ExpectedSchema,
        [string[]]$RequiredFiles
    )
    $manifestPath = Join-Path $CacheDir '.env-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return @{ Hit = $false; Reason = '未找到 .env-manifest.json' }
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return @{ Hit = $false; Reason = ".env-manifest.json 解析失败：$($_.Exception.Message)" }
    }
    $schemaProp = $manifest.PSObject.Properties['schema_version']
    $fpProp = $manifest.PSObject.Properties['fingerprint']
    $filesProp = $manifest.PSObject.Properties['files']
    if (-not $schemaProp -or [string]$schemaProp.Value -ne $ExpectedSchema) {
        return @{ Hit = $false; Reason = '缓存结构版本不匹配' }
    }
    if (-not $fpProp -or [string]$fpProp.Value -ne $ExpectedFingerprint) {
        return @{ Hit = $false; Reason = '运行环境指纹已变化' }
    }
    if (-not $filesProp -or -not $filesProp.Value) {
        return @{ Hit = $false; Reason = '.env-manifest.json 缺少 files 校验清单' }
    }
    foreach ($dirRel in 'python', 'node', 'tesseract', 'app\ui_tars\node_modules') {
        if (-not (Test-Path -LiteralPath (Join-Path $CacheDir $dirRel) -PathType Container)) {
            return @{ Hit = $false; Reason = "缓存缺少目录：$dirRel" }
        }
    }
    $entryMap = @{}
    foreach ($item in @($filesProp.Value)) {
        if ($item -and $item.PSObject.Properties['path']) {
            $entryMap[[string]$item.path] = $item
        }
    }
    foreach ($relPath in $RequiredFiles) {
        if (-not $entryMap.ContainsKey($relPath)) {
            return @{ Hit = $false; Reason = "manifest 缺少必需校验文件：$relPath" }
        }
        $entry = $entryMap[$relPath]
        $fullPath = Join-Path $CacheDir ($relPath.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            return @{ Hit = $false; Reason = "缓存缺失必需文件：$relPath" }
        }
        $actualSize = (Get-Item -LiteralPath $fullPath).Length
        if ($actualSize -ne [int64]$entry.size) {
            return @{ Hit = $false; Reason = "缓存文件大小不一致：$relPath" }
        }
        $actualSha = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -ine [string]$entry.sha256) {
            return @{ Hit = $false; Reason = "缓存文件 SHA-256 校验失败：$relPath" }
        }
    }
    return @{ Hit = $true; Reason = '校验通过' }
}

function Save-RuntimeEnvCache {
    param(
        [string]$CacheDir,
        [string]$BundleDir,
        [string]$Fingerprint,
        [string]$SchemaVersion,
        [string]$CrtVersionName,
        [string[]]$RequiredFiles
    )
    $null = New-Item -ItemType Directory -Path $CacheDir -Force
    $manifestPath = Join-Path $CacheDir '.env-manifest.json'
    # 先移除旧 manifest，再写入目录，最后写新 manifest，避免中断留下看似有效的半成品缓存。
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    foreach ($dirRel in 'python', 'node', 'tesseract', 'app') {
        $targetDir = Join-Path $CacheDir $dirRel
        if (Test-Path -LiteralPath $targetDir) {
            Remove-Item -LiteralPath $targetDir -Recurse -Force
        }
    }
    Copy-DirectoryFast (Join-Path $BundleDir 'python') (Join-Path $CacheDir 'python')
    Copy-DirectoryFast (Join-Path $BundleDir 'node') (Join-Path $CacheDir 'node')
    Copy-DirectoryFast (Join-Path $BundleDir 'tesseract') (Join-Path $CacheDir 'tesseract')
    Copy-DirectoryFast (Join-Path $BundleDir 'app\ui_tars\node_modules') (Join-Path $CacheDir 'app\ui_tars\node_modules')

    $fileEntries = @()
    foreach ($relPath in $RequiredFiles) {
        $fullPath = Join-Path $CacheDir ($relPath.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Cannot write runtime env cache; missing required file: $relPath"
        }
        $fileEntries += [ordered]@{
            path   = $relPath
            size   = (Get-Item -LiteralPath $fullPath).Length
            sha256 = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $manifest = [ordered]@{
        schema_version = $SchemaVersion
        fingerprint    = $Fingerprint
        crt_version    = $CrtVersionName
        created_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        files          = $fileEntries
    }
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5), $utf8NoBom)
}

function Test-LauncherCache {
    param(
        [string]$CacheDir,
        [string]$ExpectedFingerprint,
        [string]$ExpectedSchema
    )
    $manifestPath = Join-Path $CacheDir '.launcher-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return @{ Hit = $false; Reason = '未找到 .launcher-manifest.json' }
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return @{ Hit = $false; Reason = ".launcher-manifest.json 解析失败：$($_.Exception.Message)" }
    }
    $schemaProp = $manifest.PSObject.Properties['schema_version']
    $fpProp = $manifest.PSObject.Properties['fingerprint']
    $exeShaProp = $manifest.PSObject.Properties['exe_sha256']
    $exeSizeProp = $manifest.PSObject.Properties['exe_size']
    $countProp = $manifest.PSObject.Properties['file_count']
    if (-not $schemaProp -or [string]$schemaProp.Value -ne $ExpectedSchema) {
        return @{ Hit = $false; Reason = '启动器缓存结构版本不匹配' }
    }
    if (-not $fpProp -or [string]$fpProp.Value -ne $ExpectedFingerprint) {
        return @{ Hit = $false; Reason = '启动器指纹已变化' }
    }
    $distDir = Join-Path $CacheDir 'dist'
    $exePath = Join-Path $distDir 'MenuCapture.exe'
    $internalDir = Join-Path $distDir '_internal'
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf) -or -not (Test-Path -LiteralPath $internalDir -PathType Container)) {
        return @{ Hit = $false; Reason = '启动器缓存缺少 MenuCapture.exe 或 _internal 目录' }
    }
    if (-not $exeSizeProp -or (Get-Item -LiteralPath $exePath).Length -ne [int64]$exeSizeProp.Value) {
        return @{ Hit = $false; Reason = 'MenuCapture.exe 大小不一致' }
    }
    $actualSha = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $exeShaProp -or $actualSha -ine [string]$exeShaProp.Value) {
        return @{ Hit = $false; Reason = 'MenuCapture.exe SHA-256 校验失败' }
    }
    $actualCount = @(Get-ChildItem -LiteralPath $distDir -Recurse -File).Count
    if (-not $countProp -or $actualCount -ne [int]$countProp.Value) {
        return @{ Hit = $false; Reason = "启动器缓存文件数量不一致（期望 $([int]$countProp.Value)，实际 $actualCount）" }
    }
    return @{ Hit = $true; Reason = '校验通过' }
}

function Save-LauncherCache {
    param(
        [string]$CacheDir,
        [string]$LauncherDistDir,
        [string]$Fingerprint,
        [string]$SchemaVersion
    )
    $null = New-Item -ItemType Directory -Path $CacheDir -Force
    $manifestPath = Join-Path $CacheDir '.launcher-manifest.json'
    $targetDist = Join-Path $CacheDir 'dist'
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $targetDist) {
        Remove-Item -LiteralPath $targetDist -Recurse -Force
    }
    Copy-DirectoryFast $LauncherDistDir $targetDist
    $exePath = Join-Path $targetDist 'MenuCapture.exe'
    $fileCount = @(Get-ChildItem -LiteralPath $targetDist -Recurse -File).Count
    $manifest = [ordered]@{
        schema_version = $SchemaVersion
        fingerprint    = $Fingerprint
        created_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        exe_size       = (Get-Item -LiteralPath $exePath).Length
        exe_sha256     = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
        file_count     = $fileCount
    }
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 4), $utf8NoBom)
}

$sevenZip = Find-BuildCommand '7z.exe' (Join-Path $env:ProgramFiles '7-Zip\7z.exe')
$uv = (Get-Command -Name 'uv.exe' -CommandType Application | Select-Object -First 1).Source
$null = New-Item -ItemType Directory -Path $Output -Force
$Output = (Resolve-Path $Output).Path
# 不覆盖已存在的产物：Velopack 的 Setup 名称不含版本号，同名文件一律视为已发布或上次构建残留。
$conflicts = @(
    Get-ChildItem -LiteralPath $Output -Filter '*-Setup.exe' -File -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $Output -Filter "*-$Version-*.nupkg" -File -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $Output -Filter "releases.$channel.json" -File -ErrorAction SilentlyContinue
)
if ($conflicts.Count -gt 0) { throw "Installer already exists: $($conflicts[0].FullName)" }
# Windows 工具链对长路径仍有限制；构建目录放在短路径下，避免 node_modules 深层文件打包失败。
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$stage = Join-Path $tempRoot ('mc-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$bundle = Join-Path $stage 'bundle'
$null = New-Item -ItemType Directory -Path $bundle

try {
    # 发布前置校验：React 是唯一业务前端，产物必须已由 web-react 构建并提交。
    # 打包脚本不运行 npm build，避免发布包在构建机临时生成、与源码不一致的产物。
    $reactEntry = Join-Path $runner 'web\react\index.html'
    $reactManifest = Join-Path $runner 'web\react\.vite\manifest.json'
    foreach ($required in $reactEntry, $reactManifest) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Missing React build output: $required. Run npm run build in web-react and commit web/react."
        }
    }
    if (Test-Path -LiteralPath (Join-Path $runner 'web\index.html')) {
        throw 'Legacy frontend web/index.html still exists; remove it before packaging.'
    }

    # 应用本地使用构建机 Visual Studio 随附的可再发行 CRT，避免依赖用户机器预装 C++ 运行库。
    # 提前解析 CRT 版本以便纳入稳定运行环境与启动器指纹。
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if (-not $vs) { throw 'Missing Visual Studio C++ redistributable files on the build machine.' }
    $crtVersion = Get-ChildItem (Join-Path $vs 'VC\Redist\MSVC') -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'x64\Microsoft.VC143.CRT\msvcp140.dll') } |
        Sort-Object { [version]$_.Name } -Descending |
        Select-Object -First 1
    if (-not $crtVersion) { throw 'Microsoft.VC143.CRT x64 files not found.' }

    # ---------- 阶段 1：稳定运行环境准备（支持可选 MC_RUNTIME_ENV_CACHE 复用） ----------
    $envStopwatch = [Diagnostics.Stopwatch]::StartNew()
    $runtimesJsonPath = Join-Path $PSScriptRoot 'runtimes.json'
    $uvLockPath = Join-Path $runner 'uv.lock'
    $uiTarsLockPath = Join-Path $runner 'ui_tars\package-lock.json'
    $preparePyScript = Join-Path $PSScriptRoot 'prepare_python_bundle.py'

    $envFingerprintParts = @(
        "schema=$runtimeEnvCacheSchema",
        "crt=$($crtVersion.Name)"
    )
    foreach ($fpFile in $runtimesJsonPath, $uvLockPath, $uiTarsLockPath, $preparePyScript) {
        if (-not (Test-Path -LiteralPath $fpFile -PathType Leaf)) {
            throw "Missing runtime environment fingerprint input: $fpFile"
        }
        $fpHash = (Get-FileHash -LiteralPath $fpFile -Algorithm SHA256).Hash.ToLowerInvariant()
        $envFingerprintParts += "$([IO.Path]::GetFileName($fpFile))=$fpHash"
    }
    $runtimeEnvFingerprint = Get-StringSha256 ($envFingerprintParts -join "`n")

    $requiredEnvFiles = @(
        'python/python.exe',
        'node/node.exe',
        'tesseract/tesseract.exe',
        'tesseract/tessdata/chi_sim.traineddata',
        'tesseract/tessdata/eng.traineddata',
        'app/ui_tars/node_modules/@gui-agent/agent-sdk/package.json'
    )

    $envCacheDir = [Environment]::GetEnvironmentVariable('MC_RUNTIME_ENV_CACHE')
    $runtimeEnvCacheHit = $false
    if ($envCacheDir) {
        $check = Test-RuntimeEnvCache -CacheDir $envCacheDir -ExpectedFingerprint $runtimeEnvFingerprint `
            -ExpectedSchema $runtimeEnvCacheSchema -RequiredFiles $requiredEnvFiles
        if ($check.Hit) {
            try {
                Copy-DirectoryFast (Join-Path $envCacheDir 'python') (Join-Path $bundle 'python')
                Copy-DirectoryFast (Join-Path $envCacheDir 'node') (Join-Path $bundle 'node')
                Copy-DirectoryFast (Join-Path $envCacheDir 'tesseract') (Join-Path $bundle 'tesseract')
                Copy-DirectoryFast (Join-Path $envCacheDir 'app\ui_tars\node_modules') (Join-Path $bundle 'app\ui_tars\node_modules')
                $runtimeEnvCacheHit = $true
                Write-Host "[build.ps1] 命中稳定运行环境缓存（fingerprint=$($runtimeEnvFingerprint.Substring(0, 12))）。"
            } catch {
                Write-Warning "从 MC_RUNTIME_ENV_CACHE 恢复失败（$($_.Exception.Message)），已清理并回退完整冷构建。"
                foreach ($partialDir in 'python', 'node', 'tesseract', 'app') {
                    $partialPath = Join-Path $bundle $partialDir
                    if (Test-Path -LiteralPath $partialPath) {
                        Remove-Item -LiteralPath $partialPath -Recurse -Force
                    }
                }
            }
        } elseif (Test-Path -LiteralPath $envCacheDir) {
            Write-Warning "MC_RUNTIME_ENV_CACHE 未命中或校验未通过（$($check.Reason)），回退完整冷构建。"
        }
    }

    $pythonDir = Join-Path $bundle 'python'
    $python = Join-Path $pythonDir 'python.exe'
    $nodeDir = Join-Path $bundle 'node'
    $ocrDir = Join-Path $bundle 'tesseract'
    $tessdata = Join-Path $ocrDir 'tessdata'
    $app = Join-Path $bundle 'app'
    $adapter = Join-Path $app 'ui_tars'

    if (-not $runtimeEnvCacheHit) {
        $pythonTar = Join-Path $stage 'python.tar.gz'
        Get-VerifiedFile $runtime.python.url $pythonTar $runtime.python.sha256
        Invoke-Checked 'tar.exe' @('-xzf', $pythonTar, '-C', $bundle)
        Copy-Item (Join-Path $crtVersion.FullName 'x64\Microsoft.VC143.CRT\*.dll') $pythonDir -Force

        $requirements = Join-Path $stage 'requirements.txt'
        Invoke-Checked $uv @('export', '--project', $runner, '--locked', '--no-dev', '--no-emit-project', '--output-file', $requirements)
        Invoke-Checked $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--only-binary=:all:', '--require-hashes', '-r', $requirements)
        Get-ChildItem -LiteralPath $pythonDir -Filter '*.pdb' -Recurse -File | Remove-Item -Force
        # 提前规范化 Python 运行时目录（清除未入 PATH 的 Scripts 启动器与残余字节码），保证缓存内容干净且幂等。
        Invoke-Checked $python @('-B', $preparePyScript, $pythonDir)

        $nodeZip = Join-Path $stage 'node.zip'
        Get-VerifiedFile $runtime.node.url $nodeZip $runtime.node.sha256
        Expand-Archive -LiteralPath $nodeZip -DestinationPath $stage
        $nodeArchiveRoot = Join-Path $stage "node-v$($runtime.node.version)-win-x64"
        $null = New-Item -ItemType Directory -Path $nodeDir -Force
        Copy-Item -LiteralPath (Join-Path $nodeArchiveRoot 'node.exe'), (Join-Path $nodeArchiveRoot 'LICENSE') -Destination $nodeDir -Force

        $ocrExe = Join-Path $stage 'tesseract-installer.exe'
        Get-VerifiedFile $runtime.tesseract.url $ocrExe $runtime.tesseract.sha256
        $ocrUnpack = Join-Path $stage 'tesseract-unpacked'
        Invoke-Checked $sevenZip @('x', '-y', "-o$ocrUnpack", $ocrExe)
        $null = New-Item -ItemType Directory -Path $tessdata -Force
        Get-ChildItem -LiteralPath $ocrUnpack -File |
            Where-Object { $_.Name -eq 'tesseract.exe' -or $_.Extension -in '.dll', '.txt', '.md' } |
            Copy-Item -Destination $ocrDir -Force
        foreach ($file in $runtime.tessdata.files.PSObject.Properties) {
            Get-VerifiedFile ($runtime.tessdata.base_url + $file.Name) (Join-Path $tessdata $file.Name) $file.Value
        }

        # 仅拷贝锁文件安装 Node 运行时依赖，随后立刻裁剪不参与运行的快照/sourcemap/类型声明。
        $null = New-Item -ItemType Directory -Path $adapter -Force
        Copy-Item -LiteralPath (Join-Path $runner 'ui_tars\package.json'), $uiTarsLockPath -Destination $adapter -Force
        $npm = Join-Path $nodeArchiveRoot 'node_modules\npm\bin\npm-cli.js'
        $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
        $savedPath = $env:PATH
        $env:PATH = "$nodeDir;$savedPath"
        try {
            Invoke-Checked (Join-Path $nodeDir 'node.exe') @($npm, 'ci', '--omit=dev', '--no-audit', '--no-fund', '--prefix', $adapter)
        } finally {
            $env:PATH = $savedPath
        }
        $nodeModulesDir = Join-Path $adapter 'node_modules'
        Get-ChildItem -LiteralPath $nodeModulesDir -Recurse -Force -Directory -Filter '__image_snapshots__' |
            Remove-Item -Recurse -Force
        Get-ChildItem -LiteralPath $nodeModulesDir -Recurse -Force -File |
            Where-Object {
                $_.Extension -eq '.map' -or
                $_.Name -like '*.d.ts' -or $_.Name -like '*.d.mts' -or $_.Name -like '*.d.cts'
            } |
            Remove-Item -Force

        if ($envCacheDir) {
            try {
                Save-RuntimeEnvCache -CacheDir $envCacheDir -BundleDir $bundle `
                    -Fingerprint $runtimeEnvFingerprint -SchemaVersion $runtimeEnvCacheSchema `
                    -CrtVersionName $crtVersion.Name -RequiredFiles $requiredEnvFiles
                Write-Host "[build.ps1] 已写入稳定运行环境缓存（fingerprint=$($runtimeEnvFingerprint.Substring(0, 12))）。"
            } catch {
                Write-Warning "写入 MC_RUNTIME_ENV_CACHE 失败（$($_.Exception.Message)），不影响本次安装包构建。"
            }
        }
    }
    $env:PATH = "$nodeDir;$env:PATH"
    $env:TESSERACT_CMD = Join-Path $ocrDir 'tesseract.exe'
    $env:TESSDATA_PREFIX = $tessdata
    Write-StageTiming 'runtime_env_prepare' $envStopwatch ("mode=$(if ($runtimeEnvCacheHit) { 'cache_hit' } else { 'cold_build' })")

    # ---------- 阶段 2：叠加最新业务代码、前端产物、Prompt 与发布清单 ----------
    $overlayStopwatch = [Diagnostics.Stopwatch]::StartNew()
    $null = New-Item -ItemType Directory -Path $app, $adapter -Force
    foreach ($folder in 'menu_capture', 'web') {
        $targetFolder = Join-Path $app $folder
        if (Test-Path -LiteralPath $targetFolder) {
            Remove-Item -LiteralPath $targetFolder -Recurse -Force
        }
        Copy-Item -LiteralPath (Join-Path $runner $folder) -Destination $app -Recurse -Force
    }
    Get-ChildItem -LiteralPath $app -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath (Join-Path $runner 'ui_tars') -File |
        Where-Object { $_.Extension -in '.mjs', '.json' } |
        Copy-Item -Destination $adapter -Force
    $promptsTarget = Join-Path $adapter 'prompts'
    if (Test-Path -LiteralPath $promptsTarget) {
        Remove-Item -LiteralPath $promptsTarget -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $runner 'ui_tars\prompts') -Destination $adapter -Recurse -Force

    # 发布清单：记录版本、源码提交、前端入口与哈希，升级/回滚后可用 /api/version 与文件哈希核对。
    $frontendEntry = (Get-Content (Join-Path $app 'web\react\.vite\manifest.json') -Raw | ConvertFrom-Json).
        PSObject.Properties.Value | Where-Object { $_.isEntry } | Select-Object -First 1
    if (-not $frontendEntry) { throw 'React manifest has no entry.' }
    $frontendAsset = $frontendEntry.file
    $releaseManifest = [ordered]@{
        version          = $Version
        built_at         = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        git_commit       = (& git -C $repo rev-parse HEAD 2>$null)
        frontend_entry   = $frontendAsset
        frontend_sha256  = (Get-FileHash -LiteralPath (Join-Path $app "web\react\$($frontendAsset.Replace('/', '\'))") -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    [IO.File]::WriteAllText((Join-Path $bundle 'release-manifest.json'), ($releaseManifest | ConvertTo-Json), $utf8NoBom)

    # 构建机也使用将要分发的可执行文件做校验；禁止回退到全局 Python、Node 或 OCR。
    $env:PYTHONNOUSERSITE = '1'
    Push-Location $app
    try {
        Invoke-Checked $python @('-c', 'import fastapi, cv2, zxingcpp, screeninfo, rapidocr_onnxruntime, menu_capture.api.server')
        Invoke-Checked (Join-Path $nodeDir 'node.exe') @('--version')
        Invoke-Checked $env:TESSERACT_CMD @('--version')
        $languages = & $env:TESSERACT_CMD --list-langs
        if ($LASTEXITCODE -ne 0 -or $languages -notcontains 'chi_sim' -or $languages -notcontains 'eng') {
            throw 'Bundled Tesseract cannot load both chi_sim and eng.'
        }
        Push-Location $adapter
        try {
            Invoke-Checked (Join-Path $nodeDir 'node.exe') @('--input-type=module', '-e', "await import('@gui-agent/agent-sdk'); await import('./prompts/base.mjs'); await import('./prompts/business.mjs')")
        } finally {
            Pop-Location
        }
    } finally {
        Pop-Location
    }
    # 清理构建期导入生成的 Python 字节码和未进入运行 PATH 的 Scripts 启动器。
    # 运行时、依赖代码及版本指纹保持受限；首个干净基线若有变化会安全走全量安装。
    Invoke-Checked $python @(
        '-B',
        $preparePyScript,
        $pythonDir,
        '--app-root',
        $app
    )
    Copy-Item -LiteralPath $runtimesJsonPath -Destination $bundle -Force
    Set-Content -LiteralPath (Join-Path $bundle 'vc-runtime-version.txt') -Value $crtVersion.Name -Encoding ASCII
    Write-StageTiming 'app_overlay' $overlayStopwatch

    # ---------- 阶段 3：PyInstaller 启动器构建（支持可选 MC_LAUNCHER_CACHE 复用） ----------
    $launcherStopwatch = [Diagnostics.Stopwatch]::StartNew()
    # 启动器为 PyInstaller onedir：用临时目录中的独立虚拟环境安装打包工具与 Velopack Python SDK（1.2.161），
    # 避免把 PyInstaller 等构建依赖带进随包分发的 python/ 运行时。产物（MenuCapture.exe + _internal）复制到
    # bundle 根目录后作为 vpk 的 mainExe。
    $launcherSource = Join-Path $PSScriptRoot 'MenuCaptureLauncher.py'
    if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) {
        throw "Missing launcher source: $launcherSource"
    }
    $launcherFpParts = @(
        "schema=$launcherCacheSchema",
        "crt=$($crtVersion.Name)",
        "launcher=$((Get-FileHash -LiteralPath $launcherSource -Algorithm SHA256).Hash.ToLowerInvariant())",
        "uv_lock=$((Get-FileHash -LiteralPath $uvLockPath -Algorithm SHA256).Hash.ToLowerInvariant())",
        "runtimes=$((Get-FileHash -LiteralPath $runtimesJsonPath -Algorithm SHA256).Hash.ToLowerInvariant())"
    )
    $launcherFingerprint = Get-StringSha256 ($launcherFpParts -join "`n")

    $launcherCacheDir = [Environment]::GetEnvironmentVariable('MC_LAUNCHER_CACHE')
    $launcherCacheHit = $false
    if ($launcherCacheDir) {
        $launcherCheck = Test-LauncherCache -CacheDir $launcherCacheDir `
            -ExpectedFingerprint $launcherFingerprint -ExpectedSchema $launcherCacheSchema
        if ($launcherCheck.Hit) {
            try {
                Copy-DirectoryFast (Join-Path $launcherCacheDir 'dist') $bundle
                $launcherCacheHit = $true
                Write-Host "[build.ps1] 命中启动器产物缓存（fingerprint=$($launcherFingerprint.Substring(0, 12))）。"
            } catch {
                Write-Warning "从 MC_LAUNCHER_CACHE 恢复失败（$($_.Exception.Message)），回退冷构建。"
                Remove-Item -LiteralPath (Join-Path $bundle 'MenuCapture.exe') -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath (Join-Path $bundle '_internal') -Recurse -Force -ErrorAction SilentlyContinue
            }
        } elseif (Test-Path -LiteralPath $launcherCacheDir) {
            Write-Warning "MC_LAUNCHER_CACHE 未命中或校验未通过（$($launcherCheck.Reason)），回退冷构建。"
        }
    }

    if (-not $launcherCacheHit) {
        $launcherVenv = Join-Path $stage 'launcher-venv'
        Invoke-Checked $python @('-m', 'venv', $launcherVenv)
        $launcherPython = Join-Path $launcherVenv 'Scripts\python.exe'
        # 构建工具同样由 uv.lock 锁定；禁止在这里安装未固定版本的最新 PyInstaller。
        $buildRequirements = Join-Path $stage 'launcher-requirements.txt'
        Invoke-Checked $uv @(
            'export', '--project', $runner, '--locked', '--only-group', 'build',
            '--no-emit-project', '--output-file', $buildRequirements
        )
        Invoke-Checked $launcherPython @(
            '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
            '--require-hashes', '-r', $buildRequirements
        )
        $launcherDist = Join-Path $stage 'launcher-dist'
        # 启动器只负责拉起并守护本地服务；显式排除运行时已自带的大模块，避免在 _internal 中重复打包。
        $launcherExcludes = 'cv2', 'onnxruntime', 'rapidocr_onnxruntime', 'fastapi', 'uvicorn', 'starlette',
            'pydantic', 'numpy', 'pandas', 'screeninfo', 'zxingcpp', 'playwright', 'menu_capture'
        $pyInstallerArgs = @(
            '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'MenuCapture',
            '--distpath', $launcherDist,
            '--workpath', (Join-Path $stage 'launcher-build'),
            '--specpath', $stage,
            '--collect-all', 'velopack',
            '--collect-all', 'cryptography',
            '--hidden-import', 'sqlite3',
            '--hidden-import', 'urllib.request',
            '--hidden-import', 'urllib.error'
        )
        foreach ($module in $launcherExcludes) { $pyInstallerArgs += @('--exclude-module', $module) }
        $pyInstallerArgs += $launcherSource
        Invoke-Checked $launcherPython $pyInstallerArgs
        $launcherDir = Join-Path $launcherDist 'MenuCapture'
        if (-not (Test-Path -LiteralPath (Join-Path $launcherDir 'MenuCapture.exe') -PathType Leaf)) {
            throw 'PyInstaller did not produce MenuCapture.exe.'
        }
        Copy-DirectoryFast $launcherDir $bundle
        if ($launcherCacheDir) {
            try {
                Save-LauncherCache -CacheDir $launcherCacheDir -LauncherDistDir $launcherDir `
                    -Fingerprint $launcherFingerprint -SchemaVersion $launcherCacheSchema
                Write-Host "[build.ps1] 已写入启动器产物缓存（fingerprint=$($launcherFingerprint.Substring(0, 12))）。"
            } catch {
                Write-Warning "写入 MC_LAUNCHER_CACHE 失败（$($_.Exception.Message)），不影响本次安装包构建。"
            }
        }
    }

    # 更新前会把协调器复制到业务安装目录之外；源文件必须随同一发布包交付。
    $watchdogSource = Join-Path $PSScriptRoot 'update-watchdog.ps1'
    if (-not (Test-Path -LiteralPath $watchdogSource -PathType Leaf)) {
        throw "Missing external recovery coordinator: $watchdogSource"
    }
    Copy-Item -LiteralPath $watchdogSource -Destination (Join-Path $bundle 'update-watchdog.ps1') -Force
    Write-StageTiming 'launcher_build' $launcherStopwatch ("mode=$(if ($launcherCacheHit) { 'cache_hit' } else { 'cold_build' })")

    # Velopack CLI 固定 1.2.161：已预装时只校验版本，缺失时按锁定版本安装，避免产物行为漂移。
    $dotnet = Find-BuildCommand 'dotnet.exe' (Join-Path $env:ProgramFiles 'dotnet\dotnet.exe')
    $vpkCommand = Get-Command 'vpk' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($vpkCommand) {
        $vpk = $vpkCommand.Source
    } else {
        Invoke-Checked $dotnet @('tool', 'install', '-g', 'vpk', '--version', '1.2.161')
        $vpk = Join-Path $env:USERPROFILE '.dotnet\tools\vpk.exe'
    }
    $toolList = (& $dotnet tool list -g | Out-String)
    if ($toolList -notmatch '(?m)^\s*vpk\s+1\.2\.161\b') { throw "vpk must be 1.2.161; installed tools:`n$toolList" }

    $releaseNotes = Join-Path $stage 'release-notes.md'
    [IO.File]::WriteAllText($releaseNotes, (Get-ReleaseNotes $Version), $utf8NoBom)

    # ---------- 阶段 4：拉取历史包用于 Velopack Delta ----------
    $vpkDownloadStopwatch = [Diagnostics.Stopwatch]::StartNew()
    # 历史包只用于生成 delta，放在临时目录，避免旧版本 nupkg 混入本次发布产物。
    $packDir = Join-Path $stage 'velopack'
    $null = New-Item -ItemType Directory -Path $packDir
    if ($FullOnly) {
        # 全量过渡不生成 Delta，也就不需要下载任何历史包。
        Write-Host '[build.ps1] FullOnly：跳过历史包下载，本次只生成 Full 与 Setup。'
    } else {
        $token = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } elseif ($env:GH_TOKEN) { $env:GH_TOKEN } else { '' }
        $downloadArgs = @(
            'download', 'github',
            '--repoUrl', 'https://github.com/zhaoyongze123/wechat-miniapp-menu-agent',
            '--channel', $channel,
            '--outputDir', $packDir
        )
        if ($channel -eq 'win-x64-preview') { $downloadArgs += '--pre' }
        if ($token) { $downloadArgs += @('--token', $token) }
        & $vpk @downloadArgs
        if ($LASTEXITCODE -ne 0) {
            # SDK 失败可能是首次发布，也可能是网络/认证错误；不把失败当作已确认无历史包。
            Write-Warning "历史包获取失败（channel=$channel，vpk download 退出码 $LASTEXITCODE）；本次仅保证生成完整包，请检查上方日志。"
            Remove-Item -LiteralPath $packDir -Recurse -Force
            $null = New-Item -ItemType Directory -Path $packDir
        }
    }
    Write-StageTiming 'vpk_download' $vpkDownloadStopwatch

    # ---------- 阶段 5：Velopack 打包与资产摘要输出 ----------
    $vpkPackStopwatch = [Diagnostics.Stopwatch]::StartNew()
    $packArgs = @(
        'pack',
        '-u', 'Yipu.MenuCapture',
        '-v', $Version,
        '-p', $bundle,
        '-e', 'MenuCapture.exe',
        '--packTitle', '菜单采集工作台',
        '--packAuthors', '意谱科技',
        '--releaseNotes', $releaseNotes,
        '-c', $channel,
        '--outputDir', $packDir
    )
    if ($FullOnly) { $packArgs += @('--delta', 'None') }
    Invoke-Checked $vpk $packArgs
    Write-StageTiming 'vpk_pack' $vpkPackStopwatch

    $artifactStopwatch = [Diagnostics.Stopwatch]::StartNew()
    # 复用同一索引解析器选择本次资产；不依赖 vpk 文件名的通道后缀。
    $assetArgs = @('-B', (Join-Path $PSScriptRoot 'velopack_assets.py'), $packDir, $Version)
    if ($FullOnly) { $assetArgs += '--full-only' }
    $assetJson = Invoke-Checked $python $assetArgs
    $products = @($assetJson | ConvertFrom-Json)
    if ($FullOnly) {
        $deltaArtifacts = @($products | Where-Object { $_ -like '*-delta.nupkg' })
        if ($deltaArtifacts.Count -gt 0) {
            throw "FullOnly 构建仍产出 Delta：$($deltaArtifacts -join ', ')"
        }
    }
    foreach ($name in $products) {
        $source = Join-Path $packDir $name
        $target = Join-Path $Output $name
        if (Test-Path -LiteralPath $target) { throw "Artifact already exists: $target" }
        Copy-Item -LiteralPath $source -Destination $target
        $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        Set-Content -LiteralPath "$target.sha256" -Value "$hash  $name" -Encoding ASCII
        Write-Host "Artifact: $target"
    }
    if ($BundleOutput) {
        $bundleOutputPath = [IO.Path]::GetFullPath($BundleOutput)
        if (Test-Path -LiteralPath $bundleOutputPath) { throw "Bundle output already exists: $bundleOutputPath" }
        $bundleParent = Split-Path -Parent $bundleOutputPath
        $null = New-Item -ItemType Directory -Path $bundleParent -Force
        Copy-DirectoryFast $bundle $bundleOutputPath
        Write-Host "Bundle: $bundleOutputPath"
    }
    Write-StageTiming 'artifacts' $artifactStopwatch
    Write-StageTiming 'total' $totalStopwatch
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
