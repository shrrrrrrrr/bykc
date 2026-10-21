Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host 'Run .\setup.ps1 first.'
    Read-Host 'Press Enter to exit'
    exit 1
}
& $pythonPath -X utf8 (Join-Path $PSScriptRoot 'bykc.py') @args
exit $LASTEXITCODE
