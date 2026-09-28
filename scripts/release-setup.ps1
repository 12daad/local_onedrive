[CmdletBinding()]
param(
    [string]$ReleaseDir,
    [string]$OpenListBaseUrl,
    [string]$OpenListExecutable,
    [string]$OpenListDataDir,
    [switch]$SkipOpenList,
    [switch]$RunTests,
    [switch]$NoPersistEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'QuarkSync release setup requires Windows.'
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

function Get-EnvironmentValue {
    param([Parameter(Mandatory)][string]$Name)

    foreach ($scope in @('Process', 'User', 'Machine')) {
        $value = [Environment]::GetEnvironmentVariable($Name, $scope)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $null
}

function Set-ReleaseEnvironmentValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )

    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    if (-not $NoPersistEnvironment) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    }
}

function New-RandomSecret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function ConvertFrom-SecureValue {
    param([Parameter(Mandatory)][Security.SecureString]$Value)

    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Test-OpenListPing {
    param([Parameter(Mandatory)][string]$BaseUrl)

    try {
        $response = Invoke-RestMethod -Uri "$BaseUrl/ping" -Method Get -UseBasicParsing -TimeoutSec 3
        return ([string]$response).Trim() -eq 'pong'
    }
    catch {
        return $false
    }
}

function Resolve-OpenListExecutable {
    param([string]$ConfiguredPath)

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        return (Resolve-Path -LiteralPath $ConfiguredPath -ErrorAction Stop).Path
    }
    $fromEnvironment = Get-EnvironmentValue 'OPENLIST_EXECUTABLE'
    if (-not [string]::IsNullOrWhiteSpace($fromEnvironment)) {
        return (Resolve-Path -LiteralPath $fromEnvironment -ErrorAction Stop).Path
    }
    $command = Get-Command 'openlist.exe' -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command 'openlist' -ErrorAction SilentlyContinue
    }
    if ($command) {
        return $command.Source
    }
    $wingetLink = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\openlist.exe'
    if (Test-Path -LiteralPath $wingetLink) {
        return (Resolve-Path -LiteralPath $wingetLink).Path
    }

    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'OpenList is missing and winget.exe is unavailable. Install OpenList and set OPENLIST_EXECUTABLE.'
    }
    Write-Host 'Installing OpenList with WinGet...'
    & $winget.Source install --exact --id OpenListTeam.OpenList --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet failed to install OpenList (exit code $LASTEXITCODE)."
    }
    if (Test-Path -LiteralPath $wingetLink) {
        return (Resolve-Path -LiteralPath $wingetLink).Path
    }
    $command = Get-Command 'openlist.exe' -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw 'OpenList was installed but openlist.exe was not found. Start a new terminal or set OPENLIST_EXECUTABLE.'
}

