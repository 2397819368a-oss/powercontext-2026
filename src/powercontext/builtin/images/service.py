# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Durable image acceptance, historical reads and opt-in worker ownership."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import hashlib
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from sqlalchemy import insert, inspect, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext._logging import log_safely
from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.images import tables
from powercontext.builtin.images.config import ImageConfig, parse_profile
from powercontext.builtin.images.errors import ImageError
from powercontext.builtin.images.identity import canonical_image_digest, image_profile_digest, original_image_source_id
from powercontext.builtin.images.models import IMAGE_ANALYSIS_TYPE, ORIGINAL_IMAGE_TYPE, OriginalImageSource
from powercontext.builtin.images.tasks import ImageTaskQueue, task_value
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.processing import ArtifactProcessingPendingRepository
from powercontext.builtin.persistence.schema import create_tables
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SCOPES_TABLE, SOURCE_DEFINITION_MANIFESTS_TABLE
from powercontext.http import (
    ExperimentalCaptureImageRequest,
    ExperimentalCaptureImageResponse,
    ExperimentalGetImageAssetResponse,
    ExperimentalImageAnalysisPayload,
    ExperimentalImageAsset,
    ExperimentalImageAttempt,
    ExperimentalImageOriginalPayload,
    ExperimentalImageParseProfile,
    ExperimentalImageStatus,
    ExperimentalImageTask,
    ExperimentalImageTaskPage,
    ExperimentalListImageTasksRequest,
)
from powercontext.paths import powercontext_data_dir
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceRef,
)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_T = TypeVar("_T")


