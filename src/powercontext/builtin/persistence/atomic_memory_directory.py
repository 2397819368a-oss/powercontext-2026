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

"""Internal compact directory storage over expanded shared Scope/Artifact tables.

The caller owns authorization, readiness and a consistent connection. This
repository neither opens transactions nor enables an application operation.
Production schema/readiness and writer composition must be integrated before publication.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal, cast

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from sqlalchemy import BigInteger, ColumnElement, column, func, literal_column, select, table, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemory, AtomicMemoryContent, AtomicMemoryState
from powercontext.builtin.persistence.atomic_memory import atomic_memory_state_expression
from powercontext.builtin.persistence.cursor_codec import SignedCursorCodec
from powercontext.builtin.persistence.tables import ARTIFACT_HEADS_TABLE, identity_string
from powercontext.builtin.persistence.tags import tag_predicate
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.tags import TagFilter

DIRECTORY_ITEM_BYTES = 4 * 1024 * 1024
DIRECTORY_ENVELOPE_BYTES = 8192
DIRECTORY_CURSOR_LENGTH = 4096
_MAX_GENERATION = (1 << 63) - 1

# Expression views only: they do not mutate startup metadata or execute DDL.
# The production schema belongs to the unified frozen migration resources.
_SCOPES = table(
    "pc_scopes",
    column("scope_id", identity_string(256)),
    column("atomic_memory_directory_generation", BigInteger()),
)
_HEADS = table(
    "pc_artifact_heads",
    column("scope_id", identity_string(256)),
    column("family", identity_string(128)),
    column("artifact_id", identity_string(128)),
    column("revision"),
    column("content_kind", identity_string(128)),
)
_KIND = literal_column("pc_artifact_heads.content_kind", type_=identity_string(128))


class AtomicMemoryDirectoryUnavailableError(BaseAccessError):
    """Required directory data does not satisfy the declared storage contract."""


class AtomicMemoryDirectoryAuthorizationUnavailableError(BaseAccessError):
    """The configured access authorities cannot safely qualify this directory."""


class AtomicMemoryDirectoryItemTooLargeError(BaseAccessError):
    """A single compact item cannot fit the item-payload allowance."""


class AtomicMemoryDirectoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_id: str
    states: tuple[Literal["active", "forgotten", "merged", "retired"], ...] = Field(
        default=("active",), min_length=1, max_length=4
    )
    kind: str | None = None
    tag_filter: TagFilter | None = None
    limit: StrictInt = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=DIRECTORY_CURSOR_LENGTH)

    @field_validator("scope_id")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        return validate_scope_id(value)

    @field_validator("states")
    @classmethod
    def distinct_states(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("states must be distinct")  # noqa: TRY003
        return values

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str | None) -> str | None:
        return None if value is None else AtomicMemoryContent(kind=value, text="validation").kind


class AtomicMemoryDirectoryItem(AtomicMemoryState):
    artifact: ArtifactRef
    kind: str = Field(min_length=1, max_length=128)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        return AtomicMemoryContent(kind=value, text="validation").kind


class AtomicMemoryDirectoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str
    items: tuple[AtomicMemoryDirectoryItem, ...]
    next_cursor: str | None = None


class AtomicMemoryDirectoryRepository:
    """Select page metadata using a caller-supplied, authorized SQL eligibility.

    Eligibility and policy_context must come from the shared authorization
    authority. Neither arbitrary SQL nor a string context proves authorization.
    An application adapter must enforce that boundary before exposing query().
    """

    def __init__(self, cursor: SignedCursorCodec, *, available: bool = True) -> None:
        self.cursor = cursor
        self.available = available

    async def lock_scope(self, connection: AsyncConnection, scope_id: str) -> None:
        """Serialize directory-enabled writers before they acquire any head locks.

        A no-op UPDATE also takes SQLite's write lock. It does not invalidate
        cursors; only effective publications and mutations advance generation.
        """

        result = await connection.execute(
            update(_SCOPES)
            .where(
                _SCOPES.c.scope_id == scope_id,
                func.typeof(_SCOPES.c.atomic_memory_directory_generation) == "integer",
                _SCOPES.c.atomic_memory_directory_generation >= 0,
                _SCOPES.c.atomic_memory_directory_generation <= _MAX_GENERATION,
            )
            .values(atomic_memory_directory_generation=_SCOPES.c.atomic_memory_directory_generation)
        )
        if result.rowcount != 1:
            raise AtomicMemoryDirectoryUnavailableError

    async def query(
        self,
        connection: AsyncConnection,
        query: AtomicMemoryDirectoryQuery,
        *,
        subject: str,
        eligibility: ColumnElement[bool],
        policy_context: str,
    ) -> AtomicMemoryDirectoryPage:
        if not self.available:
            raise AtomicMemoryDirectoryUnavailableError
        generation = await connection.scalar(
            select(_SCOPES.c.atomic_memory_directory_generation).where(_SCOPES.c.scope_id == query.scope_id)
        )
        if type(generation) is not int or not 0 <= generation <= _MAX_GENERATION:
            raise AtomicMemoryDirectoryUnavailableError
        filters = {
            "states": sorted(query.states),
            "kind": query.kind,
            "tags": None if query.tag_filter is None else list(query.tag_filter.keys),
            "tag_match": None if query.tag_filter is None else query.tag_filter.match,
            "limit": query.limit,
        }
        bound = {
            "endpoint": "atomic_memory_directory",
            "version": 1,
            "order": "artifact_id_asc_v1",
            "context": _digest({"scope_id": query.scope_id, "subject": subject, "policy": policy_context}),
            "query": _digest(filters),
            "generation": generation,
        }
        after = self.cursor.after_text(query.cursor, bound)
        heads = ARTIFACT_HEADS_TABLE
        statement = (
            select(
                heads.c.artifact_id,
                heads.c.revision,
                _KIND.label("kind"),
                atomic_memory_state_expression().label("state"),
                heads.c.governance_generation.label("state_version"),
                heads.c.merged_into_id,
                heads.c.replacement_artifact_id,
            )
            .where(
                heads.c.scope_id == query.scope_id,
                heads.c.family == "atomic-memory",
                heads.c.artifact_id > after,
                atomic_memory_state_expression().in_(query.states),
                eligibility,
            )
            .order_by(heads.c.artifact_id)
            .limit(query.limit + 1)
        )
        if query.kind is not None:
            statement = statement.where(query.kind == _KIND)
        if query.tag_filter is not None:
            statement = statement.where(
                tag_predicate(
                    query.scope_id,
                    "atomic-memory",
                    heads.c.artifact_id,
                    "artifact",
                    heads.c.artifact_id,
                    query.tag_filter,
                )
            )
        rows = (await connection.execute(statement)).mappings().all()
        items: list[AtomicMemoryDirectoryItem] = []
        # Account for the array brackets as well as each comma-separated item.
        item_bytes = 2
        for row in rows[: query.limit]:
            item = _directory_item(row)
            encoded_size = len(item.model_dump_json().encode("utf-8"))
            if encoded_size + 2 > DIRECTORY_ITEM_BYTES:
                raise AtomicMemoryDirectoryItemTooLargeError
            next_size = item_bytes + encoded_size + bool(items)
            if next_size > DIRECTORY_ITEM_BYTES:
                break
            items.append(item)
            item_bytes = next_size
        has_more = len(rows) > len(items)
        next_cursor = self.cursor.encode(bound, items[-1].artifact.artifact_id) if has_more else None
        if next_cursor is not None and len(next_cursor) > DIRECTORY_CURSOR_LENGTH:
            raise AtomicMemoryDirectoryUnavailableError
        page = AtomicMemoryDirectoryPage(scope_id=query.scope_id, items=tuple(items), next_cursor=next_cursor)
        envelope = page.model_copy(update={"items": ()})
        if len(envelope.model_dump_json().encode("utf-8")) - 2 > DIRECTORY_ENVELOPE_BYTES:
            raise AtomicMemoryDirectoryUnavailableError
        return page

    async def record_publication(self, connection: AsyncConnection, scope_id: str, artifact: AtomicMemory) -> None:
        """Maintain metadata after an effective publish, inside its business transaction."""

        result = await connection.execute(
            update(_HEADS)
            .where(
                _HEADS.c.scope_id == scope_id,
                _HEADS.c.family == artifact.family,
                _HEADS.c.artifact_id == artifact.artifact_id,
                _HEADS.c.revision == artifact.revision,
            )
            .values(content_kind=artifact.content.kind)
        )
        if result.rowcount != 1:
            raise AtomicMemoryDirectoryUnavailableError
        await self.record_mutation(connection, scope_id)

    async def record_mutation(self, connection: AsyncConnection, scope_id: str) -> None:
        """Invalidate cursors after an effective state/tag/repair mutation; never commit."""

        result = await connection.execute(
            update(_SCOPES)
            .where(
                _SCOPES.c.scope_id == scope_id,
                func.typeof(_SCOPES.c.atomic_memory_directory_generation) == "integer",
                _SCOPES.c.atomic_memory_directory_generation >= 0,
                _SCOPES.c.atomic_memory_directory_generation < _MAX_GENERATION,
            )
            .values(atomic_memory_directory_generation=_SCOPES.c.atomic_memory_directory_generation + 1)
        )
        if result.rowcount != 1:
            raise AtomicMemoryDirectoryUnavailableError


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(rfc8785.dumps(cast(Any, value))).hexdigest()


def _directory_item(row: RowMapping | Mapping[str, Any]) -> AtomicMemoryDirectoryItem:
    if row["replacement_artifact_id"] is not None:
        raise AtomicMemoryDirectoryUnavailableError
    try:
        return AtomicMemoryDirectoryItem(
            artifact=ArtifactRef(family="atomic-memory", artifact_id=row["artifact_id"], revision=row["revision"]),
            kind=row["kind"],
            state=row["state"],
            state_version=row["state_version"],
            merged_into_id=row["merged_into_id"],
        )
    except (ValidationError, ValueError) as error:
        raise AtomicMemoryDirectoryUnavailableError from error
