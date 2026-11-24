# 在 Windows 构建机器上生成自包含安装包。运行安装包不需要本机预装这些构建工具。
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$')]
    [string]$Version = '0.1.0',
    [string]$Output = (Join-Path $PSScriptRoot '..\artifacts')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Item Env:UV_PYTHON_DOWNLOADS never
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runner = Join-Path $repo 'UFS_Menu_Scraper\macos_runner'
$runtime = Get-Content (Join-Path $PSScriptRoot 'runtimes.json') -Raw | ConvertFrom-Json

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$([IO.Path]::GetFileName($Program)) failed with exit code $LASTEXITCODE."
    }
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
    $cacheDir = $env:MC_RUNTIME_CACHE
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
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if (Test-Path -LiteralPath $DefaultPath -PathType Leaf) { return $DefaultPath }
    throw "Missing Windows build tool: $Name"
}

$sevenZip = Find-BuildCommand '7z.exe' (Join-Path $env:ProgramFiles '7-Zip\7z.exe')
$compiler = Find-BuildCommand 'ISCC.exe' (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
$uv = (Get-Command -Name 'uv.exe' -CommandType Application).Source
$null = New-Item -ItemType Directory -Path $Output -Force
$Output = (Resolve-Path $Output).Path
$asset = Join-Path $Output "MenuCapture-Setup-$Version-win-x64.exe"
if (Test-Path -LiteralPath $asset) { throw "Installer already exists: $asset" }
# ISCC 编译器不支持超过 260 字符的源路径；构建目录放在短路径下，避免 node_modules 深层文件打包失败。
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

    $pythonTar = Join-Path $stage 'python.tar.gz'
    Get-VerifiedFile $runtime.python.url $pythonTar $runtime.python.sha256
    Invoke-Checked 'tar.exe' @('-xzf', $pythonTar, '-C', $bundle)
    $pythonDir = Join-Path $bundle 'python'
    $python = Join-Path $pythonDir 'python.exe'

    # 应用本地使用构建机 Visual Studio 随附的可再发行 CRT，避免依赖用户机器预装 C++ 运行库。
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if (-not $vs) { throw 'Missing Visual Studio C++ redistributable files on the build machine.' }
    $crtVersion = Get-ChildItem (Join-Path $vs 'VC\Redist\MSVC') -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'x64\Microsoft.VC143.CRT\msvcp140.dll') } |
        Sort-Object { [version]$_.Name } -Descending |
        Select-Object -First 1
    if (-not $crtVersion) { throw 'Microsoft.VC143.CRT x64 files not found.' }
    Copy-Item (Join-Path $crtVersion.FullName 'x64\Microsoft.VC143.CRT\*.dll') $pythonDir -Force

    $requirements = Join-Path $stage 'requirements.txt'
    Invoke-Checked $uv @('export', '--project', $runner, '--locked', '--no-dev', '--no-emit-project', '--output-file', $requirements)
    Invoke-Checked $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--only-binary=:all:', '--require-hashes', '-r', $requirements)
    Get-ChildItem -LiteralPath $pythonDir -Filter '*.pdb' -Recurse -File | Remove-Item -Force

    $nodeZip = Join-Path $stage 'node.zip'
    Get-VerifiedFile $runtime.node.url $nodeZip $runtime.node.sha256
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $stage
    $nodeArchiveRoot = Join-Path $stage "node-v$($runtime.node.version)-win-x64"
    $nodeDir = Join-Path $bundle 'node'
    New-Item -ItemType Directory -Path $nodeDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $nodeArchiveRoot 'node.exe'), (Join-Path $nodeArchiveRoot 'LICENSE') -Destination $nodeDir
    $env:PATH = "$nodeDir;$env:PATH"

    $ocrExe = Join-Path $stage 'tesseract-installer.exe'
    Get-VerifiedFile $runtime.tesseract.url $ocrExe $runtime.tesseract.sha256
    $ocrUnpack = Join-Path $stage 'tesseract-unpacked'
    Invoke-Checked $sevenZip @('x', '-y', "-o$ocrUnpack", $ocrExe)
    $ocrDir = Join-Path $bundle 'tesseract'
    $tessdata = Join-Path $ocrDir 'tessdata'
    New-Item -ItemType Directory -Path $tessdata -Force | Out-Null
    Get-ChildItem -LiteralPath $ocrUnpack -File |
        Where-Object { $_.Name -eq 'tesseract.exe' -or $_.Extension -in '.dll', '.txt', '.md' } |
        Copy-Item -Destination $ocrDir
    foreach ($file in $runtime.tessdata.files.PSObject.Properties) {
        Get-VerifiedFile ($runtime.tessdata.base_url + $file.Name) (Join-Path $tessdata $file.Name) $file.Value
    }
    $env:TESSERACT_CMD = Join-Path $ocrDir 'tesseract.exe'
    $env:TESSDATA_PREFIX = $tessdata

    $app = Join-Path $bundle 'app'
    $adapter = Join-Path $app 'ui_tars'
    New-Item -ItemType Directory -Path $app, $adapter | Out-Null
    foreach ($folder in 'menu_capture', 'web') {
        Copy-Item -LiteralPath (Join-Path $runner $folder) -Destination $app -Recurse
    }
    Get-ChildItem -LiteralPath $app -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath (Join-Path $runner 'ui_tars') -File |
        Where-Object { $_.Extension -in '.mjs', '.json' } |
        Copy-Item -Destination $adapter
    Copy-Item -LiteralPath (Join-Path $runner 'ui_tars\prompts') -Destination $adapter -Recurse

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
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText((Join-Path $bundle 'release-manifest.json'), ($releaseManifest | ConvertTo-Json), $utf8NoBom)

    $npm = Join-Path $nodeArchiveRoot 'node_modules\npm\bin\npm-cli.js'
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
    Invoke-Checked (Join-Path $nodeDir 'node.exe') @($npm, 'ci', '--omit=dev', '--no-audit', '--no-fund', '--prefix', $adapter)

    # 安装包只保留运行时文件：测试快照、source map 和类型声明不参与运行，还会撑大体积并产生超长路径。
    Get-ChildItem -LiteralPath $adapter -Recurse -Force -Directory -Filter '__image_snapshots__' |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $adapter -Recurse -Force -File |
        Where-Object {
            $_.Extension -eq '.map' -or
            $_.Name -like '*.d.ts' -or $_.Name -like '*.d.mts' -or $_.Name -like '*.d.cts'
        } |
        Remove-Item -Force

    # 构建机也使用将要分发的可执行文件做校验；禁止回退到全局 Python、Node 或 OCR。
    $env:PYTHONNOUSERSITE = '1'
    Push-Location $app
    try {
        Invoke-Checked $python @('-c', 'import fastapi, cv2, zxingcpp, screeninfo, rapidocr_onnxruntime, menu_capture.server')
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
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'runtimes.json') -Destination $bundle
    Set-Content -LiteralPath (Join-Path $bundle 'vc-runtime-version.txt') -Value $crtVersion.Name -Encoding ASCII
    $language = Join-Path $stage 'ChineseSimplified.isl'
    Get-VerifiedFile $runtime.installer_language.url $language $runtime.installer_language.sha256
    # Inno Setup 与 Windows PowerShell 5.1 需要 BOM 才能在任意系统代码页正确读取中文。
    $utf8Bom = [Text.UTF8Encoding]::new($true)
    $setup = Join-Path $stage 'setup.iss'
    [IO.File]::WriteAllText($setup, [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'setup.iss')), $utf8Bom)
    [IO.File]::WriteAllText((Join-Path $bundle 'Launch.ps1'), [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Launch.ps1')), $utf8Bom)
    [IO.File]::WriteAllText((Join-Path $bundle 'rollback.ps1'), [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'rollback.ps1')), $utf8Bom)
    Invoke-Checked $compiler @("/DAppVersion=$Version", "/DBundleDir=$bundle", "/DLanguageFile=$language", "/O$Output", $setup)
    $hash = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$asset.sha256" -Value "$hash  $([IO.Path]::GetFileName($asset))" -Encoding ASCII
    Write-Host "Installer: $asset"
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
