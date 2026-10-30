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

"""Propagate Source availability through registered derived evidence.

One Scope owns one directed acyclic graph. A Source node is a root whose
availability is set by lifecycle events; a Memory or Topic Memory node is
derived and stays available only while its parents still support it under its
own ``all`` / ``any`` policy.

The graph is append-only: a node is bound once to one reference, one policy and
one parent tuple, and afterwards only availability, the Source event counter and
the last event's content change. That makes both idempotent registration and
idempotent event replay a comparison against what is already stored, and it is
what lets an interrupted propagation finish on retry: the derived state is
committed first and the native Memory state is reconciled from the artifact
itself afterwards, so a retry never has to guess how far the last attempt got.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.topic_memory import TopicMemory
from powercontext.builtin.persistence.codec import dump_model, load_model
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import (
    MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH,
    SOURCE_LIFECYCLE_EDGES_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from powercontext.builtin.artifacts.memory import MemoryEntryVersion, MemoryService

    from .application import BuiltinRuntime

_KIND_SOURCE: Final = "source"
_KIND_MEMORY: Final = "memory"
_KIND_TOPIC: Final = "topic"

_KINDS: Final = (_KIND_SOURCE, _KIND_MEMORY, _KIND_TOPIC)
_POLICIES: Final = ("all", "any")
_PURPOSES: Final = ("generation", "audit")

# Sources are registered exactly as the built-in runtime captured them, so the
# built-in registry is what turns a stored row back into a readable Source.
_SOURCE_REPOSITORY: Final = SourceRepository(BUILTIN_SOURCE_REGISTRY)

_REASON_WITHDRAWN: Final = "source-lifecycle:withdrawn"
_REASON_RESTORED: Final = "source-lifecycle:restored"


class SourceLifecycleError(ValueError):
    """Reject a registration, event, or resolution that breaks the lifecycle contract."""


class SourceLifecycleEvent(BaseModel):
    """One versioned availability decision for one registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(strict=True, gt=0)
    sequence: int = Field(strict=True, gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def _reject_blank(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class _Node:
    """One registered graph node as stored."""

    node_id: str
    kind: str
    policy: str | None
    available: bool
    revision: int
    sequence: int
    last_available: bool | None
    last_reason: str | None
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...]


def _require_text(value: object, field: str, /) -> str:
    if not isinstance(value, str):
        raise SourceLifecycleError(f"{field} must be a string")
    if not value.strip():
        raise SourceLifecycleError(f"{field} must not be blank")
    if len(value) > MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH:
        raise SourceLifecycleError(f"{field} is too long")
    return value


def _require_revision(value: object, /) -> int:
    if type(value) is not int or value < 1:
        raise SourceLifecycleError("revision must be a positive integer")
    return value


def _require_policy(value: object, /) -> str:
    if value not in _POLICIES:
        raise SourceLifecycleError("policy must be 'all' or 'any'")
    return str(value)


def _require_parents(node_id: str, parents: object, nodes: Mapping[str, _Node], /) -> tuple[str, ...]:
    if isinstance(parents, str) or not isinstance(parents, (tuple, list)):
        raise SourceLifecycleError("parents must be a sequence of node ids")
    normalized: list[str] = []
    for parent in parents:
        parent_id = _require_text(parent, "parent node id")
        if parent_id == node_id:
            raise SourceLifecycleError("a node cannot depend on itself")
        if parent_id not in nodes:
            raise SourceLifecycleError(f"parent node {parent_id!r} is not registered")
        if parent_id in normalized:
            raise SourceLifecycleError("parents must not repeat")
        normalized.append(parent_id)
    if not normalized:
        raise SourceLifecycleError("a derived node needs at least one parent")
    return tuple(normalized)


def _decode_reference(kind: str, payload: object, node_id: str, /) -> SourceRef | MemoryCitation | ArtifactRef:
    model = {_KIND_SOURCE: SourceRef, _KIND_MEMORY: MemoryCitation, _KIND_TOPIC: ArtifactRef}[kind]
    return load_model(model, payload, kind="source-lifecycle-node", name=node_id)


async def _load(connection: AsyncConnection, scope_id: str, /) -> dict[str, _Node]:
    node_rows = (
        (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
            )
        )
        .mappings()
        .all()
    )
    edge_rows = (
        (
            await connection.execute(
                select(SOURCE_LIFECYCLE_EDGES_TABLE)
                .where(SOURCE_LIFECYCLE_EDGES_TABLE.c.scope_id == scope_id)
                .order_by(SOURCE_LIFECYCLE_EDGES_TABLE.c.node_id, SOURCE_LIFECYCLE_EDGES_TABLE.c.position)
            )
        )
        .mappings()
        .all()
    )
    parents: dict[str, list[str]] = {}
    for row in edge_rows:
        parents.setdefault(str(row["node_id"]), []).append(str(row["parent_node_id"]))

    nodes: dict[str, _Node] = {}
    for row in node_rows:
        node_id = str(row["node_id"])
        kind = str(row["kind"])
        if kind not in _KINDS:
            raise SourceLifecycleError(f"stored node {node_id!r} has an unknown kind")
        nodes[node_id] = _Node(
            node_id=node_id,
            kind=kind,
            policy=None if row["policy"] is None else str(row["policy"]),
            available=bool(row["available"]),
            revision=int(row["revision"]),
            sequence=int(row["sequence"]),
            last_available=None if row["last_available"] is None else bool(row["last_available"]),
            last_reason=None if row["last_reason"] is None else str(row["last_reason"]),
            reference=_decode_reference(kind, row["reference"], node_id),
            parents=tuple(parents.get(node_id, ())),
        )
    return nodes


