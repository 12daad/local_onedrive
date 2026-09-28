# ADR-008: Startup readiness and stable runtime paths

Status: accepted

`qsync start` reports ready after the database, named pipes, native provider,
and remote control-plane layout are available. Initial recursive namespace
enumeration runs concurrently with the scheduler and device heartbeat. A large
remote tree therefore does not cause the CLI's bounded startup wait to report a
false failure.

The fixed remote `schema.json` is created only when missing. Rewriting it on
every startup exposed write-after-PUT visibility differences in storage drivers
without providing any schema benefit.

New installations use `%LOCALAPPDATA%\QuarkSync` for data and logs. Releases
persist explicit `QUARKSYNC_DATA_DIR`, `QUARKSYNC_CACHE_DIR`,
`QUARKSYNC_STATE_DB`, and `QUARKSYNC_LOG_DIR` values. If an older installation
already has `%LOCALAPPDATA%\QuarkSync\QuarkSync\state.db`, that journal remains
the data location unless a canonical journal already exists; it is never
silently abandoned or deleted. Logs use the canonical
`%LOCALAPPDATA%\QuarkSync\logs` path so startup failures can always name an
unambiguous file.
