$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'QuarkSync development requires Windows.'
}

$python = (py -3.12 -c 'import sys; print(sys.executable)')
if (-not $python) { throw 'Python 3.12 is required.' }

if (-not (Test-Path '.venv-quarksync')) {
    py -3.12 -m venv .venv-quarksync
}
& '.\.venv-quarksync\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv-quarksync\Scripts\python.exe' -m pip install -e '.[dev,windows]'

$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet -or -not (& $dotnet.Source --list-sdks | Select-String '^8\.')) {
    $installer = Join-Path $env:TEMP 'dotnet-install-quarksync.ps1'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installer
    & $installer -Channel 8.0 -InstallDir '.dotnet' -Architecture x64 -NoPath
    $dotnetPath = Join-Path $repo '.dotnet\dotnet.exe'
} else {
    $dotnetPath = $dotnet.Source
}

& $dotnetPath build '.\windows-provider\QuarkSync.Provider.sln' -c Debug -p:Platform=x64
& '.\.venv-quarksync\Scripts\python.exe' -m pytest -q
& '.\.venv-quarksync\Scripts\ruff.exe' check src tests

Write-Host 'QuarkSync development environment is ready.' -ForegroundColor Green