class ImageService:
    """Own image acceptance, budgets, assets and pending tasks on one SQLite DB."""

    @classmethod
    async def open(
        cls,
        database: AsyncDatabase,
        sources: SourceRepository,
        pending: ArtifactProcessingPendingRepository,
        config: ImageConfig,
        inference: object,
        registry: SourceDefinitionRegistry,
    ) -> ImageService:
        try:
            profile = parse_profile(inference) if config.enabled else None
        except ValueError:
            profile = None
        service = cls(database, sources, pending, config, profile, registry)
        service.vision_connection = "generation" if getattr(inference, "vision_use_generation", False) else "dedicated"
        try:
            await service.initialize()
        except (ImageError, ValueError, SQLAlchemyError) as error:
            service.available = False
            service.unavailable_code = error.code if isinstance(error, ImageError) else "image_storage_unavailable"
        if config.enabled and service.available:
            try:
                await service.cleanup_orphan_assets()
            except (ImageError, OSError, ValueError, SQLAlchemyError) as error:
                service.admission_error = error.code if isinstance(error, ImageError) else "image_storage_unavailable"
                log_safely(
                    logger,
                    logging.WARNING,
                    "Image orphan cleanup failed; new image submissions are unavailable",
                    extra={"event": "image.storage.maintenance_failed", "error_code": service.admission_error},
                )
        return service

    def __init__(
        self,
        database: AsyncDatabase,
        sources: SourceRepository,
        pending: ArtifactProcessingPendingRepository,
        config: ImageConfig,
        profile: ExperimentalImageParseProfile | None,
        registry: SourceDefinitionRegistry,
    ) -> None:
        self.database = database
        self.sources = sources
        self.pending = pending
        self.config = config
        self.profile = profile
        self.registry = registry
        self.root = (config.asset_directory or powercontext_data_dir() / "image-assets").expanduser().resolve()
        self.available = False
        self.admission_error: str | None = None
        self.unavailable_code = "image_storage_unavailable"
        self.wake = asyncio.Event()
        self.queue = ImageTaskQueue(self)
        self.worker = None
        self.had_metadata = False
        self.vision_connection = "dedicated"

    def start_worker(self, inference: object, record_usage: Callable[[str, InferenceUsage], Awaitable[None]]) -> None:
        """Called by the worker-capable Runtime only after composition succeeds."""
        if self.config.enabled and self.available and self.worker is None:
            from powercontext.builtin.images.worker import ImageWorker

            self.worker = ImageWorker(self, inference, record_usage)
            self.worker.start()

    async def aclose(self) -> None:
        if self.worker is not None:
            await self.worker.aclose()

    async def initialize(self) -> None:
        """Open existing metadata even when disabled; never create assets here."""

        if self.database.engine.dialect.name != "sqlite":
            self.unavailable_code = "image_backend_unsupported"
            return
        async with self.database.transaction() as connection:
            # sqlite3 legacy mode does not start a transaction for schema reads
            # or DDL. Serialize inspection/migration and make every ALTER atomic.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            databases = (await connection.exec_driver_sql("PRAGMA database_list")).mappings().all()
            journal = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
            synchronous = (await connection.exec_driver_sql("PRAGMA synchronous")).scalar_one()
            if (
                not any(row["name"] == "main" and row["file"] for row in databases)
                or journal not in {"wal", "delete"}
                or synchronous < 2
            ):
                self.unavailable_code = "image_database_not_durable"
                return
            occupied = await connection.scalar(
                select(SOURCE_DEFINITION_MANIFESTS_TABLE.c.definition_name)
                .where(
                    SOURCE_DEFINITION_MANIFESTS_TABLE.c.definition_name.in_((
                        ORIGINAL_IMAGE_TYPE,
                        IMAGE_ANALYSIS_TYPE,
                    ))
                )
                .limit(1)
            )
            if occupied is not None:
                self.unavailable_code = "image_definition_conflict"
                return
            exists = await connection.run_sync(lambda sync: sync.dialect.has_table(sync, tables.STATE.name))
            self.had_metadata = exists
            if not exists and not self.config.enabled:
                return
            if exists:
                await self._open_existing_metadata(connection)
            else:
                await create_tables(connection, tables.TABLES)
                await connection.execute(
                    insert(tables.STATE).values(
                        id=1, format_version="1", asset_directory=str(self.root), asset_bytes=0, pending_count=0
                    )
                )
        self.available = True

    async def _open_existing_metadata(self, connection: AsyncConnection) -> None:
        """Validate core format before any SELECT using new lifecycle columns."""
        columns_by_table = {}
        for table in tables.CORE_TABLES:
            columns = await connection.run_sync(
                lambda sync, owned=table: (
                    {column["name"] for column in inspect(sync).get_columns(owned.name)}
                    if inspect(sync).has_table(owned.name)
                    else set()
                )
            )
            columns_by_table[table.name] = columns
            required = set(table.c.keys()) - tables.LIFECYCLE_COLUMNS.get(table.name, {}).keys()
            if table is tables.TASKS:
                required -= {"created_at"}
            if not required <= columns:
                raise ImageError(503, "image_format_unsupported")
        row = (
            (
                await connection.execute(
                    select(tables.STATE.c.format_version, tables.STATE.c.asset_directory).where(tables.STATE.c.id == 1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["format_version"] != "1"
            or not isinstance(row["asset_directory"], str)
            or not Path(row["asset_directory"]).is_absolute()
        ):
            raise ImageError(503, "image_format_unsupported")
        if self.config.asset_directory is None:
            stored_root = await asyncio.to_thread(lambda: Path(row["asset_directory"]).expanduser().resolve())
            if str(stored_root) != row["asset_directory"]:
                raise ImageError(503, "image_asset_directory_changed")
            self.root = stored_root
        if row["asset_directory"] != str(self.root):
            raise ImageError(503, "image_asset_directory_changed")
        if "created_at" not in columns_by_table[tables.TASKS.name]:
            # Legacy tasks keep an unknown acceptance date; never invent historical evidence.
            await connection.exec_driver_sql(
                'ALTER TABLE "pc_experimental_image_tasks" ADD COLUMN "created_at" INTEGER NOT NULL DEFAULT 0'
            )
        await self._upgrade_metadata(connection, columns_by_table)
        for index in tables.TASKS.indexes:
            await connection.run_sync(lambda sync, owned=index: owned.create(sync, checkfirst=True))

    async def _upgrade_metadata(self, connection: AsyncConnection, columns_by_table: dict[str, set[str]]) -> None:
        missing = {name: set(columns) - columns_by_table[name] for name, columns in tables.LIFECYCLE_COLUMNS.items()}
        if any(missing.values()):
            if missing != {name: set(columns) for name, columns in tables.LIFECYCLE_COLUMNS.items()}:
                raise ImageError(503, "image_format_unsupported")
            await self._migrate_phase_two_metadata(connection)
        for table in (tables.ATTEMPTS, tables.OPERATIONS):
            compatible = await connection.run_sync(
                lambda sync, owned=table: (
                    inspect(sync).has_table(owned.name)
                    and set(owned.c.keys()) <= {column["name"] for column in inspect(sync).get_columns(owned.name)}
                )
            )
            if not compatible:
                raise ImageError(503, "image_format_unsupported")

    async def _migrate_phase_two_metadata(self, connection: AsyncConnection) -> None:
        """Add bounded task metadata without rewriting any Source or identity."""
        for table in (tables.ATTEMPTS, tables.OPERATIONS):
            if await connection.run_sync(lambda sync, owned=table: inspect(sync).has_table(owned.name)):
                raise ImageError(503, "image_format_unsupported")
        old_tasks = (
            (
                await connection.execute(
                    select(tables.TASKS.c.scope_id, tables.TASKS.c.task_id, tables.TASKS.c.payload).limit(1025)
                )
            )
            .mappings()
            .all()
        )
        if len(old_tasks) > 1024 or any(
            task_value(item["payload"], item["scope_id"]).state.value != "pending" for item in old_tasks
        ):
            raise ImageError(503, "image_format_unsupported")
        for name, columns in tables.LIFECYCLE_COLUMNS.items():
            for column, sql_type in columns.items():
                await connection.exec_driver_sql(f'ALTER TABLE "{name}" ADD COLUMN "{column}" {sql_type}')
        await create_tables(connection, (tables.ATTEMPTS, tables.OPERATIONS))
        for index in tables.TASKS.indexes:
            await connection.run_sync(lambda sync, owned=index: owned.create(sync, checkfirst=True))
        for item in old_tasks:
            task = task_value(item["payload"], item["scope_id"])
            await connection.execute(
                update(tables.TASKS)
                .where(tables.TASKS.c.scope_id == item["scope_id"], tables.TASKS.c.task_id == item["task_id"])
                .values(original_source_id=task.original.source.source_id)
            )

    def _require_available(self) -> None:
        if not self.available:
            raise ImageError(503, self.unavailable_code)

    def _require_admission_available(self) -> None:
        self._require_available()
        if self.admission_error is not None:
            raise ImageError(503, self.admission_error)

    async def capture(self, request: ExperimentalCaptureImageRequest) -> ExperimentalCaptureImageResponse:  # noqa: C901
        """Accept only after the original, Source and parse intent are durable."""

        if not self.config.enabled:
            raise ImageError(503, "image_disabled")
        if not self.available and self.unavailable_code == "image_definition_conflict":
            raise ImageError(409, self.unavailable_code)
        self._require_admission_available()
        data = decode_image_bytes(request.bytes_base64)
        asset = await asyncio.to_thread(validate_image, data, request.media_type.value)
        source_id = original_image_source_id(request.scope_id, request.capture_key, request.origin.attachment_index)
        source = OriginalImageSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            asset=asset,
            capture_key=request.capture_key,
            origin=request.origin,
            context=request.context,
        )
        request_digest = canonical_image_digest(source.model_dump(mode="json"))
        await self._reserve_asset(request, asset, source_id, request_digest)
        async with self.database.transaction() as connection:
            # First statement obtains SQLite's writer lock. The fixed budget row
            # serializes acceptance across processes without a read/upgrade race.
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(pending_count=tables.STATE.c.pending_count)
            )
            replay = (
                (
                    await connection.execute(
                        select(tables.CAPTURES).where(
                            tables.CAPTURES.c.scope_id == request.scope_id, tables.CAPTURES.c.source_id == source_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                if replay["request_digest"] != request_digest:
                    raise ImageError(409, "image_capture_conflict")
                response = ExperimentalCaptureImageResponse.model_validate_json(replay["response"])
                await asyncio.to_thread(self._read, request.scope_id, response.asset)
                return response.model_copy(update={"replayed": True})
            if self.profile is None:
                raise ImageError(503, "image_vision_not_configured")
            scope = await connection.scalar(
                select(SCOPES_TABLE.c.scope_id).where(SCOPES_TABLE.c.scope_id == request.scope_id)
            )
            if scope is None:
                raise ImageError(422, "image_scope_not_found")
            await self._validate_context(connection, request)
            budget = (await connection.execute(select(tables.STATE).where(tables.STATE.c.id == 1))).mappings().one()
            scoped = await connection.scalar(
                select(tables.SCOPE_BUDGETS.c.pending_count).where(tables.SCOPE_BUDGETS.c.scope_id == request.scope_id)
            )
            if budget["pending_count"] >= self.config.max_pending_total or (
                scoped is not None and scoped >= self.config.max_pending_per_scope
            ):
                raise ImageError(503, "image_queue_full")
            existing_asset = (
                (
                    await connection.execute(
                        select(tables.ASSETS).where(
                            tables.ASSETS.c.scope_id == request.scope_id, tables.ASSETS.c.asset_id == asset.asset_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_asset is None:
                raise ImageError(500, "image_reservation_missing")
            if ExperimentalImageAsset.model_validate_json(existing_asset["payload"]) != asset:
                raise ImageError(500, "image_asset_corrupt")
            if existing_asset["state"] == "reserved":
                await _complete_file_io(self._publish, request.scope_id, asset, data)
                await connection.execute(
                    update(tables.ASSETS)
                    .where(tables.ASSETS.c.scope_id == request.scope_id, tables.ASSETS.c.asset_id == asset.asset_id)
                    .values(state="retained")
                )
            else:
                await asyncio.to_thread(self._read, request.scope_id, asset)
            stored, created = await self.sources.add_with_status(connection, request.scope_id, source)
            if created:
                await self.pending.raise_source(
                    connection, request.scope_id, TOPIC_MEMORY_SOURCE_WINDOW_BINDING, stored.journal_position
                )
            original = {"scope_id": request.scope_id, "source": stored.ref.model_dump(mode="json")}
            profile_digest = image_profile_digest(self.profile)
            context_digest = canonical_image_digest(request.context.model_dump(mode="json"))
            task_id = "it_" + canonical_image_digest({
                "contract_version": "1",
                "kind": "capture",
                "original": original,
                "profile_digest": profile_digest,
                "context_digest": context_digest,
            }).removeprefix("sha256:")
            task = ExperimentalImageTask.model_validate({
                "task_id": task_id,
                "original": original,
                "state": "pending",
                "revision": 1,
                "attempt_count": 0,
                "max_attempts": self.profile.max_attempts,
                "profile_digest": profile_digest,
                "analysis": None,
                "error_code": None,
                "next_attempt_at": None,
            })
            await connection.execute(
                insert(tables.TASKS).values(
                    scope_id=request.scope_id,
                    task_id=task_id,
                    state="pending",
                    profile=_dump(self.profile),
                    context=_dump(request.context),
                    payload=_dump(task),
                    original_source_id=source_id,
                    available_at=int(time.time()),
                    created_at=int(time.time()),
                )
            )
            if scoped is None:
                await connection.execute(
                    insert(tables.SCOPE_BUDGETS).values(scope_id=request.scope_id, pending_count=1)
                )
            else:
                await connection.execute(
                    update(tables.SCOPE_BUDGETS)
                    .where(tables.SCOPE_BUDGETS.c.scope_id == request.scope_id)
                    .values(pending_count=scoped + 1)
                )
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(pending_count=budget["pending_count"] + 1)
            )
            response = ExperimentalCaptureImageResponse.model_validate({
                "status": "accepted",
                "original": original,
                "asset": asset.model_dump(mode="json"),
                "sequence": stored.journal_position,
                "task_id": task_id,
                "replayed": False,
            })
            await connection.execute(
                insert(tables.CAPTURES).values(
                    scope_id=request.scope_id,
                    source_id=source_id,
                    request_digest=request_digest,
                    response=_dump(response),
                )
            )
        self.wake.set()
        return response

    async def _reserve_asset(
        self,
        request: ExperimentalCaptureImageRequest,
        asset: ExperimentalImageAsset,
        source_id: str,
        request_digest: str,
    ) -> None:
        """Charge bytes before file IO so a failed commit cannot evade the cap."""
        async with self.database.transaction() as connection:
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(asset_bytes=tables.STATE.c.asset_bytes)
            )
            replay = await connection.scalar(
                select(tables.CAPTURES.c.request_digest).where(
                    tables.CAPTURES.c.scope_id == request.scope_id, tables.CAPTURES.c.source_id == source_id
                )
            )
            if replay is not None:
                if replay != request_digest:
                    raise ImageError(409, "image_capture_conflict")
                return
            if self.profile is None:
                raise ImageError(503, "image_vision_not_configured")
            if (
                await connection.scalar(
                    select(SCOPES_TABLE.c.scope_id).where(SCOPES_TABLE.c.scope_id == request.scope_id)
                )
                is None
            ):
                raise ImageError(422, "image_scope_not_found")
            await self._validate_context(connection, request)
            state = (await connection.execute(select(tables.STATE).where(tables.STATE.c.id == 1))).mappings().one()
            scoped = await connection.scalar(
                select(tables.SCOPE_BUDGETS.c.pending_count).where(tables.SCOPE_BUDGETS.c.scope_id == request.scope_id)
            )
            if state["pending_count"] >= self.config.max_pending_total or (
                scoped is not None and scoped >= self.config.max_pending_per_scope
            ):
                raise ImageError(503, "image_queue_full")
            present = await connection.scalar(
                select(tables.ASSETS.c.state).where(
                    tables.ASSETS.c.scope_id == request.scope_id, tables.ASSETS.c.asset_id == asset.asset_id
                )
            )
            if present is not None:
                if present == "reserved":
                    # Recovered reservations may be older than the orphan grace
                    # period. Renew before releasing the writer lock so another
                    # process cannot reclaim an upload now being accepted.
                    await connection.execute(
                        update(tables.ASSETS)
                        .where(tables.ASSETS.c.scope_id == request.scope_id, tables.ASSETS.c.asset_id == asset.asset_id)
                        .values(reserved_at=int(time.time()))
                    )
                return
            if state["asset_bytes"] + asset.byte_length > self.config.max_asset_bytes_total:
                raise ImageError(503, "image_storage_budget")
            await connection.execute(
                insert(tables.ASSETS).values(
                    scope_id=request.scope_id,
                    asset_id=asset.asset_id,
                    payload=_dump(asset),
                    state="reserved",
                    reserved_at=int(time.time()),
                )
            )
            await connection.execute(
                update(tables.STATE)
                .where(tables.STATE.c.id == 1)
                .values(asset_bytes=state["asset_bytes"] + asset.byte_length)
            )

    async def cleanup_orphan_assets(self) -> int:
        """Reclaim at most 100 old reservations without file-tree or Source scans."""
        removed = 0
        async with self.database.transaction() as connection:
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(asset_bytes=tables.STATE.c.asset_bytes)
            )
            rows = (
                (
                    await connection.execute(
                        select(tables.ASSETS)
                        .where(
                            tables.ASSETS.c.state == "reserved", tables.ASSETS.c.reserved_at < int(time.time()) - 86_400
                        )
                        .order_by(tables.ASSETS.c.reserved_at)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                asset = ExperimentalImageAsset.model_validate_json(row["payload"])
                await _complete_file_io(self._discard, row["scope_id"], asset.asset_id)
                await connection.execute(
                    tables.ASSETS.delete().where(
                        tables.ASSETS.c.scope_id == row["scope_id"], tables.ASSETS.c.asset_id == asset.asset_id
                    )
                )
                await connection.execute(
                    update(tables.STATE)
                    .where(tables.STATE.c.id == 1)
                    .values(asset_bytes=tables.STATE.c.asset_bytes - asset.byte_length)
                )
                removed += 1
        return removed

    async def _validate_context(self, connection: AsyncConnection, request: ExperimentalCaptureImageRequest) -> None:
        context_source = request.context.source
        if context_source is None:
            return
        if context_source.scope_id != request.scope_id:
            raise ImageError(422, "image_context_scope_mismatch")
        try:
            stored = await self.sources.get(
                connection, request.scope_id, SourceRef.model_validate(context_source.source.model_dump(mode="json"))
            )
            if TEXT_EVIDENCE_PROJECTION_KEY not in self.registry.projection_keys(stored.value):
                raise ImageError(422, "image_context_not_text")
        except RepositoryNotFoundError as error:
            raise ImageError(422, "image_context_not_found") from error

    async def get_asset(self, scope_id: str, asset_id: str) -> ExperimentalGetImageAssetResponse:
        """Read exact retained bytes even when capture and Vision are disabled."""

        self._require_available()
        async with self.database.transaction() as connection:
            payload = await connection.scalar(
                select(tables.ASSETS.c.payload).where(
                    tables.ASSETS.c.scope_id == scope_id,
                    tables.ASSETS.c.asset_id == asset_id,
                    tables.ASSETS.c.state == "retained",
                )
            )
        if payload is None:
            raise ImageError(404, "image_asset_not_found")
        asset = ExperimentalImageAsset.model_validate_json(payload)
        data = await asyncio.to_thread(self._read, scope_id, asset)
        return ExperimentalGetImageAssetResponse(asset=asset, bytes_base64=base64.b64encode(data).decode("ascii"))

    async def status(self, scope_id: str) -> ExperimentalImageStatus:
        count = 0
        if self.available:
            async with self.database.transaction() as connection:
                count = (
                    await connection.scalar(
                        select(tables.SCOPE_BUDGETS.c.pending_count).where(tables.SCOPE_BUDGETS.c.scope_id == scope_id)
                    )
                    or 0
                )
        running = self.worker is not None and self.worker.task is not None and not self.worker.task.done()
        return ExperimentalImageStatus.model_validate({
            "enabled": self.config.enabled,
            "available": self.available,
            "vision_configured": self.profile is not None,
            "vision_connection": self.vision_connection,
            "model_id": None if self.profile is None else self.profile.model_id,
            "pending_count": count,
            "worker_running": running,
            "error_code": (
                self.unavailable_code
                if not self.available and (self.config.enabled or self.had_metadata)
                else self.admission_error
            )
            or ("image_vision_not_configured" if self.config.enabled and self.profile is None else None),
        })

    async def list_tasks(self, request: ExperimentalListImageTasksRequest) -> ExperimentalImageTaskPage:
        """Two bounded queries per page, using the indexed acceptance order and exact Sources."""
        from sqlalchemy import and_, or_

        self._require_available()
        table = tables.TASKS
        query = select(table.c.payload, table.c.created_at, table.c.task_id).where(table.c.scope_id == request.scope_id)
        scope_digest = hashlib.sha256(request.scope_id.encode()).hexdigest()[:16]
        if request.cursor:
            match = re.fullmatch(r"v1:([a-f0-9]{16}):(\d{1,12}):(it_[a-f0-9]{64})", request.cursor)
            if match is None or match[1] != scope_digest:
                raise ImageError(422, "image_cursor_invalid")
            accepted, task_id = int(match[2]), match[3]
            query = query.where(
                or_(table.c.created_at < accepted, and_(table.c.created_at == accepted, table.c.task_id < task_id))
            )
        query = query.order_by(table.c.created_at.desc(), table.c.task_id.desc()).limit(request.limit + 1)
        async with self.database.transaction() as connection:
            await connection.exec_driver_sql("BEGIN")
            rows = (await connection.execute(query)).mappings().all()
            tasks = [task_value(row["payload"], request.scope_id) for row in rows[: request.limit]]
            sources = await self.sources.get_many(
                connection,
                request.scope_id,
                [SourceRef.model_validate(task.original.source.model_dump(mode="json")) for task in tasks],
            )
        originals = {
            source.ref.source_id: OriginalImageSource.model_validate(source.value.model_dump(mode="json"))
            for source in sources
        }
        items = [
            {
                "task": task.model_dump(mode="json"),
                "asset": originals[task.original.source.source_id].asset.model_dump(mode="json"),
                "origin": originals[task.original.source.source_id].origin.model_dump(mode="json"),
                "created_at": datetime.fromtimestamp(row["created_at"], UTC) if row["created_at"] else None,
            }
            for row, task in zip(rows[: request.limit], tasks, strict=True)
        ]
        last = rows[request.limit - 1] if len(rows) > request.limit else None
        return ExperimentalImageTaskPage.model_validate({
            "items": items,
            "next_cursor": f"v1:{scope_digest}:{last['created_at']}:{last['task_id']}" if last else None,
        })

    async def get_task(
        self, scope_id: str, task_id: str | None = None, *, analysis_source_id: str | None = None
    ) -> ExperimentalImageTask:
        """Read exact history by task or the Source citation carried by Artifacts."""

        self._require_available()
        if (task_id is None) == (analysis_source_id is None):
            raise ImageError(422, "image_task_selector_invalid")
        async with self.database.transaction() as connection:
            # Status, Sources and receipts must belong to one actual SQLite
            # snapshot even when a worker completes during this read.
            await connection.exec_driver_sql("BEGIN")
            selector = (
                tables.TASKS.c.task_id == task_id
                if task_id is not None
                else tables.TASKS.c.analysis_source_id == analysis_source_id
            )
            payload = await connection.scalar(
                select(tables.TASKS.c.payload).where(
                    tables.TASKS.c.scope_id == scope_id,
                    selector,
                )
            )
            if payload is None:
                raise ImageError(404, "image_task_not_found")
            task = task_value(payload, scope_id)
            source = await self.sources.get(
                connection, scope_id, SourceRef.model_validate(task.original.source.model_dump(mode="json"))
            )
            original = ExperimentalImageOriginalPayload.model_validate(source.value.model_dump(mode="json"))
            analysis = None
            if task.analysis is not None:
                stored = await self.sources.get(
                    connection, scope_id, SourceRef.model_validate(task.analysis.source.model_dump(mode="json"))
                )
                analysis = ExperimentalImageAnalysisPayload.model_validate(stored.value.model_dump(mode="json"))
            attempts = (
                (
                    await connection.execute(
                        select(tables.ATTEMPTS.c.payload)
                        .where(
                            tables.ATTEMPTS.c.scope_id == scope_id,
                            tables.ATTEMPTS.c.task_id == task.task_id,
                        )
                        .order_by(tables.ATTEMPTS.c.attempt)
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
        return ExperimentalImageTask.model_validate({
            **task.model_dump(mode="json"),
            "original_payload": original.model_dump(mode="json"),
            "analysis_payload": None if analysis is None else analysis.model_dump(mode="json"),
            "attempts": [
                ExperimentalImageAttempt.model_validate_json(item).model_dump(mode="json") for item in attempts
            ],
        })

    def _path(self, scope_id: str, asset_id: str) -> Path:
        folder = self.root / hashlib.sha256(scope_id.encode("utf-8")).hexdigest()
        if sys.platform == "win32" and not str(folder).startswith("\\\\?\\"):
            # Keep the persisted root identity, but use the Win32 extended form
            # for both validation and I/O of deep workspace asset filenames.
            native = str(folder)
            folder = Path("\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native)
        path = folder / asset_id
        if self.root.resolve() != self.root or folder.resolve() != folder or path.is_symlink():
            raise ImageError(500, "image_asset_path_invalid")
        return path

    def _publish(self, scope_id: str, asset: ExperimentalImageAsset, data: bytes) -> None:
        path = self._path(scope_id, asset.asset_id)
        _create_asset_directory(path.parent)
        self._path(scope_id, asset.asset_id)
        if path.exists():
            self._read(scope_id, asset)
            return
        # One indexed reservation owns this temporary name. A crash leaves
        # enough information for retry and cleanup without scanning the tree.
        temporary = path.with_name(".image-" + asset.asset_id)
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _discard(self, scope_id: str, asset_id: str) -> None:
        path = self._path(scope_id, asset_id)
        path.unlink(missing_ok=True)
        path.with_name(".image-" + asset_id).unlink(missing_ok=True)
        if sys.platform != "win32" and path.parent.exists():
            _sync_directory(path.parent)

    def _read(self, scope_id: str, asset: ExperimentalImageAsset) -> bytes:
        try:
            with self._path(scope_id, asset.asset_id).open("rb") as handle:
                data = handle.read(asset.byte_length + 1)
        except FileNotFoundError as error:
            raise ImageError(410, "image_original_unavailable") from error
        if len(data) != asset.byte_length or "sha256:" + hashlib.sha256(data).hexdigest() != asset.digest.root:
            raise ImageError(500, "image_asset_corrupt")
        return data


def decode_image_bytes(encoded: str) -> bytes:
    """Decode bounded transport bytes; paths, URLs and data URIs are invalid."""

    if len(encoded) > MAX_ENCODED_IMAGE_BYTES:
        raise ImageError(413, "image_too_large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ImageError(422, "image_invalid_base64") from error
    if not data:
        raise ImageError(422, "image_empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(413, "image_too_large")
    return data


def validate_image(data: bytes, media_type: str) -> ExperimentalImageAsset:
    """Check format, dimensions and a fully decoded single frame."""

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise ImageError(503, "image_decoder_unavailable") from error
    expected_format = {"image/png": "PNG", "image/jpeg": "JPEG"}[media_type]
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != expected_format or getattr(image, "n_frames", 1) != 1:
                raise ImageError(415, "image_format_unsupported")
            width, height = image.size
            if min(width, height) < 1 or max(width, height) > 16_384 or width * height > 20_000_000:
                raise ImageError(413, "image_pixel_budget")
            image.verify()
        with Image.open(BytesIO(data), formats=[expected_format]) as image:
            image.load()
    except Image.DecompressionBombError as error:
        raise ImageError(413, "image_pixel_budget") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise ImageError(422, "image_invalid_data") from error
    digest = hashlib.sha256(data).hexdigest()
    return ExperimentalImageAsset.model_validate({
        "asset_id": "ia_" + digest,
        "digest": "sha256:" + digest,
        "media_type": media_type,
        "byte_length": len(data),
        "width": width,
        "height": height,
    })


def _dump(value: Any) -> bytes:
    return value.model_dump_json().encode("utf-8")


async def _complete_file_io(operation: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Keep the DB writer lock until owned file work completes on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


def _create_asset_directory(directory: Path) -> None:
    missing: list[Path] = []
    ancestor = directory
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        for created in reversed(missing):
            _sync_directory(created.parent)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    if sys.platform == "win32":
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        if not move(str(source), str(destination), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "cannot publish image asset")
    else:
        os.replace(source, destination)
        _sync_directory(destination.parent)
