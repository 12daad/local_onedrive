[CmdletBinding()]
param(
    [string]$OpenListBaseUrl,
    [string]$MountPath,
    [string]$QuarkRootFolderId,
    [switch]$UseExistingCookie,
    [switch]$NoBrowser,
    [switch]$NonInteractive,
    [switch]$NoPersistEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Quark configuration requires Windows.'
}

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

function Set-ConfigurationEnvironmentValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )

    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    if (-not $NoPersistEnvironment) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    }
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

$script:SensitiveValues = [Collections.Generic.List[string]]::new()

function Add-SensitiveValue {
    param([string]$Value)

    if (-not [string]::IsNullOrEmpty($Value)) {
        $script:SensitiveValues.Add($Value)
    }
}

function Get-RedactedText {
    param([object]$Value)

    $text = [string]$Value
    foreach ($secret in $script:SensitiveValues) {
        if (-not [string]::IsNullOrEmpty($secret)) {
            $text = $text.Replace($secret, '[REDACTED]')
        }
    }
    return [regex]::Replace(
        $text,
        '(?i)(authorization|cookie|password|token)\s*[:=]\s*[^\s,;]+',
        '$1=[REDACTED]'
    )
}

function Normalize-MountPath {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value -match '[\\?#]') {
        throw 'The Quark mount path may not contain backslash, ?, or #.'
    }
    $segments = @(
        $Value.Trim() -split '/+' |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($segments.Count -gt 0 -and $segments[0] -ieq 'dav') {
        Write-Warning "The OpenList mount path must not include the /dav WebDAV prefix; removing it."
        $segments = @($segments | Select-Object -Skip 1)
    }
    if ($segments.Count -eq 0) {
        throw 'The Quark mount path must be a dedicated path such as /quark.'
    }
    if (@($segments | Where-Object { $_ -in @('.', '..') }).Count -gt 0) {
        throw 'The Quark mount path may not contain . or .. segments.'
    }
    return '/' + ($segments -join '/')
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

function Start-ConfiguredOpenList {
    param([Parameter(Mandatory)][string]$BaseUrl)

    $executable = Get-EnvironmentValue 'OPENLIST_EXECUTABLE'
    $dataDirectory = Get-EnvironmentValue 'OPENLIST_DATA_DIR'
    if ([string]::IsNullOrWhiteSpace($executable) -or [string]::IsNullOrWhiteSpace($dataDirectory)) {
        throw 'OpenList is not running. Run scripts\release-setup.ps1 first.'
    }
    $executable = (Resolve-Path -LiteralPath $executable -ErrorAction Stop).Path
    New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
    $quotedDataDirectory = '"' + $dataDirectory.Replace('"', '\"') + '"'
    $process = Start-Process `
        -FilePath $executable `
        -ArgumentList "--data $quotedDataDirectory server" `
        -WorkingDirectory (Split-Path -Parent $executable) `
        -WindowStyle Hidden `
        -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-OpenListPing $BaseUrl) { return }
        if ($process.HasExited) {
            throw "OpenList exited before becoming ready (exit code $($process.ExitCode))."
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'Timed out waiting for OpenList to become ready.'
}

function Invoke-OpenListApi {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body,
        [string]$Token
    )

    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        $headers.Authorization = $Token
    }
    $arguments = @{
        Uri = "$script:OpenListBase$Path"
        Method = $Method
        Headers = $headers
        UseBasicParsing = $true
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $arguments.ContentType = 'application/json; charset=utf-8'
        $arguments.Body = $Body | ConvertTo-Json -Depth 12 -Compress
    }
    try {
        $response = Invoke-RestMethod @arguments
    }
    catch {
        throw "OpenList request failed ($Method $Path): $(Get-RedactedText $_.Exception.Message)"
    }
    if ($null -eq $response -or $response.code -ne 200) {
        $code = if ($null -eq $response) { 'no response' } else { $response.code }
        $message = if ($null -eq $response) { '' } else { Get-RedactedText $response.message }
        throw "OpenList rejected $Method $Path (code $code): $message"
    }
    return $response.data
}

function Set-WebDavUser {
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$Username,
        [Parameter(Mandatory)][string]$Password
    )

    # create/upload, rename, move, copy, delete, WebDAV read, WebDAV manage
    $permission = 8 + 16 + 32 + 64 + 128 + 256 + 512
    $page = Invoke-OpenListApi -Method GET -Path '/api/admin/user/list?page=1&per_page=200' -Token $Token
    $existing = @($page.content) | Where-Object { $_.username -eq $Username } | Select-Object -First 1
    $user = @{
        username = $Username
        password = $Password
        base_path = $script:MountPath
        role = 0
        disabled = $false
        permission = $permission
        sso_id = ''
        allow_ldap = $false
    }
    if ($existing) {
        if ([int]$existing.role -ne 0) {
            throw "User '$Username' exists but is not a normal dedicated account."
        }
        $user.id = $existing.id
        Invoke-OpenListApi -Method POST -Path '/api/admin/user/update' -Body $user -Token $Token | Out-Null
        Write-Host "Updated WebDAV user '$Username'."
    }
    else {
        Invoke-OpenListApi -Method POST -Path '/api/admin/user/create' -Body $user -Token $Token | Out-Null
        Write-Host "Created WebDAV user '$Username'."
    }
}

function Open-QuarkLogin {
    $loginUrl = 'https://pan.quark.cn/'
    $candidates = @(
        @(
            (Get-Command 'chrome.exe' -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty Source -First 1),

            (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),

            (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),

            (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
        ) |
        Where-Object {
            $_ -and (Test-Path -LiteralPath $_)
        } |
        Select-Object -Unique
    )

    if ($candidates.Count -gt 0) {
        Start-Process -FilePath $candidates[0] -ArgumentList $loginUrl
    }
    else {
        Start-Process $loginUrl
    }
}

function Get-QuarkCookie {
    if ($NonInteractive -or $UseExistingCookie) {
        $existingCookie = Get-EnvironmentValue 'QUARKSYNC_QUARK_COOKIE'
        if ([string]::IsNullOrWhiteSpace($existingCookie)) {
            throw 'QUARKSYNC_QUARK_COOKIE is required with -NonInteractive or -UseExistingCookie.'
        }
        return $existingCookie.Trim()
    }

    if (-not $NoBrowser) {
        Open-QuarkLogin
    }
    Write-Host 'Sign in to Quark in the opened browser.'
    Write-Host 'Then press F12, open Network, select a drive.quark.cn request, and copy the complete Cookie request-header value.'
    Write-Host 'The hidden prompt below does not echo the Cookie.'
    $secureCookie = Read-Host 'Paste the Quark Cookie' -AsSecureString
    $cookie = ConvertFrom-SecureValue $secureCookie
    if ([string]::IsNullOrWhiteSpace($cookie) -or -not $cookie.Contains('=')) {
        throw 'The Quark Cookie is empty or malformed.'
    }
    Set-ConfigurationEnvironmentValue 'QUARKSYNC_QUARK_COOKIE' $cookie.Trim()
    return $cookie.Trim()
}

function Set-QuarkStorage {
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$Cookie
    )

    $driverInfo = Invoke-OpenListApi -Method GET -Path '/api/admin/driver/info?driver=Quark' -Token $Token
    $fieldNames = @($driverInfo.additional | ForEach-Object { $_.name })
    if ('cookie' -notin $fieldNames -or 'root_folder_id' -notin $fieldNames) {
        throw 'The installed OpenList Quark driver is incompatible.'
    }
    $addition = @{
        cookie = $Cookie
        root_folder_id = $script:QuarkRootFolderId
        order_by = 'none'
        order_direction = 'asc'
        use_transcoding_address = $false
        only_list_video_file = $false
    } | ConvertTo-Json -Depth 5 -Compress
    $storage = @{
        mount_path = $script:MountPath
        order = 0
        driver = 'Quark'
        cache_expiration = 30
        custom_cache_policies = ''
        status = ''
        addition = $addition
        remark = 'Managed by QuarkSync release configuration'
        disabled = $false
        disable_index = $false
        enable_sign = $false
        order_by = 'name'
        order_direction = 'asc'
        extract_folder = 'front'
        web_proxy = $true
        webdav_policy = 'native_proxy'
        proxy_range = $false
        down_proxy_url = ''
        disable_proxy_sign = $false
    }
    $page = Invoke-OpenListApi -Method GET -Path '/api/admin/storage/list?page=1&per_page=200' -Token $Token
    $allStorage = @($page.content)
    $existing = $allStorage |
        Where-Object { $_.mount_path -eq $script:MountPath } |
        Select-Object -First 1
    if (-not $existing) {
        $managedStorage = @(
            $allStorage | Where-Object {
                $_.driver -eq 'Quark' -and
                ([string]$_.remark).StartsWith('Managed by QuarkSync')
            }
        )
        if ($managedStorage.Count -gt 1) {
            throw 'Multiple QuarkSync-managed Quark mounts exist; refusing to guess which one to move.'
        }
        if ($managedStorage.Count -eq 1) {
            $existing = $managedStorage[0]
            Write-Host "Moving the managed Quark mount from $($existing.mount_path) to $script:MountPath."
        }
    }
    if ($existing) {
        if ($existing.driver -ne 'Quark') {
            throw "Mount path $script:MountPath already belongs to '$($existing.driver)'."
        }
        $storage.id = $existing.id
        Invoke-OpenListApi -Method POST -Path '/api/admin/storage/update' -Body $storage -Token $Token | Out-Null
        Write-Host "Updated Quark storage at $script:MountPath."
    }
    else {
        Invoke-OpenListApi -Method POST -Path '/api/admin/storage/create' -Body $storage -Token $Token | Out-Null
        Write-Host "Mounted Quark storage at $script:MountPath."
    }
}

function Test-WebDav {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Username,
        [Parameter(Mandatory)][string]$Password
    )

    $credentialBytes = [Text.Encoding]::UTF8.GetBytes("${Username}:${Password}")
    $bodyBytes = [Text.Encoding]::UTF8.GetBytes(
        '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    )
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create($Url)
        $request.Method = 'PROPFIND'
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        $request.ContentType = 'application/xml; charset=utf-8'
        $request.ContentLength = $bodyBytes.Length
        $request.Headers['Authorization'] = 'Basic ' + [Convert]::ToBase64String($credentialBytes)
        $request.Headers['Depth'] = '0'
        $stream = $request.GetRequestStream()
        try { $stream.Write($bodyBytes, 0, $bodyBytes.Length) }
        finally { $stream.Dispose() }
        $response = [Net.HttpWebResponse]$request.GetResponse()
        if ([int]$response.StatusCode -notin @(200, 207)) {
            throw "unexpected status $([int]$response.StatusCode)"
        }
    }
    catch {
        throw "WebDAV verification failed: $(Get-RedactedText $_.Exception.Message)"
    }
    finally {
        if ($null -ne $response) { $response.Dispose() }
    }
}

if ([string]::IsNullOrWhiteSpace($OpenListBaseUrl)) {
    $OpenListBaseUrl = Get-EnvironmentValue 'OPENLIST_BASE_URL'
}
if ([string]::IsNullOrWhiteSpace($OpenListBaseUrl)) {
    $OpenListBaseUrl = 'http://127.0.0.1:5244'
}
$script:OpenListBase = $OpenListBaseUrl.TrimEnd('/')
$openListUri = [Uri]$script:OpenListBase
if ($openListUri.Scheme -notin @('http', 'https')) {
    throw 'OPENLIST_BASE_URL must use HTTP or HTTPS.'
}
if ($openListUri.Scheme -eq 'http' -and $openListUri.Host -notin @('localhost', '127.0.0.1', '::1')) {
    throw 'Remote OpenList administration requires HTTPS.'
}
if (-not (Test-OpenListPing $script:OpenListBase)) {
    Start-ConfiguredOpenList $script:OpenListBase
}

if ([string]::IsNullOrWhiteSpace($MountPath)) {
    $MountPath = Get-EnvironmentValue 'OPENLIST_QUARK_MOUNT_PATH'
}
if ([string]::IsNullOrWhiteSpace($MountPath)) { $MountPath = '/quark' }
$script:MountPath = Normalize-MountPath $MountPath
if ([string]::IsNullOrWhiteSpace($QuarkRootFolderId)) {
    $QuarkRootFolderId = Get-EnvironmentValue 'OPENLIST_QUARK_ROOT_FOLDER_ID'
}
if ([string]::IsNullOrWhiteSpace($QuarkRootFolderId)) { $QuarkRootFolderId = '0' }
$script:QuarkRootFolderId = $QuarkRootFolderId.Trim()

$webDavUsername = Get-EnvironmentValue 'QUARKSYNC_WEBDAV_USERNAME'
$webDavPassword = Get-EnvironmentValue 'QUARKSYNC_WEBDAV_PASSWORD'
if ([string]::IsNullOrWhiteSpace($webDavUsername) -or [string]::IsNullOrWhiteSpace($webDavPassword)) {
    throw 'QUARKSYNC_WEBDAV_USERNAME and QUARKSYNC_WEBDAV_PASSWORD must already be set in the environment.'
}
if ($webDavUsername -in @('admin', 'guest')) {
    throw 'The WebDAV account must be a dedicated user, not admin or guest.'
}
Add-SensitiveValue $webDavPassword

$adminUsername = Get-EnvironmentValue 'OPENLIST_ADMIN_USERNAME'
$adminPassword = Get-EnvironmentValue 'OPENLIST_ADMIN_PASSWORD'
if ([string]::IsNullOrWhiteSpace($adminUsername)) { $adminUsername = 'admin' }
if ([string]::IsNullOrWhiteSpace($adminPassword)) {
    throw 'OPENLIST_ADMIN_PASSWORD is missing. Run scripts\release-setup.ps1 first.'
}
Add-SensitiveValue $adminPassword

try {
    $login = Invoke-OpenListApi -Method POST -Path '/api/auth/login' -Body @{
        username = $adminUsername
        password = $adminPassword
    }
}
catch {
    throw 'OpenList admin login failed. Check OPENLIST_ADMIN_USERNAME and OPENLIST_ADMIN_PASSWORD.'
}
$token = $login.token
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'OpenList login did not return an administrator token.'
}
Add-SensitiveValue $token

# Keep the requested sequence explicit: account first, then browser Cookie, then mount.
Set-WebDavUser -Token $token -Username $webDavUsername.Trim() -Password $webDavPassword
$cookie = Get-QuarkCookie
Add-SensitiveValue $cookie
Set-QuarkStorage -Token $token -Cookie $cookie

$webDavUrl = "$script:OpenListBase/dav/"
Set-ConfigurationEnvironmentValue 'QUARKSYNC_WEBDAV_URL' $webDavUrl
Set-ConfigurationEnvironmentValue 'QUARKSYNC_WEBDAV_USERNAME' $webDavUsername.Trim()
Set-ConfigurationEnvironmentValue 'QUARKSYNC_WEBDAV_PASSWORD' $webDavPassword
Set-ConfigurationEnvironmentValue 'QUARKSYNC_REMOTE_ROOT' ''
Set-ConfigurationEnvironmentValue 'QUARKSYNC_QUARK_COOKIE' $cookie
Set-ConfigurationEnvironmentValue 'OPENLIST_QUARK_MOUNT_PATH' $script:MountPath
Set-ConfigurationEnvironmentValue 'OPENLIST_QUARK_ROOT_FOLDER_ID' $script:QuarkRootFolderId

Test-WebDav -Url $webDavUrl -Username $webDavUsername.Trim() -Password $webDavPassword

Write-Host ''
Write-Host 'Quark WebDAV configuration completed.' -ForegroundColor Green
Write-Host "WebDAV URL: $webDavUrl"
Write-Host "WebDAV user: $($webDavUsername.Trim())"
Write-Host "Quark mount: $script:MountPath"
Write-Host 'The Cookie and passwords were not printed.'
