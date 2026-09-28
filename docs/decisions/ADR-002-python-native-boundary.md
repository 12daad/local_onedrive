# ADR-002: Python/native boundary

Status: accepted

The .NET process contains CFAPI interop, callback lifetime management, and local
notifications only. Python owns all durable state and synchronization policy.
IPC is protocol-versioned JSON over local named pipes. Hydration messages carry
a cache path, never file content or Python pickle data.

This keeps callback memory/thread rules isolated from the synchronization
journal and lets the Python core use a fake platform adapter in tests.

