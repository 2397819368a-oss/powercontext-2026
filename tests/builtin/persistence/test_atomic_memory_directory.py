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

"""Compact current-directory storage, maintenance and shared authorization contracts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import BigInteger, Column, MetaData, event, insert, select, true, update

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemory, AtomicMemoryContent, AtomicMemoryDraft
from powercontext.builtin.artifacts.atomic_memory.service import AtomicMemoryMergeAdapter
from powercontext.builtin.artifacts.merge import ArtifactMergeService
from powercontext.builtin.persistence import atomic_memory_directory as directory
from powercontext.builtin.persistence.artifact_governance import ArtifactGovernanceRepository, ArtifactLifecycleState
from powercontext.builtin.persistence.artifacts import ArtifactRepository
from powercontext.builtin.persistence.atomic_memory import AtomicMemoryStateRepository
from powercontext.builtin.persistence.atomic_memory_directory import (
    AtomicMemoryDirectoryItemTooLargeError,
    AtomicMemoryDirectoryQuery,
    AtomicMemoryDirectoryRepository,
    AtomicMemoryDirectoryUnavailableError,
)
from powercontext.builtin.persistence.atomic_memory_index import PreparedAtomicMemoryProjection
from powercontext.builtin.persistence.cursor_codec import SignedCursorCodec
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import (
    ARTIFACT_HEADS_TABLE,
    ARTIFACT_TAGS_TABLE,
    ARTIFACTS_TABLE,
    SCOPE_TABLES,
    SHARED_TABLES,
    identity_string,
)
from powercontext.builtin.persistence.tags import RelationalTagService
from powercontext.builtin.records import CursorExpiredError, InvalidCursorError
from powercontext.builtin.runtime.artifact_merge import ArtifactMergeApplication, merge_artifact_tags
from powercontext.builtin.sources.content import CONTENT_SOURCE_ADAPTER
from powercontext.builtin.tags import ArtifactTagTarget, TagFilter


@asynccontextmanager
async def directory_profile(path: Path | None = None, *, extra_tables=()):
    # A disposable target-schema fixture, not a startup/migration implementation.
    metadata = MetaData()
    for shared in (*SHARED_TABLES, *SCOPE_TABLES, ARTIFACT_TAGS_TABLE, *extra_tables):
        shared.to_metadata(metadata)
    scopes = metadata.tables["pc_scopes"]
    scopes.append_column(Column("atomic_memory_directory_generation", BigInteger, nullable=False, server_default="0"))
    heads = metadata.tables["pc_artifact_heads"]
    heads.append_column(Column("content_kind", identity_string(128)))
    config = SQLiteConfig() if path is None else SQLiteConfig(url=f"sqlite+aiosqlite:///{path}")
    async with SQLiteProfile.open(config, tables=tuple(metadata.sorted_tables)) as profile:
        async with profile.database.transaction() as connection:
            await connection.execute(
                insert(scopes).values(
                    scope_id="scope-a",
                    title="A",
                    summary="A",
                    scope_id_search="scope-a",
                    title_search="a",
                    summary_search="a",
                    version=1,
                )
            )
        yield profile, scopes, heads


async def publish(connection, reader, identity="a", *, kind="rule", text="Keep this rule."):
    return await ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader).create(
        connection, "scope-a", identity, AtomicMemoryDraft(content=AtomicMemoryContent(kind=kind, text=text))
    )


async def query(connection, reader, *, eligibility=None, subject="user:a", policy_context="fixture-local", **kwargs):
    # true() is an explicit trusted storage fixture; it does not qualify authz support.
    return await reader.query(
        connection,
        AtomicMemoryDirectoryQuery(scope_id="scope-a", **kwargs),
        subject=subject,
        policy_context=policy_context,
        eligibility=true() if eligibility is None else eligibility,
    )


def test_exact_revision_directory_completes_and_content_change_requires_restart():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, _, _):
            async with profile.database.transaction() as connection:
                original = await publish(connection, reader, "a")
                await publish(connection, reader, "b")
                await publish(connection, reader, "c")
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                first = await query(connection, reader, limit=2)
                last = await query(connection, reader, limit=2, cursor=first.next_cursor)
                assert [item.artifact.artifact_id for item in first.items + last.items] == ["a", "b", "c"]
                assert first.items[0].artifact == original.as_ref()
                assert first.next_cursor is not None and last.next_cursor is None
                assert "text" not in first.items[0].model_dump()
            async with profile.database.transaction() as connection:
                revised = await ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader).revise(
                    connection,
                    "scope-a",
                    original,
                    AtomicMemoryDraft(content=AtomicMemoryContent(kind="preference", text="Use short answers.")),
                )
                assert isinstance(revised, AtomicMemory)
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                with pytest.raises(InvalidCursorError):
                    await query(connection, reader, limit=2, cursor=first.next_cursor)
                current = await query(connection, reader, kind="preference")
                assert current.items[0].artifact.revision == 2
                exact = await ArtifactRepository((AtomicMemory,)).get(connection, "scope-a", original.as_ref())
                assert exact.content.text == original.content.text

    asyncio.run(scenario())


def test_locked_evidence_reads_and_direct_merge_transitions_take_scope_lock_first():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, scopes, _), profile.database.transaction() as connection:
            artifact = await publish(connection, reader, "a")
            repository = ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader)
            governance = ArtifactGovernanceRepository(atomic_memory_directory=reader)
            before = await connection.scalar(select(scopes.c.atomic_memory_directory_generation))
            statements = []

            def observe(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement.lower().lstrip())

            event.listen(profile.database.engine.sync_engine, "before_cursor_execute", observe)
            try:
                for read in (
                    lambda: repository.latest(connection, "scope-a", "atomic-memory", "a", for_update=True),
                    lambda: repository.get(connection, "scope-a", artifact.as_ref(), for_update=True),
                ):
                    statements.clear()
                    assert await read() == artifact
                    assert statements[0].startswith("update pc_scopes")
                current = await governance.get(connection, "scope-a", "atomic-memory", "a")
                statements.clear()
                await governance.transition_merge(
                    connection, "scope-a", "atomic-memory", "a", current, ArtifactLifecycleState.ACTIVE
                )
                assert statements[0].startswith("update pc_scopes")
            finally:
                event.remove(profile.database.engine.sync_engine, "before_cursor_execute", observe)
            # Locking is not an effective directory mutation, including a no-op merge.
            assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == before

    asyncio.run(scenario())


def test_lifecycle_kind_tags_and_supplied_eligibility_are_applied_before_limit():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, _, _), profile.database.transaction() as connection:
            for identity, kind in (("a", "other"), ("b", "rule"), ("c", "rule"), ("d", "rule")):
                await publish(connection, reader, identity, kind=kind)
            governance = ArtifactGovernanceRepository(atomic_memory_directory=reader)
            current = await governance.get(connection, "scope-a", "atomic-memory", "c")
            await governance.transition_merge(
                connection, "scope-a", "atomic-memory", "c", current, ArtifactLifecycleState.DEPRECATED, "d"
            )
            for identity in ("b", "c"):
                await connection.execute(
                    insert(ARTIFACT_TAGS_TABLE).values(
                        scope_id="scope-a",
                        family="atomic-memory",
                        artifact_id=identity,
                        target_type="artifact",
                        target_id=identity,
                        tag_key="rule",
                        tag_key_hash=sha256(b"rule").digest(),
                        tag="Rule",
                        assigned_at=datetime.now(UTC),
                    )
                )
            await reader.record_mutation(connection, "scope-a")
            selected = await query(
                connection,
                reader,
                states=("active", "merged"),
                kind="rule",
                limit=1,
                tag_filter=TagFilter(tags=("RULE",)),
                eligibility=ARTIFACT_HEADS_TABLE.c.artifact_id != "b",
            )
            assert [item.artifact.artifact_id for item in selected.items] == ["c"]
            assert selected.items[0].state == "merged"
            assert selected.items[0].state_version == 1
            assert selected.items[0].merged_into_id == "d"
            assert selected.next_cursor is None

    asyncio.run(scenario())


def test_long_normalized_filters_continue_and_context_changes_reject_cursor():
    async def scenario():
        now = [datetime(2026, 10, 10, tzinfo=UTC)]
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture", clock=lambda: now[0]))
        labels = tuple(chr(0x4E00 + index) * 64 for index in range(16))
        async with directory_profile() as (profile, _, _), profile.database.transaction() as connection:
            for identity in ("a", "b"):
                await publish(connection, reader, identity)
                await connection.execute(
                    insert(ARTIFACT_TAGS_TABLE),
                    [
                        {
                            "scope_id": "scope-a",
                            "family": "atomic-memory",
                            "artifact_id": identity,
                            "target_type": "artifact",
                            "target_id": identity,
                            "tag_key": label,
                            "tag_key_hash": sha256(label.encode()).digest(),
                            "tag": label,
                            "assigned_at": datetime.now(UTC),
                        }
                        for label in labels
                    ],
                )
            first = await query(connection, reader, limit=1, tag_filter=TagFilter(tags=labels))
            assert first.next_cursor is not None and len(first.next_cursor) <= 4096
            tail = await query(
                connection,
                reader,
                limit=1,
                cursor=first.next_cursor,
                tag_filter=TagFilter(tags=tuple(reversed(labels))),
            )
            assert tail.items[0].artifact.artifact_id == "b" and tail.next_cursor is None
            for changes in ({"subject": "user:b"}, {"policy_context": "revoked"}, {"limit": 2}, {"kind": "other"}):
                with pytest.raises(InvalidCursorError):
                    await query(
                        connection,
                        reader,
                        cursor=first.next_cursor,
                        tag_filter=TagFilter(tags=labels),
                        **{"limit": 1, **changes},
                    )
            with pytest.raises(InvalidCursorError):
                await query(
                    connection, reader, limit=1, tag_filter=TagFilter(tags=labels), cursor="!" + first.next_cursor[1:]
                )
            now[0] += timedelta(hours=1)
            with pytest.raises(CursorExpiredError):
                await query(connection, reader, limit=1, tag_filter=TagFilter(tags=labels), cursor=first.next_cursor)

    asyncio.run(scenario())


def test_atomic_state_noop_preserves_cursor_but_forget_and_restore_invalidate_it():
    async def scenario():
        from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemoryStateValue

        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        states = AtomicMemoryStateRepository(atomic_memory_directory=reader)
        async with directory_profile() as (profile, scopes, _), profile.database.transaction() as connection:
            await publish(connection, reader, "a")
            await publish(connection, reader, "b")
            first = await query(connection, reader, limit=1)
            generation = await connection.scalar(select(scopes.c.atomic_memory_directory_generation))
            original = await states.get(connection, "scope-a", "a")
            unchanged = await states.transition(connection, "scope-a", "a", original, AtomicMemoryStateValue.ACTIVE)
            assert unchanged == original
            assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == generation
            assert (await query(connection, reader, limit=1, cursor=first.next_cursor)).items[
                0
            ].artifact.artifact_id == "b"
            forgotten = await states.transition(connection, "scope-a", "a", original, AtomicMemoryStateValue.FORGOTTEN)
            with pytest.raises(InvalidCursorError):
                await query(connection, reader, limit=1, cursor=first.next_cursor)
            page = await query(connection, reader, states=("active", "forgotten"), limit=1)
            assert page.items[0].state == "forgotten"
            await states.transition(connection, "scope-a", "a", forgotten, AtomicMemoryStateValue.ACTIVE)
            with pytest.raises(InvalidCursorError):
                await query(connection, reader, states=("active", "forgotten"), limit=1, cursor=page.next_cursor)

    asyncio.run(scenario())


def test_tag_replace_noop_preserves_pages_and_display_change_invalidates_unfiltered_pages():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, scopes, _):
            artifacts = ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader)
            tags = RelationalTagService(profile.database, artifacts)
            target = ArtifactTagTarget(family="atomic-memory", artifact_id="a")
            async with profile.database.transaction() as connection:
                await publish(connection, reader, "a")
                await publish(connection, reader, "b")
            for identity in ("a", "b"):
                current_target = target.model_copy(update={"artifact_id": identity})
                empty = await tags.get("scope-a", current_target)
                await tags.replace("scope-a", current_target, ("Rule", "中文"), expected_etag=empty.etag)
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                plain = await query(connection, reader, limit=1)
                filtered = await query(connection, reader, limit=1, tag_filter=TagFilter(tags=("rule",)))
                generation = await connection.scalar(select(scopes.c.atomic_memory_directory_generation))
            current = await tags.get("scope-a", target)
            unchanged = await tags.replace("scope-a", target, ("中文", "Rule"), expected_etag=current.etag)
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == generation
                assert (await query(connection, reader, limit=1, cursor=plain.next_cursor)).items
            # The normalized membership is identical, but the displayed tag changed.
            await tags.replace("scope-a", target, ("RULE", "中文"), expected_etag=unchanged.etag)
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                for page, filters in ((plain, {}), (filtered, {"tag_filter": TagFilter(tags=("rule",))})):
                    with pytest.raises(InvalidCursorError):
                        await query(connection, reader, limit=1, cursor=page.next_cursor, **filters)
            assert "RULE" in (await tags.get("scope-a", target)).tags

    asyncio.run(scenario())


class _TrustedStorageSecurity:
    """An explicit storage fixture, without production ownership or policy claims."""

    def subject(self, context):
        return context

    async def authorize(self, connection, scope_id, context, action, ref=None):
        pass

    async def authorize_sources(self, connection, scope_id, context, sources):
        pass

    async def establish_owner(self, connection, scope_id, artifact_id, context):
        pass


class _StorageProjections:
    """Keep search outside this test and inject a failure after a business write."""

    fail_publication = False

    async def prepare(self, content):
        return PreparedAtomicMemoryProjection(
            kind=content.kind,
            text=content.text,
            searchable_text=content.text,
            content_hash=sha256(content.text.encode()).hexdigest(),
        )

    def validate_prepared(self, prepared):
        pass

    async def publish(self, connection, scope_id, record, prepared):
        if self.fail_publication:
            raise RuntimeError("projection publication failed")  # noqa: TRY003

    async def remove(self, connection, scope_id, artifact_id):
        pass


def test_cascade_merge_restore_and_failed_publication_keep_directory_atomic(tmp_path):
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile(tmp_path / "merge.sqlite") as (profile, scopes, _):
            sources = SourceRepository((CONTENT_SOURCE_ADAPTER,))
            artifacts = ArtifactRepository((AtomicMemory,), sources=sources, atomic_memory_directory=reader)
            projections = _StorageProjections()

            async def merge_tags(connection, scope_id, result_id, inputs):
                await merge_artifact_tags(connection, scope_id, result_id, inputs, family="atomic-memory")

            service = ArtifactMergeService(
                artifacts=artifacts,
                adapter=AtomicMemoryMergeAdapter(projections),
                security=_TrustedStorageSecurity(),
                merge_tags=merge_tags,
            )
            memory = ArtifactMergeApplication(profile.database, (service,), default_context="fixture-owner").for_scope(
                "scope-a", "atomic-memory"
            )
            async with profile.database.transaction() as connection:
                for identity in ("a", "b", "d"):
                    await publish(connection, reader, identity)
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                initial = await query(connection, reader, limit=1)
            first = await memory.merge(
                tuple([(await memory.get(identity)).as_read() for identity in ("a", "b")]),
                AtomicMemoryContent(kind="combined", text="Keep both rules."),
                artifact_id="c",
            )
            await memory.merge(
                (first.primary.as_read(), (await memory.get("d")).as_read()),
                AtomicMemoryContent(kind="summary", text="Keep all three rules."),
                artifact_id="e",
            )
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                with pytest.raises(InvalidCursorError):
                    await query(connection, reader, limit=1, cursor=initial.next_cursor)
                active = await query(connection, reader)
                assert [(item.artifact.artifact_id, item.kind) for item in active.items] == [("e", "summary")]
                before = await query(connection, reader, states=("active", "merged"), limit=1)
                generation = await connection.scalar(select(scopes.c.atomic_memory_directory_generation))
            projections.fail_publication = True
            with pytest.raises(RuntimeError, match="projection publication failed"):
                await memory.restore("b")
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == generation
                assert (await query(connection, reader)).items == active.items
                assert (
                    await query(connection, reader, states=("active", "merged"), limit=1, cursor=before.next_cursor)
                ).items
            projections.fail_publication = False
            restored = await memory.restore("b")
            assert restored.undo_merge_results == ("e", "c")
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                with pytest.raises(InvalidCursorError):
                    await query(connection, reader, states=("active", "merged"), limit=1, cursor=before.next_cursor)
                final = await query(connection, reader, states=("active", "retired"))
                assert {item.artifact.artifact_id: (item.state.value, item.kind) for item in final.items} == {
                    "a": ("active", "rule"),
                    "b": ("active", "rule"),
                    "c": ("retired", "combined"),
                    "d": ("active", "rule"),
                    "e": ("retired", "summary"),
                }
                assert all(item.artifact.revision == 2 for item in final.items)
                generation = await connection.scalar(select(scopes.c.atomic_memory_directory_generation))
            assert not (await memory.restore("a")).changed
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == generation

    asyncio.run(scenario())


def test_copy_exact_maintains_kind_and_overflow_rolls_back_business_publication():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        artifacts = ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader)
        async with directory_profile() as (profile, scopes, _):
            async with profile.database.transaction() as connection:
                original = await publish(connection, reader)
                copied = await artifacts.copy_exact(
                    connection,
                    "scope-a",
                    "copy",
                    ArtifactAddress(scope_id="scope-a", artifact=original.as_ref()),
                    original,
                    sha256(original.content.model_dump_json().encode()).hexdigest(),
                )
                page = await query(connection, reader)
                assert page.items[1].artifact == copied.as_ref() and page.items[1].kind == original.content.kind
                await connection.execute(update(scopes).values(atomic_memory_directory_generation=(1 << 63) - 1))
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                async with profile.database.transaction() as connection:
                    await publish(connection, reader, "never-committed")
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert {item.artifact.artifact_id for item in (await query(connection, reader)).items} == {"a", "copy"}
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == (1 << 63) - 1

    asyncio.run(scenario())


def test_fractional_sqlite_generation_cannot_commit_business_writes_or_be_incremented():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, scopes, heads):
            async with profile.database.transaction() as connection:
                await publish(connection, reader)
                # SQLite BIGINT affinity and a nonnegative CHECK still admit fractions.
                await connection.execute(update(scopes).values(atomic_memory_directory_generation=0.5))
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                async with profile.database.transaction() as connection:
                    await publish(connection, reader, "must-not-commit")
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                async with profile.database.transaction() as connection:
                    await reader.record_mutation(connection, "scope-a")
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert (await connection.execute(select(heads.c.artifact_id))).scalars().all() == ["a"]
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == 0.5

    asyncio.run(scenario())


def test_other_scopes_and_families_do_not_invalidate_atomic_directory():
    async def scenario():
        from tests.builtin.persistence.contract import Handoff, HandoffContent, HandoffDraft

        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        artifacts = ArtifactRepository((AtomicMemory, Handoff), atomic_memory_directory=reader)
        async with directory_profile() as (profile, scopes, _), profile.database.transaction() as connection:
            for identity in ("a", "b"):
                await publish(connection, reader, identity)
            first = await query(connection, reader, limit=1)
            await connection.execute(
                insert(scopes).values(
                    scope_id="scope-b",
                    title="B",
                    summary="B",
                    scope_id_search="scope-b",
                    title_search="b",
                    summary_search="b",
                    version=1,
                )
            )
            await artifacts.create(
                connection,
                "scope-b",
                "other-memory",
                AtomicMemoryDraft(content=AtomicMemoryContent(kind="rule", text="Another Scope.")),
            )
            handoff = await artifacts.create(
                connection, "scope-a", "handoff", HandoffDraft(content=HandoffContent(summary="A different Family."))
            )
            await artifacts.revise(
                connection, "scope-a", handoff, HandoffDraft(content=HandoffContent(summary="A revised handoff."))
            )
            assert (await query(connection, reader, limit=1, cursor=first.next_cursor)).items[
                0
            ].artifact.artifact_id == "b"
            assert (
                await connection.scalar(
                    select(scopes.c.atomic_memory_directory_generation).where(scopes.c.scope_id == "scope-b")
                )
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("size", (200, 1000, 5000))
def test_scope_owner_ready_uses_one_bounded_result_and_exact_canonical_owner_keys(size, record_property, monkeypatch):
    async def scenario():
        from time import perf_counter

        from powercontext.server.authz import AccessUnavailableError, ResourceRef
        from powercontext.server.authz.repository import ACCESS_OWNERS_TABLE, ACCESS_TABLES, RelationalAccessRepository

        async with directory_profile(extra_tables=ACCESS_TABLES) as (profile, _, heads):
            async with profile.database.transaction() as connection:
                ids = tuple(f"记忆-{index:05d}" for index in range(size))
                await connection.execute(
                    insert(ARTIFACTS_TABLE),
                    [
                        {
                            "scope_id": "scope-a",
                            "family": "atomic-memory",
                            "artifact_id": identity,
                            "revision": 1,
                            "content": b"not decoded",
                        }
                        for identity in ids
                    ],
                )
                await connection.execute(
                    insert(heads),
                    [
                        {"scope_id": "scope-a", "family": "atomic-memory", "artifact_id": identity, "revision": 1}
                        for identity in ids
                    ],
                )
                await connection.execute(
                    insert(ACCESS_OWNERS_TABLE),
                    [
                        {
                            "owner_kind": "artifact",
                            "object_key_hash": sha256(
                                ResourceRef.artifact(
                                    "scope-a", family="atomic-memory", artifact_id=identity
                                ).key.encode()
                            ).hexdigest(),
                            "scope_id": "scope-a",
                            "family": "atomic-memory",
                            "artifact_id": identity,
                            "owner_type": "user",
                            "owner_id": "reader",
                            "established_at": datetime.now(UTC).isoformat(),
                            "policy_revision": "0",
                            "idempotency_key": identity,
                        }
                        for identity in ids
                    ],
                )
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                execute = type(connection).execute
                returned_rows = []

                async def measured(current, statement, *args, **kwargs):
                    result = await execute(current, statement, *args, **kwargs)
                    if current is connection and result.returns_rows:
                        frozen = result.freeze()
                        returned_rows.append(len(frozen.data))
                        return frozen()
                    return result

                monkeypatch.setattr(type(connection), "execute", measured)
                started = perf_counter()
                await RelationalAccessRepository(profile.database, connection=connection).require_scope_content_ready(
                    "scope-a"
                )
                record_property("owner_ready_ms", round((perf_counter() - started) * 1000, 3))
                assert returned_rows == [0]
            # A coordinate-matching row with the wrong canonical hash is not an owner.
            async with profile.database.transaction() as connection:
                await connection.execute(
                    update(ACCESS_OWNERS_TABLE)
                    .where(ACCESS_OWNERS_TABLE.c.artifact_id == ids[-1])
                    .values(object_key_hash="0" * 64)
                )
                with pytest.raises(AccessUnavailableError, match="owner"):
                    await RelationalAccessRepository(
                        profile.database, connection=connection
                    ).require_scope_content_ready("scope-a")

    asyncio.run(scenario())


def test_custom_directory_policy_is_rejected_without_replacing_ordinary_scope_read():
    async def scenario():
        from powercontext.server.authz import (
            AccessAuditContext,
            AccessBinding,
            AccessBindingState,
            AccessControlService,
            AccessRole,
            AccessUnavailableError,
            BuiltinAuthorizationProvider,
            PrincipalRef,
            ResourceRef,
        )
        from powercontext.server.authz.repository import ACCESS_TABLES, RelationalAccessRepository

        class CustomPolicy(BuiltinAuthorizationProvider):
            pass

        principal = PrincipalRef(type="user", id="reader")
        context = AccessAuditContext(transport="test", operation="custom-policy")
        async with directory_profile(extra_tables=ACCESS_TABLES) as (profile, _, _):
            repository = RelationalAccessRepository(profile.database)
            await repository.create_binding(
                AccessBinding(
                    binding_id="scope-reader",
                    subject=principal,
                    resource=ResourceRef.scope("scope-a"),
                    role=AccessRole.SCOPE_VIEWER,
                    granted_by=principal,
                    reason="fixture",
                    created_at=datetime.now(UTC),
                    expires_at=None,
                    state=AccessBindingState.ACTIVE,
                    version=1,
                    policy_revision="pending",
                    idempotency_key="fixture",
                )
            )
            provider = CustomPolicy(repository)
            access = AccessControlService(provider, relationships=repository, audit=repository)
            async with (
                access.defer_decision_audit(),
                profile.database.transaction(consistent_snapshot=True) as connection,
            ):
                assert (
                    await access.require_scope_read(principal, "scope-a", connection=connection, context=context)
                ).allowed
                with pytest.raises(AccessUnavailableError) as unavailable:
                    await access.require_scope_directory_read(
                        principal, "scope-a", connection=connection, context=context
                    )
                assert unavailable.value.code == "safe_resource_filtering_unavailable"
                assert access.provider is provider

    asyncio.run(scenario())


def test_byte_boundary_preserves_the_next_eligible_item_and_oversized_item_fails(monkeypatch):
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, _, _), profile.database.transaction() as connection:
            await publish(connection, reader, "a")
            await publish(connection, reader, "b")
            initial = await query(connection, reader, limit=2)
            first_size = len(initial.items[0].model_dump_json().encode()) + 2
            monkeypatch.setattr(directory, "DIRECTORY_ITEM_BYTES", first_size)
            first = await query(connection, reader, limit=2)
            assert len(first.items) == 1 and first.next_cursor is not None
            last = await query(connection, reader, limit=2, cursor=first.next_cursor)
            assert [item.artifact.artifact_id for item in first.items + last.items] == ["a", "b"]
            assert last.next_cursor is None
            monkeypatch.setattr(directory, "DIRECTORY_ITEM_BYTES", first_size - 1)
            with pytest.raises(AtomicMemoryDirectoryItemTooLargeError):
                await query(connection, reader, limit=2)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", (200, 1000, 5000))
def test_large_sparse_kind_directory_reads_no_bodies_or_unbounded_application_candidates(size, monkeypatch):
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, _, heads):
            async with profile.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE),
                    [
                        {
                            "scope_id": "scope-a",
                            "family": "atomic-memory",
                            "artifact_id": f"m-{index:05d}",
                            "revision": 1,
                            "content": b"not a decodable body: directory must not read it",
                        }
                        for index in range(size)
                    ],
                )
                await connection.execute(
                    insert(heads),
                    [
                        {
                            "scope_id": "scope-a",
                            "family": "atomic-memory",
                            "artifact_id": f"m-{index:05d}",
                            "revision": 1,
                            "content_kind": "rare" if index % 10 == 0 else "common",
                        }
                        for index in range(size)
                    ],
                )
            statements = []

            def observed(_connection, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement)

            event.listen(profile.database.engine.sync_engine, "before_cursor_execute", observed)
            try:
                async with profile.database.transaction(consistent_snapshot=True) as connection:
                    execute = type(connection).execute
                    returned_rows = []

                    async def measured(current, statement, *args, **kwargs):
                        result = await execute(current, statement, *args, **kwargs)
                        if current is connection and result.returns_rows:
                            frozen = result.freeze()
                            returned_rows.append(len(frozen.data))
                            return frozen()
                        return result

                    monkeypatch.setattr(type(connection), "execute", measured)
                    page = await query(connection, reader, limit=10, kind="rare")
                    assert [item.artifact.artifact_id for item in page.items] == [
                        f"m-{index:05d}" for index in range(0, 100, 10)
                    ]
                    assert page.next_cursor is not None
                    # Measure actual result cardinality, not SQL text or private call order.
                    assert returned_rows and max(returned_rows) <= 11
            finally:
                event.remove(profile.database.engine.sync_engine, "before_cursor_execute", observed)
            # The storage budget prohibits body access regardless of body size/validity.
            assert all("pc_artifacts" not in statement for statement in statements)

    asyncio.run(scenario())


def test_missing_metadata_and_generation_overflow_fail_without_publishing_a_page():
    async def scenario():
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, scopes, heads), profile.database.transaction() as connection:
            await publish(connection, reader)
            await connection.execute(update(heads).values(content_kind=None))
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                await query(connection, reader)
            for generation in (-1, (1 << 63) - 1):
                await connection.execute(update(scopes).values(atomic_memory_directory_generation=generation))
                with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                    await reader.record_mutation(connection, "scope-a")
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == generation

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "values",
    (
        {"limit": True},
        {"limit": 0},
        {"states": ()},
        {"states": ("active", "active")},
        {"kind": " "},
        {"kind": "x" * 129},
        {"cursor": "x" * 4097},
    ),
)
def test_query_validation_rejects_invalid_contract_inputs(values):
    with pytest.raises(ValidationError):
        AtomicMemoryDirectoryQuery(scope_id="scope-a", **values)
