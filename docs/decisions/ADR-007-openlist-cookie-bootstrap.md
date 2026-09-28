# ADR-007: Keep Quark Cookie authentication behind OpenList

## Status

Accepted.

## Context

QuarkSync is constrained to access remote files through OpenList WebDAV and
must not implement or depend directly on Quark's private HTTP API. OpenList's
legacy Quark driver authenticates with a browser Cookie. A repeatable developer
bootstrap is needed without embedding that Cookie or broad administrator
credentials in source files or logs.

## Decision

Provisioning is split into two idempotent scripts. `scripts/release-setup.ps1`
builds the release artifacts and installs/starts OpenList.
`scripts/release-quarkconfig.ps1` reads the WebDAV username and password from
the environment, creates that account, and then configures OpenList through its
local authenticated management API. Interactive mode opens the login page and
places the manually copied Cookie into `QUARKSYNC_QUARK_COOKIE` through a
non-echoing prompt. The script does not read browser profile databases or call
Quark APIs.

The OpenList Quark driver is mounted at `/quark` with native WebDAV proxying. A
dedicated normal user is restricted to that base path and receives only the
create/upload, rename, move, copy, delete, WebDAV-read, and WebDAV-manage bits.
QuarkSync receives only this user's WebDAV credentials. The admin token and
Cookie are redacted from errors and never printed.

Secrets are persisted in user-scoped environment variables because that is the
explicit bootstrap contract. OpenList necessarily stores the Cookie in its own
driver database. Neither value is copied into QuarkSync's SQLite journal.

## Consequences

- Quark-specific behavior remains owned by OpenList, preserving QuarkSync's
  WebDAV-only backend boundary.
- Browser login requires a manual developer-tools copy; bypassing browser
  protections to scrape Cookies is intentionally unsupported.
- The OpenList data directory and the user's environment store must be treated
  as sensitive and protected by the Windows user account.
- Re-running either release script updates the managed installation, mount, and
  account instead of creating duplicates.
