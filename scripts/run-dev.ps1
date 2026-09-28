$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

foreach ($name in @(
    'QUARKSYNC_WEBDAV_URL',
    'QUARKSYNC_WEBDAV_USERNAME',
    'QUARKSYNC_WEBDAV_PASSWORD'
)) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "$name must be set in the environment."
    }
}

if (Test-Path '.\.dotnet') { $env:DOTNET_ROOT = (Resolve-Path '.\.dotnet').Path }
$provider = Get-ChildItem `
    '.\windows-provider\src\QuarkSync.Provider\bin\x64\Debug' `
    -Filter 'QuarkSync.Provider.exe' -Recurse | Select-Object -First 1
if (-not $provider) { throw 'Build the native provider with scripts/dev-setup.ps1 first.' }
$env:QUARKSYNC_PROVIDER_PATH = $provider.FullName

& '.\.venv-quarksync\Scripts\qsync.exe' start --foreground

