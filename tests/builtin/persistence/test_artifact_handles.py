# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio

import pytest
from sqlalchemy import insert

from powercontext.artifacts import ArtifactRef, StableHandle
from powercontext.builtin.persistence.artifact_handles import ArtifactHandleRepository
from powercontext.builtin.persistence.errors import RepositoryNotFoundError, StoredPayloadConflictError
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import ARTIFACTS_TABLE, BUILTIN_TABLES


def test_bind_and_resolve_exact_artifact_revision() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            target = ArtifactRef(family="memory", artifact_id="memory-1", revision=1)
            handle = StableHandle(family="memory", key="payment-retry-policy")
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="scope-a",
                        family=target.family,
                        artifact_id=target.artifact_id,
                        revision=target.revision,
                        content=b"{}",
                    )
                )

            assert await repository.bind("scope-a", handle, target) == target
            assert await repository.bind("scope-a", handle, target) == target
            assert await repository.resolve("scope-a", handle) == target

    asyncio.run(scenario())


def test_handle_is_scope_local_and_cannot_be_rebound() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            handle = StableHandle(family="memory", key="payment-retry-policy")
            first = ArtifactRef(family="memory", artifact_id="memory-1", revision=1)
            second = ArtifactRef(family="memory", artifact_id="memory-2", revision=1)
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE),
                    [
                        {
                            "scope_id": scope_id,
                            "family": target.family,
                            "artifact_id": target.artifact_id,
                            "revision": target.revision,
                            "content": b"{}",
                        }
                        for scope_id in ("scope-a", "scope-b")
                        for target in (first, second)
                    ],
                )

            await repository.bind("scope-a", handle, first)
            await repository.bind("scope-b", handle, second)

            assert await repository.resolve("scope-a", handle) == first
            assert await repository.resolve("scope-b", handle) == second
            with pytest.raises(StoredPayloadConflictError):
                await repository.bind("scope-a", handle, second)

    asyncio.run(scenario())


def test_missing_handle_and_target_are_explicit() -> None:
    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
            repository = ArtifactHandleRepository(profile.database)
            handle = StableHandle(family="memory", key="missing")
            target = ArtifactRef(family="memory", artifact_id="missing", revision=1)

            with pytest.raises(RepositoryNotFoundError, match="artifact_handle"):
                await repository.resolve("scope-a", handle)
            with pytest.raises(RepositoryNotFoundError, match="artifact"):
                await repository.bind("scope-a", handle, target)

    asyncio.run(scenario())