def _topological(nodes: Mapping[str, _Node], /) -> tuple[str, ...]:
    """Order nodes so every node follows all of its parents, or reject a cycle."""

    indegree = {node_id: len(node.parents) for node_id, node in nodes.items()}
    children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        for parent in node.parents:
            children.setdefault(parent, []).append(node_id)
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child in children.get(node_id, ()):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    if len(order) != len(nodes):
        raise SourceLifecycleError("the dependency graph contains a cycle")
    return tuple(order)


def _derive(nodes: Mapping[str, _Node], /) -> dict[str, bool]:
    """Recompute every derived node's availability from its parents' current state."""

    available: dict[str, bool] = {}
    for node_id in _topological(nodes):
        node = nodes[node_id]
        if node.kind == _KIND_SOURCE:
            available[node_id] = node.available
            continue
        support = [available[parent] for parent in node.parents]
        available[node_id] = all(support) if node.policy == "all" else any(support)
    return available


def _support(nodes: Mapping[str, _Node], parents: tuple[str, ...], policy: str, /) -> bool:
    support = [nodes[parent].available for parent in parents]
    return all(support) if policy == "all" else any(support)


class ScopedSourceLifecycle:
    """Register, withdraw, restore, and resolve one Scope's dependency graph."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str, /) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Bind one node id to one captured Source and its lifecycle version."""

        node_id = _require_text(node_id, "node_id")
        revision = _require_revision(revision)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("register_source requires a SourceRef")
        async with self._runtime._context(self.scope_id), self._runtime._locked(self.scope_id):
            await self._require_source(source_ref)
            async with self._database.transaction() as connection:
                nodes = await _load(connection, self.scope_id)
                existing = nodes.get(node_id)
                candidate = _Node(
                    node_id=node_id,
                    kind=_KIND_SOURCE,
                    policy=None,
                    available=True,
                    revision=revision,
                    sequence=0,
                    last_available=None,
                    last_reason=None,
                    reference=source_ref,
                    parents=(),
                )
                if existing is not None:
                    self._require_identical(existing, candidate)
                    return
                await self._insert(connection, candidate)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one node id to one Memory entry and the parents that support it."""

        node_id = _require_text(node_id, "node_id")
        policy = _require_policy(policy)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("register_memory requires a MemoryCitation")
        async with self._runtime._context(self.scope_id) as context:
            await context.artifacts.memory.validate_citation(citation)
            async with self._runtime._locked(self.scope_id):
                async with self._database.transaction() as connection:
                    nodes = await _load(connection, self.scope_id)
                    resolved = _require_parents(node_id, parents, nodes)
                    candidate = _Node(
                        node_id=node_id,
                        kind=_KIND_MEMORY,
                        policy=policy,
                        # The state is decided here, not on a later event: a
                        # node registered over withdrawn support is unavailable
                        # from the moment it exists.
                        available=_support(nodes, resolved, policy),
                        revision=1,
                        sequence=0,
                        last_available=None,
                        last_reason=None,
                        reference=citation,
                        parents=resolved,
                    )
                    if node_id in nodes:
                        self._require_identical(nodes[node_id], candidate)
                        return
                    _topological({**nodes, node_id: candidate})
                    await self._insert(connection, candidate)
                    nodes = {**nodes, node_id: candidate}
                await self._reconcile_memory(context, nodes)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one node id to one exact Topic Memory revision and its supporting parents."""

        node_id = _require_text(node_id, "node_id")
        policy = _require_policy(policy)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("register_topic requires an ArtifactRef")
        if artifact_ref.family != TopicMemory.family:
            raise SourceLifecycleError("register_topic requires a Topic Memory reference")
        await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
            artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
        )
        async with self._runtime._context(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database.transaction() as connection:
                nodes = await _load(connection, self.scope_id)
                resolved = _require_parents(node_id, parents, nodes)
                candidate = _Node(
                    node_id=node_id,
                    kind=_KIND_TOPIC,
                    policy=policy,
                    available=_support(nodes, resolved, policy),
                    revision=1,
                    sequence=0,
                    last_available=None,
                    last_reason=None,
                    reference=artifact_ref,
                    parents=resolved,
                )
                if node_id in nodes:
                    self._require_identical(nodes[node_id], candidate)
                    return
                _topological({**nodes, node_id: candidate})
                await self._insert(connection, candidate)

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event and return every node id that is available afterwards."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                async with self._database.transaction() as connection:
                    nodes = await _load(connection, self.scope_id)
                    node = nodes.get(event.node_id)
                    if node is None or node.kind != _KIND_SOURCE:
                        raise SourceLifecycleError(f"{event.node_id!r} is not a registered Source")
                    if event.revision != node.revision:
                        raise SourceLifecycleError(
                            f"revision {event.revision} does not match the lifecycle version {node.revision}"
                        )
                    if event.sequence < node.sequence:
                        raise SourceLifecycleError("sequence must not move backwards")
                    if event.sequence == node.sequence:
                        # A replay has to carry the same decision: the same
                        # sequence number with different content is a lost
                        # update, not a retry of the one that was applied.
                        if (
                            node.last_available is None
                            or bool(node.last_available) != event.available
                            or node.last_reason != event.reason
                        ):
                            raise SourceLifecycleError("sequence was already used by a different event")
                    else:
                        nodes[event.node_id] = replace(
                            node,
                            available=event.available,
                            sequence=event.sequence,
                            last_available=event.available,
                            last_reason=event.reason,
                        )
                        derived = _derive(nodes)
                        nodes = {
                            node_id: replace(item, available=derived[node_id]) for node_id, item in nodes.items()
                        }
                        await self._persist(connection, nodes, event)
            # The graph is committed before anything native is touched, so an
            # interrupted propagation is always "graph ahead of Memory" and the
            # next attempt can finish it from the artifact's own state.
            await self._reconcile_memory(context, nodes)
        return sorted(node_id for node_id, node in nodes.items() if node.available)

    async def resolve(self, node_id: str, /, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return the exact reference a node was registered with.

        ``generation`` refuses a node that lost its support, so withdrawn
        evidence cannot enter new output. ``audit`` always answers, which is
        what lets a past result be checked against the body actually used.
        """

        node_id = _require_text(node_id, "node_id")
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose must be 'generation' or 'audit'")
        async with self._database.transaction() as connection:
            nodes = await _load(connection, self.scope_id)
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"{node_id!r} is not registered in this scope")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"{node_id!r} is not available for generation")
        return node.reference

    # -- registration ---------------------------------------------------------

    async def _require_source(self, source_ref: SourceRef, /) -> None:
        async with self._database.transaction() as connection:
            try:
                await _SOURCE_REPOSITORY.get(connection, self.scope_id, source_ref)
            except Exception as error:  # noqa: BLE001 - any decode failure means "not readable"
                raise SourceLifecycleError(f"{source_ref.source_id!r} is not a readable Source") from error

    @staticmethod
    def _require_identical(existing: _Node, candidate: _Node, /) -> None:
        if existing.kind != candidate.kind:
            raise SourceLifecycleError(f"{existing.node_id!r} is already registered as {existing.kind}")
        if existing.policy != candidate.policy or existing.parents != candidate.parents:
            raise SourceLifecycleError(f"{existing.node_id!r} is already bound to a different registration")
        if existing.reference != candidate.reference:
            raise SourceLifecycleError(f"{existing.node_id!r} is already bound to a different reference")
        if candidate.kind == _KIND_SOURCE and existing.revision != candidate.revision:
            raise SourceLifecycleError(f"{existing.node_id!r} is already bound to a different revision")

    async def _insert(self, connection: AsyncConnection, candidate: _Node, /) -> None:
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=self.scope_id,
                node_id=candidate.node_id,
                kind=candidate.kind,
                policy=candidate.policy,
                available=candidate.available,
                revision=candidate.revision,
                sequence=candidate.sequence,
                last_available=candidate.last_available,
                last_reason=candidate.last_reason,
                reference=dump_model(candidate.reference, kind="source-lifecycle-node", name=candidate.node_id),
            )
        )
        for position, parent in enumerate(candidate.parents):
            await connection.execute(
                insert(SOURCE_LIFECYCLE_EDGES_TABLE).values(
                    scope_id=self.scope_id,
                    node_id=candidate.node_id,
                    parent_node_id=parent,
                    position=position,
                )
            )

    # -- propagation ----------------------------------------------------------

    async def _persist(
        self,
        connection: AsyncConnection,
        nodes: Mapping[str, _Node],
        event: SourceLifecycleEvent,
        /,
    ) -> None:
        for node_id, node in nodes.items():
            await connection.execute(
                update(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
                .values(available=node.available)
            )
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == event.node_id,
            )
            .values(sequence=event.sequence, last_available=event.available, last_reason=event.reason)
        )

    async def _reconcile_memory(self, context: Any, nodes: Mapping[str, _Node], /) -> None:
        """Bring native Memory entry states in line with the committed graph.

        The desired state is compared against the artifact rather than against a
        mirror column, so a write that never landed stays visible as a
        difference this pass can finish, and one that did land is a no-op that
        adds no revision.
        """

        service: MemoryService = context.artifacts.memory
        by_artifact: dict[str, list[_Node]] = {}
        for node in nodes.values():
            if node.kind == _KIND_MEMORY:
                by_artifact.setdefault(node.reference.memory_ref.artifact_id, []).append(node)
        for artifact_id in sorted(by_artifact):
            try:
                head = await service.head(artifact_id)
            except RepositoryNotFoundError:
                continue
            states = {item.entry_id: item.state for item in head.content.manifest.entries}
            deactivate: list[MemoryEntryVersion] = []
            reactivate: list[MemoryEntryVersion] = []
            for node in sorted(by_artifact[artifact_id], key=lambda item: item.node_id):
                desired: Literal["active", "inactive"] = "active" if node.available else "inactive"
                if states.get(node.reference.entry_id) == desired:
                    continue
                version = await service.validate_citation(node.reference)
                (deactivate if desired == "inactive" else reactivate).append(version)
            if deactivate:
                head = await service.forget(head, entries=tuple(deactivate), reason=_REASON_WITHDRAWN)
            if reactivate:
                head = await service.reactivate(head, entries=tuple(reactivate), reason=_REASON_RESTORED)


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, /) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, scope_id)

    async def unavailable_topic_ids(self, scope_id: str, /) -> frozenset[str]:
        """Return the Topic Memory artifact ids that lost their support in one Scope."""

        async with self._database.transaction() as connection:
            nodes = await _load(connection, scope_id)
        return frozenset(
            node.reference.artifact_id for node in nodes.values() if node.kind == _KIND_TOPIC and not node.available
        )
