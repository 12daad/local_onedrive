from __future__ import annotations

import json
import struct
import uuid
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
CORE_PIPE = r"\\.\pipe\QuarkSync.Core.v1"
CONTROL_PIPE = r"\\.\pipe\QuarkSync.Control.v1"
PROVIDER_PIPE = r"\\.\pipe\QuarkSync.Provider.v1"

MESSAGE_TYPES = frozenset(
    {
        "PING",
        "FETCH_FILE",
        "CANCEL_FETCH",
        "LOCAL_MODIFIED",
        "LOCAL_CREATED",
        "LOCAL_RENAMED",
        "LOCAL_DELETED",
        "PIN_CHANGED",
        "SYNC_STATE_CHANGED",
        "GET_FILE_METADATA",
        "LIST_NAMESPACE",
        "CREATE_PLACEHOLDERS",
        "UPDATE_PLACEHOLDER",
        "REMOVE_PLACEHOLDER",
        "SET_IN_SYNC",
        "REPORT_PROGRESS",
        "HYDRATE",
        "DEHYDRATE",
        "PIN",
        "UNPIN",
        "STATUS",
        "PENDING",
        "CONFLICTS",
        "SYNC_NOW",
        "STOP",
    }
)


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Request:
    protocol_version: int
    request_id: str
    type: str
    payload: dict[str, Any]

    @classmethod
    def create(cls, message_type: str, payload: dict[str, Any] | None = None) -> Request:
        if message_type not in MESSAGE_TYPES:
            raise ProtocolError(f"Unknown message type: {message_type}")
        return cls(PROTOCOL_VERSION, str(uuid.uuid4()), message_type, payload or {})

    @classmethod
    def parse(cls, raw: bytes) -> Request:
        try:
            value = json.loads(raw)
            request = cls(
                protocol_version=int(value["protocol_version"]),
                request_id=str(value["request_id"]),
                type=str(value["type"]),
                payload=dict(value.get("payload", {})),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError("Malformed request") from exc
        if request.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"Unsupported protocol version {request.protocol_version}; "
                f"expected {PROTOCOL_VERSION}"
            )
        if request.type not in MESSAGE_TYPES:
            raise ProtocolError(f"Unknown message type: {request.type}")
        return request

    def encode(self) -> bytes:
        return json.dumps(
            {
                "protocol_version": self.protocol_version,
                "request_id": self.request_id,
                "type": self.type,
                "payload": self.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


def response(request_id: str, result: dict[str, Any] | None = None) -> bytes:
    return json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "result": result or {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def error_response(request_id: str, code: str, message: str) -> bytes:
    return json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code, "message": message[:2000]},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def frame(payload: bytes) -> bytes:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("IPC message exceeds size limit")
    return struct.pack("<I", len(payload)) + payload


def parse_header(header: bytes) -> int:
    if len(header) != 4:
        raise ProtocolError("Incomplete IPC frame header")
    length = struct.unpack("<I", header)[0]
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError("IPC message exceeds size limit")
    return length