function Start-OpenList {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$DataDirectory,
        [Parameter(Mandatory)][string]$BaseUrl
    )

    $uri = [Uri]$BaseUrl
    if ($uri.Host -notin @('localhost', '127.0.0.1', '::1')) {
        throw 'release-setup.ps1 only starts a local OpenList service.'
    }
    New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
    $quotedDataDirectory = '"' + $DataDirectory.Replace('"', '\"') + '"'
    $process = Start-Process `
        -FilePath $Executable `
        -ArgumentList "--data $quotedDataDirectory server" `
        -WorkingDirectory (Split-Path -Parent $Executable) `
        -WindowStyle Hidden `
        -PassThru
    [IO.File]::WriteAllText(
        (Join-Path $DataDirectory 'quarksync-openlist.pid'),
        [string]$process.Id,
        [Text.Encoding]::ASCII
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-OpenListPing $BaseUrl) {
            return
        }
        if ($process.HasExited) {
            throw "OpenList exited before becoming ready (exit code $($process.ExitCode))."
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'Timed out waiting for OpenList to become ready.'
}

if ([string]::IsNullOrWhiteSpace($ReleaseDir)) {
    $ReleaseDir = Join-Path $repo 'dist\QuarkSync'
}
$ReleaseDir = [IO.Path]::GetFullPath($ReleaseDir)
$venvDir = Join-Path $ReleaseDir 'venv'
$packagesDir = Join-Path $ReleaseDir 'packages'
$providerDir = Join-Path $ReleaseDir 'provider'
New-Item -ItemType Directory -Force -Path $ReleaseDir, $packagesDir, $providerDir | Out-Null

# Pin runtime paths so a release does not depend on platformdirs' historical
# app-author default. Keep an existing legacy journal in place rather than
# silently starting with a new database.
$canonicalDataDir = Join-Path $env:LOCALAPPDATA 'QuarkSync'
$legacyDataDir = Join-Path $canonicalDataDir 'QuarkSync'
$dataDir = Get-EnvironmentValue 'QUARKSYNC_DATA_DIR'
if ([string]::IsNullOrWhiteSpace($dataDir)) {
    if ((Test-Path -LiteralPath (Join-Path $legacyDataDir 'state.db')) -and
        -not (Test-Path -LiteralPath (Join-Path $canonicalDataDir 'state.db'))) {
        $dataDir = $legacyDataDir
        Write-Host "Preserving the existing QuarkSync journal at $dataDir."
    }
    else {
        $dataDir = $canonicalDataDir
    }
}
$cacheDir = Get-EnvironmentValue 'QUARKSYNC_CACHE_DIR'
if ([string]::IsNullOrWhiteSpace($cacheDir)) {
    $cacheDir = Join-Path $dataDir 'cache'
}
$stateDb = Get-EnvironmentValue 'QUARKSYNC_STATE_DB'
if ([string]::IsNullOrWhiteSpace($stateDb)) {
    $stateDb = Join-Path $dataDir 'state.db'
}
$logDir = Get-EnvironmentValue 'QUARKSYNC_LOG_DIR'
if ([string]::IsNullOrWhiteSpace($logDir)) {
    $logDir = Join-Path $canonicalDataDir 'logs'
}
New-Item -ItemType Directory -Force -Path `
    $dataDir, $cacheDir, $logDir, (Split-Path -Parent $stateDb) | Out-Null
Set-ReleaseEnvironmentValue 'QUARKSYNC_DATA_DIR' ([IO.Path]::GetFullPath($dataDir))
Set-ReleaseEnvironmentValue 'QUARKSYNC_CACHE_DIR' ([IO.Path]::GetFullPath($cacheDir))
Set-ReleaseEnvironmentValue 'QUARKSYNC_STATE_DB' ([IO.Path]::GetFullPath($stateDb))
Set-ReleaseEnvironmentValue 'QUARKSYNC_LOG_DIR' ([IO.Path]::GetFullPath($logDir))

$pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    throw 'Python Launcher for Windows is required. Install Python 3.12+ with py.exe.'
}
$python = (& $pyLauncher.Source -3.12 -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($python)) {
    throw 'Python 3.12 is required.'
}
$python = $python.Trim()

if (-not (Test-Path -LiteralPath (Join-Path $venvDir 'Scripts\python.exe'))) {
    Write-Host 'Creating the release Python virtual environment...'
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the release virtual environment.' }
}
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
& $venvPython -m pip --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'pip is unavailable in the release virtual environment.' }
$uv = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
if ($uv) {
    & $uv.Source build --wheel --out-dir $packagesDir $repo
}
else {
    & $venvPython -m pip wheel --no-deps --wheel-dir $packagesDir $repo
}
if ($LASTEXITCODE -ne 0) { throw 'Failed to build the QuarkSync wheel.' }
$wheel = Get-ChildItem -LiteralPath $packagesDir -Filter 'quarksync-*.whl' |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $wheel) { throw 'The QuarkSync wheel was not produced.' }
& $venvPython -m pip install --no-deps --force-reinstall $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw 'Failed to install QuarkSync into the release virtual environment.' }
& $venvPython -m pip install `
    'httpx>=0.27,<1' `
    'platformdirs>=4.2,<5' `
    'defusedxml>=0.7,<1' `
    'pywin32>=306'
