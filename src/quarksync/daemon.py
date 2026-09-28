from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from pathlib import Path

from quarksync.backend.webdav import OpenListWebDAVBackend
from quarksync.cache.manager import CacheManager
from quarksync.config import Settings
from quarksync.db.database import Database
from quarksync.ipc.client import IpcError, request
from quarksync.ipc.protocol import CONTROL_PIPE, CORE_PIPE, PROVIDER_PIPE, Request
from quarksync.ipc.server import NamedPipeServer
from quarksync.logging_config import bootstrap_log_dir, configure_logging
from quarksync.platform.ipc_adapter import IpcPlatformAdapter
from quarksync.status.devices import DeviceStatusStore, classify_presence
from quarksync.sync.engine import SyncEngine
from quarksync.sync.scanner_local import LocalSafetyScanner
from quarksync.sync.scheduler import Scheduler

logger = logging.getLogger(__name__)


def _normalized_absolute_path(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(value))


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.database = Database(settings.state_db)
        self.backend = OpenListWebDAVBackend(
            settings.webdav_url,
            settings.webdav_username,
            settings.webdav_password,
            remote_root=settings.remote_root,
            verify=settings.tls_verify,
        )
        self.platform = IpcPlatformAdapter(timeout_seconds=settings.ipc_timeout_seconds)
        self.cache = CacheManager(
            settings.cache_dir,
            self.database,
            max_bytes=int(settings.cache_max_gb * 1024**3),
            min_free_bytes=int(settings.cache_min_free_gb * 1024**3),
        )
        self.engine = SyncEngine(
            database=self.database,
            backend=self.backend,
            cache=self.cache,
            sync_root=settings.sync_root,
            device_name=settings.device_name,
            writeback_delay_seconds=settings.writeback_delay_seconds,
            delete_grace_seconds=settings.delete_grace_seconds,
            max_backoff_seconds=settings.max_backoff_seconds,
            platform=self.platform,
        )
        self.devices = DeviceStatusStore(
            settings.data_dir,
            self.database,
            self.backend,
            device_name=settings.device_name,
        )
        self.local_scanner = LocalSafetyScanner(
            settings.sync_root, self.database, self.engine
        )
        self.core_server: NamedPipeServer | None = None
        self.control_server: NamedPipeServer | None = None
        self.provider_process: subprocess.Popen | None = None
        self.fetch_tasks: dict[str, asyncio.Task[object]] = {}
        self.ready = False

    async def run(self) -> None:
        self.settings.ensure_directories()
        self.settings.sync_root.mkdir(parents=True, exist_ok=True)
        await self.engine.initialize()
        loop = asyncio.get_running_loop()
        self.core_server = NamedPipeServer(CORE_PIPE, self._handle_core, loop)
        self.control_server = NamedPipeServer(CONTROL_PIPE, self._handle_control, loop)
        self.core_server.start()
        self.control_server.start()
        try:
            await self._adopt_or_start_provider()
            await self._wait_for_provider()
            await self.devices.ensure_remote_layout()
            self.ready = True
            logger.info("QuarkSync core is ready; initial namespace population is starting")
            scheduler = Scheduler(
                self.engine,
                sync_interval_seconds=self.settings.sync_interval_seconds,
                remote_poll_seconds=self.settings.remote_poll_seconds,
                max_uploads=self.settings.max_uploads,
                max_downloads=self.settings.max_downloads,
                local_scanner=self.local_scanner,
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(scheduler.run(self.stop_event))
                group.create_task(self._heartbeat())
                if not self.settings.allow_core_only:
                    group.create_task(self._populate_initial_namespace())
                await self.stop_event.wait()
        finally:
            if self.control_server:
                self.control_server.close()
            if self.core_server:
                self.core_server.close()
            with contextlib.suppress(IpcError):
                await request(PROVIDER_PIPE, "STOP", timeout_seconds=3)
            if self.provider_process and self.provider_process.poll() is None:
                self.provider_process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.provider_process.wait(timeout=5)
            await self.backend.close()

    async def _populate_initial_namespace(self) -> None:
        try:
            populated = await self.engine.populate_namespace()
            logger.info("Initial namespace population completed: %d entries", populated)
        except Exception:
            logger.exception(
                "Initial namespace population failed; background polling will continue"
            )

    def _provider_executable(self) -> Path:
        if self.settings.provider_path:
            return self.settings.provider_path
        repository = Path(__file__).resolve().parents[2]
        candidates = (
            repository
            / "windows-provider"
            / "src"
            / "QuarkSync.Provider"
            / "bin"
            / "x64"
            / "Debug"
            / "net8.0-windows10.0.17763.0"
            / "win-x64"
            / "QuarkSync.Provider.exe",
            repository
            / "windows-provider"
            / "src"
            / "QuarkSync.Provider"
            / "bin"
            / "Debug"
            / "net8.0-windows"
            / "win-x64"
            / "QuarkSync.Provider.exe",
            repository
            / "windows-provider"
            / "src"
            / "QuarkSync.Provider"
            / "bin"
            / "Debug"
            / "net8.0-windows"
            / "QuarkSync.Provider.exe",
        )
        return next((path for path in candidates if path.exists()), candidates[0])

    def _start_provider(self) -> None:
        executable = self._provider_executable()
        if not executable.exists():
            if self.settings.allow_core_only:
                logger.warning(
                    "Native provider is absent; running in explicitly enabled core-only mode"
                )
                return
            raise FileNotFoundError(
                f"Native provider not found at {executable}; build windows-provider first"
            )
        provider_environment = os.environ.copy()
        provider_environment["QUARKSYNC_LOG_DIR"] = str(self.settings.log_dir)
        provider_environment["QUARKSYNC_IPC_TIMEOUT_SECONDS"] = str(
            self.settings.ipc_timeout_seconds
        )
        provider_environment["QUARKSYNC_IPC_CONNECT_TIMEOUT_SECONDS"] = str(
            self.settings.ipc_connect_timeout_seconds
        )
        provider_environment["QUARKSYNC_HYDRATION_TIMEOUT_SECONDS"] = str(
            self.settings.hydration_timeout_seconds
        )
        local_dotnet = Path(__file__).resolve().parents[2] / ".dotnet"
        if local_dotnet.exists() and "DOTNET_ROOT" not in provider_environment:
            provider_environment["DOTNET_ROOT"] = str(local_dotnet)
        self.provider_process = subprocess.Popen(  # noqa: S603 - configured local executable only
            [str(executable), "--root", str(self.settings.sync_root)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
            env=provider_environment,
        )

    async def _adopt_or_start_provider(self) -> None:
        try:
            status = await request(PROVIDER_PIPE, "STATUS", timeout_seconds=1)
        except IpcError:
            self._start_provider()
            return
        existing_root, expected_root = await asyncio.gather(
            asyncio.to_thread(
                _normalized_absolute_path, str(status.get("root", ""))
            ),
            asyncio.to_thread(_normalized_absolute_path, self.settings.sync_root),
        )
        if status.get("connected") and existing_root == expected_root:
            logger.warning(
                "Adopted an existing native provider after daemon restart: %s",
                existing_root,
            )
            return
        logger.warning(
            "Stopping native provider for a different or disconnected root: %s",
            existing_root,
        )
        with contextlib.suppress(IpcError):
            await request(PROVIDER_PIPE, "STOP", timeout_seconds=3)
        for _ in range(30):
            try:
                await request(PROVIDER_PIPE, "PING", timeout_seconds=0.1)
            except IpcError:
                break
            await asyncio.sleep(0.1)
        self._start_provider()

    async def _wait_for_provider(self) -> None:
        if self.settings.allow_core_only and self.provider_process is None:
            return
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            if self.provider_process and self.provider_process.poll() is not None:
                raise RuntimeError(
                    f"Native provider exited with code {self.provider_process.returncode}"
                )
            try:
                await self.platform.ping()
                return
            except IpcError:
                await asyncio.sleep(0.25)
        raise TimeoutError("Native provider did not become ready within 30 seconds")

    async def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.devices.publish()
                await self.devices.refresh_all()
            except Exception:
                logger.exception("Device heartbeat failed")
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.settings.heartbeat_seconds
                )
            except TimeoutError:
                pass

    async def _handle_core(self, message: Request) -> dict[str, object]:
        payload = message.payload
        if message.type == "PING":
            return {"service": "quarksync-core", "protocol_version": 1}
        if message.type == "FETCH_FILE":
            transfer_id = str(payload["transfer_id"])
            last_reported_bytes = -1
            last_reported_at = 0.0

            async def report_progress(completed: int, total: int) -> None:
                nonlocal last_reported_at, last_reported_bytes
                now = asyncio.get_running_loop().time()
                if (
                    completed < total
                    and completed - last_reported_bytes < 8 * 1024 * 1024
                    and now - last_reported_at < 0.25
                ):
                    return
                last_reported_bytes = completed
                last_reported_at = now
                try:
                    await self.platform.report_progress(
                        transfer_id, total, completed
                    )
                except (IpcError, OSError):
                    logger.debug(
                        "Unable to report hydration progress for transfer %s",
                        transfer_id,
                        exc_info=True,
                    )

            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("FETCH_FILE is not running in an asyncio task")
            self.fetch_tasks[transfer_id] = task
            try:
                return await self.engine.fetch_file(
                    str(payload["path"]),
                    payload.get("etag") and str(payload["etag"]),
                    progress=report_progress,
                )
            finally:
                self.fetch_tasks.pop(transfer_id, None)
        if message.type == "CANCEL_FETCH":
            transfer_id = str(payload["transfer_id"])
            task = self.fetch_tasks.get(transfer_id)
            if task:
                task.cancel()
            return {"cancelled": task is not None}
        if message.type == "LOCAL_CREATED":
            operation_id = await self.engine.local_created(
                str(payload["path"]), is_directory=bool(payload.get("is_directory", False))
            )
            return {"operation_id": operation_id}
        if message.type == "LOCAL_MODIFIED":
            operation_id = await self.engine.local_modified(str(payload["path"]))
            return {"operation_id": operation_id}
        if message.type == "LOCAL_RENAMED":
            operation_id = await self.engine.local_renamed(
                str(payload["source"]), str(payload["destination"])
            )
            return {"operation_id": operation_id}
        if message.type == "LOCAL_DELETED":
            operation_id = await self.engine.local_deleted(str(payload["path"]))
            return {"operation_id": operation_id}
        if message.type == "SYNC_STATE_CHANGED" and payload.get("state") == "HYDRATED":
            self.engine.hydration_completed(
                str(payload["path"]), str(payload["cache_key"])
            )
            return {}
        if message.type == "LIST_NAMESPACE":
            return {"entries": self.engine.remote_scanner.namespace()}
        if message.type == "GET_FILE_METADATA":
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM files WHERE path_key=?",
                    (str(payload["path"]).casefold(),),
                ).fetchone()
            return {"file": dict(row) if row else None}
        if message.type in {"PIN_CHANGED"}:
            return {}
        raise ValueError(f"Unsupported core message: {message.type}")

    async def _handle_control(self, message: Request) -> dict[str, object]:
        if message.type == "PING":
            return {
                "service": "quarksync-daemon",
                "pid": os.getpid(),
                "ready": self.ready,
            }
        if message.type == "STATUS":
            return self._status()
        if message.type == "PENDING":
            return {
                "operations": [
                    self._operation_dict(item) for item in self.engine.operations.pending()
                ]
            }
        if message.type == "CONFLICTS":
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM conflicts WHERE status='OPEN' ORDER BY created_at"
                ).fetchall()
            return {"conflicts": [dict(row) for row in rows]}
        if message.type == "SYNC_NOW":
            await self.engine.poll_remote_directory("")
            count = 0
            while await self.engine.process_one():
                count += 1
            return {"processed": count}
        if message.type == "STOP":
            self.stop_event.set()
            return {"stopping": True}
        raise ValueError(f"Unsupported control message: {message.type}")

    def _status(self) -> dict[str, object]:
        with self.database.connect() as connection:
            file_counts = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT sync_state, COUNT(*) FROM files GROUP BY sync_state"
                )
            }
            devices = []
            for row in connection.execute("SELECT * FROM devices"):
                device = dict(row)
                device["presence"] = classify_presence(device["last_heartbeat"])
                devices.append(device)
        return {
            "pid": os.getpid(),
            "sync_root": str(self.settings.sync_root),
            "states": file_counts,
            "pending": len(self.engine.operations.pending()),
            "devices": devices,
            "ready": self.ready,
        }

    @staticmethod
    def _operation_dict(operation) -> dict[str, object]:
        return {
            "id": operation.id,
            "type": operation.type.value,
            "path": operation.path,
            "destination_path": operation.destination_path,
            "status": operation.status.value,
            "retry_count": operation.retry_count,
            "next_retry_at": operation.next_retry_at,
            "last_error": operation.last_error,
        }


async def async_main() -> None:
    configure_logging(
        bootstrap_log_dir(), os.getenv("QUARKSYNC_LOG_LEVEL", "INFO").upper()
    )
    settings = Settings.from_env()
    settings.ensure_directories()
    daemon = Daemon(settings)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, daemon.stop_event.set)
    await daemon.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except Exception:
        logging.getLogger(__name__).exception("QuarkSync daemon stopped with an error")
        raise


if __name__ == "__main__":
    main()
