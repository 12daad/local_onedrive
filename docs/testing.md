# Test plan

Run the default suite:

```powershell
.\.venv-quarksync\Scripts\python.exe -m pytest -q
.\.venv-quarksync\Scripts\ruff.exe check src tests
dotnet build .\windows-provider\QuarkSync.Provider.sln -c Debug -p:Platform=x64
```

The local Mock WebDAV covers PROPFIND depth 0/1, Unicode URL encoding, GET,
ranges, PUT, temporary MOVE, MKCOL, DELETE, ETag preconditions, auth errors, and
timeouts. Core tests cover migration idempotence, operation recovery, debounce,
retry, protected cache pressure, state transitions, conflicts, offline queue
retention, and a fake platform adapter.

The opt-in 1 GiB constant-memory test uses a sparse file:

```powershell
$env:QUARKSYNC_RUN_LARGE_TESTS = '1'
pytest -m large_file tests/integration/test_large_streaming.py
```

For a real CFAPI smoke test, start `tests/manual/run_mock_webdav.py`, build the
provider, then use `tests/manual/run_cfapi_smoke.ps1`. The verified sequence is:
placeholder population, FULL hydration, local modification/write-back, new file
conversion, MOVE ordering, visible delete grace, and remote DELETE. Use the
`Unregister` action only for disposable smoke-test roots.

Tests requiring a suitable Windows runner should use marker `windows_cfapi` and
remain skipped in generic CI.

The Windows 11 development smoke test has also been exercised end-to-end with a
generated 1 GiB WebDAV object. Local and remote SHA-256 values matched while the
Python daemon and provider working sets stayed in the tens of MiB. This is a
development result, not a substitute for repeating the test against each target
OpenList storage driver.

Crash adoption was fault-injected by force-terminating the Python daemon while
leaving the provider connected. A subsequent `qsync start` retained the same
provider PID and successfully hydrated a placeholder through the replacement
daemon. Queue recovery remains separately covered by SQLite integration tests.
