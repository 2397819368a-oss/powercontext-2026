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

"""Metadata-only bounded recall over real SQLite storage."""

from __future__ import annotations

import asyncio

from sqlalchemy import Engine, event

from tests.e2e.test_access_control_regressions import _scope, _server


def test_manifest_is_bounded_metadata_and_refs_are_readable(tmp_path):
    async def scenario():
        async with _server(tmp_path) as (app, client, _):
            scope = await _scope(client)
            missing = await client.post(
                "/v1/memory/remember",
                json={
                    "scope_id": scope,
                    "kind": "fact",
                    "text": "No summary and no model",
                },
            )
            assert missing.status_code == 503, missing.text
            assert missing.json()["error"]["code"] == "inference_unavailable"
            empty = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert empty.status_code == 200, empty.text
            assert empty.json() == {"items": [], "next_cursor": None, "code": "empty_inventory"}
            for number in range(3):
                response = await client.post(
                    f"/v1/scopes/{scope}/artifacts",
                    json={
                        "family": "atomic-memory",
                        "content": {
                            "kind": "decision",
                            "text": f"Full private body {number}",
                            "description": f"Summary {number}",
                        },
                    },
                )
                assert response.status_code == 201, response.text
            prepare_request = {
                "scope_id": scope,
                "query": "private body",
                "assembly": {"sections": [{"family": "memory", "limit": 8}]},
            }
            prepared_before = await client.post("/v1/context/prepare", json=prepare_request)
            assert prepared_before.status_code == 200, prepared_before.text
            statements = []

            def capture(connection, cursor, statement, parameters, context, executemany):
                statements.append(statement.lower())

            event.listen(Engine, "before_cursor_execute", capture)
            try:
                response = await client.post("/v1/recall/manifest", json={"scope_id": scope, "limit": 2})
            finally:
                event.remove(Engine, "before_cursor_execute", capture)
            assert any("pc_atomic_memory_current.description" in sql for sql in statements)
            assert not any(
                forbidden in sql
                for sql in statements
                for forbidden in ("pc_artifacts", "searchable_text", "pc_atomic_memory_current.text", ".embedding")
            )
            assert response.status_code == 200, response.text
            first = response.json()
            assert first["code"] == "page"
            assert len(first["items"]) == 2
            assert first["next_cursor"]
            assert "Full private body" not in response.text
            assert first["items"][0]["description"].startswith("Summary")
            import httpx
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport

            from powercontext.server.mcp import create_mcp_server

            mcp_app = create_mcp_server(app).http_app(path="/")
            app.mount("/manifest-test-mcp", mcp_app)

            def http_client_factory(headers=None, timeout=None, auth=None, **kwargs):
                return httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers=headers,
                    timeout=timeout,
                    auth=auth,
                    follow_redirects=True,
                )

            transport = StreamableHttpTransport(
                "http://test/manifest-test-mcp/",
                httpx_client_factory=http_client_factory,
            )
            async with mcp_app.lifespan(mcp_app), Client(transport) as mcp:
                via_mcp = await mcp.call_tool("get_recall_manifest", {"scope_id": scope, "limit": 2})
                assert via_mcp.structured_content["items"] == first["items"]
                assert via_mcp.structured_content["code"] == "page"
            second = await client.post(
                "/v1/recall/manifest",
                json={
                    "scope_id": scope,
                    "limit": 2,
                    "cursor": first["next_cursor"],
                },
            )
            assert second.status_code == 200, second.text
            assert len(second.json()["items"]) == 1
            assert second.json()["next_cursor"] is None
            refs = [item["ref"]["artifact_id"] for item in first["items"] + second.json()["items"]]
            assert len(set(refs)) == 3
            ref = first["items"][0]["ref"]
            exact = await client.get(
                f"/v1/scopes/{scope}/artifacts/atomic-memory/{ref['artifact_id']}/revisions/{ref['revision']}"
            )
            assert exact.status_code == 200, exact.text
            assert exact.json()["content"]["description"] == first["items"][0]["description"]
            changed_filter = await client.post(
                "/v1/recall/manifest",
                json={
                    "scope_id": scope,
                    "kind": "fact",
                    "cursor": first["next_cursor"],
                },
            )
            assert changed_filter.status_code == 400, changed_filter.text
            denied = await client.post(
                "/v1/recall/manifest", json={"scope_id": scope}, headers={"Authorization": "Bearer stranger"}
            )
            assert denied.status_code == 403, denied.text
            bad = await client.post("/v1/recall/manifest", json={"scope_id": scope, "limit": 101})
            assert bad.status_code == 422
            assert bad.json()["error"]["code"]
            prepared_after = await client.post("/v1/context/prepare", json=prepare_request)
            assert prepared_after.status_code == 200, prepared_after.text
            assert prepared_after.content == prepared_before.content

    asyncio.run(scenario())


