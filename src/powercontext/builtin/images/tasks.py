# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Durable, fenced single-worker queue and immutable retry/reparse intents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.images import tables
from powercontext.builtin.images.errors import ImageError
from powercontext.builtin.images.identity import canonical_image_digest, image_profile_digest
from powercontext.builtin.images.models import ImageAnalysisSource, OriginalImageSource
from powercontext.builtin.images.vision import VisionFailure, VisionResult
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.http import (
    ExperimentalImageAttempt,
    ExperimentalImageContext,
    ExperimentalImageDigest,
    ExperimentalImageParseProfile,
    ExperimentalImageTask,
    ExperimentalReparseImageRequest,
    ExperimentalRetryImageTaskRequest,
)
from powercontext.sources import SourceMaterialization, SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.images.service import ImageService


def dump(value: BaseModel) -> bytes:
    return value.model_dump_json().encode("utf-8")


def task_value(payload: bytes, scope_id: str) -> ExperimentalImageTask:
    """Validate persisted state invariants as well as the generated shape."""

    task = ExperimentalImageTask.model_validate_json(payload)
    state = task.state.value
    if (
        task.original.scope_id != scope_id
        or task.original.source.source_type != "experimental.image.original"
        or task.attempt_count > task.max_attempts
        or (task.analysis is not None) != (state == "succeeded")
        or (task.next_attempt_at is not None) != (state == "retry_wait")
        or (state == "failed" and task.error_code is None)
        or (state == "succeeded" and task.error_code is not None)
        or (
            task.analysis is not None
            and (
                task.analysis.scope_id != scope_id or task.analysis.source.source_type != "experimental.image.analysis"
            )
        )
    ):
        raise ImageError(500, "image_task_corrupt")
    return task


def transition(task: ExperimentalImageTask, state: str, **values: Any) -> ExperimentalImageTask:
    return task_value(
        dump(
            ExperimentalImageTask.model_validate({
                **task.model_dump(mode="json"),
                "state": state,
                "revision": task.revision + 1,
                **values,
            })
        ),
        task.original.scope_id,
    )


@dataclass(frozen=True)
class ImageClaim:
    owner: str
    token: str
    task: ExperimentalImageTask
    profile: ExperimentalImageParseProfile
    context: ExperimentalImageContext

    @property
    def scope_id(self) -> str:
        return self.task.original.scope_id


