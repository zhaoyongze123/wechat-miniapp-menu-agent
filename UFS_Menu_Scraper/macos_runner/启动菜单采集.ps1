$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & uv sync --frozen --python 3.12
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if (-not (Test-Path -LiteralPath 'ui_tars/node_modules/@gui-agent/agent-sdk/package.json' -PathType Leaf)) {
    & npm.cmd ci --prefix ui_tars
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host 'Menu capture console: http://127.0.0.1:8765/'
& $python -m menu_capture.server
exit $LASTEXITCODE
