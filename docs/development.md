# Development guide

## Toolchain

Run `scripts/dev-setup.ps1`. It creates `.venv-quarksync` with Python 3.12,
installs test dependencies, installs a repository-local .NET 8 SDK when no
suitable SDK is available, builds the x64 provider, and runs the unit/integration
suite.

The provider requires Windows 10 1709+ and an NTFS sync root. Development is
tested primarily on Windows 11 x64. Long paths must be enabled in Windows for
namespaces that can exceed the legacy MAX_PATH boundary.

## Configuration

Set secrets in the OS/process environment; `.env` is intentionally not loaded.
Required variables are `QUARKSYNC_WEBDAV_URL`, `QUARKSYNC_WEBDAV_USERNAME`,
`QUARKSYNC_WEBDAV_PASSWORD`, and usually `QUARKSYNC_REMOTE_ROOT`. See
`.env.example` for all tuning knobs.

OpenList users need WebDAV read and management plus create/upload, delete,
rename, and move permissions. OpenList drivers vary: OPTIONS capability claims
are recorded, but MOVE is treated as available rather than assumed atomic.

For a release installation, run `scripts/release-setup.ps1`. It builds a wheel,
installs qsync into `dist/QuarkSync/venv`, publishes a self-contained x64
provider, and installs/starts OpenList. Then set `QUARKSYNC_WEBDAV_USERNAME` and
`QUARKSYNC_WEBDAV_PASSWORD` and run `scripts/release-quarkconfig.ps1`.

The configuration script creates the WebDAV account before opening the Quark
login page. It accepts the browser Cookie through a non-echoing prompt, saves it
as `QUARKSYNC_QUARK_COOKIE`, and configures the OpenList Quark driver. It does
not inspect browser profile databases. Existing OpenList instances require
`OPENLIST_ADMIN_PASSWORD`; fresh local instances get a generated administrator
secret from the setup script. See `.env.example` for all variables.

## Run

```powershell
.\scripts\run-dev.ps1
```

Use another terminal for `qsync status`, `qsync pending`, and `qsync conflicts`.
`qsync stop` disconnects the provider but intentionally leaves registration
persistent. The daemon reconnects on the next start.

## Diagnostics

`qsync doctor` checks Windows, NTFS, CFAPI, SQLite integrity, both named pipes,
provider connection/registration state, WebDAV authentication, the remote root,
and advertised capabilities. Python and provider rotating logs are under
`%LOCALAPPDATA%\QuarkSync\logs` unless overridden.