class ImageTaskQueue:
    """Serialize short metadata transitions, never decoder or provider work."""

    def __init__(self, service: ImageService) -> None:
        self.service = service
        self.database = service.database
        self.owner = str(uuid4())
        self.lease_seconds = 30

    async def _lock(self, connection: AsyncConnection) -> Any:
        await connection.execute(
            update(tables.STATE).where(tables.STATE.c.id == 1).values(pending_count=tables.STATE.c.pending_count)
        )
        return (await connection.execute(select(tables.STATE).where(tables.STATE.c.id == 1))).mappings().one()

    async def acquire_owner(self) -> bool:
        async with self.database.transaction() as connection:
            row = await self._lock(connection)
            now = int(time.time())
            if row["worker_owner"] not in {None, self.owner} and row["worker_lease_until"] > now:
                return False
            await connection.execute(
                update(tables.STATE)
                .where(tables.STATE.c.id == 1)
                .values(
                    worker_owner=self.owner,
                    worker_lease_until=now + self.lease_seconds,
                )
            )
        return True

    async def renew_owner(self, claim: ImageClaim | None) -> bool:
        async with self.database.transaction() as connection:
            row = await self._lock(connection)
            now = int(time.time())
            if row["worker_owner"] != self.owner or row["worker_lease_until"] <= now:
                return False
            until = now + self.lease_seconds
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(worker_lease_until=until)
            )
            if claim is not None:
                await connection.execute(update(tables.TASKS).where(*self._fence(claim)).values(lease_until=until))
        return True

    async def release_owner(self) -> None:
        async with self.database.transaction() as connection:
            await self._lock(connection)
            await connection.execute(
                update(tables.STATE)
                .where(
                    tables.STATE.c.id == 1,
                    tables.STATE.c.worker_owner == self.owner,
                )
                .values(worker_owner=None, worker_lease_until=0)
            )

    @staticmethod
    def _claim_identity(claim: ImageClaim) -> tuple[Any, ...]:
        return (
            tables.TASKS.c.scope_id == claim.scope_id,
            tables.TASKS.c.task_id == claim.task.task_id,
            tables.TASKS.c.state == "running",
            tables.TASKS.c.claim_token == claim.token,
        )

    @classmethod
    def _fence(cls, claim: ImageClaim) -> tuple[Any, ...]:
        return (*cls._claim_identity(claim), tables.TASKS.c.lease_until > int(time.time()))

    async def _claimed(self, connection: AsyncConnection, claim: ImageClaim) -> bool:
        owner = await connection.scalar(
            select(tables.STATE.c.worker_owner).where(
                tables.STATE.c.id == 1,
                tables.STATE.c.worker_lease_until > int(time.time()),
            )
        )
        if owner != claim.owner:
            return False
        payload = await connection.scalar(select(tables.TASKS.c.payload).where(*self._fence(claim)))
        return payload is not None and task_value(payload, claim.scope_id).revision == claim.task.revision

    async def claim(self) -> ImageClaim | None:
        async with self.database.transaction() as connection:
            row = await self._lock(connection)
            now = int(time.time())
            if row["worker_owner"] != self.owner or row["worker_lease_until"] <= now:
                return None
            await self._recover(connection, now)
            ready = (
                select(tables.TASKS.c.scope_id)
                .where(
                    tables.TASKS.c.state.in_(("pending", "retry_wait")),
                    tables.TASKS.c.available_at <= now,
                )
                .distinct()
                .order_by(tables.TASKS.c.scope_id)
                .limit(1)
            )
            scope = None
            if row["last_scope"] is not None:
                scope = await connection.scalar(ready.where(tables.TASKS.c.scope_id > row["last_scope"]))
            if scope is None:
                scope = await connection.scalar(ready)
            if scope is None:
                self.lease_seconds = 30
                return None
            item = (
                (
                    await connection.execute(
                        select(tables.TASKS)
                        .where(
                            tables.TASKS.c.scope_id == scope,
                            tables.TASKS.c.state.in_(("pending", "retry_wait")),
                            tables.TASKS.c.available_at <= now,
                        )
                        .order_by(tables.TASKS.c.available_at, tables.TASKS.c.task_id)
                        .limit(1)
                    )
                )
                .mappings()
                .one()
            )
            task = task_value(item["payload"], scope)
            profile = ExperimentalImageParseProfile.model_validate_json(item["profile"])
            context = ExperimentalImageContext.model_validate_json(item["context"])
            if image_profile_digest(profile) != task.profile_digest.root or task.attempt_count >= task.max_attempts:
                raise ImageError(500, "image_task_corrupt")
            claimed = transition(
                task, "running", attempt_count=task.attempt_count + 1, next_attempt_at=None, error_code=None
            )
            token = str(uuid4())
            self.lease_seconds = profile.timeout_seconds + 30
            await connection.execute(
                update(tables.TASKS)
                .where(
                    tables.TASKS.c.scope_id == scope,
                    tables.TASKS.c.task_id == task.task_id,
                )
                .values(state="running", payload=dump(claimed), claim_token=token, lease_until=now + self.lease_seconds)
            )
            await connection.execute(
                update(tables.STATE)
                .where(tables.STATE.c.id == 1)
                .values(
                    last_scope=scope,
                    worker_lease_until=now + self.lease_seconds,
                )
            )
            attempt = ExperimentalImageAttempt.model_validate({
                "attempt": claimed.attempt_count,
                "claim_revision": claimed.revision,
                "state": "started",
                "started_at": datetime.fromtimestamp(now, UTC),
                "finished_at": None,
                "provider_requests": 0,
                "input_digest": None,
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": None,
                "http_status": None,
                "error_code": None,
            })
            await connection.execute(
                tables.ATTEMPTS.insert().values(
                    scope_id=scope,
                    task_id=task.task_id,
                    attempt=claimed.attempt_count,
                    payload=dump(attempt),
                )
            )
        return ImageClaim(self.owner, token, claimed, profile, context)

    async def record_request(self, claim: ImageClaim, input_digest: str) -> bool:
        """Fence once more immediately before the one outbound request attempt."""
        async with self.database.transaction() as connection:
            await self._lock(connection)
            if not await self._claimed(connection, claim):
                return False
            attempt = await self._attempt(connection, claim)
            if attempt.input_digest is not None:
                return False
            updated = ExperimentalImageAttempt.model_validate({
                **attempt.model_dump(mode="json"),
                "provider_requests": None,
                "input_digest": input_digest,
            })
            await self._write_attempt(connection, claim, updated)
        return True

    async def finish(
        self,
        claim: ImageClaim,
        *,
        result: VisionResult | None = None,
        input_digest: str | None = None,
        failure: VisionFailure | None = None,
    ) -> bool:
        async with self.database.transaction() as connection:
            await self._lock(connection)
            if not await self._claimed(connection, claim):
                return False
            attempt = await self._attempt(connection, claim)
            if result is not None and input_digest is not None:
                original = await self._original(connection, claim.scope_id, claim.task.original.source.source_id)
                analysis = ImageAnalysisSource(
                    name="imga_"
                    + canonical_image_digest({
                        "contract_version": "1",
                        "task_id": claim.task.task_id,
                    }).removeprefix("sha256:"),
                    materialization=SourceMaterialization.CAPTURED,
                    original=claim.task.original,
                    task_id=claim.task.task_id,
                    profile=claim.profile,
                    profile_digest=claim.task.profile_digest,
                    context_digest=ExperimentalImageDigest(
                        root=canonical_image_digest(claim.context.model_dump(mode="json"))
                    ),
                    input_digest=ExperimentalImageDigest(root=input_digest),
                    output=result.output,
                )
                if original.context != claim.context:
                    raise ImageError(500, "image_task_corrupt")
                stored, created = await self.service.sources.add_with_status(connection, claim.scope_id, analysis)
                if created:
                    await self.service.pending.raise_source(
                        connection,
                        claim.scope_id,
                        TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
                        stored.journal_position,
                    )
                task = transition(
                    claim.task,
                    "succeeded",
                    analysis={
                        "scope_id": claim.scope_id,
                        "source": stored.ref.model_dump(mode="json"),
                    },
                    error_code=None,
                    next_attempt_at=None,
                )
                await self._decrement(connection, claim.scope_id)
                receipt = {"state": "succeeded", "error_code": None}
                usage, latency, http_status = result.usage, result.latency_ms, result.http_status
            else:
                if failure is None:
                    raise ValueError("result or failure required")  # noqa: TRY003
                wait = failure.retry_after is not None and claim.task.attempt_count < claim.task.max_attempts
                delay = max(2 if claim.task.attempt_count == 1 else 8, min(30, failure.retry_after or 0))
                task = transition(
                    claim.task,
                    "retry_wait" if wait else "failed",
                    error_code=failure.code,
                    next_attempt_at=datetime.fromtimestamp(int(time.time()) + delay, UTC) if wait else None,
                )
                if not wait:
                    await self._decrement(connection, claim.scope_id)
                receipt = {"state": "failed", "error_code": failure.code}
                usage, latency, http_status = failure.usage, failure.latency_ms, failure.http_status
            await connection.execute(
                update(tables.TASKS)
                .where(*self._claim_identity(claim))
                .values(
                    state=task.state.value,
                    payload=dump(task),
                    claim_token=None,
                    lease_until=0,
                    analysis_source_id=None if task.analysis is None else task.analysis.source.source_id,
                    available_at=0 if task.next_attempt_at is None else int(task.next_attempt_at.timestamp()),
                )
            )
            completed = ExperimentalImageAttempt.model_validate({
                **attempt.model_dump(mode="json"),
                **receipt,
                "finished_at": datetime.now(UTC),
                "provider_requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "latency_ms": latency,
                "http_status": http_status,
            })
            await self._write_attempt(connection, claim, completed)
        return True

    async def abandon(self, claim: ImageClaim) -> None:
        """Graceful interruption spends the attempt and invalidates the old token."""
        async with self.database.transaction() as connection:
            await self._lock(connection)
            if not await self._claimed(connection, claim):
                return
            row = (await connection.execute(select(tables.TASKS).where(*self._fence(claim)))).mappings().one()
            await self._interrupt(connection, row, int(time.time()))

    async def _recover(self, connection: AsyncConnection, now: int) -> None:
        rows = (
            (
                await connection.execute(
                    select(tables.TASKS)
                    .where(
                        tables.TASKS.c.state == "running",
                        tables.TASKS.c.lease_until <= now,
                    )
                    .order_by(tables.TASKS.c.lease_until)
                    .limit(16)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            await self._interrupt(connection, row, now)

    async def _interrupt(self, connection: AsyncConnection, row: Any, now: int) -> None:
        task = task_value(row["payload"], row["scope_id"])
        wait = task.attempt_count < task.max_attempts
        recovered = transition(
            task,
            "retry_wait" if wait else "failed",
            error_code="image_attempt_interrupted",
            next_attempt_at=datetime.fromtimestamp(now + 2, UTC) if wait else None,
        )
        if not wait:
            await self._decrement(connection, row["scope_id"])
        await connection.execute(
            update(tables.TASKS)
            .where(
                tables.TASKS.c.scope_id == row["scope_id"],
                tables.TASKS.c.task_id == row["task_id"],
            )
            .values(
                state=recovered.state.value,
                payload=dump(recovered),
                claim_token=None,
                lease_until=0,
                available_at=now + 2 if wait else 0,
            )
        )
        payload = await connection.scalar(
            select(tables.ATTEMPTS.c.payload).where(
                tables.ATTEMPTS.c.scope_id == row["scope_id"],
                tables.ATTEMPTS.c.task_id == row["task_id"],
                tables.ATTEMPTS.c.attempt == task.attempt_count,
            )
        )
        if payload is not None:
            receipt = ExperimentalImageAttempt.model_validate_json(payload)
            interrupted = ExperimentalImageAttempt.model_validate({
                **receipt.model_dump(mode="json"),
                "state": "interrupted",
                "finished_at": datetime.fromtimestamp(now, UTC),
                "error_code": "image_attempt_interrupted",
            })
            await connection.execute(
                update(tables.ATTEMPTS)
                .where(
                    tables.ATTEMPTS.c.scope_id == row["scope_id"],
                    tables.ATTEMPTS.c.task_id == row["task_id"],
                    tables.ATTEMPTS.c.attempt == task.attempt_count,
                )
                .values(payload=dump(interrupted))
            )

    @staticmethod
    async def _decrement(connection: AsyncConnection, scope: str) -> None:
        await connection.execute(
            update(tables.STATE).where(tables.STATE.c.id == 1).values(pending_count=tables.STATE.c.pending_count - 1)
        )
        await connection.execute(
            update(tables.SCOPE_BUDGETS)
            .where(tables.SCOPE_BUDGETS.c.scope_id == scope)
            .values(pending_count=tables.SCOPE_BUDGETS.c.pending_count - 1)
        )

    async def _attempt(self, connection: AsyncConnection, claim: ImageClaim) -> ExperimentalImageAttempt:
        payload = await connection.scalar(
            select(tables.ATTEMPTS.c.payload).where(
                tables.ATTEMPTS.c.scope_id == claim.scope_id,
                tables.ATTEMPTS.c.task_id == claim.task.task_id,
                tables.ATTEMPTS.c.attempt == claim.task.attempt_count,
            )
        )
        if payload is None:
            raise ImageError(500, "image_task_corrupt")
        return ExperimentalImageAttempt.model_validate_json(payload)

    @staticmethod
    async def _write_attempt(connection: AsyncConnection, claim: ImageClaim, attempt: ExperimentalImageAttempt) -> None:
        await connection.execute(
            update(tables.ATTEMPTS)
            .where(
                tables.ATTEMPTS.c.scope_id == claim.scope_id,
                tables.ATTEMPTS.c.task_id == claim.task.task_id,
                tables.ATTEMPTS.c.attempt == claim.task.attempt_count,
            )
            .values(payload=dump(attempt))
        )

    async def _original(self, connection: AsyncConnection, scope: str, source_id: str) -> OriginalImageSource:
        try:
            stored = await self.service.sources.get(
                connection, scope, SourceRef(source_type="experimental.image.original", source_id=source_id)
            )
        except RepositoryNotFoundError as error:
            raise ImageError(404, "image_original_not_found") from error
        if not isinstance(stored.value, OriginalImageSource):
            raise ImageError(500, "image_task_corrupt")
        return stored.value

    async def retry(self, request: ExperimentalRetryImageTaskRequest) -> ExperimentalImageTask:
        return await self._enqueue("retry", request.retry_key, request)

    async def reparse(self, request: ExperimentalReparseImageRequest) -> ExperimentalImageTask:
        return await self._enqueue("reparse", request.reparse_key, request)

    async def _enqueue(  # noqa: C901 - replay, profile selection and queue charge share one writer transaction
        self, kind: str, key: str, request: ExperimentalRetryImageTaskRequest | ExperimentalReparseImageRequest
    ) -> ExperimentalImageTask:
        if not self.service.config.enabled:
            raise ImageError(503, "image_disabled")
        self.service._require_admission_available()
        digest = canonical_image_digest(request.model_dump(mode="json"))
        scope = request.scope_id
        async with self.database.transaction() as connection:
            state = await self._lock(connection)
            receipt = (
                (
                    await connection.execute(
                        select(tables.OPERATIONS).where(
                            tables.OPERATIONS.c.scope_id == scope,
                            tables.OPERATIONS.c.kind == kind,
                            tables.OPERATIONS.c.operation_key == key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if receipt is not None:
                if receipt["request_digest"] != digest:
                    raise ImageError(409, "image_operation_conflict")
                payload = await connection.scalar(
                    select(tables.TASKS.c.payload).where(
                        tables.TASKS.c.scope_id == scope, tables.TASKS.c.task_id == receipt["task_id"]
                    )
                )
                if payload is None:
                    raise ImageError(500, "image_task_corrupt")
                return task_value(payload, scope)
            if isinstance(request, ExperimentalRetryImageTaskRequest):
                row = (
                    (
                        await connection.execute(
                            select(tables.TASKS).where(
                                tables.TASKS.c.scope_id == scope, tables.TASKS.c.task_id == request.task_id
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise ImageError(404, "image_task_not_found")
                parent = task_value(row["payload"], scope)
                if parent.state.value != "failed":
                    raise ImageError(409, "image_task_not_failed")
                original = await self._original(connection, scope, parent.original.source.source_id)
                profile = ExperimentalImageParseProfile.model_validate_json(row["profile"])
                context = ExperimentalImageContext.model_validate_json(row["context"])
                identity = {
                    "contract_version": "1",
                    "kind": "retry",
                    "scope_id": scope,
                    "parent_task_id": request.task_id,
                    "retry_key": key,
                }
            else:
                if request.original.source_type != "experimental.image.original":
                    raise ImageError(422, "image_invalid_original_reference")
                original = await self._original(connection, scope, request.original.source_id)
                profile = self.service.profile
                if profile is None:
                    raise ImageError(503, "image_vision_not_configured")
                if (
                    request.expected_profile_digest is not None
                    and request.expected_profile_digest.root != image_profile_digest(profile)
                ):
                    raise ImageError(409, "image_profile_conflict")
                context = original.context
                identity = {
                    "contract_version": "1",
                    "kind": "reparse",
                    "scope_id": scope,
                    "original_source_id": original.name,
                    "reparse_key": key,
                }
            # File read is bounded by the original 8 MiB contract. No provider work
            # occurs here; acceptance must reject a missing original before queuing.
            import asyncio

            await asyncio.to_thread(self.service._read, scope, original.asset)
            count = await connection.scalar(
                select(tables.SCOPE_BUDGETS.c.pending_count).where(tables.SCOPE_BUDGETS.c.scope_id == scope)
            )
            if (
                state["pending_count"] >= self.service.config.max_pending_total
                or (count or 0) >= self.service.config.max_pending_per_scope
            ):
                raise ImageError(503, "image_queue_full")
            task = ExperimentalImageTask.model_validate({
                "task_id": "it_" + canonical_image_digest(identity).removeprefix("sha256:"),
                "original": {
                    "scope_id": scope,
                    "source": {"source_type": "experimental.image.original", "source_id": original.name},
                },
                "state": "pending",
                "revision": 1,
                "attempt_count": 0,
                "max_attempts": profile.max_attempts,
                "profile_digest": image_profile_digest(profile),
                "analysis": None,
                "error_code": None,
                "next_attempt_at": None,
            })
            await connection.execute(
                tables.TASKS.insert().values(
                    scope_id=scope,
                    task_id=task.task_id,
                    state="pending",
                    profile=dump(profile),
                    context=dump(context),
                    payload=dump(task),
                    original_source_id=original.name,
                    available_at=int(time.time()),
                    created_at=int(time.time()),
                )
            )
            await connection.execute(
                tables.OPERATIONS.insert().values(
                    scope_id=scope, kind=kind, operation_key=key, request_digest=digest, task_id=task.task_id
                )
            )
            if count is None:
                await connection.execute(tables.SCOPE_BUDGETS.insert().values(scope_id=scope, pending_count=1))
            else:
                await connection.execute(
                    update(tables.SCOPE_BUDGETS)
                    .where(tables.SCOPE_BUDGETS.c.scope_id == scope)
                    .values(pending_count=count + 1)
                )
            await connection.execute(
                update(tables.STATE).where(tables.STATE.c.id == 1).values(pending_count=state["pending_count"] + 1)
            )
        self.service.wake.set()
        return task
