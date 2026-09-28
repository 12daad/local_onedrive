from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from quarksync.backend.webdav import OpenListWebDAVBackend
from quarksync.config import ConfigError, Settings
from quarksync.db.database import Database
from quarksync.ipc.client import IpcError, request_sync
from quarksync.ipc.protocol import CONTROL_PIPE, PROVIDER_PIPE
from quarksync.logging_config import bootstrap_log_dir


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _start(foreground: bool) -> int:
    daemon_exists = False
    try:
        running = request_sync(CONTROL_PIPE, "PING", timeout_seconds=0.2)
        daemon_exists = True
        if running.get("ready"):
            print(f"QuarkSync is already running (PID {running.get('pid')}).")
            return 0
    except IpcError:
        pass
    if foreground:
        from quarksync.daemon import main as daemon_main

        daemon_main()
        return 0
    if not daemon_exists:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen(
            [sys.executable, "-m", "quarksync.daemon"],  # noqa: S603
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            result = request_sync(CONTROL_PIPE, "PING", timeout_seconds=0.5)
            if result.get("ready"):
                print(f"QuarkSync started (PID {result.get('pid')}).")
                return 0
        except IpcError:
            time.sleep(0.25)
    print(
        "QuarkSync failed to become ready; inspect "
        f"{bootstrap_log_dir() / 'quarksync.log'}."
    )
    return 1


def _doctor() -> int:
    checks: list[dict[str, object]] = []
    settings = Settings.from_env(require_credentials=False)
    is_windows = sys.platform == "win32"
    checks.append({"check": "Windows", "ok": is_windows, "detail": sys.platform})
    if is_windows:
        info = (ctypes.c_uint32 * 3)()
        try:
            result = ctypes.windll.CldApi.CfGetPlatformInfo(ctypes.byref(info))
            checks.append(
                {
                    "check": "CFAPI",
                    "ok": result >= 0,
                    "detail": f"HRESULT=0x{result & 0xFFFFFFFF:08X}",
                }
            )
        except OSError as exc:
            checks.append({"check": "CFAPI", "ok": False, "detail": str(exc)})
        root = settings.sync_root.anchor or str(Path.home().anchor)
        filesystem = ctypes.create_unicode_buffer(32)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, None, None, None, filesystem, len(filesystem)
        )
        checks.append(
            {
                "check": "NTFS",
                "ok": bool(ok) and filesystem.value.upper() == "NTFS",
                "detail": filesystem.value,
            }
        )
    database = Database(settings.state_db)
    try:
        database.migrate()
        checks.append({"check": "database", "ok": database.integrity_check() == "ok"})
    except Exception as exc:
        checks.append({"check": "database", "ok": False, "detail": str(exc)})
    for label, pipe in (("Python daemon", CONTROL_PIPE), ("native provider", PROVIDER_PIPE)):
        try:
            result = request_sync(pipe, "PING", timeout_seconds=0.2)
            checks.append({"check": label, "ok": True, "detail": result})
            if label == "native provider":
                checks.append(
                    {
                        "check": "sync root registration",
                        "ok": bool(result.get("connected")),
                        "detail": result.get("root"),
                    }
                )
        except IpcError as exc:
            checks.append({"check": label, "ok": False, "detail": str(exc)})
            if label == "native provider":
                checks.append(
                    {"check": "sync root registration", "ok": False, "detail": str(exc)}
                )
    if settings.webdav_url and settings.webdav_username and settings.webdav_password:
        async def webdav_check() -> dict[str, object]:
            backend = OpenListWebDAVBackend(
                settings.webdav_url,
                settings.webdav_username,
                settings.webdav_password,
                remote_root=settings.remote_root,
                verify=settings.tls_verify,
            )
            try:
                await backend.detect_capabilities()
                await backend.stat("")
                await backend.mkdir(".quarksync-meta")
                probe_id = uuid.uuid4().hex
                local_probe = settings.data_dir / f"doctor-{probe_id}.tmp"
                remote_probe = f".quarksync-meta/.doctor-{probe_id}.tmp"
                local_probe.parent.mkdir(parents=True, exist_ok=True)
                local_probe.write_bytes(b"QuarkSync doctor probe\n")
                try:
                    await backend.upload(local_probe, remote_probe)
                    await backend.delete(remote_probe)
                finally:
                    local_probe.unlink(missing_ok=True)
                return {
                    "ok": True,
                    "capabilities": asdict(backend.capabilities),
                    "metadata_directory": ".quarksync-meta",
                    "write_delete_probe": "passed",
                }
            except Exception as exc:
                return {"ok": False, "detail": str(exc)}
            finally:
                await backend.close()

        checks.append({"check": "WebDAV/auth/root", **asyncio.run(webdav_check())})
    else:
        checks.append(
            {"check": "WebDAV/auth/root", "ok": False, "detail": "credentials not configured"}
        )
    _print(checks)
    return 0 if all(bool(item["ok"]) for item in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qsync")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--foreground", action="store_true")
    sub.add_parser("stop")
    status = sub.add_parser("status")
    status.add_argument("--all-devices", action="store_true")
    sub.add_parser("pending")
    sub.add_parser("conflicts")
    sub.add_parser("sync-now")
    for command in ("hydrate", "dehydrate", "pin", "unpin"):
        item = sub.add_parser(command)
        item.add_argument("path")
    sub.add_parser("doctor")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "start":
            raise SystemExit(_start(arguments.foreground))
        if arguments.command == "doctor":
            raise SystemExit(_doctor())
        if arguments.command == "stop":
            _print(request_sync(CONTROL_PIPE, "STOP"))
        elif arguments.command in {"status", "pending", "conflicts", "sync-now"}:
            message = arguments.command.replace("-", "_").upper()
            result = request_sync(CONTROL_PIPE, message)
            if arguments.command == "status" and not arguments.all_devices:
                result.pop("devices", None)
            _print(result)
        else:
            _print(
                request_sync(
                    PROVIDER_PIPE,
                    arguments.command.upper(),
                    {"path": arguments.path},
                    timeout_seconds=180,
                )
            )
    except (ConfigError, IpcError, OSError) as exc:
        print(f"qsync: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