def test_explicit_writes_generate_summaries_through_configured_model(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from powercontext.builtin.runtime import InferenceConfig, composition

    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"description":"Login policy"}')]))

    async def open_local_model(*args, **kwargs):
        return model, model

    monkeypatch.setattr(composition, "_open_pydantic_ai_model", open_local_model)

    async def scenario():
        # Controlled inference exercises real composition and SQLite, without contacting a provider.
        async with _server(tmp_path, inference=InferenceConfig(generation_model="test")) as (app, client, _):
            scope = await _scope(client)
            created = await client.post(
                "/v1/memory/remember",
                json={
                    "scope_id": scope,
                    "kind": "decision",
                    "text": "Use JWT for login.",
                },
            )
            assert created.status_code == 200, created.text
            manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert manifest.status_code == 200, manifest.text
            entry = manifest.json()["items"][0]
            assert entry["description"]
            ref = entry["ref"]
            path = f"/v1/scopes/{scope}/artifacts/atomic-memory/{ref['artifact_id']}"
            current = await client.get(path)
            revised = await client.put(
                path,
                headers={"If-Match": current.headers["ETag"]},
                json={
                    "content": {"kind": "decision", "text": "Use a shorter JWT expiry."},
                },
            )
            assert revised.status_code == 200, revised.text
            assert revised.json()["content"]["description"]
            assert revised.json()["content"]["text"] == "Use a shorter JWT expiry."
            old = await client.get(path + "/revisions/1")
            assert old.status_code == 200
            assert old.json()["content"]["text"] == "Use JWT for login."

            other = await client.post(
                "/v1/memory/remember",
                json={
                    "scope_id": scope,
                    "kind": "decision",
                    "text": "Keep refresh tokens.",
                    "description": "Refresh token policy",
                },
            )
            assert other.status_code == 200, other.text
            from powercontext.builtin.artifacts.atomic_memory import AtomicMemoryContent

            generic = app.state.application.artifact_merge.for_scope(scope, "atomic-memory")
            first = await generic.get(ref["artifact_id"])
            second = await generic.get(other.json()["records"][0]["artifact"]["artifact_id"])
            merged = await generic.merge(
                (first.as_read(), second.as_read()),
                AtomicMemoryContent(kind="decision", text="Use JWT and retain refresh tokens."),
            )
            assert merged.primary.artifact.content.description == "Login policy"
            final_manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert len(final_manifest.json()["items"]) == 1
            assert final_manifest.json()["items"][0]["description"] == "Login policy"

    asyncio.run(scenario())