if ($LASTEXITCODE -ne 0) { throw 'Failed to install QuarkSync runtime dependencies.' }
$qsyncPath = Join-Path $venvDir 'Scripts\qsync.exe'
if (-not (Test-Path -LiteralPath $qsyncPath)) {
    throw 'qsync.exe was not created in the release virtual environment.'
}
& $qsyncPath --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The release qsync command failed its startup smoke test.' }

$dotnet = Get-Command 'dotnet.exe' -ErrorAction SilentlyContinue
$hasDotnet8 = $false
if ($dotnet) {
    $hasDotnet8 = [bool](& $dotnet.Source --list-sdks | Select-String '^8\.')
}
if (-not $hasDotnet8) {
    $dotnetInstallDir = Join-Path $repo '.dotnet'
    $localDotnet = Join-Path $dotnetInstallDir 'dotnet.exe'
    $hasLocalDotnet8 = (Test-Path -LiteralPath $localDotnet) -and
        [bool](& $localDotnet --list-sdks | Select-String '^8\.')
    if ($hasLocalDotnet8) {
        $dotnetPath = $localDotnet
    }
    else {
        $installer = Join-Path $env:TEMP 'dotnet-install-quarksync-release.ps1'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installer
        & $installer -Channel 8.0 -InstallDir $dotnetInstallDir -Architecture x64 -NoPath
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install the .NET 8 SDK.' }
        $dotnetPath = $localDotnet
    }
}
else {
    $dotnetPath = $dotnet.Source
}

