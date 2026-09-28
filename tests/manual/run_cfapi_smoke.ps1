param(
    [ValidateSet('Start', 'Status', 'Stop', 'Unregister')]
    [string]$Action = 'Start'
)

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$env:QUARKSYNC_WEBDAV_URL = 'http://127.0.0.1:52441/dav/'
$env:QUARKSYNC_WEBDAV_USERNAME = 'syncuser'
# This credential belongs only to tests/manual/run_mock_webdav.py.
$env:QUARKSYNC_WEBDAV_PASSWORD = 'secret'
$env:QUARKSYNC_REMOTE_ROOT = 'quark'
$env:QUARKSYNC_ROOT = Join-Path $repo '.cfapi-test-root'
$env:QUARKSYNC_DATA_DIR = Join-Path $repo '.cfapi-test-data'
$env:QUARKSYNC_CACHE_DIR = Join-Path $repo '.cfapi-test-cache'
$env:QUARKSYNC_CACHE_MIN_FREE_GB = '0'
$env:QUARKSYNC_WRITEBACK_DELAY_SECONDS = '1'
$env:QUARKSYNC_SYNC_INTERVAL_SECONDS = '1'
$env:QUARKSYNC_REMOTE_POLL_SECONDS = '60'
$env:QUARKSYNC_HEARTBEAT_SECONDS = '2'
$env:QUARKSYNC_DELETE_GRACE_SECONDS = '2'
$env:DOTNET_ROOT = Join-Path $repo '.dotnet'
$provider = Join-Path $repo 'windows-provider\src\QuarkSync.Provider\bin\x64\Debug\net8.0-windows10.0.17763.0\win-x64\QuarkSync.Provider.exe'
$env:QUARKSYNC_PROVIDER_PATH = $provider
$qsync = Join-Path $repo '.venv-quarksync\Scripts\qsync.exe'

switch ($Action) {
    'Start' { & $qsync start }
    'Status' { & $qsync status --all-devices }
    'Stop' { & $qsync stop }
    'Unregister' { & $provider --root $env:QUARKSYNC_ROOT --unregister }
}
