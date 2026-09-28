from __future__ import annotations

import asyncio
import logging

from quarksync.cache.manager import CachePressureError
from quarksync.sync.engine import SyncEngine
from quarksync.sync.operations import OperationType
from quarksync.sync.scanner_local import LocalSafetyScanner

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        engine: SyncEngine,
        *,
        sync_interval_seconds: int,
        remote_poll_seconds: int,
        max_uploads: int,
        max_downloads: int,
        local_scanner: LocalSafetyScanner | None = None,
    ) -> None:
        self.engine = engine
        self.sync_interval_seconds = sync_interval_seconds
        self.remote_poll_seconds = remote_poll_seconds
        self.max_uploads = max_uploads
        self.max_downloads = max_downloads
        self.local_scanner = local_scanner

    async def run(self, stop: asyncio.Event) -> None:
        workers = [
            *(self._worker(stop, OperationType.UPLOAD) for _ in range(self.max_uploads)),
            *(self._worker(stop, OperationType.DOWNLOAD) for _ in range(self.max_downloads)),
            self._worker(stop, None),
            self._remote_poll(stop),
            self._local_scan(stop),
            self._cache_maintenance(stop),
        ]
        async with asyncio.TaskGroup() as group:
            for coroutine in workers:
                group.create_task(coroutine)

    async def _worker(
        self, stop: asyncio.Event, operation_type: OperationType | None
    ) -> None:
        while not stop.is_set():
            processed = await self.engine.process_one(operation_type)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=0.1 if processed else self.sync_interval_seconds
                )
            except TimeoutError:
                pass

    async def _remote_poll(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                directory = self.engine.remote_scanner.next_directory()
                await self.engine.poll_remote_directory(directory)
            except Exception:
                logger.exception("Remote poll failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.remote_poll_seconds)
            except TimeoutError:
                pass

    async def _local_scan(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.local_scanner:
                try:
                    changed = await self.local_scanner.scan()
                    if changed:
                        logger.info("Local safety scan queued %d changed files", changed)
                except Exception:
                    logger.exception("Local safety scan failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.sync_interval_seconds)
            except TimeoutError:
                pass

    async def _cache_maintenance(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                removed = await asyncio.to_thread(self.engine.cache.evict)
                if removed:
                    logger.info("Evicted %d unprotected cache entries", removed)
            except CachePressureError as exc:
                logger.warning("Cache pressure cannot be relieved safely: %s", exc)
            except Exception:
                logger.exception("Cache maintenance failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.sync_interval_seconds)
            except TimeoutError:
                pass
