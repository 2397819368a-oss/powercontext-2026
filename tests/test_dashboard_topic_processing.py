# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Processing inspection stays passive, scoped, and independent of saved topics."""

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, insert

from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import (
    TOPIC_MEMORY_PROCESSING_TARGETS_TABLE,
    TOPIC_MEMORY_WORK_BUDGETS_TABLE,
)
from powercontext.builtin.runtime.scope_reads import ScopeReadSnapshot
from powercontext.builtin.runtime.topic_memory_recovery import TopicMemoryRecoveryService
from powercontext.builtin.sources import SourceCursor
from powercontext.server.authz import PrincipalRef
from tests.test_dashboard import create_scope
from tests.test_dashboard import dashboard as dashboard


def _pending(client: TestClient, title: str) -> str:
    scope = create_scope(client, title)["scope_id"]
    response = client.post(
        "/v1/sources/content", json={"scope_id": scope, "source_id": "input", "content": "Saved evidence."}
    )
    assert response.status_code == 202, response.text
    return scope


def _budget(client: TestClient, scope: str) -> None:
    runtime = cast(FastAPI, client.app).state.application

    async def seed():
        async with runtime.primary_database.transaction() as connection:
            await connection.execute(
                insert(TOPIC_MEMORY_WORK_BUDGETS_TABLE).values(
                    scope_id=scope,
                    binding_name=TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
                    source_after=0,
                    source_through=1,
                    attempt_id="retained-attempt",
                    attempts=3,
                    requests=9,
                    tokens=1000,
                    failure_code="",
                )
            )

    assert client.portal is not None
    client.portal.call(seed)


def test_topic_processing_is_passive_even_without_a_retained_block(dashboard: TestClient) -> None:
    scope = _pending(dashboard, "Pending scope")
    _budget(dashboard, scope)
    runtime = cast(FastAPI, dashboard.app).state.application
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = runtime.primary_database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(2):
            response = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
            assert response.status_code == 200, response.text
            assert "Pending inputs" in response.text
            assert "Current execution state is unknown" in response.text
            assert "No retained block diagnostics" in response.text
            assert "Source head" in response.text
    finally:
        event.remove(engine, "before_cursor_execute", record)
    processing = ("pc_topic_memory_", "pc_source_", "pc_artifact_processing_")
    writes = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        and any(table in statement for table in processing)
    ]
    assert not writes, writes
    # The added inspection is four bounded reads, independent of journal size.
    for table in (
        "pc_source_cursors",
        "pc_source_journal_heads",
        "pc_topic_memory_processing_targets",
        "pc_topic_memory_work_blocks",
    ):
        reads = [
            statement for statement in statements if statement.startswith("SELECT") and f"FROM {table}" in statement
        ]
        assert len(reads) == 2, reads
        assert all("FOR UPDATE" not in statement for statement in reads)


def test_topic_processing_follows_scope_and_does_not_claim_artifact_completion(dashboard: TestClient) -> None:
    pending = _pending(dashboard, "Pending topic inputs")
    caught_up = create_scope(dashboard, "Caught up topic inputs")["scope_id"]
    for scope, expected, absent in (
        (pending, "Pending inputs", "Inputs caught up"),
        (caught_up, "Inputs caught up", "Pending inputs"),
        (pending, "Pending inputs", "Inputs caught up"),
    ):
        response = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
        assert response.status_code == 200, response.text
        assert expected in response.text and absent not in response.text
        assert "Input progress does not prove Topic Memory publication" in response.text
        assert "Current execution state is unknown" in response.text


def test_topic_processing_failure_preserves_saved_topics(dashboard: TestClient, monkeypatch) -> None:
    scope = create_scope(dashboard, "Readable topics")["scope_id"]
    saved = dashboard.post(
        f"/v1/scopes/{scope}/artifacts",
        json={"family": "topic-memory", "content": {"title": "Saved topic", "summary": "Summary", "detail": "Detail"}},
    )
    assert saved.status_code == 201, saved.text

    async def unavailable(_snapshot):
        message = "private storage failure"
        raise RuntimeError(message)

    monkeypatch.setattr(ScopeReadSnapshot, "topic_memory_processing_status", unavailable)
    response = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
    assert response.status_code == 200, response.text
    assert "Saved topic" in response.text
    assert "Processing status could not be loaded" in response.text
    assert "Processing status unavailable" not in response.text
    assert 'role="alert"' in response.text
    assert "Request ID" in response.text
    assert "private storage failure" not in response.text


def test_retained_block_disappears_from_current_status_after_progress(dashboard: TestClient) -> None:
    scope = _pending(dashboard, "Blocked topic inputs")
    _budget(dashboard, scope)
    runtime = cast(FastAPI, dashboard.app).state.application
    service = TopicMemoryRecoveryService(runtime.primary_database)
    assert dashboard.portal is not None
    # The explicit recovery inspection may persist diagnostics; Dashboard GET may not.
    retained = dashboard.portal.call(service.status, scope).block
    assert retained is not None
    observed_at = retained.model_dump(mode="json")["blocked_at"]
    response = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
    assert response.status_code == 200, response.text
    assert "Known processing block" in response.text
    assert "window_attempt_limit" in response.text
    assert observed_at in response.text
    assert "Exact attempt end position</dt><dd>Unknown" in response.text

    dashboard.portal.call(service.retry, scope, retained.block_id)
    retried = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
    assert retried.status_code == 200, retried.text
    assert "Pending inputs" in retried.text
    assert "window_attempt_limit" not in retried.text

    async def progress():
        # Persist the same frontier changes as a no-change publication. Keep the
        # historical block to check that it cannot become a current failure.
        async with runtime.primary_database.transaction() as connection:
            await SourceCursorRepository().save(
                connection,
                scope,
                TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
                SourceCursor(sequence=1),
                expected_generation=None,
            )
            for table in (TOPIC_MEMORY_WORK_BUDGETS_TABLE, TOPIC_MEMORY_PROCESSING_TARGETS_TABLE):
                await connection.execute(delete(table).where(table.c.scope_id == scope))

    dashboard.portal.call(progress)
    caught_up = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
    assert caught_up.status_code == 200, caught_up.text
    assert "Inputs caught up" in caught_up.text
    assert "window_attempt_limit" not in caught_up.text
    assert observed_at not in caught_up.text
    assert "No matching topics" in caught_up.text


def test_processing_is_not_inspected_without_scope_authority(dashboard: TestClient, monkeypatch) -> None:
    scope = _pending(dashboard, "Private processing scope")
    authentication = cast(FastAPI, dashboard.app).state.authentication_provider
    reads = []

    async def inspect(snapshot):
        reads.append(snapshot.scope_id)
        raise AssertionError

    monkeypatch.setattr(ScopeReadSnapshot, "topic_memory_processing_status", inspect)
    monkeypatch.setattr(authentication, "_principal", PrincipalRef(type="user", id="unrelated-reader"))
    response = dashboard.get("/dashboard/topics", params={"scope": scope, "lang": "en"})
    assert response.status_code == 403, response.text
    assert "Private processing scope" not in response.text
    assert "Source head" not in response.text
    assert not reads
