from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pywintypes
import win32file
import win32pipe

from quarksync.ipc.protocol import Request, frame, parse_header
from quarksync.ipc.server import _read_exact


class IpcError(RuntimeError):
    pass


def request_sync(
    pipe_name: str,
    message_type: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            win32pipe.WaitNamedPipe(pipe_name, max(1, int((deadline - time.monotonic()) * 1000)))
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            break
        except pywintypes.error as exc:
            if time.monotonic() >= deadline:
                raise IpcError(f"Named pipe unavailable: {pipe_name}") from exc
            time.sleep(0.05)
    try:
        request = Request.create(message_type, payload)
        try:
            win32file.WriteFile(handle, frame(request.encode()))
            length = parse_header(_read_exact(handle, 4))
            value = json.loads(_read_exact(handle, length))
        except pywintypes.error as exc:
            raise IpcError(f"Named pipe request failed: {pipe_name}") from exc
    finally:
        win32file.CloseHandle(handle)
    if value.get("request_id") != request.request_id:
        raise IpcError("Mismatched IPC response")
    if not value.get("ok"):
        error = value.get("error", {})
        raise IpcError(f"{error.get('code', 'ERROR')}: {error.get('message', '')}")
    return dict(value.get("result", {}))


async def request(
    pipe_name: str,
    message_type: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        request_sync,
        pipe_name,
        message_type,
        payload,
        timeout_seconds=timeout_seconds,
    )