def test_existing_projection_adds_summary_column_and_rebuild_copies_authority(tmp_path):
    import sqlite3

    from powercontext.builtin.runtime.atomic_memory_rebuild import rebuild_atomic_memory_projection

    async def scenario():
        async with _server(tmp_path) as (_, client, _):
            scope = await _scope(client)
            created = await client.post(
                f"/v1/scopes/{scope}/artifacts",
                json={
                    "family": "atomic-memory",
                    "content": {
                        "kind": "fact",
                        "text": "Historical authoritative body",
                        "description": "Stored summary",
                    },
                },
            )
            assert created.status_code == 201, created.text
            identifier = created.json()["artifact_id"]
            exact_path = f"/v1/scopes/{scope}/artifacts/atomic-memory/{identifier}/revisions/1"
            before = await client.get(exact_path)
            assert before.status_code == 200
        with sqlite3.connect(tmp_path / "regressions.db") as connection:
            connection.execute("ALTER TABLE pc_atomic_memory_current DROP COLUMN description")
        async with _server(tmp_path) as (app, client, _):
            manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert manifest.status_code == 200, manifest.text
            assert manifest.json()["items"][0]["description"] is None
            application = app.state.application.atomic_memory._application
            report = await rebuild_atomic_memory_projection(
                application.database,
                application.index,
                maintenance_confirmed=True,
            )
            assert report.ready, report
            rebuilt = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert rebuilt.json()["items"][0]["description"] == "Stored summary"
            after = await client.get(exact_path)
            assert after.content == before.content
        async with _server(tmp_path) as (_, client, _):
            manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert manifest.json()["items"][0]["description"] == "Stored summary"

    asyncio.run(scenario())


def test_summary_inference_releases_transaction_and_rechecks_revision(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from powercontext.builtin.runtime import InferenceConfig, composition

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        async def respond(messages, info):
            started.set()
            await resume.wait()
            return ModelResponse(parts=[TextPart('{"description":"Generated summary"}')])

        model = FunctionModel(respond)

        async def open_local_model(*args, **kwargs):
            return model, model

        monkeypatch.setattr(composition, "_open_pydantic_ai_model", open_local_model)
        async with _server(tmp_path, inference=InferenceConfig(generation_model="test")) as (_, client, _):
            scope = await _scope(client)
            created = await client.post(
                f"/v1/scopes/{scope}/artifacts",
                json={
                    "family": "atomic-memory",
                    "content": {
                        "kind": "decision",
                        "text": "Original policy",
                        "description": "Original summary",
                    },
                },
            )
            assert created.status_code == 201, created.text
            path = f"/v1/scopes/{scope}/artifacts/atomic-memory/{created.json()['artifact_id']}"
            current = await client.get(path)
            headers = {"If-Match": current.headers["ETag"]}
            pending = asyncio.create_task(
                client.put(
                    path,
                    headers=headers,
                    json={
                        "content": {"kind": "decision", "text": "Pending policy"},
                    },
                )
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                concurrent = await asyncio.wait_for(
                    client.put(
                        path,
                        headers=headers,
                        json={
                            "content": {
                                "kind": "decision",
                                "text": "Concurrent policy",
                                "description": "Concurrent summary",
                            },
                        },
                    ),
                    timeout=5,
                )
                assert concurrent.status_code == 200, concurrent.text
            finally:
                resume.set()
            stale = await asyncio.wait_for(pending, timeout=5)
            assert stale.status_code in {409, 412}, stale.text
            latest = await client.get(path)
            assert latest.json()["content"]["text"] == "Concurrent policy"
            manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert manifest.json()["items"][0]["description"] == "Concurrent summary"

    asyncio.run(scenario())


def test_invalid_injected_summary_is_not_published_or_logged(tmp_path, caplog):
    from types import SimpleNamespace

    class InvalidSummarizer:
        async def generate(self, value):
            return SimpleNamespace(output={"description": "private-summary-marker\nsecond line"})

    async def scenario():
        async with _server(tmp_path) as (app, client, _):
            scope = await _scope(client)
            app.state.application.atomic_memory._application.service.shared.summarizer = InvalidSummarizer()
            response = await client.post(
                "/v1/memory/remember",
                json={
                    "scope_id": scope,
                    "kind": "fact",
                    "text": "A new fact",
                },
            )
            assert response.status_code == 500, response.text
            manifest = await client.post("/v1/recall/manifest", json={"scope_id": scope})
            assert manifest.json()["items"] == []
            assert "private-summary-marker" not in response.text

    asyncio.run(scenario())
    assert "private-summary-marker" not in caplog.text