Write-Host 'Publishing the Windows CFAPI provider...'
& $dotnetPath publish `
    '.\windows-provider\src\QuarkSync.Provider\QuarkSync.Provider.csproj' `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:Platform=x64 `
    -o $providerDir
if ($LASTEXITCODE -ne 0) { throw 'Failed to publish the Windows provider.' }
$providerPath = Join-Path $providerDir 'QuarkSync.Provider.exe'
if (-not (Test-Path -LiteralPath $providerPath)) {
    throw 'QuarkSync.Provider.exe was not produced.'
}

if ($RunTests) {
    $devVenv = Join-Path $repo '.venv-quarksync'
    $testPython = Join-Path $devVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $testPython)) {
        & $python -m venv $devVenv
    }
    & $testPython -m pip install --upgrade '.[dev,windows]'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install test dependencies.' }
    & $testPython -m pytest -q -m 'not large_file'
    if ($LASTEXITCODE -ne 0) { throw 'QuarkSync tests failed.' }
    & (Join-Path $devVenv 'Scripts\ruff.exe') check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff checks failed.' }
}

Set-ReleaseEnvironmentValue 'QUARKSYNC_RELEASE_DIR' $ReleaseDir
Set-ReleaseEnvironmentValue 'QUARKSYNC_QSYNC_PATH' $qsyncPath
Set-ReleaseEnvironmentValue 'QUARKSYNC_PROVIDER_PATH' $providerPath

if (-not $SkipOpenList) {
    if ([string]::IsNullOrWhiteSpace($OpenListBaseUrl)) {
        $OpenListBaseUrl = Get-EnvironmentValue 'OPENLIST_BASE_URL'
    }
    if ([string]::IsNullOrWhiteSpace($OpenListBaseUrl)) {
        $OpenListBaseUrl = 'http://127.0.0.1:5244'
    }
    $OpenListBaseUrl = $OpenListBaseUrl.TrimEnd('/')
    $openListUri = [Uri]$OpenListBaseUrl
    if ($openListUri.Scheme -notin @('http', 'https')) {
        throw 'OPENLIST_BASE_URL must use HTTP or HTTPS.'
    }
    if ($openListUri.Scheme -eq 'http' -and $openListUri.Host -notin @('localhost', '127.0.0.1', '::1')) {
        throw 'Remote OpenList administration requires HTTPS.'
    }
    if ([string]::IsNullOrWhiteSpace($OpenListDataDir)) {
        $OpenListDataDir = Get-EnvironmentValue 'OPENLIST_DATA_DIR'
    }
    if ([string]::IsNullOrWhiteSpace($OpenListDataDir)) {
        $OpenListDataDir = Join-Path $env:LOCALAPPDATA 'QuarkSync\openlist'
    }

    $openListRunning = Test-OpenListPing $OpenListBaseUrl
    if (-not $openListRunning) {
        $OpenListExecutable = Resolve-OpenListExecutable $OpenListExecutable
    }
    elseif ([string]::IsNullOrWhiteSpace($OpenListExecutable)) {
        $OpenListExecutable = Get-EnvironmentValue 'OPENLIST_EXECUTABLE'
    }

    $adminUsername = Get-EnvironmentValue 'OPENLIST_ADMIN_USERNAME'
    if ([string]::IsNullOrWhiteSpace($adminUsername)) { $adminUsername = 'admin' }
    $adminPassword = Get-EnvironmentValue 'OPENLIST_ADMIN_PASSWORD'
    if ([string]::IsNullOrWhiteSpace($adminPassword)) {
        $existingDatabase = Test-Path -LiteralPath (Join-Path $OpenListDataDir 'data.db')
        if ($openListRunning -or $existingDatabase) {
            $securePassword = Read-Host 'Enter the existing OpenList admin password' -AsSecureString
            $adminPassword = ConvertFrom-SecureValue $securePassword
            if ([string]::IsNullOrWhiteSpace($adminPassword)) {
                throw 'OPENLIST_ADMIN_PASSWORD may not be empty.'
            }
        }
        else {
            $adminPassword = New-RandomSecret
            Write-Host 'Generated a new OpenList administrator password without displaying it.'
        }
    }
    Set-ReleaseEnvironmentValue 'OPENLIST_ADMIN_USERNAME' $adminUsername
    Set-ReleaseEnvironmentValue 'OPENLIST_ADMIN_PASSWORD' $adminPassword
    Set-ReleaseEnvironmentValue 'OPENLIST_BASE_URL' $OpenListBaseUrl
    Set-ReleaseEnvironmentValue 'OPENLIST_DATA_DIR' $OpenListDataDir
    if (-not [string]::IsNullOrWhiteSpace($OpenListExecutable)) {
        Set-ReleaseEnvironmentValue 'OPENLIST_EXECUTABLE' $OpenListExecutable
    }

    if (-not $openListRunning) {
        Start-OpenList -Executable $OpenListExecutable -DataDirectory $OpenListDataDir -BaseUrl $OpenListBaseUrl
        Write-Host 'OpenList is installed and running.'
    }
    else {
        Write-Host 'Using the running OpenList service.'
    }
    Set-ReleaseEnvironmentValue 'QUARKSYNC_WEBDAV_URL' "$OpenListBaseUrl/dav/"
    Set-ReleaseEnvironmentValue 'QUARKSYNC_REMOTE_ROOT' ''
    Set-ReleaseEnvironmentValue 'OPENLIST_QUARK_MOUNT_PATH' '/quark'
    Set-ReleaseEnvironmentValue 'OPENLIST_QUARK_ROOT_FOLDER_ID' '0'
}

Write-Host ''
Write-Host 'QuarkSync release setup completed.' -ForegroundColor Green
Write-Host "qsync: $qsyncPath"
Write-Host "provider: $providerPath"
if (-not $SkipOpenList) {
    Write-Host 'Next: set QUARKSYNC_WEBDAV_USERNAME and QUARKSYNC_WEBDAV_PASSWORD, then run scripts\release-quarkconfig.ps1.'
}
