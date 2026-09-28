# Architecture

QuarkSync has one data-owner and one platform adapter. The Python daemon owns
all WebDAV, conflict, retry, cache, device-status, and SQLite decisions. The C#
process owns only Windows Cloud Files API calls and filesystem notifications.

```text
Explorer / applications
        |
   cldflt.sys / CFAPI
        |
QuarkSync.Provider (.NET 8, x64)
        |  versioned JSON, named pipes, no file bytes
        |
Python 3.12 daemon
   |        |          |
SQLite    cache     StorageBackend
                         |
                  OpenList WebDAV
```

The provider sends `FETCH_FILE` when CFAPI invokes `FETCH_DATA`. Python verifies
the remote ETag, streams the complete object into a protected cache file, and
returns only its local path and metadata. The provider reads 1 MiB aligned
chunks and submits repeated `CF_OPERATION_TYPE_TRANSFER_DATA` operations. At no
point is a large file represented as one in-memory byte array. Daemon connection
failure uses a short timeout, while the full-cache hydration deadline is
separately configurable for 10 GB+ objects. CFAPI cancellation is propagated to
the matching Python download task by transfer ID.

Local writes complete on NTFS. Close/change notifications create a protected
upload staging snapshot and a durable SQLite operation. The debounce due time is
reset by subsequent saves. Network workers later perform WebDAV PUT/MOVE and
mark the CFAPI placeholder in-sync only after the remote result is verified.

## Ownership boundaries

- `backend/`: the only code allowed to issue remote HTTP requests.
- `db/`: schema migrations and transactional connection policy.
- `sync/`: state machine, durable operations, scanners, conflicts, scheduling.
- `cache/`: allocation, protection, accounting, and safe LRU eviction.
- `ipc/`: protocol framing and Windows named-pipe transport.
- `status/`: stable device identity and `.quarksync-meta` status documents.
- `windows-provider/`: registration, placeholders, callbacks, hydration transfer,
  and Shell state. It has no WebDAV or conflict policy.

## Failure invariants

1. A dirty upload snapshot remains `protected=1` until a newer version succeeds.
2. A worker claim is transactional; `RUNNING` operations return to `PENDING` on
   process restart.
3. Remote ETag divergence from `base_etag` enters `CONFLICT`; it never overwrites.
4. Deletes are tombstones and durable `DELETE` operations with a grace due time.
5. IPC and provider failures can affect hydration availability, but not SQLite
   integrity or already-hydrated local writes.
6. After an abrupt Python exit, a replacement daemon adopts a still-connected
   provider for the same root instead of creating competing CFAPI connections.

The current registration uses the official desktop `CfRegisterSyncRoot` API.
Shell hydration, pin, and in-sync states are CFAPI-native; no overlay icon or
simulated Explorer state exists. A future packaged distribution may additionally
publish richer `Windows.Storage.Provider` branding without changing this boundary.
