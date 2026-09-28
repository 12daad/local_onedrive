# QuarkSync

QuarkSync is a Windows 10/11 x64 cloud-files sync client. It exposes an NTFS
Cloud Files API sync root while a Python 3.12 daemon performs local-first,
delayed write-back synchronization through OpenList WebDAV.

> Status: development MVP. Data-safety behavior is implemented conservatively;
> use a dedicated test WebDAV root until the test checklist in `docs/testing.md`
> has passed for your OpenList storage driver.

## Prerequisites

- Windows 10 version 1709 or later; Windows 11 x64 is recommended.
- An NTFS volume for the sync root.
- Python 3.12 or later.
- .NET 8 SDK and Windows SDK 10.0.22621 or later.
- An OpenList user with **WebDAV read**, **WebDAV management**, create/upload,
  delete, rename, and move permissions.

## Release setup and Quark configuration

Build the Python wheel and release virtual environment, publish the x64 CFAPI
provider as a self-contained .NET application, and install/start OpenList:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\release-setup.ps1
```

Release output is written to `dist\QuarkSync` by default. The script stores
`QUARKSYNC_QSYNC_PATH`, `QUARKSYNC_PROVIDER_PATH`, explicit data/cache/database/log
paths, and the local OpenList configuration in the user environment. Existing
legacy journals are preserved in place. Add `-RunTests` to run the non-large-file
test suite and Ruff before completing setup.

Next, provide the dedicated WebDAV credentials through environment variables and
configure the account and Quark mount:

```powershell
$env:QUARKSYNC_WEBDAV_USERNAME = 'quarksync'
$env:QUARKSYNC_WEBDAV_PASSWORD = '<new-dedicated-secret>'
.\scripts\release-quarkconfig.ps1
```

`release-quarkconfig.ps1` creates or updates the WebDAV user first, then opens
the Quark login page in Chrome when available (otherwise the default browser).
After login, copy the complete `Cookie` request-header value from browser
developer tools and paste it into the hidden prompt. The Cookie is saved as
`QUARKSYNC_QUARK_COOKIE`, then used to create or update the `/quark` OpenList
mount. The script finishes with an authenticated WebDAV `PROPFIND` check.
OpenList mount paths are virtual paths such as `/quark` or `/Users/hongbo`; do
not include the `/dav` URL prefix. Duplicate slashes and an accidental leading
`/dav` are normalized by the script.

Both scripts are idempotent. For unattended reconfiguration, prepopulate
`QUARKSYNC_QUARK_COOKIE` and use `release-quarkconfig.ps1 -UseExistingCookie -NonInteractive`.
Use `-NoPersistEnvironment` for a process-only/test run.

The Quark Cookie is necessarily persisted by OpenList in its own storage-driver
database. QuarkSync itself never receives that Cookie: it authenticates only to
the local OpenList WebDAV endpoint. Treat both the OpenList data directory and
the user environment as sensitive.

## Development setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\dev-setup.ps1
Copy-Item .env.example .env.local
```

QuarkSync deliberately does not load `.env` files. Set real secrets in the
process or user environment:

```powershell
$env:QUARKSYNC_WEBDAV_URL = 'https://openlist.example/dav/'
$env:QUARKSYNC_WEBDAV_USERNAME = 'quarksync'
$env:QUARKSYNC_WEBDAV_PASSWORD = '<secret>'
$env:QUARKSYNC_REMOTE_ROOT = ''
$env:QUARKSYNC_ROOT = 'D:\QuarkSync'
```

Plain HTTP is accepted only for `localhost`, `127.0.0.1`, and `::1` unless
`QUARKSYNC_ALLOW_INSECURE_HTTP=true` is explicitly set. TLS verification is on
by default.

Build and run:

```powershell
dotnet build .\windows-provider\QuarkSync.Provider.sln -c Debug -p:Platform=x64
.\scripts\run-dev.ps1
```

Or invoke components directly:

```powershell
.\.venv-quarksync\Scripts\qsync.exe doctor
.\.venv-quarksync\Scripts\qsync.exe start
.\.venv-quarksync\Scripts\qsync.exe status
```

## Validation walkthrough

1. Put `test/hello.txt` and a large test file in the configured OpenList root.
2. Run `qsync doctor`, then `qsync start`.
3. Open the configured sync root in Explorer. Files should initially be cloud
   placeholders, not full local copies.
4. Open `test\hello.txt`; CFAPI requests full hydration and the content must
   match WebDAV.
5. Modify and close the file. Saving remains local; after the write-back delay,
   `qsync pending` clears and the WebDAV copy changes.
6. Create, rename, and delete a test file. Deletes remain `DELETE_PENDING` until
   the grace period expires.

Use `qsync stop` before changing provider builds. Registration persists; the
provider reconnects it when started again. See [architecture](docs/architecture.md),
[development guide](docs/development.md), and [test plan](docs/testing.md).

If startup fails, the CLI prints the exact rotating-log path. Its default is
`%LOCALAPPDATA%\QuarkSync\logs\quarksync.log`.

## Safety model

- Dirty local files and upload staging data are never cache-eviction targets.
- A remote ETag change against the recorded base creates a conflict instead of
  overwriting either side.
- Deletes are durable tombstones with a configurable 24-hour default grace.
- Network work is always outside an application's local save path.
- Credentials and authorization headers are not stored in SQLite or logs.

This project does not call Quark private APIs. All remote data access is through
the `StorageBackend` abstraction and its OpenList WebDAV implementation.
