# ADR-006: Provider lifetime and daemon crash adoption

## Status

Accepted.

## Context

The CFAPI provider and Python daemon are separate processes. A forced Python
termination does not reliably terminate its provider child, and launching a
second provider could create competing sync-root connections and named-pipe
instances.

## Decision

At startup, the daemon probes the current-user provider pipe before launching a
process. If a connected provider owns the configured normalized Sync Root, the
daemon adopts it. The provider opens a fresh core-pipe connection for each
message, so callbacks naturally reach the replacement daemon. A provider for a
different or disconnected root is stopped before a replacement is launched.

Hydration downloads are keyed by the CFAPI transfer key. `CANCEL_FETCH` cancels
the matching Python task. Connection timeout and full-hydration timeout are
separate so a missing daemon fails quickly while a legitimate 10 GB+ download
can continue.

## Consequences

- Abrupt daemon restart does not create duplicate CFAPI providers.
- Durable operations still recover from SQLite `RUNNING` to `PENDING`.
- A cache file held by a surviving provider is not forcibly removed during
  recovery; it remains protected until transfer completion or a later restart.
