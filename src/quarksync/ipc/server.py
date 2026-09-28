from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security

from quarksync.ipc.protocol import Request, error_response, frame, parse_header, response

logger = logging.getLogger(__name__)
Handler = Callable[[Request], Awaitable[dict[str, object]]]
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


def _handler_timeout(request: Request) -> float | None:
    # FETCH_FILE can legitimately run for hours for multi-GB files. Its caller
    # applies QUARKSYNC_HYDRATION_TIMEOUT_SECONDS and CANCEL_FETCH cancels the
    # asyncio task through a second pipe connection.
    return None if request.type == "FETCH_FILE" else 180


def _current_user_security_attributes():
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        user_sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _read_exact(handle, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        _, data = win32file.ReadFile(handle, length - len(chunks))
        if not data:
            raise EOFError("Named pipe closed")
        chunks.extend(data)
    return bytes(chunks)


class NamedPipeServer:
    def __init__(self, name: str, handler: Handler, loop: asyncio.AbstractEventLoop) -> None:
        self.name = name
        self.handler = handler
        self.loop = loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name=f"pipe:{self.name}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            handle = win32file.CreateFile(
                self.name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            win32file.CloseHandle(handle)
        except pywintypes.error:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def _serve(self) -> None:
        security_attributes = _current_user_security_attributes()
        while not self._stop.is_set():
            handle = win32pipe.CreateNamedPipe(
                self.name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT
                | PIPE_REJECT_REMOTE_CLIENTS,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                64 * 1024,
                64 * 1024,
                1000,
                security_attributes,
            )
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                        raise
                if self._stop.is_set():
                    continue
                threading.Thread(
                    target=self._connection,
                    args=(handle,),
                    name="pipe-client",
                    daemon=True,
                ).start()
                handle = None
            except Exception:
                logger.exception("Named pipe accept failed")
            finally:
                if handle is not None:
                    win32file.CloseHandle(handle)

    def _connection(self, handle) -> None:
        request_id = "unknown"
        try:
            length = parse_header(_read_exact(handle, 4))
            request = Request.parse(_read_exact(handle, length))
            request_id = request.request_id
            future = asyncio.run_coroutine_threadsafe(self.handler(request), self.loop)
            result = future.result(timeout=_handler_timeout(request))
            payload = response(request.request_id, result)
        except Exception as exc:
            logger.exception("IPC request failed")
            payload = error_response(request_id, type(exc).__name__, str(exc))
        try:
            win32file.WriteFile(handle, frame(payload))
            win32file.FlushFileBuffers(handle)
        except pywintypes.error:
            pass
        finally:
            with contextlib.suppress(pywintypes.error):
                win32pipe.DisconnectNamedPipe(handle)
            win32file.CloseHandle(handle)
