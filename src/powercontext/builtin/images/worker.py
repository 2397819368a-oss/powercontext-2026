# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runtime-owned image worker, independent of Artifact processing families."""

from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError

from powercontext._logging import log_safely
from powercontext.builtin.images.errors import ImageError
from powercontext.builtin.images.tasks import ImageClaim
from powercontext.builtin.images.vision import VisionFailure, VisionParser, prepare_image
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.persistence.errors import StoredPayloadConflictError

if TYPE_CHECKING:
    from powercontext.builtin.images.service import ImageService

logger = logging.getLogger(__name__)


def _transient_sqlite_failure(error: OperationalError) -> bool:
    code = getattr(error.orig, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
    }


class ImageWorker:
    def __init__(
        self, service: ImageService, inference: object, record_usage: Callable[[str, InferenceUsage], Awaitable[None]]
    ) -> None:
        self.service = service
        self.queue = service.queue
        self.parser = VisionParser(inference)
        self.record_usage = record_usage
        self.task: asyncio.Task[None] | None = None
        self.current: ImageClaim | None = None
        self.closing = False
        self.owner_lost = False
        self.heartbeat_stop = asyncio.Event()

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="powercontext.image-worker")

    async def aclose(self) -> None:
        self.closing = True
        self.service.wake.set()
        if self.task is not None:
            # Never cancel an aiosqlite write midway through ownership or
            # publication. The one network call has its own finite deadline.
            await asyncio.shield(self.task)

    async def _heartbeat(self) -> None:
        interval = 10
        try:
            while True:
                try:
                    await asyncio.wait_for(self.heartbeat_stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
                else:
                    return
                try:
                    renewed = await self.queue.renew_owner(self.current)
                except OperationalError as error:
                    if not _transient_sqlite_failure(error):
                        raise
                    log_safely(
                        logger,
                        logging.WARNING,
                        "Image lease renewal deferred by a recoverable database failure",
                        extra={"event": "image.worker.lease_deferred"},
                    )
                    interval = 1
                    continue
                interval = 10
                if not renewed:
                    break
        except Exception:
            log_safely(
                logger,
                logging.WARNING,
                "Image lease renewal failed; stopping this worker",
                extra={"event": "image.worker.lease_lost"},
            )
            self.closing = True
            self.service.admission_error = "image_worker_unavailable"
        self.owner_lost = True
        self.service.wake.set()

    async def _run(self) -> None:
        delay = 1
        while self.service.config.enabled and not self.closing:
            try:
                await self._run_owned()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, OperationalError) and _transient_sqlite_failure(error):
                    log_safely(
                        logger,
                        logging.WARNING,
                        "Image worker will reacquire ownership after a recoverable database failure",
                        extra={"event": "image.worker.retry_owner"},
                    )
                else:
                    # Do not retry corrupt metadata or an unknown invariant.
                    self.service.admission_error = "image_worker_unavailable"
                    log_safely(
                        logger,
                        logging.ERROR,
                        "Image worker stopped; pending tasks remain durable",
                        extra={"event": "image.worker.failed", "error_code": "image_worker_unavailable"},
                    )
                    return
            if self.closing or not self.service.config.enabled:
                return
            self.service.wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.service.wake.wait(), timeout=delay)
            delay = min(30, delay * 2)

    async def _run_owned(self) -> None:
        """Run one leased session and finish its cleanup before reacquiring."""
        heartbeat = None
        self.owner_lost = False
        self.heartbeat_stop.clear()
        try:
            while self.service.config.enabled and not self.closing:
                if await self.queue.acquire_owner():
                    break
                self.service.wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.service.wake.wait(), timeout=1)
            else:
                return
            heartbeat = asyncio.create_task(self._heartbeat(), name="powercontext.image-lease")
            while self.service.config.enabled and not self.closing and not self.owner_lost:
                self.service.wake.clear()
                self.current = await self.queue.claim()
                if self.current is None:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.service.wake.wait(), timeout=1)
                    continue
                await self._process(self.current)
                self.current = None
        finally:
            if heartbeat is not None:
                self.heartbeat_stop.set()
                await heartbeat
            try:
                if self.current is not None:
                    await self.queue.abandon(self.current)
                await self.queue.release_owner()
            except Exception:
                log_safely(
                    logger,
                    logging.WARNING,
                    "Image lease will recover after expiry",
                    extra={"event": "image.worker.recovery_pending", "error_code": "image_worker_unavailable"},
                )
            self.current = None

    async def _process(self, claim: ImageClaim) -> None:
        usage = InferenceUsage(requests=0)
        try:
            headers = self.parser.headers_for(claim.profile)
            task = await self.service.get_task(claim.scope_id, claim.task.task_id)
            if task.original_payload is None:
                raise VisionFailure("image_original_unavailable")  # noqa: TRY301 - normalize a persisted invariant at this boundary
            original = await self.service.get_asset(claim.scope_id, task.original_payload.asset.asset_id)
            prepared = await asyncio.to_thread(
                prepare_image, base64.b64decode(original.bytes_base64, validate=True), original.asset.media_type.value
            )
            if not await self.queue.record_request(claim, prepared.digest):
                return
            result = await self.parser.parse(claim.profile, claim.context, prepared, headers)
            usage = result.usage
            await self.queue.finish(claim, result=result, input_digest=prepared.digest)
        except VisionFailure as error:
            usage = error.usage
            await self.queue.finish(claim, failure=error)
        except ImageError as error:
            await self.queue.finish(claim, failure=VisionFailure(error.code, usage=usage))
        except StoredPayloadConflictError:
            await self.queue.finish(claim, failure=VisionFailure("image_analysis_conflict", usage=usage))
        finally:
            if usage.requests:
                try:
                    await self.record_usage(claim.scope_id, usage)
                except Exception:
                    log_safely(
                        logger,
                        logging.WARNING,
                        "Image usage telemetry unavailable; durable attempt retained",
                        extra={"event": "image.worker.telemetry_unavailable"},
                    )
