# ADR-005: Sync-root registration and Windows packaging

## Status

Accepted for the development MVP; revisit when producing an MSIX installer.

## Context

Windows exposes two related surfaces. `CldApi.dll` provides the desktop CFAPI
contract used by a sync engine (`CfRegisterSyncRoot`, `CfConnectSyncRoot`,
placeholder operations, callbacks, and `CfExecute`). The
`Windows.Storage.Provider` WinRT namespace also exposes sync-root registration
metadata used by packaged providers for richer Shell identity and branding.

The first QuarkSync deliverable is intentionally unpackaged and has no package
identity or installed icon resources. Pretending those resources exist would
make development registration dependent on an absent installer and would not
improve hydration correctness.

## Decision

The provider first checks for package identity. Packaged execution registers
through `StorageProviderSyncRootManager` with FULL hydration and population
policies. Unpackaged development execution registers through the official
desktop `CfRegisterSyncRoot` function. Both paths connect and perform file
operations through CFAPI; QuarkSync does not draw overlay icons or simulate
Explorer state.

An MSIX distribution must still add and validate the package manifest and
installed icon/string resources. That packaging work must continue to delegate
callbacks and all sync policy to the existing thin provider/Python boundary.
Absence of CFAPI itself remains a fatal startup/doctor error and never triggers
a filesystem-only fallback.

## Consequences

- The current provider is directly runnable from a development checkout.
- Cloud Files behavior is real and testable without an installer.
- The WinRT path is compiled but product-grade packaged resources are not
  claimed until an MSIX identity is supplied and tested.
- Before distribution, registration must be tested from the final package
  identity on every supported Windows baseline.
